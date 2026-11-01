"""REST endpoints backing the ModelicAI Studio frontend (static/index.html).

These complement the routes in main.py: they expose model sources, fast
(LLM-free) simulation runs and raw result series so the browser can plot
natively instead of embedding the Plotly dashboard.
"""
import json
import os
import re
import threading
import urllib.request
from typing import Any, Dict, List, Optional

import numpy as np
import scipy.io as sio
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from utils.omc_graphics import DiagramBuilder
from api import chat_store, store
from api.services import Services, omc_lock
from api.bake import bake

MODEL_ROOT = "models"
# Callbacks run after a model is deleted (e.g. clearing the agent's thread).
on_model_deleted = []
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# One OMC session is shared by every request; omc_lock lives in api.services.


def build_router(orchestrator) -> APIRouter:
  router = APIRouter(prefix="/api/studio", tags=["studio"])
  client = orchestrator.client
  services = Services(orchestrator)
  builder = {"b": DiagramBuilder(client.omc)}

  def model_dir(name: str) -> str:
    if not _NAME_RE.match(name):
      raise HTTPException(400, f"Invalid model name: {name}")
    return os.path.join(MODEL_ROOT, name)

  def mo_path(name: str) -> str:
    path = os.path.join(model_dir(name), f"{name}.mo")
    if not os.path.exists(path):
      raise HTTPException(404, f"Model source not found: {name}")
    return path

  def result_path(name: str) -> str:
    path = os.path.join(model_dir(name), f"{name}_res.mat")
    if not os.path.exists(path):
      raise HTTPException(404, f"No simulation results yet for {name}")
    return path

  def regen_graph(name: str) -> Dict[str, Any]:
    try:
      graph = orchestrator.graph_parser.parse_mo_file(mo_path(name))
      with open(os.path.join(model_dir(name), "graph_schema.json"), "w") as f:
        json.dump(graph, f, indent=2)
      return graph
    except Exception:
      return {"nodes": [], "edges": []}

  # ---------------------------------------------------------------- models
  @router.get("/models")
  def list_models() -> List[Dict[str, Any]]:
    if not os.path.isdir(MODEL_ROOT):
      return []
    out = []
    for name in sorted(os.listdir(MODEL_ROOT)):
      d = os.path.join(MODEL_ROOT, name)
      mo = os.path.join(d, f"{name}.mo")
      if _NAME_RE.match(name) and os.path.exists(mo):
        out.append({
            "name": name,
            "has_results": os.path.exists(os.path.join(d, f"{name}_res.mat")),
            "modified": os.path.getmtime(mo),
        })
    return out

  @router.get("/models/{name}")
  def get_model(name: str) -> Dict[str, Any]:
    path = mo_path(name)
    with open(path) as f:
      code = f.read()
    graph_file = os.path.join(model_dir(name), "graph_schema.json")
    if os.path.exists(graph_file):
      with open(graph_file) as f:
        graph = json.load(f)
    else:
      graph = regen_graph(name)
    return {
        "name": name,
        "code": code,
        "graph": graph,
        "has_results": os.path.exists(
            os.path.join(model_dir(name), f"{name}_res.mat")),
        "results_stale": os.path.exists(os.path.join(model_dir(name), f"{name}_res.mat")) and
            os.path.getmtime(os.path.join(model_dir(name), f"{name}_res.mat")) < os.path.getmtime(path),
    }

  class SourceUpdate(BaseModel):
    code: str

  @router.put("/models/{name}")
  def save_model(name: str, body: SourceUpdate) -> Dict[str, Any]:
    """Saves edited code and re-checks it with OpenModelica."""
    os.makedirs(model_dir(name), exist_ok=True)
    path = os.path.join(model_dir(name), f"{name}.mo")
    previous = open(path).read() if os.path.exists(path) else None
    changed = previous != body.code
    with open(path, "w") as f:
      f.write(body.code)
    hint = ""
    declared = re.search(r"^\s*(?:partial\s+)?model\s+([A-Za-z_]\w*)", body.code, re.M)
    if declared and declared.group(1) != name:
      hint = (f"The code declares model '{declared.group(1)}' but this file is '{name}'. "
              f"Keep the name '{name}', or ask Modi to create a new model.")
    with omc_lock:
      res = client.compile_model(file_path=path, model_name=name)
    builder["b"] = DiagramBuilder(client.omc)
    ok = res["status"] == "SUCCESS"
    graph = regen_graph(name) if ok else None
    if ok and changed:
      store.add_revision(name, body.code, "edited in the code editor", source="gui")
    res_file = os.path.join(model_dir(name), f"{name}_res.mat")
    return {"status": res["status"], "error": "\n".join(x for x in (hint, res.get("error", "")) if x),
            "message": res.get("message", ""), "warnings": res.get("warnings", ""),
            "changed": changed, "graph": graph,
            "results_stale": os.path.exists(res_file) and os.path.getmtime(res_file) < os.path.getmtime(path)}

  @router.delete("/models/{name}")
  def delete_model(name: str) -> Dict[str, Any]:
    """Erases the model's memory (chat, runs, revisions, notes, proposals, agent thread)
    and moves its files to models/.trash/ as a safety net."""
    import shutil, time as _time
    folder = model_dir(name)
    if not os.path.isdir(folder):
      raise HTTPException(404, f"Model not found: {name}")
    stamp = _time.strftime("%Y%m%d-%H%M%S")
    trash = os.path.join(MODEL_ROOT, ".trash")
    os.makedirs(trash, exist_ok=True)
    dest = os.path.join(trash, f"{name}-{stamp}")
    shutil.move(folder, dest)
    purged = store.purge_model(name)
    for pid in purged["proposal_ids"]:  # scratch folders of this model's proposals
      shutil.rmtree(os.path.join(MODEL_ROOT, ".proposals", pid), ignore_errors=True)
    for cb in on_model_deleted:
      try:
        cb(name)
      except Exception:
        pass
    return {"deleted": name, "files_moved_to": dest, "memory_erased": purged["deleted_rows"]}

  class BakeRequest(BaseModel):
    values: Dict[str, Any]
    code: Optional[str] = None  # unsaved editor content; defaults to the saved file

  @router.post("/models/{name}/bake")
  def bake_parameters(name: str, req: BakeRequest) -> Dict[str, Any]:
    """Rewrites parameter values into the source ("Write into model"). Does not save."""
    with open(mo_path(name)) as f:
      code = req.code if req.code is not None else f.read()
    new_code, applied, skipped = bake(code, {k: str(v) for k, v in req.values.items()})
    return {"code": new_code, "applied": applied, "skipped": skipped}

  @router.get("/models/{name}/diagram")
  def diagram(name: str) -> Dict[str, Any]:
    """Full diagram from OMC's graphical API: original icons, placements, wires."""
    path = os.path.abspath(mo_path(name))
    with open(path) as f:
      source = f.read()
    with omc_lock:
      client.omc.sendExpression(f'loadFile("{path}")')
      try:
        return builder["b"].scene(name, source)
      except Exception as e:
        raise HTTPException(500, f"Diagram extraction failed: {e}")

  @router.get("/models/{name}/parameters")
  def parameters(name: str) -> Dict[str, Any]:
    path = os.path.abspath(mo_path(name))
    with omc_lock:
      client.omc.sendExpression(f'loadFile("{path}")')
      params = client.get_model_parameters(name)
      opts = client.get_simulation_options(name)
    return {"parameters": params, "simulation_options": opts}

  # ------------------------------------------------------------ simulation
  class RunRequest(BaseModel):
    start_time: float = 0.0
    stop_time: float = 0.01
    number_of_intervals: int = 500
    method: str = "dassl"
    parameter_overrides: Dict[str, Any] = {}

  @router.post("/models/{name}/run")
  def run(name: str, req: RunRequest) -> Dict[str, Any]:
    """Simulates directly (no LLM calls), records the run, returns the variable list."""
    mo_path(name)
    bad = [k for k, v in req.parameter_overrides.items()
           if not re.match(r"^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$|^(true|false)$", str(v).strip())]
    if bad:
      raise HTTPException(400, "Parameter values must be numbers (or true/false): " + ", ".join(bad))
    try:
      r = services.simulate(name, {"start_time": req.start_time, "stop_time": req.stop_time,
                                   "number_of_intervals": req.number_of_intervals, "method": req.method},
                            req.parameter_overrides, source="gui")
    except ValueError as e:
      raise HTTPException(400, str(e))
    if r["status"] != "SUCCESS":
      diag = services.diagnose(os.path.abspath(mo_path(name)), name)
      return {"status": "FAILED", "messages": "\n".join(x for x in (r["messages"], diag) if x)}
    return {"status": "SUCCESS", "messages": r["messages"], "timing": {"wall": r["wall"]},
            "variables": read_variable_names(result_path(name))}

  @router.get("/models/{name}/runs")
  def run_history(name: str) -> List[Dict[str, Any]]:
    mo_path(name)
    return store.runs(name, 20)

  @router.get("/models/{name}/units")
  def units(name: str) -> Dict[str, str]:
    """Variable units from OpenModelica's model description (<Name>_init.xml, written on every build)."""
    import xml.etree.ElementTree as ET
    path = os.path.join(model_dir(name), f"{name}_init.xml")
    if not os.path.exists(path):
      return {}
    out = {}
    try:
      for sv in ET.parse(path).getroot().iter("ScalarVariable"):
        for child in sv:
          unit = child.get("unit")  # result files hold base units (K, not degC), so ignore displayUnit
          if unit:
            out[sv.get("name")] = unit
    except ET.ParseError:
      return {}
    return out

  @router.get("/models/{name}/variables")
  def variables(name: str) -> List[str]:
    return read_variable_names(result_path(name))

  @router.get("/models/{name}/results")
  def results(name: str, vars: str = Query("")) -> Dict[str, Any]:
    wanted = [v for v in vars.split(",") if v]
    return read_series(result_path(name), wanted)

  # ------------------------------------------------------------------ chat
  @router.get("/models/{name}/messages")
  def messages(name: str) -> List[Dict[str, Any]]:
    if name != chat_store.WORKSPACE:
      model_dir(name)  # validates the name
    return chat_store.history(name)

  # ------------------------------------------------------------- MSL icons
  @router.get("/icon/{class_name}")
  def icon(class_name: str):
    """Original MSL icon (BSD-3 Modelica License) for a full class name.

    Looks in static/icons/<class>.svg first (offline override), then fetches
    the class page on doc.modelica.org, takes its first Icons/*.svg and
    caches it in static/icons/.
    """
    if not re.match(r"^Modelica(\.[A-Za-z_]\w*)+$", class_name):
      raise HTTPException(400, "Only Modelica Standard Library classes")
    os.makedirs(ICON_DIR, exist_ok=True)
    cached = os.path.join(ICON_DIR, f"{class_name}.svg")
    if not os.path.exists(cached):
      try:
        page = _http_get(f"{ICON_BASE}{class_name}.html").decode("utf-8", "ignore")
        m = re.search(r'src="(?:\./)?(Icons/[0-9a-f]+\.svg)"', page)
        if not m:
          raise HTTPException(404, "No icon on class page")
        with open(cached, "wb") as f:
          f.write(_http_get(ICON_BASE + m.group(1)))
      except HTTPException:
        raise
      except Exception as e:
        raise HTTPException(502, f"Icon fetch failed: {e}")
    return FileResponse(cached, media_type="image/svg+xml",
                        headers={"Cache-Control": "max-age=604800"})

  return router


