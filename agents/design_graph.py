"""The original designer -> validator -> retry StateGraph, revived as a subgraph.

Same nodes and routing as the earlier main.py pipeline, with two changes:
  * it compiles in a scratch folder (models/.proposals/<id>/), so a proposal
    never touches the real model until the user approves it;
  * for edits it starts from the current source (`base_code`).
"""
import os
import re
from typing import Callable, Optional, TypedDict

from langgraph.graph import END, StateGraph

from api import services as svc
from api import cancel
from utils import tracing


class DesignState(TypedDict, total=False):
  prompt: str          # what to build or change
  base_code: str       # current source when editing ("" for a new model)
  proposal_id: str     # scratch folder name
  model_name: str      # extracted from the generated code
  code: str
  error_log: str
  prev_error: str      # error of the attempt before, to detect repeats
  status: str          # PENDING / SUCCESS / FAILED
  retry_count: int
  require_components: bool  # enforce a component-based (diagram) model


# Attempts per design request (compile + balance + structure + test run must all pass).
# Configurable with DESIGN_MAX_RETRIES (1..10), default 5.
try:
  MAX_RETRIES = max(1, min(10, int(os.getenv("DESIGN_MAX_RETRIES", "5"))))
except ValueError:
  MAX_RETRIES = 5


MIN_COMPONENTS = 2
STRUCTURE_FEEDBACK = (
    "STRUCTURE CHECK FAILED: the model compiles but has only {n} component instance(s) ({names}), so it has "
    "no diagram. Rebuild it from Modelica Standard Library components connected with connect(): replace the "
    "raw equations (der(...) = ...) by library components (e.g. Translational Mass/Spring/Damper, Thermal "
    "HeatCapacitor/ThermalConductor, Electrical Resistor/Capacitor, Fluid OpenTank/MassFlowSource_T, "
    "Blocks Integrator/Gain/Add). Keep the same physics, parameters and model name, and give every "
    "component a Placement and every connect() a Line route.")


def _same_error(a: str, b: str) -> bool:
  """Errors match if they are equal after removing numbers, paths and whitespace noise."""
  norm = lambda t: re.sub(r"[\d./\\]+", "#", re.sub(r"\s+", " ", t or "")).strip()[:400]
  return bool(a and b) and norm(a) == norm(b)


def _category(err: str) -> str:
  """Coarse failure class for filtering in Phoenix."""
  rules = [("structure", r"STRUCTURE CHECK"), ("unbalanced", r"equation\(s\) and \d+ variable|under-determined|over-determined"),
           ("initialization", r"initialization failed|initial"), ("connector", r"connect|connector|incompatible"),
           ("syntax", r"syntax|parse|expected|missing token"), ("lookup", r"not found|lookup|undeclared|class .* not"),
           ("runtime", r"division by zero|assert|integrator|solver failed")]
  for name, pat in rules:
    if re.search(pat, err, re.I):
      return name
  return "other"


