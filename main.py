"""ModelicAI Studio backend.

  /studio/              the web app (built from frontend/ into static/)
  /api/studio/...       models, diagram, parameters, simulation, results, chat history (api/workspace.py)
  /api/studio/agent     Modi, the Manager Agent: streaming chat + approvals (api/agent_api.py)

Run:  uvicorn main:app --host 0.0.0.0 --port 8000
"""
import os
import re

from utils import tracing

tracing.setup()  # before the agents are built, so LangChain/LangGraph get instrumented

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from agents.manager import build_manager
from agents.orchestrator import SimulationOrchestrator
from api.agent_api import build_agent_router
from api.services import Services
from api.workspace import build_router

app = FastAPI(
    title="ModelicAI Studio",
    description="Agentic Modelica modelling and simulation workspace, by Anurag Dogra.",
    version="1.0.0",
)

# The dev server (Vite on :5173) proxies /api, so this is only needed for other local clients.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# One trace per Studio action (Check, Simulate, Bake, Delete), in the model's session.
_ACTION = re.compile(r"^/api/studio/models/([A-Za-z_]\w*)(?:/(run|bake))?$")


@app.middleware("http")
async def trace_actions(request: Request, call_next):
  m = _ACTION.match(request.url.path)
  if not tracing.enabled() or not m or request.method not in ("PUT", "POST", "DELETE"):
    return await call_next(request)
  action = {"PUT": "check", "DELETE": "delete"}.get(request.method) or m.group(2) or "action"
  from api import store
  with tracing.span(f"studio.{action}", "CHAIN", agent="studio", session=store.session_of(m.group(1)),
                    **{"studio.action": action}) as sp:
    response = await call_next(request)
    sp.set_attribute("http.status_code", response.status_code)
    return response


print("=== Starting ModelicAI Studio ===")
toolbox = SimulationOrchestrator()  # OMC session + designer + simulation agent + graph parser

# Studio REST API and Modi (Manager Agent: LangGraph, tools, human approval, SQLite checkpointer)
app.include_router(build_router(toolbox))
app.include_router(build_agent_router(build_manager(Services(toolbox))))

# Built frontend: cd frontend && npm run build  ->  ./static, served at /studio
os.makedirs("static", exist_ok=True)
app.mount("/studio", StaticFiles(directory="static", html=True), name="studio")


@app.get("/", include_in_schema=False)
def root():
  return RedirectResponse(url="/studio/")


if __name__ == "__main__":
  import uvicorn
  uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
