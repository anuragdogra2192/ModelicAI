import os
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

  def compile_model(self, file_path: str, model_name: str) -> dict:
    """Loads a Modelica file into the OMC session and runs checkModel()

    to validate DAE balance and syntax. Returns status and error logs if any.
    """
    try:
      # Absolute path required for containerized file loading
      abs_path = os.path.abspath(file_path)

      # Load the Modelica model file
      load_res = self.omc.sendExpression(f'loadFile("{abs_path}")')
      if not load_res:
        return {
            "status": "FAILED",
            "error": f"Failed to load file into OMC: {abs_path}",
        }

      # Check model syntax and equations
      check_res = self.omc.sendExpression(f"checkModel({model_name})")
      check_str = str(check_res)

      # OpenModelica typically returns a success string or error details
      if "successfully" in check_str.lower():
        return {"status": "SUCCESS", "error": ""}
      else:
        return {"status": "FAILED", "error": check_str}

    except Exception as e:
      return {"status": "FAILED", "error": str(e)}

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
    opts = self.omc.sendExpression(f"getSimulationOptions({model_name})")
    return opts if isinstance(opts, dict) else {"raw": str(opts)}

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