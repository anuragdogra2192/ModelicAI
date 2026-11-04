"""The MSL knowledge graph at work: search, describe, examples, pre-compile check and designer context.

Reads data/msl_index.db (built from the installed MSL by tools/build_msl_index.py). No LLM, no OMC calls:
plain SQLite queries, so every answer is exact and takes milliseconds. If the index is not built yet,
`available()` is False and every caller falls back to the old behaviour.
"""
import difflib
import json
import os
import re
import sqlite3
import threading
from typing import Any, Dict, List, Optional, Set, Tuple

INDEX_PATH = os.getenv("MSL_INDEX", os.path.join("data", "msl_index.db"))
_local = threading.local()

STOP = set("""a an the and or of to in on at by for with from into onto as is are be it its this that these those
then than so very some any each every one two three first second third model models simulate simulation build
create make design system simple show plot connected connect using use add new please run seconds second minutes
s ms min hour hours day kg m mm cm v kv mv a ma w kw nm rpm rad deg c k pa bar l""".split())
SYNONYMS = {
    "tank": ["tank", "vessel"], "tanks": ["tank", "vessel"], "container": ["tank", "vessel"],
    "gearbox": ["gear"], "motor": ["emf", "motor", "machine"], "heater": ["heat flow", "heater"],
    "controller": ["pid", "controller"], "regulator": ["pid", "controller"], "thermostat": ["hysteresis", "on off"],
    "battery": ["battery", "voltage source"], "load": ["inertia", "mass", "resistor"], "wheel": ["inertia"],
    "radiator": ["heat flow", "convection"], "room": ["heat capacitor"], "wall": ["thermal conductor"],
}
PLAYBOOKS = {
    "Modelica.Fluid": (
        "Fluid: declare `inner Modelica.Fluid.System system;`. Redeclare the same Medium in every fluid component "
        "(`redeclare package Medium = Medium`). Vessels have `nPorts` and ports `ports[i]` (set nPorts, connect "
        "ports[1], ports[2], ...). For simple tanks use `use_portsData=false`. A fluid port must be connected exactly "
        "where flow happens; every fluid component needs a pressure/flow boundary somewhere in the circuit."),
    "Modelica.Thermal": (
        "Thermal: give every HeatCapacitor a start temperature `T(start=..., fixed=true)`; temperatures are in K "
        "(20 degC = 293.15 K). Every heat port must be connected."),
    "Modelica.Mechanics": (
        "Mechanics: fix the reference with a `Fixed` component; give each mass/inertia consistent start values "
        "(s/v or phi/w with fixed=true) only where they are independent; connect flange_a/flange_b in a chain."),
    "Modelica.Electrical": (
        "Electrical: every circuit needs a `Ground` connected to it; connect p/n pins; sources need both pins connected."),
    "events": (
        "Events: models with impacts or switching (bouncing, contact, on/off thermostats) must not chatter. "
        "Stop re-triggering below a threshold (e.g. a bouncing ball rests when |v| < 0.01 m/s) and use hysteresis "
        "for switching (Modelica.Blocks.Logical.Hysteresis)."),
}
UNITS_OLD = re.compile(r"\bModelica\.SIunits\b")


def _db() -> Optional[sqlite3.Connection]:
  """One read-only connection per thread (SQLite connections must not be shared across threads)."""
  path = os.path.abspath(INDEX_PATH)
  con = getattr(_local, "con", None)
  if con is not None and getattr(_local, "path", None) == path and getattr(_local, "mtime", None) == _mtime(path):
    return con
  if not os.path.exists(path):
    return None
  try:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
  except sqlite3.Error:
    return None
  _local.con, _local.path, _local.mtime = con, path, _mtime(path)
  return con


def _mtime(path: str) -> float:
  try:
    return os.path.getmtime(path)
  except OSError:
    return 0.0


def available() -> bool:
  return _db() is not None


