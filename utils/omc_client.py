import os
import re
from OMPython import OMCSessionLocal


class OpenModelicaClient:

  def __init__(self):
    """Initializes the local OpenModelica session via OMPython."""
    print("[OMCClient] Initializing OpenModelica session...")
    try:
      self.omc = OMCSessionLocal()
      print("[OMCClient] OpenModelica session active.")
    except Exception as e:
      print(f"[OMCClient] Error initializing OMC session: {e}")
      raise e

    # Generated models import Modelica.*, which OMC only resolves once the
    # Standard Library is loaded into the session.
    if not self.omc.sendExpression("loadModel(Modelica)"):
      print("[OMCClient] Modelica Standard Library not found. Installing package via OpenModelica...")
      self.omc.sendExpression('installPackage(Modelica)')
      if not self.omc.sendExpression("loadModel(Modelica)"):
        raise RuntimeError("OpenModelica could not load or install the Modelica Standard Library")
    print("[OMCClient] Modelica Standard Library loaded successfully.")

  def _error_buffer(self) -> str:
    """OMC keeps diagnostics in an error buffer; most API calls only return false/""."""
    try:
      return str(self.omc.sendExpression("getErrorString()") or "").strip()
    except Exception:
      return ""

  def compile_model(self, file_path: str, model_name: str) -> dict:
    """Loads a Modelica file into the OMC session and runs checkModel()

    to validate syntax, types and DAE balance. Returns status, the full
    OpenModelica diagnostics on failure, and the checkModel summary
    ("Class X has N equation(s) and N variable(s)...") on success.
    """
    abs_path = os.path.abspath(file_path)
    try:
      self._error_buffer()  # clear stale messages from earlier calls
      load_res = self.omc.sendExpression(f'loadFile("{abs_path}")')
      if not load_res:
        return {"status": "FAILED", "message": "",
                "error": self._error_buffer() or f"Failed to load file into OMC: {abs_path}"}

      check_res = self.omc.sendExpression(f"checkModel({model_name})")
      check_str = str(check_res or "").strip()
      diagnostics = self._error_buffer()

      if "successfully" in check_str.lower():
        # checkModel can report success for an unbalanced system ("has 126 equation(s) and 127 variable(s)");
        # such a model cannot be simulated, so treat it as a failure the designer must fix.
        counts = re.search(r"has (\d+) equation\(s\) and (\d+) variable\(s\)", check_str)
        if counts and counts.group(1) != counts.group(2):
          n_eq, n_var = int(counts.group(1)), int(counts.group(2))
          kind = "Too few equations (under-determined)" if n_eq < n_var else "Too many equations (over-determined)"
          return {"status": "FAILED", "message": check_str,
                  "error": (f"{kind}: the model has {n_eq} equation(s) and {n_var} variable(s). "
                            "Typical causes: an unconnected connector or input (e.g. a RealInput or fluid port "
                            "left open), a missing start value or boundary condition, or a variable declared "
                            "without an equation.\n" + "\n".join(x for x in (check_str, diagnostics) if x))}
        # Warnings may still be present; keep them for the UI.
        return {"status": "SUCCESS", "error": "", "message": check_str, "warnings": diagnostics}
      return {"status": "FAILED", "message": check_str,
              "error": "\n".join(x for x in (check_str, diagnostics) if x)
                       or f"checkModel({model_name}) failed without a message"}

    except Exception as e:
      # Newer OMPython raises on OMC errors; the exception text carries the diagnostics.
      return {"status": "FAILED", "message": "", "error": str(e)}

  def get_model_parameters(self, model_name: str) -> dict:
    """Returns the current value of every parameter of a loaded model."""
    names = self.omc.sendExpression(f"getParameterNames({model_name})")
    if not isinstance(names, (list, tuple)):
      return {}
    return {
        name: self.omc.sendExpression(
            f'getParameterValue({model_name}, "{name}")'
        )
        for name in names
    }
  
  def get_simulation_options(self, model_name: str) -> dict:
    """Returns the default simulation options (startTime, stopTime, etc.)."""
    # OMC returns a tuple: (startTime, stopTime, tolerance, numberOfIntervals, interval)
    opts = self.omc.sendExpression(f"getSimulationOptions({model_name})")
    if isinstance(opts, dict):
      return opts
    try:
      start, stop, tol, n, interval = (float(x) for x in opts)
      return {"startTime": start, "stopTime": stop, "tolerance": tol,
              "numberOfIntervals": int(round(n)), "interval": interval}
    except (TypeError, ValueError):
      return {"raw": str(opts)}

  def run_simulation(
      self,
      model_name: str,
      start_time: float,
      stop_time: float,
      number_of_intervals: int,
      method: str,
      parameter_overrides: dict,
  ) -> dict:
    """Runs simulate() and returns the path to the OMC result file."""
    overrides = ",".join(f"{k}={v}" for k, v in parameter_overrides.items())
    simflags = f"-override={overrides}" if overrides else ""
    result = self.omc.sendExpression(
        f'simulate({model_name}, startTime={start_time}, '
        f'stopTime={stop_time}, numberOfIntervals={number_of_intervals}, '
        f'method="{method}", simflags="{simflags}")'
    )
    result_file = result.get("resultFile") if isinstance(result, dict) else None
    return {"resultFile": result_file or str(result)}