ICON_BASE = "https://doc.modelica.org/om/"
ICON_DIR = os.path.join("static", "icons")


def _http_get(url: str) -> bytes:
  req = urllib.request.Request(url, headers={"User-Agent": "ModelicAI-Studio"})
  with urllib.request.urlopen(req, timeout=15) as r:
    return r.read()


# ------------------------------------------------- OMC v4 .mat result reader
def _load(path: str):
  m = sio.loadmat(path)
  transposed = "Aclass" in m and "binTrans" in "".join(
      str(r) for r in m["Aclass"])
  # scipy returns char matrices as an array of equal-length strings (rows).
  raw = [str(s) for s in m["name"]]
  width = max(len(s) for s in raw)
  chars = np.array([list(s.ljust(width, "\x00")) for s in raw])
  rows = chars.T if transposed else chars
  names = ["".join(r).replace("\x00", "").strip() for r in rows]
  info = m["dataInfo"]
  if transposed:
    info = info.T
  d1 = m["data_1"].T if transposed else m["data_1"]
  d2 = m["data_2"].T if transposed else m["data_2"]
  return names, info, d1, d2


def read_variable_names(path: str) -> List[str]:
  names, _, _, _ = _load(path)
  return [n for n in names if n and n != "time"]


def read_series(path: str, wanted: List[str]) -> Dict[str, Any]:
  names, info, d1, d2 = _load(path)
  index = {n: i for i, n in enumerate(names)}
  time = d2[:, 0] if d2.ndim == 2 else np.array([])
  series: Dict[str, Optional[List[float]]] = {}
  for var in wanted:
    i = index.get(var)
    if i is None:
      series[var] = None
      continue
    block, col = int(info[i][0]), int(info[i][1])
    sign = -1.0 if col < 0 else 1.0
    col = abs(col) - 1
    if block == 2 or block == 0:
      vals = d2[:, col] * sign
    else:  # parameter: constant over time
      vals = np.full_like(time, d1[0, col] * sign, dtype=float)
    series[var] = np.nan_to_num(vals.astype(float)).tolist()
  return {"time": time.astype(float).tolist(), "series": series}
