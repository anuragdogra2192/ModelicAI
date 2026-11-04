"""Streaming chat endpoints for the Manager Agent (Server-Sent Events).

POST /api/studio/agent          {message, model, ui}               -> event stream
POST /api/studio/agent/resume   {model, proposal_id, action, text} -> event stream
GET  /api/studio/agent/pending?model=...                           -> open approval, if any

Event types sent to the browser:
  step        progress from tools / the design subgraph
  tool        a tool call started            tool_result  it finished
  approval    a proposal waits for Apply / Discard / refine
  applied     the approval node acted (UI reloads model / results)
  ui          plot / set_parameters / refresh_results
  message     assistant text                 done / error
"""
import json
import os
import queue
import re
import threading
from typing import Any, Dict, Iterator, Optional

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command
from pydantic import BaseModel

from agents.guardrails import check_input
from api import cancel, chat_store, store
from api import services as svc
from utils import tracing

_lock = threading.Lock()  # one agent run at a time (single OMC session, shared designer)


def _text(msg) -> str:
  c = msg.content
  if isinstance(c, list):
    return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in c)
  return str(c or "")


def build_agent_router(graph) -> APIRouter:
  router = APIRouter(prefix="/api/studio/agent", tags=["agent"])

  def is_draft(name: Optional[str]) -> bool:
    # "+ New" opens a fresh draft conversation: _new_<random>
    return bool(name and re.match(r"^_new_[a-z0-9]{4,16}$", name))

  def thread_of(model: Optional[str]) -> str:
    if model and svc.exists(model):
      return model
    return model if is_draft(model) else chat_store.WORKSPACE

  def sse(events: Iterator[Dict[str, Any]]) -> StreamingResponse:
    def gen():
      for e in events:
        yield f"data: {json.dumps(e, default=str)}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

  def run(graph_input, thread: str, kind: str = "message", text: str = "") -> Iterator[Dict[str, Any]]:
    """Runs the graph in its own worker thread (one trace = one turn, session = model) and streams its events.

    Starlette pulls a sync generator from changing pool threads, which would break the tracing context,
    so the turn runs start to finish in one thread and hands events over through a queue."""
    q: "queue.Queue" = queue.Queue()
    END = object()

    def work():
      draft = is_draft(thread)
      try:
        with tracing.span(f"modi.{kind}", "AGENT", agent="modi", session=store.session_of(thread),
                          **{"input.value": text, "modi.draft": draft}) as sp:
          tid, sid = tracing.trace_id(sp), tracing.span_id(sp)
          if draft:
            store.touch_draft(thread, text if kind == "message" else "", sid)
            sp.set_attribute("draft.id", thread)
          produced, outcome = None, None
          for e in _run(graph_input, thread):
            if e.get("type") == "_design_failed":  # internal: not sent to the browser
              outcome = ("design_failed", e.get("category"), e.get("error", ""))
              continue
            if e.get("type") == "applied" and e.get("kind") == "new_model":
              outcome = ("created", None, "") if e.get("action") == "applied" else ("discarded", None, text)
            if e.get("type") == "approval":
              sp.set_attribute("modi.proposal", True)
            if e.get("type") == "applied":
              sp.set_attribute("modi.decision", str(e.get("action") or e.get("status") or ""))
            if e.get("type") == "failed":
              sp.set_attribute("modi.failed", e.get("kind"))
            if e.get("type") == "error":
              sp.set_attribute("error.message", e.get("text", ""))
            if e.get("type") == "done":
              produced = e.get("model")
              if tid:
                e = {**e, "trace_id": tid}
            q.put(e)
          if produced and produced != thread:  # a draft turned into a model: link it for filtering
            sp.set_attribute("modelica.model", produced)
            sp.set_attribute("modi.created_model", produced)
          if draft and outcome:
            label, category, why = outcome
            if label != "created":  # "created" is recorded by apply_proposal (with the model name)
              store.set_draft_outcome(thread, label, category=category)
            sp.set_attribute("draft.outcome", label)
            if category:
              sp.set_attribute("error.category", category)
            tracing.annotate(sid, "draft.outcome", label, score=1.0 if label == "created" else 0.0,
                             explanation=why, metadata={"draft": thread, "model": produced or "",
                                                        "error_category": category or ""})
      except Exception as ex:
        q.put({"type": "error", "text": f"{type(ex).__name__}: {ex}"})
      finally:
        q.put(END)

    threading.Thread(target=work, daemon=True).start()
    while True:
      e = q.get()
      if e is END:
        return
      yield e

  def _run(graph_input, thread: str) -> Iterator[Dict[str, Any]]:
    # LangGraph puts thread_id (= model name) into the run metadata, and the OpenInference LangChain
    # instrumentor uses it as session.id unless session_id is given. Pass the real session explicitly.
    config = {"configurable": {"thread_id": thread}, "metadata": {"session_id": store.session_of(thread)}}
    replies, model_after = [], thread if (thread != chat_store.WORKSPACE and not is_draft(thread)) else None
    with _lock:
      cancel.STOP.clear()
      try:
        for mode, chunk in graph.stream(graph_input, config, stream_mode=["updates", "custom"]):
          if cancel.STOP.is_set():
            _close_turn(config)
            chat_store.add(thread, "assistant", "Stopped.", {"status": "STOPPED"})
            yield {"type": "message", "text": "Stopped."}
            yield {"type": "done", "model": model_after, "pending": False, "stopped": True}
            return
          if mode == "custom":
            yield chunk
            continue
          for node, upd in (chunk or {}).items():
            if node == "__interrupt__":
              for intr in upd:
                payload = intr.value
                chat_store.add(thread, "assistant", payload.get("summary", "Proposal"),
                               {"kind": "proposal", "proposal": payload})
                yield {"type": "approval", **payload}
              continue
            if not upd:
              continue
            if node == "manager":
              msg = upd["messages"][-1]
              text = _text(msg).strip()
              if text:
                replies.append(text)
                yield {"type": "message", "text": text}
              for tc in getattr(msg, "tool_calls", None) or []:
                yield {"type": "tool", "name": tc["name"], "args": tc.get("args", {})}
            elif node == "tools":
              for m in upd.get("messages", []):
                if not isinstance(m, ToolMessage):
                  continue
                try:
                  data = json.loads(m.content)
                except (TypeError, ValueError):
                  data = {}
                if m.name == "propose_new_model" and isinstance(data, dict) and data.get("error"):
                  yield {"type": "_design_failed", "category": data.get("error_category"),
                         "error": str(data.get("last_error") or data.get("error"))[:2000]}
                if isinstance(data, dict) and data.get("ui"):
                  yield {"type": "ui", **data["ui"]}
                if isinstance(data, dict) and m.name in ("simulate", "validate") and data.get("status") == "FAILED":
                  yield {"type": "failed", "kind": "simulation" if m.name == "simulate" else "check",
                         "message": "\n".join(x for x in (data.get("messages") or data.get("details") or "Failed",
                                                           data.get("diagnosis") or "") if x)}
                yield {"type": "tool_result", "name": m.name,
                       "ok": not (isinstance(data, dict) and data.get("error")),
                       "error": data.get("error") if isinstance(data, dict) else None}
            elif node == "approval":
              act = upd.get("last_action") or {}
              if act.get("model"):
                model_after = act["model"]
              if isinstance(act.get("ui"), dict):
                yield {"type": "ui", **act["ui"]}
              yield {"type": "applied", **{k: v for k, v in act.items() if k != "ui"}}
      except Exception as e:  # surface failures in the chat instead of a dead stream
        yield {"type": "error", "text": f"{type(e).__name__}: {e}"}
        chat_store.add(thread, "assistant", f"Something went wrong: {e}", {"status": "FAILED"})
        return
    if replies:
      chat_store.add(thread, "assistant", "\n\n".join(replies), {"status": "SUCCESS"})
    pending = _pending(thread)
    yield {"type": "done", "model": model_after, "pending": bool(pending)}

  def _close_turn(config) -> None:
    """After Stop: answer any tool call left open and end the turn, so the next message starts cleanly
    (Claude rejects a conversation with a tool call that has no result)."""
    try:
      st = graph.get_state(config)
      msgs = st.values.get("messages", [])
      answered = {m.tool_call_id for m in msgs if isinstance(m, ToolMessage)}
      last_ai = next((m for m in reversed(msgs) if isinstance(m, AIMessage)), None)
      open_calls = [tc for tc in (getattr(last_ai, "tool_calls", None) or []) if tc["id"] not in answered]
      add = [ToolMessage(content=json.dumps({"error": "Stopped by the user."}), tool_call_id=tc["id"], name=tc["name"])
             for tc in open_calls]
      graph.update_state(config, {"messages": add + [AIMessage(content="Stopped by the user.")]}, as_node="manager")
    except Exception as e:
      print(f"Stop: could not close the turn cleanly: {e}")

  @router.post("/stop")
  def stop():
    """Stop button: ends Modi's turn at the next step and stops a running simulation."""
    from utils import sim_runner
    cancel.STOP.set()
    sim_runner.request_stop()
    return {"stopping": True}

  @router.get("/proposal/{pid}/check")
  def proposal_check(pid: str):
    """Has the model changed since this proposal was made? (Apply would overwrite those changes.)"""
    import hashlib
    p = store.proposal(pid)
    if not p:
      return {"stale": False}
    base, model = p["payload"].get("base_hash"), p.get("model")
    if not base or not model or not svc.exists(model):
      return {"stale": False}
    return {"stale": hashlib.sha1(svc.read_source(model).encode()).hexdigest() != base, "model": model}

  def _pending(thread: str) -> Optional[Dict[str, Any]]:
    try:
      st = graph.get_state({"configurable": {"thread_id": thread}})
    except Exception:
      return None
    for task in st.tasks or ():
      for intr in getattr(task, "interrupts", ()) or ():
        return intr.value
    return None

  class AgentRequest(BaseModel):
    message: str
    model: Optional[str] = None
    ui: Dict[str, Any] = {}

  @router.post("")
  def chat(req: AgentRequest):
    thread = thread_of(req.model)
    refusal = check_input(req.message)
    if refusal:  # layer 1: off-topic messages never reach the model
      with tracing.span("modi.message", "AGENT", agent="guardrail", session=store.session_of(thread),
                        **{"input.value": req.message, "guardrail.refused": True, "output.value": refusal}) as sp:
        if is_draft(thread):
          store.touch_draft(thread, req.message, tracing.span_id(sp))
          store.set_draft_outcome(thread, "refused")
          sp.set_attribute("draft.outcome", "refused")
          tracing.annotate(tracing.span_id(sp), "draft.outcome", "refused", score=0.0,
                           explanation=req.message, metadata={"draft": thread})
      chat_store.add(thread, "user", req.message)
      chat_store.add(thread, "assistant", refusal, {"status": "SUCCESS", "guardrail": True})
      return sse(iter([{"type": "message", "text": refusal},
                       {"type": "done", "model": None if (thread == chat_store.WORKSPACE or is_draft(thread)) else thread,
                        "pending": bool(_pending(thread))}]))
    if _pending(thread):
      # A proposal is waiting. A plain "apply"/"yes" or "discard"/"no" typed in the chat is the same as the
      # card's buttons; anything else asks Modi to refine the proposal.
      chat_store.add(thread, "user", req.message)
      word = req.message.strip().lower().rstrip(".!")
      action = ("apply" if word in ("apply", "yes", "ok", "okay", "approve", "accept", "do it", "go ahead", "apply it")
                else "discard" if word in ("discard", "no", "reject", "cancel", "discard it") else "refine")
      return sse(run(Command(resume={"action": action, "text": "" if action != "refine" else req.message}),
                     thread, "decision." + action if action != "refine" else "refine", req.message))
    chat_store.add(thread, "user", req.message)
    graph_input = {"messages": [HumanMessage(content=req.message)],
                   "model": None if (thread == chat_store.WORKSPACE or is_draft(thread)) else thread, "ui": req.ui}
    return sse(run(graph_input, thread, "message", req.message))

  class ResumeRequest(BaseModel):
    model: Optional[str] = None
    proposal_id: str
    action: str  # apply | discard | refine
    text: str = ""

  @router.post("/resume")
  def resume(req: ResumeRequest):
    thread = thread_of(req.model)
    label = {"apply": "Applied", "discard": "Discarded", "refine": "Asked for changes"}.get(req.action, req.action)
    chat_store.add(thread, "user", f"{label} proposal" + (f": {req.text}" if req.text else ""),
                   {"kind": "decision", "proposal_id": req.proposal_id, "action": req.action})
    return sse(run(Command(resume={"action": req.action, "text": req.text}), thread, f"decision.{req.action}",
                   req.text))

  @router.get("/pending")
  def pending(model: Optional[str] = None):
    return {"pending": _pending(thread_of(model))}

  # Forget the agent's conversation for a deleted model (thread_id = model name).
  def _forget(name: str, draft_ids=()):
    cp = getattr(graph, "checkpointer", None)
    if cp is not None and hasattr(cp, "delete_thread"):
      for t in (name, *draft_ids):
        cp.delete_thread(t)

  # Drafts with no activity for DRAFT_ABANDON_HOURS (default 24) are labelled "abandoned".
  def _sweep():
    import time as _t
    hours = float(os.getenv("DRAFT_ABANDON_HOURS", "24"))
    while True:
      try:
        for d in store.stale_drafts(hours * 3600):
          if store.set_draft_outcome(d["id"], "abandoned"):
            tracing.annotate(d.get("last_span_id"), "draft.outcome", "abandoned", score=0.0,
                             explanation=d.get("first_prompt") or "", metadata={"draft": d["id"], "turns": d["turns"]})
      except Exception as e:
        print(f"Draft sweep failed: {e}")
      _t.sleep(3600)
  threading.Thread(target=_sweep, daemon=True).start()
  from api import workspace
  workspace.on_model_deleted.append(_forget)

  return router
