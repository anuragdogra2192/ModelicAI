import json
import os
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage


class SimulationAgent:

  def __init__(self):
    self.llm = ChatAnthropic(
        model=os.getenv("MODEL_NAME", "claude-haiku-5-5")
    )

  def suggest_simulation_config(
      self, model_name: str, parameters: dict, user_intent: str
  ) -> dict:
    """Analyzes model parameters and user intent to suggest optimal

    solver settings and parameter overrides. Returns a clean JSON dictionary.
    """
    system_prompt = (
        "You are an expert CAE simulation engineer. "
        "Given a Modelica model's parameters, current values, and a user simulation intent, "
        "recommend optimal simulation settings and parameter overrides. "
        "You must return ONLY a valid JSON object with the following keys:\n"
        "{\n"
        '  "startTime": 0.0,\n'
        '  "stopTime": <float>,\n'
        '  "numberOfIntervals": <int>,\n'
        '  "method": "dassl",\n'
        '  "parameter_overrides": { "ParamName": value, ... }\n'
        "}"
    )

    user_content = (
        f"Model Name: {model_name}\n"
        f"Available Parameters: {json.dumps(parameters, indent=2)}\n"
        f"Simulation Goal/Intent: {user_intent}"
    )

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_content),
    ]

    response = self.llm.invoke(messages)

    # Extract string content robustly
    content = response.content
    if isinstance(content, list):
      content_str = "".join(
          [
              block.get("text", "") if isinstance(block, dict) else str(block)
              for block in content
          ]
      )
    else:
      content_str = str(content)

    # Clean markdown json blocks if present
    if "```json" in content_str:
      content_str = content_str.split("```json")[1].split("```")[0].strip()
    elif "```" in content_str:
      content_str = content_str.split("```")[1].split("```")[0].strip()

    try:
      return json.loads(content_str)
    except json.JSONDecodeError:
      # Fallback defaults if parsing fails
      return {
          "startTime": 0.0,
          "stopTime": 0.01,
          "numberOfIntervals": 500,
          "method": "dassl",
          "parameter_overrides": {},
      }