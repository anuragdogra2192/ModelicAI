import json
import os
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage


class VisualizationAgent:
  """Plans which result variables to show (titles, axis labels, colours) with an LLM.

  The old render_dashboard() path (Plotly HTML dashboard written to interactive_dashboard.html)
  was removed: ModelicAI Studio plots results natively in the browser.
  """

  def __init__(self):
    self.llm = ChatAnthropic(
        model=os.getenv("MODEL_NAME", "claude-haiku-5-5")
    )

  def design_dashboard_layout(
      self, available_variables: list, user_intent: str
  ) -> dict:
    """Uses the LLM to inspect available variables and user intent,

    then designs an optimal interactive dashboard layout in JSON.
    """
    system_prompt = (
        "You are an expert CAE data visualization engineer. "
        "Given the available simulation variables and user intent, design an interactive dashboard layout. "
        "Return ONLY a valid JSON object with this structure:\n"
        "{\n"
        '  "title": "Dashboard Title",\n'
        '  "subplots": [\n'
        "    {\n"
        '      "variable": "vC",\n'
        '      "title": "Capacitor Voltage",\n'
        '      "y_axis_label": "Voltage (V)",\n'
        '      "color": "blue"\n'
        "    }\n"
        "  ]\n"
        "}"
    )

    user_content = (
        f"Available Variables in Result File: {available_variables}\n"
        f"User Visualization Goal: {user_intent}"
    )

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_content),
    ]
    response = self.llm.invoke(messages)

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

    if "```json" in content_str:
      content_str = content_str.split("```json")[1].split("```")[0].strip()
    elif "```" in content_str:
      content_str = content_str.split("```")[1].split("```")[0].strip()

    try:
      return json.loads(content_str)
    except json.JSONDecodeError:
      return {
          "title": "Simulation Results Dashboard",
          "subplots": [{
              "variable": available_variables[0] if available_variables else "vC",
              "title": "Simulation Variable",
              "y_axis_label": "Value",
              "color": "blue",
          }],
      }
