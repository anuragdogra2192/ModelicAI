"""One place that decides which LLM each agent uses: Claude (Anthropic) or Meta Muse (Meta Model API).

Environment variables
  LLM_PROVIDER        default provider for every agent: "anthropic" (default) or "meta"
  MODI_PROVIDER       per-agent override for Modi (the manager)
  DESIGNER_PROVIDER   per-agent override for the designer (writes Modelica)
  SIMULATION_PROVIDER per-agent override for the simulation agent
  VISUALIZATION_PROVIDER

  MODEL_NAME          Claude model id            (default claude-haiku-5-5)
  ANTHROPIC_API_KEY   Claude key

  META_MODEL          Muse model id              (default muse-wonder-1.0; or muse-spark-1.3)
  MODEL_API_KEY       Meta Model API key (Bearer token)
  META_BASE_URL       default https://api.meta.ai/v1 (OpenAI-compatible Chat Completions)

Example: Modi on Claude, the designer on Muse Wonder
  LLM_PROVIDER=anthropic DESIGNER_PROVIDER=meta META_MODEL=muse-wonder-1.0

Notes
  * Never pass `temperature`: some models reject it (see CLAUDE.md).
  * Meta is reached through its OpenAI-compatible Chat Completions endpoint, which supports tool calling
    (needed by Modi) but does not carry reasoning between turns. Meta also offers an Anthropic-compatible
    /v1/messages endpoint; Chat Completions is used here because it takes the Bearer key directly.
"""
import os
from typing import Any, Dict

PROVIDERS = ("anthropic", "meta")


def provider_for(role: str) -> str:
  p = (os.getenv(f"{role.upper()}_PROVIDER") or os.getenv("LLM_PROVIDER") or "anthropic").strip().lower()
  if p not in PROVIDERS:
    raise ValueError(f"Unknown LLM provider {p!r} for {role}; use one of {', '.join(PROVIDERS)}.")
  return p


def model_for(role: str) -> str:
  if provider_for(role) == "meta":
    return os.getenv(f"{role.upper()}_MODEL") or os.getenv("META_MODEL", "muse-wonder-1.0")
  return os.getenv(f"{role.upper()}_MODEL") or os.getenv("MODEL_NAME", "claude-haiku-5-5")


def make_llm(role: str, max_tokens: int = 4096):
  """Chat model for an agent role: "modi", "designer", "simulation" or "visualization"."""
  provider, model = provider_for(role), model_for(role)
  if provider == "meta":
    key = os.getenv("MODEL_API_KEY")
    if not key:
      raise RuntimeError(f"{role}: LLM provider is 'meta' but MODEL_API_KEY is not set.")
    from langchain_openai import ChatOpenAI
    # Muse models reason before answering, and the reasoning counts against the output limit: too small a
    # limit gives an empty answer. Give Meta a larger budget and a moderate reasoning effort.
    effort = os.getenv(f"{role.upper()}_REASONING_EFFORT") or os.getenv("META_REASONING_EFFORT", "medium")
    return ChatOpenAI(model=model, api_key=key, base_url=os.getenv("META_BASE_URL", "https://api.meta.ai/v1"),
                      max_tokens=max(max_tokens, int(os.getenv("META_MAX_TOKENS", "32000"))),
                      reasoning_effort=effort, timeout=600, max_retries=2)
  from langchain_anthropic import ChatAnthropic
  return ChatAnthropic(model=model, max_tokens=max_tokens, timeout=300, max_retries=2)


def describe() -> Dict[str, Any]:
  """Which provider/model each agent uses (printed at startup, shown by /api/studio/llm)."""
  return {r: {"provider": provider_for(r), "model": model_for(r)} for r in ("modi", "designer", "simulation")}
