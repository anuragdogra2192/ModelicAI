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
import re
import threading
from typing import Any, Dict, Iterator, Optional

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command
from pydantic import BaseModel

from agents.guardrails import check_input
from api import chat_store
from api import services as svc

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

  def run(graph_input, thread: str) -> Iterator[Dict[str, Any]]:
    config = {"configurable": {"thread_id": thread}}
    replies, model_after = [], thread if (thread != chat_store.WORKSPACE and not is_draft(thread)) else None
    with _lock:
      try:
        for mode, chunk in graph.stream(graph_input, config, stream_mode=["updates", "custom"]):
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
      chat_store.add(thread, "user", req.message)
      chat_store.add(thread, "assistant", refusal, {"status": "SUCCESS", "guardrail": True})
      return sse(iter([{"type": "message", "text": refusal},
                       {"type": "done", "model": None if (thread == chat_store.WORKSPACE or is_draft(thread)) else thread,
                        "pending": bool(_pending(thread))}]))
    if _pending(thread):
      # A proposal is waiting: treat the message as a refinement request.
      chat_store.add(thread, "user", req.message)
      return sse(run(Command(resume={"action": "refine", "text": req.message}), thread))
    chat_store.add(thread, "user", req.message)
    graph_input = {"messages": [HumanMessage(content=req.message)],
                   "model": None if (thread == chat_store.WORKSPACE or is_draft(thread)) else thread, "ui": req.ui}
    return sse(run(graph_input, thread))

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
    return sse(run(Command(resume={"action": req.action, "text": req.text}), thread))

  @router.get("/pending")
  def pending(model: Optional[str] = None):
    return {"pending": _pending(thread_of(model))}

  # Forget the agent's conversation for a deleted model (thread_id = model name).
  def _forget(name: str):
    cp = getattr(graph, "checkpointer", None)
    if cp is not None and hasattr(cp, "delete_thread"):
      cp.delete_thread(name)
  from api import workspace
  workspace.on_model_deleted.append(_forget)

  return router