def meta() -> Dict[str, str]:
  db = _db()
  return {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM meta")} if db else {}


def _rows(sql: str, args=()) -> List[sqlite3.Row]:
  db = _db()
  return db.execute(sql, args).fetchall() if db else []


def node(cid: str) -> Optional[sqlite3.Row]:
  r = _rows("SELECT * FROM nodes WHERE id=?", (cid,))
  return r[0] if r else None


# --------------------------------------------------------------------------------- graph
def resolve(name: str) -> Optional[str]:
  """Full class id for a full path or a unique short name ('OpenTank', 'Vessels.OpenTank')."""
  name = (name or "").strip()
  if not name:
    return None
  if node(name):
    return name
  rows = _rows("SELECT id FROM nodes WHERE id LIKE ? AND kind NOT IN ('package')", (f"%.{name}",))
  ids = [r["id"] for r in rows]
  plain = [i for i in ids if not re.search(r"\.(Examples|BaseClasses|Internal)\.", i)]
  pick = plain or ids
  return pick[0] if len(pick) == 1 else None


def candidates(name: str, limit: int = 6) -> List[str]:
  """All classes whose path ends with `name` (for ambiguous short names like 'OpenTank')."""
  name = (name or "").strip()
  rows = _rows("SELECT id FROM nodes WHERE id LIKE ? AND kind NOT IN ('package')", (f"%.{name}",)) if name else []
  ids = [r["id"] for r in rows]
  ids.sort(key=lambda i: (bool(re.search(r"\.(Examples|BaseClasses|Internal)\.", i)), len(i)))
  return ids[:limit]


def chain(cid: str) -> List[str]:
  """The class and all its base classes (leaf first)."""
  out, todo, seen = [], [cid], set()
  while todo and len(out) < 40:
    c = todo.pop(0)
    if c in seen:
      continue
    seen.add(c)
    out.append(c)
    todo += [r["dst"] for r in _rows("SELECT dst FROM edges WHERE src=? AND type='extends'", (c,))]
  return out


def elements(cid: str, include_protected: bool = False) -> Dict[str, Dict[str, Any]]:
  """Every declared element (parameters, variables, connectors, sub-components) incl. inherited; nearest wins."""
  out: Dict[str, Dict[str, Any]] = {}
  for c in chain(cid):
    for r in _rows("SELECT * FROM components WHERE cls=?", (c,)):
      if r["name"] in out or (r["protected"] and not include_protected):
        continue
      out[r["name"]] = {**dict(r), "declared_in": c}
  return out


def chain_complete(cid: str) -> bool:
  """True if every base class of `cid` is in the index (otherwise inherited elements may be missing)."""
  return all(node(c) is not None for c in chain(cid))


def replaceables(cid: str) -> Dict[str, Dict[str, Any]]:
  """name -> {kind, default, default_partial, redeclared_to}; a redeclare in a subclass overrides the default."""
  out: Dict[str, Dict[str, Any]] = {}
  redeclared: Dict[str, str] = {}
  for c in chain(cid):
    for r in _rows("SELECT * FROM edges WHERE src=? AND type IN ('redeclare','replaceable')", (c,)):
      if r["type"] == "redeclare":
        # `redeclare package Medium = Medium` only hands the outer medium on to a sub-component;
        # it does not choose a concrete one, so it does not count as redeclared.
        if str(r["dst"]).split(".")[-1] != r["name"]:
          redeclared.setdefault(r["name"], r["dst"])
      elif r["name"] not in out:
        info = json.loads(r["info"] or "{}")
        out[r["name"]] = {**info, "declared_in": c, "redeclared_to": redeclared.get(r["name"])}
  for name, info in out.items():
    if not info.get("redeclared_to") and name in redeclared:
      info["redeclared_to"] = redeclared[name]
  return out


def _partial_default(name: str, info: Dict[str, Any]) -> bool:
  return bool(info.get("default_partial")) or "PartialMedium" in str(info.get("default", ""))


def options(base: str, limit: int = 6) -> List[str]:
  """Usable (non-partial) classes that extend `base`, e.g. the pump characteristics for flowCharacteristic."""
  out, todo, seen = [], [base], set()
  while todo and len(out) < limit:
    b = todo.pop(0)
    for r in _rows("SELECT e.src, n.partial FROM edges e JOIN nodes n ON n.id=e.src "
                   "WHERE e.dst=? AND e.type='extends'", (b,)):
      if r["src"] in seen:
        continue
      seen.add(r["src"])
      todo.append(r["src"])
      parent = node(r["src"].rsplit(".", 1)[0])
      if (not r["partial"] and not re.search(r"\.(Examples|Internal)\.", r["src"])
          and parent is not None and parent["kind"] == "package"):
        out.append(r["src"])
  return out[:limit]


def _short_type(t: str) -> str:
  return re.sub(r"^Modelica\.Units\.SI\.", "SI.", t or "")


def describe(name: str) -> Dict[str, Any]:
  cid = resolve(name)
  if not cid:
    many = candidates(name)
    if len(many) > 1:
      return {"error": f"{name!r} is ambiguous: several MSL classes have this name. Call again with the full path.",
              "candidates": many}
    return {"error": f"No MSL class named {name!r}.", "did_you_mean": similar(name)}
  n = node(cid)
  els = elements(cid)
  params = [{"name": k, "type": _short_type(v["type_id"] or v["type"]), "comment": v["comment"]}
            for k, v in els.items() if v["variability"] == "parameter"]
  conns = [{"name": k, "type": v["type_id"] or v["type"], "dims": v["dims"] or ""}
           for k, v in els.items() if v["is_connector"]]
  # inputs that are not connectors (e.g. FixedShape's r_shape, length, color) are set like parameters
  inputs = [{"name": k, "type": _short_type(v["type_id"] or v["type"])} for k, v in els.items()
            if v["causality"] == "input" and not v["is_connector"] and v["variability"] not in ("parameter", "constant")
            and not re.search(r"color|specular|animation|label", k, re.I)]
  variables = [k for k, v in els.items() if v["variability"] not in ("parameter", "constant") and not v["is_connector"]
               and v["causality"] != "input"]
  reps = {k: {"default": v.get("default"), "must_redeclare": _partial_default(k, v) and not v.get("redeclared_to"),
              "options": options(v.get("default")) if _partial_default(k, v) else []}
          for k, v in replaceables(cid).items()}
  return {"id": cid, "kind": n["kind"], "partial": bool(n["partial"]), "description": n["description"],
          "parameters": params, "inputs": inputs, "connectors": conns, "variables": variables[:20], "replaceable": reps,
          "used_in_examples": [r["src"] for r in _rows("SELECT DISTINCT src FROM edges WHERE type='uses' AND dst=? LIMIT 5", (cid,))],
          "docs": (n["docs"] or "")[:500]}


def describe_text(name: str, max_params: int = 14) -> str:
  d = describe(name)
  if d.get("error"):
    if d.get("candidates"):
      return d["error"] + " Candidates: " + ", ".join(d["candidates"])
    return d["error"] + (f" Did you mean: {', '.join(d['did_you_mean'])}?" if d.get("did_you_mean") else "")
  lines = [f"{d['id']} ({d['kind']}{', PARTIAL: cannot be used directly' if d['partial'] else ''}) \"{d['description']}\""]
  if d["parameters"]:
    ps = [f"{p['name']}: {p['type']}" for p in d["parameters"][:max_params]]
    lines.append("  parameters: " + ", ".join(ps) + (" ..." if len(d["parameters"]) > max_params else ""))
  if d.get("variables"):
    lines.append("  variables (set initial values as name(start=..., fixed=true)): " + ", ".join(d["variables"][:10]))
  if d.get("inputs"):
    lines.append("  inputs (set like parameters): " + ", ".join(f"{i['name']}: {i['type']}" for i in d["inputs"][:12]))
  if d["connectors"]:
    lines.append("  connectors: " + ", ".join(f"{c['name']}{'[...]' if c['dims'] else ''}: {c['type'].split('.')[-1]}"
                                               for c in d["connectors"]))
  for k, v in d["replaceable"].items():
    if v["must_redeclare"]:
      if k == "Medium" or "Media.Interfaces" in str(v["default"]):
        lines.append(f"  replaceable {k}: PARTIAL, must redeclare, e.g. redeclare package {k} = "
                     "Modelica.Media.Water.ConstantPropertyLiquidWater (or the model's own Medium)")
      elif v["options"]:
        lines.append(f"  replaceable {k}: PARTIAL, must redeclare, e.g. "
                     + ", ".join(o.split('.')[-1] for o in v["options"][:4]) + f" (from {v['default'].rsplit('.', 1)[0]})")
      else:
        lines.append(f"  replaceable {k}: PARTIAL, must redeclare (default {v['default']} is partial)")
    else:
      lines.append(f"  replaceable {k} (default {str(v['default']).split('.')[-1]})")
  return "\n".join(lines)


def similar(name: str, n: int = 4) -> List[str]:
  leaf = (name or "").split(".")[-1]
  rows = _rows("SELECT id FROM nodes WHERE id LIKE ? LIMIT 200", (f"%{leaf[:4]}%",)) if leaf else []
  ids = [r["id"] for r in rows]
  return difflib.get_close_matches(name, ids, n, 0.5) or [i for i in ids if i.endswith("." + leaf)][:n]


# -------------------------------------------------------------------------------- search
_BAD_PATH = re.compile(r"\.(Examples|BaseClasses|Internal|Interfaces|Icons|Types|Utilities|Obsolete)\.")


def search(query: str, limit: int = 8, kinds=("model", "block"), include_partial: bool = False) -> List[Dict[str, Any]]:
  words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9]+", query or "") if w.lower() not in STOP and len(w) > 1]
  terms: List[str] = []
  for w in words:
    terms += SYNONYMS.get(w.lower(), [w])
  terms = [t for t in dict.fromkeys(t.lower() for t in terms)]
  if not terms:
    return []
  fts = " OR ".join(" ".join(f'"{p}"*' for p in t.split()) if " " in t else f'"{t}"*' for t in terms)
  try:
    rows = _rows("SELECT s.id, n.kind, n.partial, n.description, bm25(search, 0.0, 10.0, 3.0, 0.5) AS score "
                 "FROM search s JOIN nodes n ON n.id = s.id WHERE search MATCH ? ORDER BY score LIMIT 300", (fts,))
  except sqlite3.Error:
    return []
  out = []
  for r in rows:
    if kinds and r["kind"] not in kinds:
      continue
    if r["partial"] and not include_partial:
      continue
    score = r["score"]  # bm25: lower is better
    if _BAD_PATH.search(r["id"]):
      score += 5
    leaf = r["id"].split(".")[-1].lower()
    score -= 4 * sum(t.replace(" ", "") in leaf for t in terms)
    score += 0.15 * len(leaf)  # IdealGear before IdealGearR2T
    out.append((score, {"id": r["id"], "kind": r["kind"], "description": r["description"]}))
  out.sort(key=lambda x: x[0])
  seen, res = set(), []
  for _, d in out:
    if d["id"] not in seen:
      seen.add(d["id"])
      res.append(d)
  return res[:limit]


