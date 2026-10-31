import os
from langchain_core.messages import HumanMessage, SystemMessage
#from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic

class DesignAgent:

  def __init__(self, model_name: str = "claude-haiku-5-5"):
    """Initializes the Design Agent using Claude Haiku."""
    self.llm = ChatAnthropic(
        model=os.getenv("MODEL_NAME", model_name),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
    )

  def generate_code(self, prompt: str, error_log: str = None) -> str:
    """Generates valid Modelica (.mo) syntax based on user instructions.

    If an error_log is provided from the compilation check, it executes the
    self-correction loop by feeding the traceback back to the model[cite: 1].
    """

    system_prompt = (
            "You are an expert Modelica code generation agent.\n"
            "You write valid, syntactically correct Modelica code (.mo) without markdown syntax errors.\n"
            "CRITICAL MODELICA RULES:\n"
            "1. Always include standard library imports if needed (e.g., `import Modelica.Electrical.Analog;`).\n"
            "2. For an RC Circuit, use these exact component paths:\n"
            "   - DC Voltage Source: `Modelica.Electrical.Analog.Sources.ConstantVoltage` (with parameter `V`)\n"
            "   - Resistor: `Modelica.Electrical.Analog.Basic.Resistor` (with parameter `R`)\n"
            "   - Capacitor: `Modelica.Electrical.Analog.Basic.Capacitor` (with parameter `C`)\n"
            "   - Ground: `Modelica.Electrical.Analog.Basic.Ground`\n"
            "3. Connect components properly in the `equation` section using `connect(pin1, pin2);`.\n"
            "4. Return ONLY raw Modelica code or code inside a clean ```modelica block. No conversational text."
        )

    user_content = f"User Request: {prompt}"

    # ReAct Self-Correction Loop: Ingest compiler error tracebacks if validation fails
    if error_log:
      user_content += (
          f"\n\nPrevious compilation failed with this error log:\n{error_log}\n"
          "Please analyze this traceback, fix the syntax, balance the DAE equations, "
          "or correct the component definitions, and output the corrected Modelica code."
      )

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_content),
    ]

    response = self.llm.invoke(messages)
    content = response.content
    # Anthropic returns a list of content blocks; keep only the text parts.
    if isinstance(content, list):
      content = "".join(
          block.get("text", "") for block in content
          if isinstance(block, dict) and block.get("type") == "text"
      )

    # Cleanly extract the code block if wrapped in markdown
    if "```modelica" in content:
      content = content.split("```modelica")[1].split("```")[0].strip()
    elif "```" in content:
      content = content.split("```")[1].split("```")[0].strip()

    return content