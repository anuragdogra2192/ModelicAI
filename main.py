import os
import json
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
from typing import Dict, Any, Optional

from agents.orchestrator import SimulationOrchestrator, MODEL_ROOT
from fastapi.staticfiles import StaticFiles
from api.workspace import build_router
from api.services import Services
from api.agent_api import build_agent_router
from agents.manager import build_manager

# Initialize FastAPI app
app = FastAPI(
    title="ModelicAI Backend",
    description="Agentic CAE Platform API for Modelica-based simulation and visualization.",
    version="1.0.0"
)

# Enable CORS for future React / frontend development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Adjust in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize the Master Simulation Orchestrator
print("=== Initializing ModelicAI Agent Swarm ===")
orchestrator = SimulationOrchestrator()

# ModelicAI Studio: REST routes for the web UI, plus the built frontend
# (cd frontend && npm run build  ->  ./static, served at /studio).
app.include_router(build_router(orchestrator))

# Manager Agent (LangGraph): chat, tools, human approval, SQLite checkpointer.
manager_graph = build_manager(Services(orchestrator))
app.include_router(build_agent_router(manager_graph))
os.makedirs("static", exist_ok=True)
app.mount("/studio", StaticFiles(directory="static", html=True), name="studio")


# --- Pydantic Request Models ---
class ChatRequest(BaseModel):
    prompt: str
    thread_id: Optional[str] = "default_session"

class SimulateRequest(BaseModel):
    model_name: str
    start_time: float = 0.0
    stop_time: float = 0.01
    number_of_intervals: int = 500
    method: str = "dassl"
    parameter_overrides: Dict[str, Any] = {}


# --- API Endpoints ---

@app.get("/")
def root():
    return {"status": "online", "message": "Welcome to ModelicAI Agentic CAE Platform API."}