def examples(names: List[str], limit: int = 3) -> List[Dict[str, Any]]:
  """MSL examples that use the most of the given classes."""
  ids = [i for i in (resolve(n) for n in names) if i]
  if not ids:
    return []
  q = ",".join("?" * len(ids))
  rows = _rows(f"SELECT src, COUNT(DISTINCT dst) AS hits FROM edges WHERE type='uses' AND dst IN ({q}) "
               f"GROUP BY src ORDER BY hits DESC, LENGTH(src) LIMIT ?", (*ids, limit))
  out = []
  for r in rows:
    n = node(r["src"])
    used = [x["dst"] for x in _rows("SELECT DISTINCT dst FROM edges WHERE type='uses' AND src=?", (r["src"],))]
    out.append({"id": r["src"], "description": n["description"] if n else "", "matches": r["hits"],
                "uses": [u.split(".")[-1] for u in used][:15]})
  return out


def example_source(cid: str) -> str:
  n = node(cid)
  return (n["source"] or "") if n else ""


# ----------------------------------------------------------------------- pre-compile check
_DECL = re.compile(r"^[ \t]*(?:(?:inner|outer|parameter|final|replaceable|constant|discrete|input|output)\s+)*"
                   r"(Modelica\.[A-Za-z_][\w.]*)\s+([A-Za-z_]\w*)\s*(\[[^\]]*\])?\s*(\()?", re.M)


