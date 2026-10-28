from agents.orchestrator import SimulationOrchestrator

if __name__ == "__main__":
  print("=== Initializing ModelicAI Agent Swarm ===")
  orchestrator = SimulationOrchestrator()

  # Unique session ID for short-term memory tracking
  thread_id = "test-session-01"

  # Test 1: Full end-to-end run
  # (Triggers Designer -> Self-Correction Validator -> Introspector -> Solver -> Visualization)
  print("\n--- Running Test Turn 1: Design & Simulate ---")
  orchestrator.process_human_input(
      "Create a simple RC electrical circuit model named RCCircuit with a DC"
      " voltage source (10V), a resistor (100 ohm), and a capacitor (1e-6"
      " farad) in series. Simulate it and render an interactive dashboard.",
      thread_id=thread_id,
  )

  # Test 2: Follow-up interaction leveraging short-term memory
  # (Triggers Visualization update using the existing session result file)
  print("\n--- Running Test Turn 2: Interactive Update ---")
  orchestrator.process_human_input(
      "Update the visualization dashboard layout to focus on the loop current.",
      thread_id=thread_id,
  )

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