@app.post("/api/chat")
def chat_with_agent(req: ChatRequest):
    """Sends user intent to the LangGraph orchestrator swarm (Designer, Validator, Solver, Visualizer)."""
    try:
        orchestrator.process_human_input(human_input=req.prompt, thread_id=req.thread_id)
        return {
            "status": "SUCCESS",
            "model_name": orchestrator.state["model_name"],
            "message": f"Successfully processed intent for model '{orchestrator.state['model_name']}'.",
            "dashboard_available": orchestrator.state["dashboard_path"] is not None
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/models/{model_name}/graph")
def get_model_graph(model_name: str):
    """Serves the React Flow JSON graph schema for frontend diagram rendering before/after simulation."""
    schema_path = os.path.join(MODEL_ROOT, model_name, "graph_schema.json")
    if not os.path.exists(schema_path):
        raise HTTPException(status_code=404, detail=f"Graph schema not found for model: {model_name}")
    
    with open(schema_path, "r") as f:
        graph_data = json.load(f)
    return graph_data


@app.get("/api/models/{model_name}/parameters")
def get_model_parameters(model_name: str):
    """Serves model parameters and simulation options for the UI sidebar / property editor."""
    model_dir = os.path.join(MODEL_ROOT, model_name)
    mo_file = os.path.join(model_dir, f"{model_name}.mo")
    
    if not os.path.exists(mo_file):
        raise HTTPException(status_code=404, detail=f"Model source not found: {model_name}")
    
    try:
        orchestrator.client.omc.sendExpression(f'loadFile("{os.path.abspath(mo_file)}")')
        parameters = orchestrator.client.get_model_parameters(model_name)
        options = orchestrator.client.get_simulation_options(model_name)
        return {
            "parameters": parameters,
            "simulation_options": options
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/models/simulate")
def run_custom_simulation(req: SimulateRequest):
    """Triggers solver execution directly with custom parameter overrides from the UI."""
    try:
        model_dir = os.path.join(MODEL_ROOT, req.model_name)
        if not os.path.exists(model_dir):
            raise HTTPException(status_code=404, detail=f"Model directory not found: {req.model_name}")
        
        orchestrator.client.omc.sendExpression(f'cd("{os.path.abspath(model_dir)}")')
        sim_res = orchestrator.client.run_simulation(
            model_name=req.model_name,
            start_time=req.start_time,
            stop_time=req.stop_time,
            number_of_intervals=req.number_of_intervals,
            method=req.method,
            parameter_overrides=req.parameter_overrides
        )
        
        result_file = sim_res.get("resultFile", f"{req.model_name}_res.mat")
        
        # Automatically re-render dashboard with new results
        dashboard_path = orchestrator.vis_agent.render_dashboard(
            omc_session=orchestrator.client.omc,
            result_file=os.path.join(model_dir, result_file),
            user_intent=f"Simulate {req.model_name} with custom overrides {req.parameter_overrides}",
            output_dir=model_dir
        )
        
        return {
            "status": "SUCCESS",
            "result_file": result_file,
            "dashboard_path": dashboard_path
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/models/{model_name}/dashboard", response_class=HTMLResponse)
def get_dashboard_html(model_name: str):
    """Serves the interactive Plotly HTML visualization dashboard directly to the browser/iframe."""
    dashboard_path = os.path.join(MODEL_ROOT, model_name, "interactive_dashboard.html")
    if not os.path.exists(dashboard_path):
        raise HTTPException(status_code=404, detail=f"Dashboard not found for model: {model_name}")
    
    with open(dashboard_path, "r") as f:
        html_content = f.read()
    return HTMLResponse(content=html_content)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)


# from agents.orchestrator import SimulationOrchestrator

# if __name__ == "__main__":
#   print("=== Initializing ModelicAI Agent Swarm ===")
#   orchestrator = SimulationOrchestrator()

#   # Unique session ID for short-term memory tracking
#   thread_id = "test-session-01"

#   # Test 1: Full end-to-end run
#   # (Triggers Designer -> Self-Correction Validator -> Introspector -> Solver -> Visualization)
#   print("\n--- Running Test Turn 1: Design & Simulate ---")
#   orchestrator.process_human_input(
#       "Create a simple RC electrical circuit model named RCCircuit with a DC"
#       " voltage source (10V), a resistor (100 ohm), and a capacitor (1e-6"
#       " farad) in series. Simulate it and render an interactive dashboard.",
#       thread_id=thread_id,
#   )

#   # Test 2: Follow-up interaction leveraging short-term memory
#   # (Triggers Visualization update using the existing session result file)
#   print("\n--- Running Test Turn 2: Interactive Update ---")
#   orchestrator.process_human_input(
#       "Update the visualization dashboard layout to focus on the loop current.",
#       thread_id=thread_id,
#   )

# import os
# from typing import TypedDict
# from langgraph.graph import END, StateGraph
# from agents.designer import DesignAgent
# from utils.omc_client import OpenModelicaClient


# # 1. Define the Shared State for LangGraph
# class SimulationState(TypedDict):
#   prompt: str
#   code: str
#   error_log: str
#   status: str
#   retry_count: int


# # Initialize global agents and clients
# designer = DesignAgent()
# omc_client = OpenModelicaClient()


# # 2. Define Graph Nodes
# def designer_node(state: SimulationState) -> SimulationState:
#   """Generates or corrects Modelica code using Claude Haiku."""
#   print(
#       f"\n[Designer Node] Generating Modelica code (Attempt:"
#       f" {state.get('retry_count', 0) + 1})..."
#   )

#   prompt = state["prompt"]
#   error_log = state.get("error_log", None)
#   retry_count = state.get("retry_count", 0)

#   code = designer.generate_code(prompt=prompt, error_log=error_log)

#   return {**state, "code": code, "retry_count": retry_count + 1}


# def validation_node(state: SimulationState) -> SimulationState:
#   """Validates and compiles the Modelica code using OpenModelica."""
#   print("[Validation Node] Compiling code with OpenModelica...")

#   code = state["code"]

#   # Save code temporarily to a container-managed file
#   file_path = "temp_model.mo"
#   with open(file_path, "w") as f:
#     f.write(code)

#   # Extract model name heuristic (assumes 'model ClassName' structure)
#   model_name = "Model"
#   for line in code.split("\n"):
#     if line.strip().startswith("model "):
#       parts = line.strip().split()
#       if len(parts) > 1:
#         model_name = parts[1].split("(")[0]
#         break

#   # Run OMC compilation check
#   result = omc_client.compile_model(file_path=file_path, model_name=model_name)

#   return {
#       **state,
#       "status": result["status"],
#       "error_log": result["error"],
#   }


# # 3. Define Conditional Routing Logic
# def should_retry(state: SimulationState) -> str:
#   """Decides whether to loop back for self-correction or finish/pause."""
#   if state["status"] == "SUCCESS":
#     print("[Router] Validation SUCCESS! Proceeding to output.")
#     return "success"
#   elif state["retry_count"] >= 3:
#     print("[Router] Max retries reached. Stopping loop.")
#     return "max_retries"
#   else:
#     print(
#         "[Router] Validation FAILED. Feeding error back to Designer for"
#         " correction..."
#     )
#     return "retry"


# # 4. Build the LangGraph Workflow
# workflow = StateGraph(SimulationState)

# workflow.add_node("designer", designer_node)
# workflow.add_node("validator", validation_node)

# workflow.set_entry_point("designer")
# workflow.add_edge("designer", "validator")

# workflow.add_conditional_edges(
#     "validator",
#     should_retry,
#     {"success": END, "retry": "designer", "max_retries": END},
# )

# app = workflow.compile()


# # 5. CLI Execution Handler when running the container
# if __name__ == "__main__":
#   print("\n=== ModelicAI Simulation Pipeline Initialized ===")

#   # Example test prompt for a simple RC Circuit or Mass-Spring-Damper
#   test_prompt = (
#       "Create a simple RC electrical circuit model in Modelica with a DC voltage"
#       " source (10V), a resistor (100 ohm), and a capacitor (1e-6 farad) in"
#       " series. Name the model RCCircuit."
#   )

#   initial_state = {
#       "prompt": test_prompt,
#       "code": "",
#       "error_log": "",
#       "status": "PENDING",
#       "retry_count": 0,
#   }

#   final_state = app.invoke(initial_state)

#   print("\n=== Final Simulation Pipeline Result ===")
#   print(f"Status: {final_state['status']}")
#   print(f"Attempts: {final_state['retry_count']}")
#   print("\nGenerated & Validated Modelica Code:\n")
#   print(final_state["code"])

#   if final_state["error_log"]:
#     print(f"\nLast Error Log:\n{final_state['error_log']}")