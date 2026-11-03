"""Tracing with Arize Phoenix (OpenTelemetry + OpenInference).

Off unless PHOENIX_COLLECTOR_ENDPOINT is set (e.g. http://phoenix:6006/v1/traces). When off, every helper
here is a no-op, so the app behaves exactly as before.

Layout in Phoenix:
  session  = one model (session.id = model name, or the _new_... draft id before the model exists)
  trace    = one turn: a Modi message / approval, or a Check / Simulate / Bake click
  spans    = agents (modi, designer, validator, simulation) and OpenModelica calls, each with
             metadata.agent, modelica.model, app.commit, app.prompt_version

Claude and LangGraph spans come from the LangChain auto-instrumentor; they pick up the session and the
agent label from the OpenInference context set here (using_session / using_metadata).
"""
import contextvars
import functools
import json
import os
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

_tracer = None
_session: contextvars.ContextVar = contextvars.ContextVar("modelicai_session", default=None)
_agent: contextvars.ContextVar = contextvars.ContextVar("modelicai_agent", default=None)

COMMIT = os.getenv("GIT_COMMIT", "dev")
PROMPT_VERSION = os.getenv("PROMPT_VERSION", "1")


def setup() -> bool:
  """Connects to Phoenix and instruments LangChain/LangGraph. Returns True when tracing is on."""
  global _tracer
  if not os.getenv("PHOENIX_COLLECTOR_ENDPOINT"):
    print("Tracing: off (set PHOENIX_COLLECTOR_ENDPOINT to send traces to Phoenix)")
    return False
  try:
    # Plain OpenTelemetry SDK + OpenInference (no phoenix.otel wrapper: its exporter subclass breaks with
    # some opentelemetry-exporter versions, e.g. "'HTTPSpanExporter' object has no attribute '_headers'").
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from openinference.instrumentation.langchain import LangChainInstrumentor

    # Always HTTP to <phoenix>:6006/v1/traces (port 4317 is gRPC and fails with an HTTP/2 BadStatusLine).
    base = (_phoenix_base() or "http://localhost:6006").replace(":4317", ":6006")
    headers = {"authorization": f"Bearer {os.getenv('PHOENIX_API_KEY')}"} if os.getenv("PHOENIX_API_KEY") else {}
    project = os.getenv("PHOENIX_PROJECT_NAME", "modelicai-studio")
    provider = TracerProvider(resource=Resource.create({"openinference.project.name": project,
                                                        "service.name": "modelicai-studio"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{base}/v1/traces", headers=headers)))
    trace.set_tracer_provider(provider)
    LangChainInstrumentor().instrument(tracer_provider=provider)  # Claude + LangGraph spans
    _tracer = provider.get_tracer("modelicai")
    print(f"Tracing: on -> {base}/v1/traces (http, project {project}{', with API key' if headers else ''})")
    return True
  except Exception as e:  # tracing must never stop the app
    print(f"Tracing: disabled ({type(e).__name__}: {e})")
    _tracer = None
    return False


def enabled() -> bool:
  return _tracer is not None


class _NoSpan:
  def set_attribute(self, *a, **k): pass
  def set_attributes(self, *a, **k): pass
  def record_exception(self, *a, **k): pass
  def set_status(self, *a, **k): pass
  def get_span_context(self): return None


def _ctx(session: Optional[str], agent: Optional[str]):
  """OpenInference context so auto-instrumented Claude/LangGraph spans carry session and agent."""
  from contextlib import ExitStack
  stack = ExitStack()
  try:
    from openinference.instrumentation import using_metadata, using_session
    if session:
      stack.enter_context(using_session(session))
    meta = {"agent": agent or "", "model": session or "", "commit": COMMIT, "prompt_version": PROMPT_VERSION}
    stack.enter_context(using_metadata(meta))
  except Exception:
    pass
  return stack


def _origin(sess: str) -> Optional[str]:
  if sess.startswith("_new_"):
    return None
  try:
    from api import store
    return store.origin_draft(sess)
  except Exception:
    return None


def _clip(v: Any, n: int = 4000) -> Any:
  if isinstance(v, (int, float, bool)):
    return v
  s = v if isinstance(v, str) else json.dumps(v, default=str)
  return s if len(s) <= n else s[:n] + "…"


@contextmanager
def span(name: str, kind: str = "CHAIN", agent: Optional[str] = None, session: Optional[str] = None,
         **attrs: Any) -> Iterator[Any]:
  """A span; `session` starts/overrides the model session, `agent` labels this span and everything under it."""
  if _tracer is None:
    yield _NoSpan()
    return
  sess = session or _session.get()
  ag = agent or _agent.get()
  t_s, t_a = _session.set(sess), _agent.set(ag)
  try:
    with _ctx(sess, ag), _tracer.start_as_current_span(name) as sp:
      sp.set_attribute("openinference.span.kind", kind)
      if sess:
        sp.set_attribute("session.id", sess)
        sp.set_attribute("modelica.model", sess)
        origin = _origin(sess)
        if origin:
          sp.set_attribute("modelica.origin_draft", origin)  # the + New conversation this model came from
      if ag:
        sp.set_attribute("metadata", json.dumps({"agent": ag, "commit": COMMIT, "prompt_version": PROMPT_VERSION}))
        sp.set_attribute("agent", ag)
      sp.set_attribute("app.commit", COMMIT)
      sp.set_attribute("app.prompt_version", PROMPT_VERSION)
      for k, v in attrs.items():
        if v is not None:
          sp.set_attribute(k, _clip(v))
      yield sp
  finally:
    _session.reset(t_s)
    _agent.reset(t_a)


def traced(name: str, kind: str = "TOOL", agent: Optional[str] = None, out=None):
  """Decorator: wraps a function in a span; `out(result) -> dict` adds attributes from the result."""
  def deco(fn):
    @functools.wraps(fn)
    def wrapper(*a, **k):
      if _tracer is None:
        return fn(*a, **k)
      with span(name, kind, agent=agent) as sp:
        result = fn(*a, **k)
        if out is not None:
          try:
            for key, val in (out(result) or {}).items():
              if val is not None:
                sp.set_attribute(key, _clip(val))
          except Exception:
            pass
        return result
    return wrapper
  return deco


def current_session() -> Optional[str]:
  return _session.get()


def trace_id(sp) -> Optional[str]:
  """Hex trace id of a span (sent to the browser so a turn can be found in Phoenix)."""
  try:
    c = sp.get_span_context()
    return format(c.trace_id, "032x") if c and c.trace_id else None
  except Exception:
    return None


def span_id(sp) -> Optional[str]:
  try:
    c = sp.get_span_context()
    return format(c.span_id, "016x") if c and c.span_id else None
  except Exception:
    return None


def _phoenix_base() -> Optional[str]:
  ep = os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "")
  if not ep:
    return None
  base = ep.split("/v1/")[0].rstrip("/")
  return base if base.startswith("http") else None


def annotate(span_id_hex: Optional[str], name: str, label: str, score: Optional[float] = None,
             explanation: str = "", metadata: Optional[Dict[str, Any]] = None) -> bool:
  """Adds a label to a span in Phoenix (e.g. draft.outcome=design_failed). Best effort, never raises."""
  base = _phoenix_base()
  if _tracer is None or not base or not span_id_hex:
    return False
  import urllib.request
  body = {"data": [{"span_id": span_id_hex, "name": name, "annotator_kind": "CODE",
                    "result": {"label": label, **({"score": score} if score is not None else {}),
                               **({"explanation": explanation[:2000]} if explanation else {})},
                    "metadata": metadata or {}}]}
  headers = {"Content-Type": "application/json"}
  if os.getenv("PHOENIX_API_KEY"):
    headers["Authorization"] = f"Bearer {os.getenv('PHOENIX_API_KEY')}"
  try:
    req = urllib.request.Request(f"{base}/v1/span_annotations", data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    urllib.request.urlopen(req, timeout=5).read()
    return True
  except Exception as e:
    print(f"Tracing: annotation failed ({type(e).__name__}: {e})")
    return False


def status_attrs(result: Dict[str, Any]) -> Dict[str, Any]:
  """Common attributes for OMC results: status and a short error."""
  if not isinstance(result, dict):
    return {}
  err = result.get("error") or result.get("messages") or ""
  return {"omc.status": result.get("status"), "omc.error": str(err)[:2000] if result.get("status") != "SUCCESS" else None}