def build_design_graph(designer, check_file: Callable[[str, str], dict],
                       progress: Callable[[dict], None] = lambda e: None,
                       count_components: Optional[Callable[[str, str], list]] = None,
                       smoke_test: Optional[Callable[[str, str], dict]] = None,
                       diagnose: Optional[Callable[[str, str], str]] = None):
  """designer: DesignAgent. check_file(path, model_name) -> {status, error}.
  count_components(path, model_name) -> list of component instance names (for the structure check).
  smoke_test(path, model_name) -> {status, messages}: short trial simulation (initialization/runtime).
  diagnose(path, model_name) -> text naming unconnected inputs/ports, added to equation-count errors."""

  def with_diagnosis(error: str, path: str, name: str) -> str:
    if diagnose is not None and re.search(r"equation\(s\)|under-determined|over-determined|too (few|many) equations|"
                                          r"initialization failed|singular", error or "", re.I):
      return f"{error}\n{diagnose(path, name)}"
    return error

  def designer_node(state: DesignState) -> DesignState:
    with tracing.span("designer.attempt", "AGENT", agent="designer",
                      **{"design.attempt": state.get("retry_count", 0) + 1,
                         "design.edit": bool(state.get("base_code")),
                         "input.value": state.get("error_log") or state["prompt"]}) as sp:
      out = _designer(state)
      sp.set_attribute("design.same_error", "SAME error" in (out.get("error_log") or "") or
                       _same_error(state.get("error_log") or "", state.get("prev_error", "")))
      sp.set_attribute("output.value", (out.get("code") or "")[:4000])
      return out

  def _designer(state: DesignState) -> DesignState:
    cancel.check()  # the user pressed Stop: end the loop before the next LLM call
    attempt = state.get("retry_count", 0) + 1
    progress({"type": "step", "text": f"Designer: writing Modelica (attempt {attempt}/{MAX_RETRIES})"})
    request = state["prompt"]
    if state.get("base_code"):
      request = (
          "Modify the existing Modelica model below. Keep everything that the request does not ask to change, "
          "including the model name, existing component names, parameters, Placement and Line annotations. "
          "Give new components a Placement next to the components they relate to and route new connections.\n\n"
          f"Change request: {state['prompt']}\n\nCurrent model:\n```modelica\n{state['base_code']}\n```")
    error = state.get("error_log") or ""
    if error and _same_error(error, state.get("prev_error", "")):
      # Same failure twice in a row: the last change did not help, so ask for a different approach.
      error += ("\n\nNOTE: this is the SAME error as in the previous attempt, so your last change did not fix it. "
                "Do not repeat it. Re-read the error and the structural diagnosis, and change the approach "
                "(e.g. connect or remove the named connector, switch use_..._in off, use a simpler component).")
      progress({"type": "step", "text": "Designer: same error as before, asking for a different approach"})
    code = designer.generate_code(prompt=request, error_log=error or None)
    return {**state, "code": code, "retry_count": attempt, "prev_error": state.get("error_log") or ""}

  def validator_node(state: DesignState) -> DesignState:
    with tracing.span("validator", "CHAIN", agent="validator",
                      **{"design.attempt": state.get("retry_count", 0)}) as sp:
      out = _validator(state)
      err = out.get("error_log") or ""
      gate = ("structure" if err.startswith("STRUCTURE") else "smoke_test" if err.startswith("SIMULATION TEST")
              else "compile" if out["status"] != "SUCCESS" else None)
      sp.set_attribute("design.status", out["status"])
      sp.set_attribute("design.model_name", out.get("model_name") or "")
      if gate:
        sp.set_attribute("design.failed_gate", gate)
        sp.set_attribute("design.error", err[:3000])
        sp.set_attribute("error.category", _category(err))
      return out

  def _validator(state: DesignState) -> DesignState:
    code = state["code"]
    name = svc.model_name_from_code(code) or "Model"
    folder = os.path.join(svc.PROPOSAL_ROOT, state["proposal_id"])
    os.makedirs(folder, exist_ok=True)
    path = os.path.abspath(os.path.join(folder, f"{name}.mo"))
    with open(path, "w") as f:
      f.write(code)
    progress({"type": "step", "text": f"Validator: checking {name} with OpenModelica"})
    result = check_file(path, name)
    if result["status"] != "SUCCESS":
      progress({"type": "step", "text": "Validator: failed, feeding the compiler error back to the designer",
                "detail": str(result.get("error", ""))[:600]})
      return {**state, "model_name": name, "status": "FAILED", "error_log": with_diagnosis(result.get("error", ""), path, name)}
    progress({"type": "step", "text": f"Validator: {name} compiles"})
    # Structure check: a diagram needs library components, not just equations.
    if state.get("require_components") and count_components is not None:
      comps = count_components(path, name)
      if len(comps) < MIN_COMPONENTS:
        msg = STRUCTURE_FEEDBACK.format(n=len(comps), names=", ".join(comps) or "none")
        progress({"type": "step", "text": f"Structure check: only {len(comps)} library component(s), asking the designer "
                                          "to rebuild it from Modelica Standard Library components", "detail": msg})
        return {**state, "model_name": name, "status": "FAILED", "error_log": msg}
      progress({"type": "step", "text": f"Structure check: {len(comps)} library components, diagram OK"})
    # Test run: checkModel cannot see initialization or runtime failures, a short simulation can.
    if smoke_test is not None:
      progress({"type": "step", "text": f"Test run: simulating {name} briefly to check it initializes and runs"})
      run = smoke_test(path, name)
      if run["status"] != "SUCCESS":
        msg = ("SIMULATION TEST FAILED: the model compiles but the trial simulation (0 to "
               f"{run.get('stop_time')} s) failed:\n{with_diagnosis(run.get('messages', ''), path, name)}\n"
               "Fix the cause: consistent start values / initial equations, missing boundary conditions, "
               "division by zero, or unrealistic parameter values. Keep the model name and structure.")
        progress({"type": "step", "text": "Test run: failed, feeding the simulation error back to the designer",
                  "detail": msg})
        return {**state, "model_name": name, "status": "FAILED", "error_log": msg}
      progress({"type": "step", "text": "Test run: initializes and simulates"})
    return {**state, "model_name": name, "status": "SUCCESS", "error_log": ""}

  def should_retry(state: DesignState) -> str:
    if state["status"] == "SUCCESS":
      return "success"
    if state["retry_count"] >= MAX_RETRIES:
      return "max_retries"
    return "retry"

  g = StateGraph(DesignState)
  g.add_node("designer", designer_node)
  g.add_node("validator", validator_node)
  g.set_entry_point("designer")
  g.add_edge("designer", "validator")
  g.add_conditional_edges("validator", should_retry,
                          {"success": END, "retry": "designer", "max_retries": END})
  return g.compile()
