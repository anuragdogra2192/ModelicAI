import json
import os
from typing import Dict, Any, List
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from utils.omc_client import OpenModelicaClient
from agents.designer import DesignAgent
from agents.simulation import SimulationAgent
from agents.visualization import VisualizationAgent
from agents.graph_parser import ModelicaGraphParser

# Each model gets its own folder: models/<ModelName>/
MODEL_ROOT = "models"

class SimulationOrchestrator:
    def __init__(self):
        self.llm = ChatAnthropic(model=os.getenv("MODEL_NAME", "claude-haiku-5-5"))
        self.client = OpenModelicaClient()
        
        # Initialize sub-agents & parsers
        self.designer = DesignAgent()
        self.sim_agent = SimulationAgent()
        self.vis_agent = VisualizationAgent()
        self.graph_parser = ModelicaGraphParser()

        # Shared session state
        self.state = {
            "model_name": "RCCircuit",
            "file_path": os.path.join(MODEL_ROOT, "RCCircuit", "RCCircuit.mo"),
            "code": "",
            "error_log": None,
            "status": "PENDING",
            "model_parameters": {},
            "solver_options": {},
            "simulation_config": {},
            "graph_schema": {},
            "result_file": None,
            "dashboard_path": None
        }

        # Initialize short-term memory checkpointer
        self.memory = MemorySaver()

    def _decide_action_plan(self, human_input: str) -> Dict[str, Any]:
        """Uses LLM to interpret human input and decide which actions to take or conversational response."""
        system_prompt = (
            "You are the Master Orchestrator for ModelicAI, a CAE simulation agent system.\n"
            "Analyze the human input and determine the appropriate intent category:\n"
            "- 'greeting': Casual conversation, hi, hello, how are you.\n"
            "- 'list_models': Asking how many models exist or to list available models.\n"
            "- 'recheck_model': Asking to recheck, revalidate, or verify an existing model.\n"
            "- 'design_and_simulate': Requesting a new model design, simulation, or full solver pipeline.\n\n"
            "Return ONLY a JSON object with two keys:\n"
            "1. 'intent': one of ['greeting', 'list_models', 'recheck_model', 'design_and_simulate']\n"
            "2. 'actions': list of action strings from ['design_and_validate', 'introspect', 'solver', 'visualization'] (empty for greeting/list_models)\n"
            "3. 'response_message': A friendly conversational response text if it's a greeting or direct answer, otherwise empty string."
        )
        
        user_content = (
            f"Human Input: '{human_input}'\n"
            f"Current Active Model: {self.state['model_name']}"
        )

        response = self.llm.invoke([SystemMessage(content=system_prompt), HumanMessage(content=user_content)])
        
        content = response.content
        if isinstance(content, list):
            content_str = "".join([b.get("text", "") if isinstance(b, dict) else str(b) for b in content])
        else:
            content_str = str(content)

        if "```json" in content_str:
            content_str = content_str.split("```json")[1].split("```")[0].strip()
        elif "```" in content_str:
            content_str = content_str.split("```")[1].split("```")[0].strip()

        try:
            return json.loads(content_str)
        except json.JSONDecodeError:
            return {
                "intent": "design_and_simulate",
                "actions": ["design_and_validate", "introspect", "solver", "visualization"],
                "response_message": ""
            }

    def process_human_input(self, human_input: str, thread_id: str = "default_session") -> Dict[str, Any]:
        print(f"\n🗣️ [Human Input - Thread {thread_id}]: '{human_input}'")
        
        plan_data = self._decide_action_plan(human_input)
        intent = plan_data.get("intent", "design_and_simulate")
        print(f"🎯 [Orchestrator Intent]: {intent} | Plan Actions: {plan_data.get('actions')}")

        # 1. Handle Greetings
        if intent == "greeting":
            msg = plan_data.get("response_message") or "Hello! I am ModelicAI, your agentic CAE assistant. How can I help you design or simulate your systems today?"
            return {"status": "SUCCESS", "message": msg, "model_name": self.state["model_name"]}

        # 2. Handle List Models Query
        elif intent == "list_models":
            models = []
            if os.path.exists(MODEL_ROOT):
                models = [d for d in os.listdir(MODEL_ROOT) if os.path.isdir(os.path.join(MODEL_ROOT, d))]
            
            msg = f"We currently have {len(models)} model(s) saved in the platform: {', '.join(models) if models else 'None'}."
            return {"status": "SUCCESS", "message": msg, "models": models}

        # 3. Handle Recheck / Revalidate Model Query
        elif intent == "recheck_model":
            print(f"\n🔍 [Orchestrator] Rechecking active model: {self.state['model_name']}...")
            if not os.path.exists(self.state["file_path"]):
                return {"status": "FAILED", "message": f"Model file not found at {self.state['file_path']}"}
            
            result = self.client.compile_model(file_path=self.state["file_path"], model_name=self.state["model_name"])
            if result["status"] == "SUCCESS":
                return {"status": "SUCCESS", "message": f"Model '{self.state['model_name']}' successfully re-validated and compiled cleanly!"}
            else:
                return {"status": "FAILED", "message": f"Re-validation failed: {result['error']}"}

        # 4. Full Execution Pipeline (Design, Introspect, Solve, Visualize)
        actions = plan_data.get("actions", ["design_and_validate", "introspect", "solver", "visualization"])
        for action in actions:
            if action == "design_and_validate":
                self._run_design_validation_loop(human_input)
            elif action == "introspect":
                self._run_introspect()
            elif action == "solver":
                self._run_solver_agent(human_input)
            elif action == "visualization":
                self._run_visualization_agent(human_input)

        return {
            "status": "SUCCESS",
            "model_name": self.state["model_name"],
            "message": f"Successfully processed intent for model '{self.state['model_name']}'.",
            "dashboard_available": self.state["dashboard_path"] is not None
        }

    def _run_design_validation_loop(self, prompt: str, max_retries: int = 3):
        """Self-correcting loop between Designer Agent and OpenModelica validation."""
        print("\n🎨 [Orchestrator] Step -> Running Designer & Validation Self-Correction Loop...")
        
        retry_count = 0
        status = "FAILED"
        error_log = None
        code = ""

        while retry_count < max_retries and status != "SUCCESS":
            retry_count += 1
            print(f"\n[Designer Node] Generating Modelica code (Attempt {retry_count}/{max_retries})...")
            
            code = self.designer.generate_code(prompt=prompt, error_log=error_log)
            
            model_name = "Model"
            for line in code.split("\n"):
                if line.strip().startswith("model "):
                    parts = line.strip().split()
                    if len(parts) > 1:
                        model_name = parts[1].split("(")[0]
                        break
            self.state["model_name"] = model_name

            model_dir = os.path.join(MODEL_ROOT, model_name)
            os.makedirs(model_dir, exist_ok=True)
            self.state["file_path"] = os.path.join(model_dir, f"{model_name}.mo")
            with open(self.state["file_path"], "w") as f:
                f.write(code)

            print("[Validation Node] Compiling code with OpenModelica...")
            result = self.client.compile_model(file_path=self.state["file_path"], model_name=model_name)
            
            status = result["status"]
            error_log = result["error"]

            if status == "SUCCESS":
                print("[Router] Validation SUCCESS! Model compiled cleanly.")
                try:
                    graph_data = self.graph_parser.parse_mo_file(self.state["file_path"])
                    schema_path = os.path.join(model_dir, "graph_schema.json")
                    with open(schema_path, "w") as sf:
                        json.dump(graph_data, sf, indent=2)
                    self.state["graph_schema"] = graph_data
                    print(f"   ✓ Generated & saved React Flow graph schema to: {schema_path}")
                except Exception as e:
                    print(f"   ⚠️ Warning: Failed to generate graph schema: {e}")
                
                break
            else:
                print(f"[Router] Validation FAILED (Attempt {retry_count}). Feeding error back to Designer for correction...")

        self.state["code"] = code
        self.state["error_log"] = error_log
        self.state["status"] = status

        if status != "SUCCESS":
            raise RuntimeError(f"Model design validation failed after {max_retries} attempts. Last error: {error_log}")

    def _run_introspect(self):
        print("\n🔍 [Orchestrator] Step -> Introspecting OpenModelica parameters...")
        self.client.omc.sendExpression(f'loadFile("{os.path.abspath(self.state["file_path"])}")')
        self.state["model_parameters"] = self.client.get_model_parameters(self.state["model_name"])
        self.state["solver_options"] = self.client.get_simulation_options(self.state["model_name"])
        print(f"   ✓ Introspected parameters for '{self.state['model_name']}'.")

    def _run_solver_agent(self, human_input: str):
        print("\n⚡ [Orchestrator] Step -> Dispatching task to Solver Agent...")
        combined_inputs = {
            "model_parameters": self.state["model_parameters"],
            "solver_parameters": self.state["solver_options"]
        }

        config = self.sim_agent.suggest_simulation_config(
            model_name=self.state["model_name"],
            parameters=combined_inputs,
            user_intent=human_input
        )
        self.state["simulation_config"] = config
        print(f"   ✓ Solver Agent suggested config: {json.dumps(config, indent=2)}")

        print("   -> Executing numerical solver...")
        model_dir = os.path.dirname(os.path.abspath(self.state["file_path"]))
        self.client.omc.sendExpression(f'cd("{model_dir}")')
        sim_res = self.client.run_simulation(
            model_name=self.state["model_name"],
            start_time=config.get("startTime", 0.0),
            stop_time=config.get("stopTime", 0.01),
            number_of_intervals=config.get("numberOfIntervals", 500),
            method=config.get("method", "dassl"),
            parameter_overrides=config.get("parameter_overrides", {})
        )
        
        self.state["result_file"] = sim_res.get("resultFile", f"{self.state['model_name']}_res.mat")
        print(f"   ✓ Simulation finished. Output file: {self.state['result_file']}")

    def _run_visualization_agent(self, human_input: str):
        print("\n📊 [Orchestrator] Step -> Dispatching task to Visualization Agent...")
        if not self.state["result_file"]:
            print("   ⚠️ No result file available yet. Running full sequence.")
            self._run_design_validation_loop(human_input)
            self._run_introspect()
            self._run_solver_agent(human_input)

        dashboard_path = self.vis_agent.render_dashboard(
            omc_session=self.client.omc,
            result_file=self.state["result_file"],
            user_intent=human_input,
            output_dir=os.path.dirname(os.path.abspath(self.state["file_path"])),
        )
        self.state["dashboard_path"] = dashboard_path
        print(f"   ✓ Visualization Agent created dashboard at: {dashboard_path}")