def _match_paren(s: str, i: int) -> int:
  depth, j = 0, i
  while j < len(s):
    c = s[j]
    if c == '"':
      k = s.find('"', j + 1)
      j = k if k > 0 else len(s)
    elif c == "(":
      depth += 1
    elif c == ")":
      depth -= 1
      if depth == 0:
        return j
    j += 1
  return -1


def _split_top(s: str) -> List[str]:
  out, depth, start, j = [], 0, 0, 0
  while j < len(s):
    c = s[j]
    if c == '"':
      k = s.find('"', j + 1)
      j = k if k > 0 else len(s)
    elif c in "({[":
      depth += 1
    elif c in ")}]":
      depth -= 1
    elif c == "," and depth == 0:
      out.append(s[start:j])
      start = j + 1
    j += 1
  out.append(s[start:])
  return [x.strip() for x in out if x.strip()]


def declarations(code: str) -> Dict[str, Dict[str, Any]]:
  """Instances of MSL classes in generated code: name -> {type, modifiers}."""
  out = {}
  for m in _DECL.finditer(code or ""):
    typ, name = m.group(1), m.group(2)
    mods = ""
    if m.group(4):
      close = _match_paren(code, m.end(4) - 1)
      mods = code[m.end(4):close] if close > 0 else ""
    out[name] = {"type": typ, "modifiers": mods, "line": code.count("\n", 0, m.start()) + 1}
  return out


