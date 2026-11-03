"""OpenModelica operations shared by the REST routes and the Manager's tools.

Every call that touches the single OMC session takes omc_lock.
"""
import difflib
import json
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

import numpy as np

from api import store
from utils import sim_runner
from utils.tracing import status_attrs, traced

MODEL_ROOT = "models"
PROPOSAL_ROOT = os.path.join(MODEL_ROOT, ".proposals")
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
omc_lock = threading.RLock()  # re-entrant: tools may nest service calls


def model_dir(name: str) -> str:
  if not NAME_RE.match(name or ""):
    raise ValueError(f"Invalid model name: {name!r}")
  return os.path.join(MODEL_ROOT, name)


def mo_path(name: str) -> str:
  return os.path.join(model_dir(name), f"{name}.mo")


def result_path(name: str) -> str:
  return os.path.join(model_dir(name), f"{name}_res.mat")


def exists(name: str) -> bool:
  return NAME_RE.match(name or "") is not None and os.path.exists(mo_path(name))


def list_models() -> List[str]:
  if not os.path.isdir(MODEL_ROOT):
    return []
  return sorted(n for n in os.listdir(MODEL_ROOT) if exists(n))


def model_name_from_code(code: str) -> Optional[str]:
  m = re.search(r"^\s*(?:partial\s+)?model\s+([A-Za-z_]\w*)", code, re.M)
  return m.group(1) if m else None


def read_source(name: str) -> str:
  with open(mo_path(name)) as f:
    return f.read()


def diff(old: str, new: str, name: str) -> str:
  return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                      f"{name}.mo (current)", f"{name}.mo (proposed)", n=2))


def _sanitize_overrides(overrides: Dict[str, Any]) -> Dict[str, Any]:
  return {k: v for k, v in (overrides or {}).items()
          if re.match(r"^[A-Za-z_][\w.\[\]]*$", k) and re.match(r"^[-+0-9.eE]+$|^(true|false)$", str(v))}


def clean_omc_error(text: str) -> str:
  """Turns an OMPython exception into the OpenModelica messages a person (or Modi) can act on."""
  lines = [l.strip() for l in str(text).splitlines()
           if not re.search(r"OMC error occurred for|sendExpression\(|OMSessionException", l)]  # OMPython wrapper lines
  msgs = [re.sub(r"^\d+:\s*", "", l) for l in lines if re.match(r"^\d+:\s*\[", l) or "Error:" in l or "error" in l.lower()[:20]]
  msgs = [re.sub(r"^(?:\[[^\]]*\]\s*)+", "", m) for m in msgs]  # drop OMC's [kind:error:id] [file:line] tags
  out = "\n".join(dict.fromkeys(m for m in msgs if m)) or str(text)
  return out[:1500]


SOLVERS = {"dassl", "ida", "cvode", "euler", "heun", "rungekutta", "impeuler", "trapezoid", "imprungekutta",
           "gbode", "irksco", "symSolver", "symSolverSsc", "qss"}


def validate_settings(s: Dict[str, Any]) -> Dict[str, Any]:
  """Simulation setup must be numeric and consistent; raises ValueError with a readable message."""
  import math
  try:
    start, stop = float(s["start_time"]), float(s["stop_time"])
  except (TypeError, ValueError):
    raise ValueError("Start and stop time must be numbers.")
  n_raw = s["number_of_intervals"]
  try:
    n = int(n_raw)
    if float(n_raw) != n:
      raise ValueError
  except (TypeError, ValueError):
    raise ValueError("Intervals must be a whole number.")
  if not (math.isfinite(start) and math.isfinite(stop)):
    raise ValueError("Start and stop time must be finite numbers.")
  if stop <= start:
    raise ValueError("Stop time must be greater than start time.")
  if not 1 <= n <= 10_000_000:
    raise ValueError("Intervals must be between 1 and 10,000,000.")
  if s.get("method") not in SOLVERS:
    raise ValueError(f"Unknown solver {s.get('method')!r}.")
  return {**s, "start_time": start, "stop_time": stop, "number_of_intervals": n}


