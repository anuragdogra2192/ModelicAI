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

class SimulationOrchestrator:
    def __init__(self):
        self.llm = ChatAnthropic(model=os.getenv("MODEL_NAME", "claude-haiku-5-5"))
        self.client = OpenModelicaClient()
        
        # Initialize sub-agents
        self.designer = DesignAgent()
        self.sim_agent = SimulationAgent()
        self.vis_agent = VisualizationAgent()

        # Shared session state
        self.state = {
            "model_name": "RCCircuit",
            "file_path": "temp_model.mo",
            "code": "",
            "error_log": None,
            "status": "PENDING",
            "model_parameters": {},
            "solver_options": {},
            "simulation_config": {},
            "result_file": None,
            "dashboard_path": None
        }

        # Initialize short-term memory checkpointer
        self.memory = MemorySaver()

    def _decide_action_plan(self, human_input: str) -> List[str]:
        """Uses LLM to interpret human input and decide which actions to take."""
        system_prompt = (
            "You are the Master Orchestrator for ModelicAI, a CAE simulation agent system.\n"
            "Analyze the human input and determine which actions to take from the following set:\n"
            "- 'design_and_validate': Use Designer Agent to write Modelica code and validate/self-correct via OpenModelica compiler.\n"
            "- 'introspect': Introspect/inspect model and solver parameters from OpenModelica.\n"
            "- 'solver': Consult Solver Agent to optimize parameters & execute simulation.\n"
            "- 'visualization': Consult Visualization Agent to generate interactive Plotly dashboard.\n\n"
            "Return ONLY a JSON array of string action names in execution order.\n"
            "Example 1 (New Model): [\"design_and_validate\", \"introspect\", \"solver\", \"visualization\"]\n"
            "Example 2 (Just Update Plot): [\"visualization\"]"
        )
        
        user_content = (
            f"Human Input: '{human_input}'\n"
            f"Current System State: Code Exists = {bool(self.state['code'])}, Result File Available = {self.state['result_file'] is not None}"
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
            return ["design_and_validate", "introspect", "solver", "visualization"]

    def process_human_input(self, human_input: str, thread_id: str = "default_session"):
        config = {"configurable": {"thread_id": thread_id}}
        print(f"\n🗣️ [Human Input - Thread {thread_id}]: '{human_input}'")
        
        plan = self._decide_action_plan(human_input)
        print(f"🎯 [Orchestrator Plan]: {plan}")

        for action in plan:
            if action == "design_and_validate":
                self._run_design_validation_loop(human_input)
            elif action == "introspect":
                self._run_introspect()
            elif action == "solver":
                self._run_solver_agent(human_input)
            elif action == "visualization":
                self._run_visualization_agent(human_input)

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
            
            # Generate code using designer agent (passing error log if it failed previously)
            code = self.designer.generate_code(prompt=prompt, error_log=error_log)
            
            # Save temporarily
            with open(self.state["file_path"], "w") as f:
                f.write(code)

            # Extract model name heuristic
            model_name = "Model"
            for line in code.split("\n"):
                if line.strip().startswith("model "):
                    parts = line.strip().split()
                    if len(parts) > 1:
                        model_name = parts[1].split("(")[0]
                        break
            self.state["model_name"] = model_name

            # Validate via OpenModelica
            print("[Validation Node] Compiling code with OpenModelica...")
            result = self.client.compile_model(file_path=self.state["file_path"], model_name=model_name)
            
            status = result["status"]
            error_log = result["error"]

            if status == "SUCCESS":
                print("[Router] Validation SUCCESS! Model compiled cleanly.")
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
        self.client.omc.sendExpression(f'loadFile("{self.state["file_path"]}")')
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

      # Directly call visualization agent with result file path
      dashboard_path = self.vis_agent.render_dashboard(
          omc_session=self.client.omc,
          result_file=self.state["result_file"],
          user_intent=human_input,
      )
      self.state["dashboard_path"] = dashboard_path
      print(f"   ✓ Visualization Agent created dashboard at: {dashboard_path}")