def _domain(cid: str) -> str:
  return ".".join(cid.split(".")[:3])


def _compatible(t1: str, t2: str) -> bool:
  """Only says False when two connectors clearly belong to different physical domains."""
  if not t1 or not t2 or t1 == t2 or _domain(t1) == _domain(t2):
    return True
  return bool(set(chain(t1)) & set(chain(t2)))


def precheck(code: str) -> List[str]:
  """Problems in generated code that OpenModelica would reject, found in milliseconds. Conservative: only flags
  what the index knows for certain (unknown class, unknown modifier, partial class or partial replaceable,
  unknown connector, connector from a different domain)."""
  if not available():
    return []
  issues: List[str] = []
  for m in UNITS_OLD.finditer(code or ""):
    issues.append("Modelica.SIunits does not exist in MSL 4; use Modelica.Units.SI (e.g. Modelica.Units.SI.Area).")
    break
  decls = declarations(code)
  for name, d in decls.items():
    typ = d["type"]
    if typ.startswith("Modelica.SIunits"):
      continue  # reported once above
    n = node(typ)
    if n is None:
      if re.match(r"^Modelica\.Media\.", typ) and typ.count(".") >= 4:
        continue  # medium internals are not indexed
      sim = similar(typ)
      issues.append(f"line {d['line']}: class {typ} does not exist in the MSL"
                    + (f"; did you mean {', '.join(sim[:3])}?" if sim else "."))
      continue
    if n["kind"] == "type":
      continue
    if n["partial"]:
      opts = options(typ, 5)
      issues.append(f"line {d['line']}: {typ} is partial and cannot be instantiated"
                    + (f"; use one of: {', '.join(o.split('.')[-1] for o in opts)}" if opts else "."))
      continue
    if not chain_complete(typ):
      continue  # inheritance not fully known: don't risk a false "has no parameter"
    els, reps = elements(typ, include_protected=True), replaceables(typ)
    redeclared_here = set()
    for item in _split_top(d["modifiers"]):
      item = re.sub(r"^(?:each|final)\s+", "", item)
      rd = re.match(r"redeclare\s+(?:replaceable\s+)?(?:package|function|model|block|record)\s+(\w+)", item)
      if rd:
        redeclared_here.add(rd.group(1))
        if rd.group(1) not in reps and rd.group(1) not in els:
          issues.append(f"line {d['line']}: {name}: {typ.split('.')[-1]} has no replaceable '{rd.group(1)}'"
                        + (f" (replaceable: {', '.join(reps)})" if reps else "."))
        continue
      key = re.match(r"([A-Za-z_]\w*)", item)
      if not key:
        continue
      k = key.group(1)
      val = re.match(r"[A-Za-z_]\w*\s*=\s*(Modelica\.[\w.]+)\.(\w+)\s*$", item)
      if k in els and val and node(val.group(1)) and node(val.group(1))["kind"] == "type":
        expected = els[k]["type_id"]
        if expected and node(expected) and node(expected)["kind"] == "type" and expected != val.group(1):
          issues.append(f"line {d['line']}: {name}.{k} expects a value of {expected} "
                        f"(e.g. {expected}.{val.group(2)}), not {val.group(1)}.{val.group(2)}.")
          continue
      if k not in els and k not in reps:
        close = difflib.get_close_matches(k, list(els) + list(reps), 3, 0.5)
        issues.append(f"line {d['line']}: {name}: {typ.split('.')[-1]} has no parameter or element '{k}'"
                      + (f"; did you mean {', '.join(close)}?" if close else "."))
    for rname, info in reps.items():
      if _partial_default(rname, info) and not info.get("redeclared_to") and rname not in redeclared_here:
        opts = options(info.get("default"), 4)
        hint = (f", e.g. redeclare package {rname} = Medium (the model's medium) or "
                "Modelica.Media.Water.ConstantPropertyLiquidWater" if "PartialMedium" in str(info.get("default", ""))
                else f", e.g. redeclare {info.get('kind', 'function')} {rname} = {opts[0]}(...)" if opts else ".")
        issues.append(f"line {d['line']}: {name}: '{rname}' is partial in {typ.split('.')[-1]} and must be redeclared" + hint)
  for a, b in re.findall(r"connect\s*\(\s*([\w.\[\]: ]+?)\s*,\s*([\w.\[\]: ]+?)\s*\)", code or ""):
    types = []
    for side in (a, b):
      parts = [re.sub(r"\[.*?\]", "", p) for p in side.split(".")]
      if (len(parts) < 2 or parts[0] not in decls or not node(decls[parts[0]]["type"])
          or not chain_complete(decls[parts[0]]["type"])):
        types.append(None)
        continue
      typ = decls[parts[0]]["type"]
      els = elements(typ, include_protected=False)
      if parts[1] not in els:
        conns = [k for k, v in els.items() if v["is_connector"]]
        issues.append(f"connect({a}, {b}): {typ.split('.')[-1]} has no connector '{parts[1]}'"
                      + (f"; its connectors are: {', '.join(conns)}" if conns else "."))
        types.append(None)
        continue
      types.append(els[parts[1]]["type_id"] if len(parts) == 2 and els[parts[1]]["is_connector"] else None)
    if types[0] and types[1] and not _compatible(types[0], types[1]):
      issues.append(f"connect({a}, {b}): {types[0].split('.')[-1]} ({_domain(types[0])}) cannot be connected to "
                    f"{types[1].split('.')[-1]} ({_domain(types[1])}).")
  return list(dict.fromkeys(issues))


