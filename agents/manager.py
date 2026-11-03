"""Manager Agent ("Modi"): the LangGraph graph that talks to the user.

    manager --(tool calls)--> tools --(proposal?)--> approval --> manager
       |                        |                     (interrupt)
       +--(reply)--> END        +--(otherwise)--> manager

* manager: Claude with tools. Answers greetings and questions from the Model
  Context Card; calls tools only for real work. Out-of-scope requests get a
  one-line refusal (system prompt + output guard + no code-running tools).
* tools: read_model, read_results, validate, simulate, plan_simulation,
  propose_edit, propose_new_model, set_parameters, plot, remember_note.
  propose_* run the design subgraph (designer -> validator -> retry) in a
  scratch folder and only *propose*; nothing is written yet.
* approval: interrupt() pauses the run until the user picks Apply / Discard /
  refine in the UI. Thanks to the SQLite checkpointer this survives reloads
  and restarts. thread_id = model name.
"""
import json
import os
import re
import sqlite3
from typing import Annotated, Any, Dict, List, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.graph import END, MessagesState, StateGraph
from langgraph.prebuilt import InjectedState, ToolNode
from langgraph.types import interrupt

from utils import tracing
from agents.context import build_card, render
from agents.design_graph import _category, build_design_graph
from api import chat_store, store
from api import services as svc

CHECKPOINT_DB = os.getenv("STUDIO_CHECKPOINT_DB", os.path.join("data", "checkpoints.db"))
HISTORY_MESSAGES = 30

SYSTEM_PROMPT = """You are Modi, the modelling assistant of ModelicAI Studio, an expert in Modelica, OpenModelica and \
physical system simulation. ModelicAI Studio was created by Anurag Dogra, its owner and maker; \
say so if asked who built it. You work inside a modelling workspace: you help design, modify, validate, \
simulate and explain the user's Modelica models, and interpret their simulation results.

How to work:
- Greetings, thanks and small talk: reply briefly and warmly, mention the open model if there is one.
- Questions about the open model or its results: answer from the context card below. Use read_results \
for numbers you do not have; never invent values. Show short calculations when helpful.
- Changes to a model (add, remove, rewire, change structure): call propose_edit. New models: call \
propose_new_model. These only create a proposal; the user approves it in the UI. Do not claim a change \
is applied until you are told it was applied.
- Parameter-only changes for experiments: prefer set_parameters (fills the GUI panel) or pass overrides \
to simulate. Permanent value changes in the source go through propose_edit.
- "revalidate"/"check": call validate. "simulate"/"resimulate"/"run": call simulate (reuse the last \
settings unless the user gives new ones). Use plan_simulation only when settings must be chosen \
(new model, results look cut off, solver failed, or the user asks for good settings).
- After tools finish, reply with a short summary of what happened and the key numbers.
- Diagrams: the Diagram tab draws component instances (with their library icons), their connections, and the model's own Diagram-annotation graphics. A pure equation-based model (no component instances) shows its own icon plus any Diagram graphics; that is expected, not an error. If the user wants a richer diagram, offer two options: add Icon/Diagram annotation graphics (visual only), or restructure the model from Modelica Standard Library components (changes the model; propose it via propose_edit).
- Animation: during playback the Diagram tab animates library icons that use DynamicSelect (e.g. the \
OpenTank level fill and level text), shows value badges and flowing wires. There is no separate Icon \
view, and a model's own Icon annotation is only drawn for equation-based models, so never propose \
custom animation graphics for a component-based model; the library icons already animate.
- New models are built from Modelica Standard Library components (enforced by the design loop).
- Errors: when validate or simulate fails (status FAILED, or an error from a tool), explain the cause in \
one or two plain sentences (e.g. "the model has 126 equations for 127 unknowns, so one value is not \
determined; most likely the pump's flow input or a fluid port is not connected"), then offer to fix it. \
When the user asks to fix it (e.g. "fix this", "fix the error"), call propose_edit with an instruction that \
quotes the exact error text and names the likely cause. After the fix is applied, validate and simulate \
again and report the result. Never claim a fix works before it has been checked.
- Write math in plain text or Unicode (tau = R*C, rho*V*c*dT/dt), never LaTeX.
- Keep replies concise. Use the user's language for replies.

"""

