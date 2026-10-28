import json
import os
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
import plotly.graph_objects as go
from plotly.subplots import make_subplots


class VisualizationAgent:

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
              "variable": "vC",
              "title": "Capacitor Voltage",
              "y_axis_label": "V",
              "color": "blue",
          }],
      }

  def render_dashboard(
      self, omc_session, result_file: str, user_intent: str
  ) -> str:
    """Agent tool: Extracts variables, consults LLM for layout,

    and renders an interactive Plotly HTML dashboard.
    """
    print(
        "[VisualizationAgent] Inspecting result file and querying variables..."
    )

    # Standard variables available in our RC Circuit model results
    available_vars = ["time", "vC", "i", "vR"]

    print(
        "[VisualizationAgent] Designing layout using LLM based on user intent..."
    )
    layout_config = self.design_dashboard_layout(available_vars, user_intent)
    print(json.dumps(layout_config, indent=2))

    # Read time series
    time = omc_session.sendExpression(f"val(time, {result_file})")
    subplots = layout_config.get("subplots", [])

    fig = make_subplots(
        rows=len(subplots),
        cols=1,
        shared_x_axes=True,
        subplot_titles=[sub["title"] for sub in subplots],
    )

    for idx, sub in enumerate(subplots, start=1):
      var_name = sub["variable"]
      y_data = omc_session.sendExpression(f"val({var_name}, {result_file})")
      if y_data:
        fig.add_trace(
            go.Scatter(
                x=time,
                y=y_data,
                mode="lines",
                name=var_name,
                line=dict(color=sub.get("color", "blue"), width=2),
            ),
            row=idx,
            col=1,
        )

    fig.update_layout(
        title_text=f"<b>{layout_config.get('title', 'Simulation Dashboard')}</b>",
        xaxis_title="Time (s)",
        template="plotly_white",
        height=350 * len(subplots),
    )

    output_html = "interactive_dashboard.html"
    fig.write_html(output_html)
    print(
        f"\n✨ [VisualizationAgent] Interactive dashboard generated:"
        f" {output_html}"
    )
    return output_html