def facts_for(code: str, issues: List[str], limit: int = 5) -> str:
  """The real interface of every MSL class involved in the pre-check problems (the designer picked these
  itself, so they were not in its context): parameters, connectors and replaceables, from the index."""
  decls = declarations(code)
  types: List[str] = []
  for issue in issues:
    for inst in re.findall(r"(?:line \d+: |connect\()([A-Za-z_]\w*)[:.]", issue):
      if inst in decls and node(decls[inst]["type"]):
        types.append(decls[inst]["type"])
    for m in re.findall(r"did you mean ([\w.]+)", issue):
      if node(m):
        types.append(m)
  types = list(dict.fromkeys(types))[:limit]
  return "\n".join(describe_text(t, max_params=12) for t in types)


# -------------------------------------------------------------------------- designer context
def context_for(request: str, base_code: str = "", max_chars: int = 6000) -> str:
  """Relevant MSL facts for the designer: the components the request is about (from the real library),
  the best matching MSL example, and the playbook rules of the domains involved."""
  if not available():
    return ""
  picked: List[str] = []
  for d in declarations(base_code).values():
    if node(d["type"]) and node(d["type"])["kind"] not in ("type",):
      picked.append(d["type"])
  words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9]+", request) if w.lower() not in STOP]
  noisy = re.compile(r"^Modelica\.(Media|Clocked|StateGraph|ComplexBlocks|ComplexMath|Math|Utilities)\b")
  asked_noisy = re.search(r"media|clocked|state ?graph|complex", request, re.I)
  candidates: List[Tuple[str, str]] = []  # (word, class id)
  for w in dict.fromkeys(words):
    names = [t.replace(" ", "") for t in SYNONYMS.get(w.lower(), [w.lower()])]
    stem = {n[:-1] if n.endswith("s") and len(n) > 4 else n for n in names}
    for hit in search(w, limit=6):
      cid = hit["id"]
      if ".Examples." in cid or (noisy.search(cid) and not asked_noisy):
        continue
      # the word must be a whole word of the class name ("Tank" in OpenTank, not "wind" in SinglePhaseWinding)
      tokens = {t.lower() for t in re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", cid.split(".")[-1])}
      if not (stem & tokens or {s + "s" for s in stem} & tokens):
        continue
      candidates.append((w, cid))
  # keep the request's dominant domain (e.g. Modelica.Fluid), so a "pipe" is a Fluid pipe in a fluid model
  dom = lambda c: ".".join(c.split(".")[:2])
  votes: Dict[str, int] = {}
  for _, c in candidates + [("", p) for p in picked]:
    votes[dom(c)] = votes.get(dom(c), 0) + 1
  main = max(votes, key=votes.get) if votes else ""
  per_word: Dict[str, str] = {}
  for w, c in candidates:
    if w not in per_word and (dom(c) == main or votes.get(dom(c), 0) == votes.get(main, 0)):
      per_word[w] = c
  picked += list(per_word.values())
  picked = list(dict.fromkeys(picked))[:8]
  if not picked:
    return ""
  parts = [f"RELEVANT MSL COMPONENTS (from the installed library, MSL {meta().get('msl_version', '?')}). "
           "Use exactly these class paths, parameter names and connector names:"]
  parts += [describe_text(c, max_params=10) for c in picked]
  ex = examples(picked, 1)
  if ex and ex[0]["matches"] >= 2:
    src = example_source(ex[0]["id"])
    parts.append(f"MSL EXAMPLE using {ex[0]['matches']} of these components: {ex[0]['id']} "
                 f"(uses {', '.join(ex[0]['uses'][:10])})" + (f"\n```modelica\n{src[:2500]}\n```" if src else ""))
  domains = {k for k in PLAYBOOKS if any(c.startswith(k) for c in picked)}
  if re.search(r"bounc|impact|collision|thermostat|on/off|on-off|switch|hysteresis", request, re.I):
    domains.add("events")
  if domains:
    parts.append("RULES:\n- " + "\n- ".join(PLAYBOOKS[d] for d in sorted(domains)))
  text = "\n\n".join(parts)
  return text[:max_chars]