class Services:
  def __init__(self, orchestrator):
    self.orc = orchestrator
    self.client = orchestrator.client

  # ---------------------------------------------------------------- OMC
  @traced("omc.check", out=status_attrs)
  def check_file(self, path: str, name: str) -> Dict[str, str]:
    with omc_lock:
      return self.client.compile_model(file_path=path, model_name=name)

  @traced("omc.components", out=lambda r: {"omc.component_count": len(r), "omc.components": r})
  def components(self, path: str, name: str) -> List[str]:
    """Names of real component instances (not parameters, constants, plain variables or SI types)."""
    from utils.omc_graphics import parse, BUILTIN
    with omc_lock:
      self.client.omc.sendExpression(f'loadFile("{os.path.abspath(path)}")')
      try:
        raw = str(self.client.omc.sendExpression(f"getComponents({name})", parsed=False) or "")
      except Exception:
        raw = ""
    items = parse(raw) if raw else None

    def from_source():  # fallback: read Modelica.* declarations from the source
      code = open(path).read()
      return [m.group(2) for m in re.finditer(r"^\s*(?:inner\s+|outer\s+)?(Modelica\.[\w.]+)\s+(\w+)", code, re.M)
              if ".Units." not in m.group(1) and not m.group(1).startswith("Modelica.SIunits")]

    if not isinstance(items, list) or not items:
      return from_source()
    out = []
    for c in items:
      if not isinstance(c, list) or len(c) < 2:
        continue
      typ, nm = str(c[0]), str(c[1])
      variability = next((str(v) for v in c[2:] if str(v) in ("parameter", "constant", "discrete", "unspecified")), "")
      if typ in BUILTIN or variability in ("parameter", "constant") or ".Units." in typ or typ.startswith(("SI.", "Modelica.SIunits")):
        continue
      out.append(nm)
    return out

  @traced("omc.diagnose", out=lambda r: {"output.value": r})
  def diagnose(self, path: str, name: str) -> str:
    """Structural diagnosis for unbalanced / failing models: which connectors are not connected.
    An unconnected causal input (RealInput, BooleanInput, ...) is the classic cause of 'too few equations';
    an open fluid port or physical connector is the next suspect."""
    from utils.omc_graphics import DiagramBuilder
    try:
      source = open(path).read()
      with omc_lock:
        self.client.omc.sendExpression(f'loadFile("{os.path.abspath(path)}")')
        scene = DiagramBuilder(self.client.omc).scene(name, source)
    except Exception as e:
      return f"(Structural diagnosis unavailable: {e})"
    connected = set()
    for w in scene.get("wires", []):
      for end in (w.get("from", ""), w.get("to", "")):
        connected.add(re.sub(r"\[.*?\]", "", end))
    inputs, fluid, physical = [], [], []
    for c in scene.get("components", []):
      for port, typ in (c.get("port_types") or {}).items():
        full = f'{c["name"]}.{port}'
        if full in connected:
          continue
        t = typ.split(".")[-1]
        line = f"{full} ({t})"
        if re.search(r"(Real|Boolean|Integer)Input$", t):
          inputs.append(line)
        elif "Fluid" in typ or t.endswith(("FluidPort_a", "FluidPort_b", "FluidPorts_b")):
          fluid.append(line)
        elif not re.search(r"(Real|Boolean|Integer)Output$", t):
          physical.append(line)
    parts = []
    if inputs:
      parts.append("UNCONNECTED INPUTS (most likely cause; each needs a connection or a fixed value, or set the "
                   "matching use_..._in=false): " + ", ".join(inputs))
    if fluid:
      parts.append("OPEN FLUID PORTS (connect them, reduce nPorts, or add a boundary): " + ", ".join(fluid))
    if physical:
      parts.append("Unconnected physical connectors (may be optional/conditional): " + ", ".join(physical[:12]))
    if not parts:
      parts.append("All connectors are connected; look for a variable declared without an equation, a missing "
                   "start value/initial equation, or a parameter set to a value that removes an equation.")
    return "STRUCTURAL DIAGNOSIS: " + " | ".join(parts)

  @traced("omc.smoke_test", out=lambda r: {**status_attrs(r), "omc.stop_time": r.get("stop_time")})
  def smoke_test(self, path: str, name: str) -> Dict[str, Any]:
    """Short trial simulation of a scratch model (proposal folder) to catch initialization and runtime
    errors that checkModel cannot see. Same watchdog as real runs (timeout, size cap, chattering hint);
    results stay in the scratch folder."""
    code = open(path).read()
    m = re.search(r"StopTime\s*=\s*([0-9.eE+-]+)", code)
    stop = float(m.group(1)) if m else 1.0
    stop = stop if 0 < stop < 1e7 else 1.0
    folder = os.path.dirname(os.path.abspath(path))
    try:
      res = sim_runner.run(self.client, omc_lock, path, name,
                           {"start_time": 0.0, "stop_time": stop, "number_of_intervals": 200, "method": "dassl"}, {},
                           os.path.join(folder, f"{name}_res.mat"), source="design",
                           timeout_s=float(os.getenv("SMOKE_TIMEOUT_S", "30")), force=True, track=False)
    except Exception as e:
      return {"status": "FAILED", "messages": clean_omc_error(str(e)), "stop_time": stop}
    ok = res["status"] == "SUCCESS"
    return {"status": "SUCCESS" if ok else "FAILED", "stop_time": stop,
            "messages": "" if ok else clean_omc_error(res.get("messages", ""))[-1200:]}

  def validate(self, name: str) -> Dict[str, str]:
    return self.check_file(os.path.abspath(mo_path(name)), name)

  @traced("omc.parameters")
  def parameters(self, name: str) -> Dict[str, Any]:
    with omc_lock:
      self.client.omc.sendExpression(f'loadFile("{os.path.abspath(mo_path(name))}")')
      return {"parameters": self.client.get_model_parameters(name),
              "simulation_options": self.client.get_simulation_options(name)}

  def default_settings(self, name: str) -> Dict[str, Any]:
    last = store.runs(name, 1)
    if last:
      return dict(last[0]["settings"])
    try:
      o = self.parameters(name)["simulation_options"]
      return {"start_time": float(o.get("startTime", 0)), "stop_time": float(o.get("stopTime", 1)),
              "number_of_intervals": int(o.get("numberOfIntervals", 500)), "method": "dassl"}
    except Exception:
      return {"start_time": 0.0, "stop_time": 1.0, "number_of_intervals": 500, "method": "dassl"}

  @traced("omc.simulate", out=lambda r: {**status_attrs(r), "omc.wall_s": r.get("wall"), "sim.settings": r.get("settings"), "sim.overrides": r.get("overrides")})
  def simulate(self, name: str, settings: Optional[Dict[str, Any]] = None,
               overrides: Optional[Dict[str, Any]] = None, source: str = "gui",
               timeout_s: Optional[float] = None, force: bool = False) -> Dict[str, Any]:
    """Runs through utils.sim_runner (stoppable, timeout, size cap, progress). status: SUCCESS | FAILED |
    STOPPED (user/timeout/too_large) | NEEDS_CONFIRM (preflight estimate above the cap) | BUSY."""
    s = {**self.default_settings(name), **{k: v for k, v in (settings or {}).items() if v is not None}}
    s = validate_settings(s)
    ov = _sanitize_overrides(overrides)
    path = os.path.abspath(mo_path(name))
    try:
      res = sim_runner.run(self.client, omc_lock, path, name, s, ov, result_path(name), source=source,
                           timeout_s=timeout_s, force=force)
    except sim_runner.Busy as e:
      return {"status": "BUSY", "settings": s, "overrides": ov, "wall": 0, "messages": str(e), "metrics": {}}
    except sim_runner.NeedsConfirm as e:
      return {"status": "NEEDS_CONFIRM", "settings": s, "overrides": ov, "wall": 0, "messages": str(e), "metrics": {},
              "estimate": {"mb": round(e.estimate_mb, 1), "variables": e.n_vars, "points": e.points,
                           "max_mb": sim_runner.CONFIG["max_mb"]}}
    except Exception as e:  # OMC errors while building
      res = {"status": "FAILED", "reason": "build", "wall": 0, "messages": clean_omc_error(str(e))}
    wall = res["wall"]
    info = {"messages": clean_omc_error(res.get("messages", ""))}
    ok = res["status"] == "SUCCESS"
    metrics = {}
    if ok:
      try:
        metrics = self.metrics(name)
      except Exception as e:  # result file too large or incomplete: report it as a failed run
        ok = False
        info["messages"] = f"{info.get('messages', '')}\n{e}".strip()
    status = "SUCCESS" if ok else ("STOPPED" if res["status"] == "STOPPED" else "FAILED")
    store.add_run(name, s, ov, status, metrics, wall, source)
    return {"status": status, "reason": res.get("reason"), "settings": s, "overrides": ov, "wall": wall,
            "messages": str(info.get("messages", ""))[-1500:], "metrics": metrics}

  # ------------------------------------------------------------ results
  def variables(self, name: str) -> List[str]:
    from api.workspace import read_variable_names
    return read_variable_names(result_path(name))

  def series(self, name: str, vars: List[str]) -> Dict[str, Any]:
    from api.workspace import read_series
    return read_series(result_path(name), vars)

  def metrics(self, name: str, vars: Optional[List[str]] = None, limit: int = 8) -> Dict[str, Any]:
    """Key numbers per variable, computed from the result file (never guessed)."""
    if not os.path.exists(result_path(name)):
      return {}
    all_vars = self.variables(name)
    if not vars:
      vars = [v for v in all_vars if not v.startswith("der(") and re.search(r"\.(v|i|s|phi|w|T|x|y|f|tau)$", v)
              and not re.search(r"\.(p|n|flange_a|flange_b|heatPort)\.", v)] or all_vars
    data = self.series(name, vars[:limit])
    t = np.array(data["time"])
    out = {}
    for v, ys in data["series"].items():
      if ys is None or not len(t):
        continue
      y = np.array(ys)
      final, span = float(y[-1]), float(np.ptp(y))
      m = {"initial": float(y[0]), "final": final, "min": float(y.min()), "max": float(y.max()),
           "t_max": float(t[int(y.argmax())])}
      if span > 0:
        outside = np.where(np.abs(y - final) > 0.02 * span)[0]
        m["settling_time_2pct"] = float(t[outside[-1] + 1]) if len(outside) and outside[-1] + 1 < len(t) else float(t[0])
      out[v] = {k: float(f"{val:.6g}") for k, val in m.items()}
    return out

  # ---------------------------------------------------------- proposals
  def apply_code(self, name: str, code: str, summary: str, source: str = "agent") -> None:
    os.makedirs(model_dir(name), exist_ok=True)
    with open(mo_path(name), "w") as f:
      f.write(code)
    store.add_revision(name, code, summary, source)
    try:
      graph = self.orc.graph_parser.parse_mo_file(mo_path(name))
      with open(os.path.join(model_dir(name), "graph_schema.json"), "w") as f:
        json.dump(graph, f, indent=2)
    except Exception:
      pass
