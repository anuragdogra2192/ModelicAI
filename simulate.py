import json
from agents.simulation import SimulationAgent
from utils.omc_client import OpenModelicaClient

if __name__ == "__main__":
  print("=== ModelicAI Solver Agent Pipeline (Blueprint Match) ===")

  client = OpenModelicaClient()
  sim_agent = SimulationAgent()

  # 1. Load model file (.mo)
  file_path = "temp_model.mo"
  model_name = "RCCircuit"
  print(f"\n[Step 1] Loading model file: {file_path}")
  load_res = client.omc.sendExpression(f'loadFile("{file_path}")')
  print(f"Load Status: {bool(load_res)}")

  # 2. Extract Input Parameters (Model Parameters & Solver Parameters)
  print("\n[Step 2] Extracting Model & Solver Parameters...")
  model_params = client.get_model_parameters(model_name)
  solver_options = client.get_simulation_options(model_name)

  combined_inputs = {
      "model_parameters": model_params,
      "solver_parameters": solver_options,
  }
  print(json.dumps(combined_inputs, indent=2))

  # 3. LLM decides best parameters (with optional Human-editable override hook)
  user_intent = (
      "Simulate the full charging transient curve of the RC circuit. Set an"
      " appropriate stop time based on the time constant tau = R * C."
  )

  print(
      "\n[Step 3] Solver Agent LLM optimizing parameters based on intent..."
  )
  agent_config = sim_agent.suggest_simulation_config(
      model_name=model_name,
      parameters=combined_inputs,
      user_intent=user_intent,
  )

  # --- HUMAN EDITABLE HOOK (As seen in your blueprint) ---
  # You can modify or override agent_config here manually if desired before running!
  # agent_config["parameter_overrides"]["R"] = 200.0

  print("\n--- Final Config (LLM Decided + Human Editable) ---")
  print(json.dumps(agent_config, indent=2))

  # 4. Simulation Run & Export Results to CSV
  print("\n[Step 4] Running Simulation via Numerical Solver...")
  sim_result = client.run_simulation(
      model_name=model_name,
      start_time=agent_config.get("startTime", 0.0),
      stop_time=agent_config.get("stopTime", 0.01),
      number_of_intervals=agent_config.get("numberOfIntervals", 500),
      method=agent_config.get("method", "dassl"),
      parameter_overrides=agent_config.get("parameter_overrides", {}),
  )

  print("\n=== Pipeline Complete ===")
  print(json.dumps(sim_result, indent=2))
  print(
      "\nOutput generated successfully! Ready for downstream visualization."
  )