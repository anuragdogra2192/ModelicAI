import json
import math
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from agents.llm import make_llm
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from utils.tracing import traced

SOLVERS = ["dassl", "ida", "cvode", "euler", "heun", "rungekutta", "impeuler", "trapezoid", "imprungekutta", "gbode"]
MAX_INTERVALS = 100_000


class SimulationAgent:
  """Chooses simulation settings with an LLM, then checks the answer before anyone uses it.

  The LLM sees the model's parameters, structure, experiment annotation and earlier runs. Its JSON is
  validated (numbers, stop > start, sensible interval count, known solver, overrides only for existing
  parameters with numeric values). Invalid answers are sent back once with the problems listed; if the
  answer is still unusable the model's own experiment annotation is used, never a silent 0.01 s.
  """

  def __init__(self):
    self.llm = make_llm("simulation", max_tokens=1024)

  # ------------------------------------------------------------------ prompt
  SYSTEM = (
      "You are an expert CAE simulation engineer for Modelica/OpenModelica models.\n"
      "Choose simulation settings that show the behaviour the user asks for:\n"
      "- stopTime: long enough for the full dynamic response, typically about 5 dominant time constants or\n"
      "  several oscillation periods (estimate them from the parameters, e.g. tau=R*C, tau=m*c/(h*A),\n"
      "  omega=sqrt(k/m)); respect the model's experiment annotation unless the goal needs otherwise.\n"
      "- numberOfIntervals: enough points to resolve the fastest dynamics shown (typically 500-5000).\n"
      "- method: 'dassl' by default; 'ida' or 'cvode' for stiff systems; never invent solver names.\n"
      "- parameter_overrides: ONLY parameters that exist in the list given, ONLY if the goal asks for a\n"
      "  change, numeric values only.\n"
      "Return ONLY a JSON object:\n"
      '{"startTime": 0.0, "stopTime": <number>, "numberOfIntervals": <integer>, "method": "dassl",\n'
      ' "parameter_overrides": {"name": <number>}, "reason": "<one sentence: which time scale you used>"}'
  )

  @traced("simulation_agent", kind="AGENT", agent="simulation",
          out=lambda c: {"sim.stop_time": c.get("stopTime"), "sim.intervals": c.get("numberOfIntervals"),
                         "sim.method": c.get("method"), "sim.checks": c.get("checks"),
                         "sim.fallback": any("experiment annotation" in x for x in c.get("checks", [])),
                         "output.value": c.get("reason")})
  def suggest_simulation_config(self, model_name: str, parameters: dict, user_intent: str,
                                context: Optional[dict] = None) -> dict:
    context = context or {}
    model_params = (parameters or {}).get("model_parameters", parameters or {}) or {}
    known = set(model_params.keys())
    user = (
        f"Model: {model_name}\n"
        f"Parameters (name: value): {json.dumps(model_params)}\n"
        f"Experiment annotation in the model: {json.dumps(context.get('experiment') or {})}\n"
        f"Components: {json.dumps(context.get('components') or [])[:2000]}\n"
        f"Last run: {json.dumps((parameters or {}).get('last_run') or context.get('last_run'), default=str)[:1500]}\n"
        f"Last error: {context.get('last_error') or 'none'}\n"
        f"Goal: {user_intent}"
    )
    messages = [SystemMessage(content=self.SYSTEM), HumanMessage(content=user)]
    notes: List[str] = []
    for attempt in range(2):
      text = self._text(self.llm.invoke(messages))
      cfg, problems = self.check(self._json(text), known)
      if not problems:
        cfg["checks"] = notes + ["settings checked: numeric, consistent, known solver, valid overrides"]
        return cfg
      notes.append(f"attempt {attempt + 1} rejected: " + "; ".join(problems))
      messages += [AIMessage(content=text),
                   HumanMessage(content="Your settings are invalid: " + "; ".join(problems) +
                                ". Return a corrected JSON object only.")]
    cfg = self.fallback(context)
    cfg["checks"] = notes + ["used the model's experiment annotation instead"]
    return cfg

  # ------------------------------------------------------------- validation
  @staticmethod
  def check(raw: Any, known_params: set) -> Tuple[Dict[str, Any], List[str]]:
    """Returns (clean settings, problems). Problems empty means the settings can be used as they are."""
    problems: List[str] = []
    if not isinstance(raw, dict):
      return {}, ["the answer is not a JSON object"]

    def num(key, default=None):
      v = raw.get(key, default)
      try:
        f = float(v)
        if not math.isfinite(f):
          raise ValueError
        return f
      except (TypeError, ValueError):
        problems.append(f"{key} must be a finite number (got {v!r})")
        return None

    start = num("startTime", 0.0)
    stop = num("stopTime")
    n = num("numberOfIntervals", 500)
    if start is not None and start < 0:
      problems.append("startTime must be >= 0")
    if start is not None and stop is not None and stop <= start:
      problems.append("stopTime must be greater than startTime")
    if n is not None:
      if n != int(n):
        problems.append("numberOfIntervals must be an integer")
      elif not 10 <= n <= MAX_INTERVALS:
        problems.append(f"numberOfIntervals must be between 10 and {MAX_INTERVALS}")
    method = str(raw.get("method", "dassl"))
    if method not in SOLVERS:
      problems.append(f"method must be one of {', '.join(SOLVERS)}")
    overrides = raw.get("parameter_overrides") or {}
    clean_ov = {}
    if not isinstance(overrides, dict):
      problems.append("parameter_overrides must be an object")
    else:
      for k, v in overrides.items():
        if known_params and k not in known_params:
          problems.append(f"override '{k}' is not a parameter of this model")
          continue
        try:
          f = float(v)
          if not math.isfinite(f):
            raise ValueError
          clean_ov[k] = f
        except (TypeError, ValueError):
          problems.append(f"override '{k}' must be numeric (got {v!r})")
    cfg = {"startTime": start or 0.0, "stopTime": stop, "numberOfIntervals": int(n) if n else 500,
           "method": method if method in SOLVERS else "dassl", "parameter_overrides": clean_ov,
           "reason": str(raw.get("reason", ""))[:300]}
    return cfg, problems

  @staticmethod
  def fallback(context: dict) -> dict:
    exp = context.get("experiment") or {}
    start = float(exp.get("StartTime", 0.0) or 0.0)
    stop = float(exp.get("StopTime", 1.0) or 1.0)
    if stop <= start:
      stop = start + 1.0
    interval = exp.get("Interval")
    n = int(round((stop - start) / float(interval))) if interval else 500
    return {"startTime": start, "stopTime": stop, "numberOfIntervals": max(10, min(MAX_INTERVALS, n)),
            "method": "dassl", "parameter_overrides": {},
            "reason": "Used the model's experiment annotation (the suggested settings did not pass the checks)."}

  # ---------------------------------------------------------------- helpers
  @staticmethod
  def _text(response) -> str:
    c = response.content
    if isinstance(c, list):
      return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in c)
    return str(c)

  @staticmethod
  def _json(text: str) -> Any:
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S) or re.search(r"(\{.*\})", text, re.S)
    if not m:
      return None
    try:
      return json.loads(m.group(1))
    except json.JSONDecodeError:
      return None


def experiment_of(code: str) -> Dict[str, float]:
  """StartTime/StopTime/Interval/Tolerance from a model's experiment(...) annotation."""
  m = re.search(r"experiment\s*\(([^)]*)\)", code or "")
  out = {}
  if m:
    for k, v in re.findall(r"(StartTime|StopTime|Interval|Tolerance)\s*=\s*([0-9.eE+-]+)", m.group(1)):
      try:
        out[k] = float(v)
      except ValueError:
        pass
  return out
