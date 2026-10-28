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
        "You are an expert Modelica simulation engineer and software architect. "
        "Your task is to write clean, valid, standalone Modelica (.mo) code "
        "based on the user's physical system description. "
        "Use components from the Modelica Standard Library (MSL) when applicable. "
        "Ensure all differential-algebraic equations (DAEs) are balanced and variables are properly typed. "
        "Return ONLY the valid Modelica code block enclosed in triple backticks "
        "using the `modelica` identifier. Do not include markdown prose outside the code block."
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