from agents.guardrails import SCOPE_REPLY, SCOPE_RULES, check_output
SYSTEM_PROMPT = SYSTEM_PROMPT + "\n\n" + SCOPE_RULES
_ALLOWED_FENCES = {"", "modelica", "mo", "text", "plaintext"}
_GENERAL_CODE = re.compile(r"^\s*(def \w+\(|class \w+\(|print\(|from \w+ import|import (numpy|pandas|os|sys|matplotlib)"
                           r"|function \w+\(|const \w+ =|#include|public static)", re.M)


def _text(msg) -> str:
  c = msg.content
  if isinstance(c, list):
    return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in c)
  return str(c or "")


def guard_reply(text: str) -> str:
  """Output check: replace replies that mention other companies or contain non-Modelica code."""
  if check_output(text):
    return SCOPE_REPLY
  for lang, body in re.findall(r"```([\w+-]*)\n(.*?)```", text, re.S):
    if lang.lower() not in _ALLOWED_FENCES or _GENERAL_CODE.search(body):
      return SCOPE_REPLY
  if _GENERAL_CODE.search(text) and "```" not in text and text.count("\n") > 3:
    return SCOPE_REPLY
  return text


def _recent(messages: List[Any]) -> List[Any]:
  """Last N messages, starting at a user turn so tool calls stay paired."""
  tail = messages[-HISTORY_MESSAGES:]
  while tail and not isinstance(tail[0], HumanMessage):
    tail = tail[1:]
  return tail or messages[-1:]


class ManagerState(MessagesState):
  model: Optional[str]          # model open in the GUI (None = workspace)
  ui: Dict[str, Any]            # GUI context: selected component, plotted vars, pending overrides
  last_action: Dict[str, Any]   # what the approval node did, for the UI


def _ok(**kw) -> str:
  return json.dumps(kw, default=str)


