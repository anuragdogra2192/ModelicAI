"""Model Context Card: what the Manager knows about the open model.

Built fresh from facts on every turn (source file, OMC parameters, run history,
revisions, notes), so GUI edits the agent did not make are still visible to it.
"""
import os
import re
from typing import Any, Dict, Optional

from api import services as svc
from api import store

MAX_SOURCE_CHARS = 6000


def _components(code: str):
  """Cheap structural summary from the source (instances of qualified classes)."""
  comps = re.findall(r"^\s*([A-Z][\w]*(?:\.[A-Za-z_]\w*)+)\s+([A-Za-z_]\w*)\s*[\(;\s]", code, re.M)
  return [f"{name}: {typ.split('.')[-1]} ({typ})" for typ, name in comps if not typ.startswith("Modelica.Units")]


def build_card(services: "svc.Services", name: Optional[str]) -> Dict[str, Any]:
  if not name or not svc.exists(name):
    return {"model": None, "workspace_models": svc.list_models()}
  code = svc.read_source(name)
  card: Dict[str, Any] = {
      "model": name,
      "workspace_models": svc.list_models(),
      "origin": store.meta(name),
      "components": _components(code),
      "connections": len(re.findall(r"\bconnect\s*\(", code)),
      "equation_based": not _components(code),
      "notes": store.notes(name),
      "revisions": [r["summary"] for r in store.revisions(name, 5)],
  }
  try:
    p = services.parameters(name)
    card["parameters"] = p["parameters"]
  except Exception as e:
    card["parameters_error"] = str(e)
  runs = store.runs(name, 2)
  if runs:
    last = runs[0]
    card["last_run"] = {"status": last["status"], "settings": last["settings"],
                        "overrides": last["overrides"], "wall_s": last["wall"],
                        "metrics": last["metrics"]}
    if len(runs) > 1:
      card["previous_run"] = {"settings": runs[1]["settings"], "overrides": runs[1]["overrides"],
                              "metrics": runs[1]["metrics"]}
  card["source"] = code if len(code) <= MAX_SOURCE_CHARS else code[:MAX_SOURCE_CHARS] + "\n// ... (truncated, use read_model)"
  return card


def render(card: Dict[str, Any], ui: Optional[Dict[str, Any]] = None) -> str:
  import json
  ui = ui or {}
  if not card.get("model"):
    head = ("No model is open. Workspace models: "
            + (", ".join(card["workspace_models"]) or "none") + ".")
  else:
    head = f"Open model: {card['model']}"
  body = json.dumps({k: v for k, v in card.items() if k not in ("model", "source")}, indent=1, default=str)
  parts = [head, "Context card (facts, kept up to date by the system):", body]
  if card.get("source"):
    parts += ["Current source:", "```modelica\n" + card["source"] + "\n```"]
  if ui:
    parts += ["Current GUI state: " + json.dumps(ui, default=str)]
  return "\n".join(parts)