def build_manager(services: "svc.Services", llm=None, checkpointer=None):
  orc = services.orc
  if llm is None:
    from langchain_anthropic import ChatAnthropic
    llm = ChatAnthropic(model=os.getenv("MODEL_NAME", "claude-haiku-5-5"), max_tokens=2048)

  def progress_writer():
    try:
      from langgraph.config import get_stream_writer
      return get_stream_writer()
    except Exception:
      return lambda e: None

  def run_design(prompt: str, base_code: str = "") -> Dict[str, Any]:
    pid = store.add_proposal(None, "draft", {})
    graph = build_design_graph(orc.designer, services.check_file, progress_writer(), services.components,
                               services.smoke_test, services.diagnose)
    # Enforce a component-based diagram unless the user explicitly wants equations, or an edit keeps an
    # existing equation-based model as it is.
    wants_equations = bool(re.search(r"equation[- ]based|equations only|raw equations|pure equations|"
                                     r"without (library )?components|as equations", prompt, re.I))
    base_has_components = bool(re.search(r"^\s*(?:inner\s+)?Modelica\.(?!Units|SIunits)[\w.]+\s+\w+", base_code, re.M))
    rebuild_requested = bool(re.search(r"rebuild|library components|standard library|MSL", prompt, re.I))
    require = not wants_equations and (not base_code or base_has_components or rebuild_requested)
    with tracing.span("design.loop", "CHAIN", agent="designer",
                      **{"design.require_components": require, "design.edit": bool(base_code), "input.value": prompt}) as sp:
      out = graph.invoke({"prompt": prompt, "base_code": base_code, "proposal_id": pid, "require_components": require,
                          "code": "", "error_log": "", "prev_error": "", "status": "PENDING", "retry_count": 0})
      sp.set_attribute("design.status", out.get("status"))
      sp.set_attribute("design.attempts", out.get("retry_count"))
      sp.set_attribute("design.model_name", out.get("model_name") or "")
    return {"pid": pid, **out}

  # ------------------------------------------------------------------ tools
  @tool
  def read_model(name: Optional[str] = None, state: Annotated[dict, InjectedState] = None) -> str:
    """Read the full Modelica source and context card of a model. Defaults to the open model;
    pass another name to compare models."""
    name = name or (state or {}).get("model")
    if not name or not svc.exists(name):
      return _ok(error=f"No such model: {name}", models=svc.list_models())
    card = build_card(services, name)
    card["source"] = svc.read_source(name)
    return _ok(**card)

  @tool
  def read_results(variables: Optional[List[str]] = None, state: Annotated[dict, InjectedState] = None) -> str:
    """Key numbers (initial, final, min, max, time of max, 2% settling time) from the latest
    simulation of the open model. Optionally pass exact variable names, e.g. ["capacitor.v"]."""
    name = (state or {}).get("model")
    if not name or not os.path.exists(svc.result_path(name)):
      return _ok(error="No simulation results yet. Run simulate first.")
    avail = services.variables(name)
    wanted = [v for v in (variables or []) if v in avail] or None
    return _ok(metrics=services.metrics(name, wanted), available_variables=avail[:120])

  @tool
  def validate(state: Annotated[dict, InjectedState] = None) -> str:
    """Re-check the open model with OpenModelica (syntax, types, balanced equations)."""
    name = (state or {}).get("model")
    if not name:
      return _ok(error="No model is open.")
    progress_writer()({"type": "step", "text": f"Checking {name} with OpenModelica"})
    r = services.validate(name)
    details = str(r.get("error") or "")[:1500]
    if r["status"] != "SUCCESS":
      progress_writer()({"type": "step", "text": "Diagnosing the model structure"})
      details += "\n" + services.diagnose(os.path.abspath(svc.mo_path(name)), name)
    return _ok(status=r["status"], details=details)

  @tool
  def simulate(stop_time: Optional[float] = None, start_time: Optional[float] = None,
               number_of_intervals: Optional[int] = None, method: Optional[str] = None,
               overrides: Optional[Dict[str, float]] = None,
               state: Annotated[dict, InjectedState] = None) -> str:
    """Simulate the open model. Omitted settings reuse the last run. overrides maps parameter
    names (e.g. "resistor.R") to values for this run only."""
    name = (state or {}).get("model")
    if not name:
      return _ok(error="No model is open.")
    progress_writer()({"type": "step", "text": f"Simulating {name}"})
    r = services.simulate(name, {"stop_time": stop_time, "start_time": start_time,
                                 "number_of_intervals": number_of_intervals, "method": method},
                          overrides or {}, source="agent")
    if r["status"] in ("STOPPED", "NEEDS_CONFIRM", "BUSY"):
      hints = {"STOPPED": "The run was stopped (reason: %s). Do not simulate again unless the user asks. If it hit the "
                          "timeout or size limit, explain it in plain words (chattering events, too many intervals, long "
                          "stop time) and offer a fix." % r.get("reason"),
               "NEEDS_CONFIRM": "The result file would be larger than the size limit. Tell the user the estimate and "
                                "suggest fewer intervals or a shorter stop time; they can also run it from Simulate.",
               "BUSY": "Another simulation is running. Tell the user to wait or press Stop."}
      return _ok(**r, hint=hints[r["status"]])
    if r["status"] != "SUCCESS":  # no new results: tell Modi why, don't ask the UI to refresh
      progress_writer()({"type": "step", "text": "Diagnosing the model structure"})
      r["diagnosis"] = services.diagnose(os.path.abspath(svc.mo_path(name)), name)
      return _ok(**r, hint="Explain the cause to the user in plain words using the diagnosis, and offer to fix it "
                           "with propose_edit (quote the error and the diagnosis in the instruction).")
    return _ok(**r, ui={"action": "refresh_results", "model": name, "settings": r["settings"]})

  @tool
  def plan_simulation(goal: str, state: Annotated[dict, InjectedState] = None) -> str:
    """Ask the Simulation Agent to choose run settings (stop time, intervals, solver, overrides)
    for a goal. Creates a proposal the user approves; on approval the run starts."""
    name = (state or {}).get("model")
    if not name:
      return _ok(error="No model is open.")
    progress_writer()({"type": "step", "text": "Simulation agent: choosing run settings"})
    from agents.simulation import experiment_of
    card = build_card(services, name)
    runs = store.runs(name, 3)
    last_error = next((r.get("metrics", {}).get("error") for r in runs if r["status"] == "FAILED"), None)
    context = {"experiment": experiment_of(svc.read_source(name)), "components": card.get("components", []),
               "last_run": card.get("last_run"), "last_error": last_error}
    cfg = orc.sim_agent.suggest_simulation_config(
        model_name=name, parameters={"model_parameters": card.get("parameters", {}), "last_run": card.get("last_run")},
        user_intent=goal, context=context) if _accepts_context(orc.sim_agent) else orc.sim_agent.suggest_simulation_config(
        model_name=name, parameters={"model_parameters": card.get("parameters", {}), "last_run": card.get("last_run")},
        user_intent=goal)
    settings = {"start_time": cfg.get("startTime", 0.0), "stop_time": cfg.get("stopTime", 1.0),
                "number_of_intervals": cfg.get("numberOfIntervals", 500), "method": cfg.get("method", "dassl")}
    try:  # final gate: the same checks every simulation goes through
      settings = svc.validate_settings(settings)
    except ValueError as e:
      return _ok(error=f"The Simulation agent's settings failed the checks: {e}")
    for check in cfg.get("checks", []):
      progress_writer()({"type": "step", "text": "Simulation agent: " + check})
    payload = {"settings": settings, "overrides": cfg.get("parameter_overrides", {}) or {},
               "summary": f"Run {name} for {settings['stop_time']:g} s with {settings['method']}, "
                          f"{settings['number_of_intervals']} intervals" + (f". {cfg['reason']}" if cfg.get("reason") else ""),
               "goal": goal, "checks": cfg.get("checks", [])}
    pid = store.add_proposal(name, "sim_settings", payload)
    return _ok(proposal_id=pid, kind="sim_settings", **payload)

  @tool
  def propose_edit(instruction: str, state: Annotated[dict, InjectedState] = None) -> str:
    """Change the open model's structure or source (add/remove/rewire components, change
    defaults). Runs the designer and compiler in a scratch copy and returns a proposal with a
    diff for the user to approve. Nothing is applied yet."""
    name = (state or {}).get("model")
    if not name:
      return _ok(error="No model is open. Use propose_new_model.")
    base = svc.read_source(name)
    if re.search(r"fix|error|fail|equation|determined|initiali[sz]", instruction, re.I):
      # Ground the fix in facts: the latest check/simulation error and which connectors are open.
      progress_writer()({"type": "step", "text": "Diagnosing the model before fixing it"})
      check = services.validate(name)
      facts = []
      if check["status"] != "SUCCESS":
        facts.append("Current OpenModelica check result: " + str(check.get("error", ""))[:1200])
      last = store.runs(name, 1)
      if last and last[0]["status"] == "FAILED":
        facts.append("The last simulation failed.")
      facts.append(services.diagnose(os.path.abspath(svc.mo_path(name)), name))
      instruction = instruction + "\n\nFacts for the fix:\n" + "\n".join(facts)
    out = run_design(instruction, base)
    if out["status"] != "SUCCESS":
      return _ok(error=f"The edit did not pass the checks after {out['retry_count']} attempts.", last_error=str(out.get("error_log"))[:1500])
    if out["model_name"] != name:
      out["code"] = re.sub(rf"\b{re.escape(out['model_name'])}\b", name, out["code"])
    d = svc.diff(base, out["code"], name)
    payload = {"code": out["code"], "diff": d, "instruction": instruction,
               "summary": instruction.strip().rstrip(".")[:160], "attempts": out["retry_count"]}
    pid = out["pid"]
    store.set_proposal_status(pid, "pending")
    _store_payload(pid, name, "edit", payload)
    return _ok(proposal_id=pid, kind="edit", model=name, attempts=out["retry_count"],
               changed_lines=sum(1 for l in d.splitlines() if l[:1] in "+-" and l[:3] not in ("+++", "---")))

  @tool
  def propose_new_model(description: str) -> str:
    """Design a brand-new model from a description. Runs the designer and compiler in a scratch
    copy and returns a proposal for the user to approve."""
    out = run_design(description)
    if out["status"] != "SUCCESS":
      return _ok(error=f"The model did not pass the checks after {out['retry_count']} attempts.",
                 last_error=str(out.get("error_log"))[:1500], error_category=_category(out.get("error_log") or ""))
    name = out["model_name"]
    payload = {"code": out["code"], "model_name": name, "description": description,
               "summary": f"New model {name}" + (" (replaces existing!)" if svc.exists(name) else ""),
               "attempts": out["retry_count"], "exists": svc.exists(name)}
    _store_payload(out["pid"], name, "new_model", payload)
    return _ok(proposal_id=out["pid"], kind="new_model", model=name, attempts=out["retry_count"])

  @tool
  def set_parameters(values: Dict[str, float], state: Annotated[dict, InjectedState] = None) -> str:
    """Fill parameter values into the GUI parameter panel as run-time overrides (not saved in the
    source). The user then runs, or you call simulate with the same overrides."""
    return _ok(status="filled in the parameter panel", ui={"action": "set_parameters", "values": values})

  @tool
  def plot(variables: List[str]) -> str:
    """Show these result variables in the plot panel, e.g. ["mass.s", "spring.f"]."""
    return _ok(status="plotted", ui={"action": "plot", "variables": variables})

  @tool
  def remember_note(text: str, state: Annotated[dict, InjectedState] = None) -> str:
    """Store a lasting fact or goal the user stated about the open model
    (e.g. "settling time must be under 2 s")."""
    name = (state or {}).get("model") or chat_store.WORKSPACE
    store.add_note(name, text)
    return _ok(status="noted")

  tools = [read_model, read_results, validate, simulate, plan_simulation, propose_edit,
           propose_new_model, set_parameters, plot, remember_note]
  llm_tools = llm.bind_tools(tools)

  def _store_payload(pid, model, kind, payload):
    import hashlib
    if model and svc.exists(model):  # base version, so Apply can detect edits made in the meantime
      payload = {**payload, "base_hash": hashlib.sha1(svc.read_source(model).encode()).hexdigest()}
    with store._lock, store._conn() as c:
      c.execute("UPDATE proposals SET model=?, kind=?, payload=?, status='pending' WHERE id=?",
                (model, kind, json.dumps(payload), pid))

  # ------------------------------------------------------------------ nodes
  def manager(state: ManagerState):
    card = build_card(services, state.get("model"))
    system = SystemMessage(SYSTEM_PROMPT + "\n\n" + render(card, state.get("ui")))
    resp = llm_tools.invoke([system] + _recent(state["messages"]))
    if not getattr(resp, "tool_calls", None):
      clean = guard_reply(_text(resp))
      if clean != _text(resp):
        resp = AIMessage(content=clean, id=resp.id)
    return {"messages": [resp], "last_action": {}}

  def pending_proposal(state: ManagerState) -> Optional[str]:
    for m in reversed(state["messages"]):
      if not isinstance(m, ToolMessage):
        break
      try:
        data = json.loads(m.content)
      except (TypeError, ValueError):
        continue
      if isinstance(data, dict) and data.get("proposal_id"):
        return data["proposal_id"]
    return None

  def approval(state: ManagerState, config: RunnableConfig):
    pid = pending_proposal(state)
    p = store.proposal(pid)
    public = {k: v for k, v in p["payload"].items() if k != "code"}
    decision = interrupt({"proposal_id": pid, "kind": p["kind"], "model": p["model"], **public})
    action = (decision or {}).get("action", "discard")
    text = (decision or {}).get("text", "")
    if action == "apply":
      result = apply_proposal(services, p, origin=_last_user_text(state),
                              thread=(config.get("configurable") or {}).get("thread_id"))
      store.set_proposal_status(pid, "applied")
      note = f"[system] The user APPLIED proposal {pid}. Result: {json.dumps(result, default=str)}"
      new_model = result.get("model") if p["kind"] == "new_model" else state.get("model")
      return {"messages": [HumanMessage(content=note)], "model": new_model,
              "last_action": {"action": "applied", "kind": p["kind"], **result}}
    if action == "refine":
      store.set_proposal_status(pid, "refined")
      note = f"[system] The user wants changes to proposal {pid} before applying: {text}"
      return {"messages": [HumanMessage(content=note)], "last_action": {"action": "refine"}}
    store.set_proposal_status(pid, "discarded")
    note = f"[system] The user DISCARDED proposal {pid}. Nothing was changed." + (f" They said: {text}" if text else "")
    return {"messages": [HumanMessage(content=note)], "last_action": {"action": "discarded", "kind": p["kind"]}}

  def after_manager(state: ManagerState) -> str:
    last = state["messages"][-1]
    return "tools" if getattr(last, "tool_calls", None) else END

  def after_tools(state: ManagerState) -> str:
    return "approval" if pending_proposal(state) else "manager"

  g = StateGraph(ManagerState)
  g.add_node("manager", manager)
  g.add_node("tools", ToolNode(tools, handle_tool_errors=True))  # a failing tool becomes an error result, not a crash
  g.add_node("approval", approval)
  g.set_entry_point("manager")
  g.add_conditional_edges("manager", after_manager, {"tools": "tools", END: END})
  g.add_conditional_edges("tools", after_tools, {"approval": "approval", "manager": "manager"})
  g.add_edge("approval", "manager")

  if checkpointer is None:
    from langgraph.checkpoint.sqlite import SqliteSaver
    os.makedirs(os.path.dirname(CHECKPOINT_DB) or ".", exist_ok=True)
    checkpointer = SqliteSaver(sqlite3.connect(CHECKPOINT_DB, check_same_thread=False))
  return g.compile(checkpointer=checkpointer)


def _accepts_context(agent) -> bool:
  import inspect
  try:
    return "context" in inspect.signature(agent.suggest_simulation_config).parameters
  except (TypeError, ValueError):
    return False


def _last_user_text(state) -> str:
  for m in reversed(state["messages"]):
    if isinstance(m, HumanMessage) and not _text(m).startswith("[system]"):
      return _text(m)
  return ""


def apply_proposal(services: "svc.Services", p: Dict[str, Any], origin: str = "",
                   thread: Optional[str] = None) -> Dict[str, Any]:
  """Carries out an approved proposal. Returns facts for the agent and the UI."""
  pl = p["payload"]
  if p["kind"] == "edit":
    services.apply_code(p["model"], pl["code"], pl.get("summary", "edit"), source="agent")
    return {"model": p["model"], "saved": True}
  if p["kind"] == "new_model":
    name = pl["model_name"]
    services.apply_code(name, pl["code"], "created: " + pl.get("description", "")[:120], source="agent")
    store.set_meta(name, origin_prompt=origin or pl.get("description"), purpose=pl.get("description"))
    if thread and re.match(r"^_new_[a-z0-9]{4,16}$", thread):
      # The model inherits the draft's conversation and tracing session (one story in Phoenix).
      chat_store.copy(thread, name)
      store.link_model_session(name, thread)
      store.set_draft_outcome(thread, "created", model=name)
    elif origin:
      chat_store.add(name, "user", origin, {"origin": True})
    chat_store.add(name, "assistant", f"Created {name} from this request. It compiles; ask me to simulate or change it.",
                   {"status": "SUCCESS"})
    return {"model": name, "created": True}
  if p["kind"] == "sim_settings":
    r = services.simulate(p["model"], pl["settings"], pl.get("overrides", {}), source="agent")
    return {"model": p["model"], "status": r["status"], "metrics": r["metrics"], "settings": r["settings"],
            "ui": {"action": "refresh_results", "model": p["model"], "settings": r["settings"]}}
  return {"error": f"Unknown proposal kind {p['kind']}"}
