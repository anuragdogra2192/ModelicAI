"""Builds the MSL knowledge graph (data/msl_index.db) from the Modelica Standard Library installed in OpenModelica.

Nothing here is written by hand or by an LLM: every class, parameter, connector, inheritance link and
replaceable comes from OMC's API (or the class's own source text, via list()).

Run inside the Studio container (takes a few minutes; Studio also starts it automatically if the index is missing):
    python3 -m tools.build_msl_index            # writes data/msl_index.db
    python3 -m tools.build_msl_index --limit Modelica.Fluid   # only one package (quick test)

Graph stored in SQLite:
  nodes(id, kind, partial, description, unit, docs, source)                   one row per class
  components(cls, name, type, type_id, variability, causality, protected, ..)  declared elements of a class
  edges(src, type, dst, name, info)    type: extends | replaceable | redeclare | uses
  search (FTS5 over id, leaf name, description, docs)
  meta(key, value)                     msl_version, built_at, counts
"""
import argparse
import html
import json
import os
import re
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional

INDEX_PATH = os.getenv("MSL_INDEX", os.path.join("data", "msl_index.db"))

# Parts of the MSL that only add noise for model building.
SKIP = re.compile(r"^Modelica\.(Icons|UsersGuide|ComplexMath\.Internal)|\.UsersGuide\b|\.Internal\b|\.Obsolete\b")
# Modelica.Media is huge: keep the medium packages themselves (e.g. Modelica.Media.Water.ConstantPropertyLiquidWater),
# skip their thousands of member functions/records.
MEDIA_MAX_DEPTH = 4
SOURCE_KINDS = {"model", "block", "connector", "expandable connector", "record", "function"}
DOC_CHARS = 1500
EXAMPLE_SOURCE_CHARS = 6000


def _strip_html(s: str) -> str:
  s = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", s or "")
  s = re.sub(r"(?s)<[^>]+>", " ", s)
  return re.sub(r"\s+", " ", html.unescape(s)).strip()


def _keep(cls: str) -> bool:
  if SKIP.search(cls):
    return False
  if cls.startswith("Modelica.Media.") and cls.count(".") >= MEDIA_MAX_DEPTH:
    return False
  return True


class Builder:
  def __init__(self, omc):
    self.omc = omc
    self.calls = 0

  def q(self, expr: str, parsed: bool = True) -> Any:
    self.calls += 1
    try:
      return self.omc.sendExpression(expr, parsed=parsed)
    except Exception:
      return None

  # ------------------------------------------------------------------ raw extraction
  def classes(self, root: str) -> List[str]:
    names = self.q(f"getClassNames({root}, recursive=true, qualified=true, sort=true)") or ()
    return [root] + [str(n) for n in names if str(n) != root and _keep(str(n))]

  def components(self, cls: str) -> List[Dict[str, Any]]:
    from utils.omc_graphics import parse
    raw = self.q(f"getComponents({cls})", parsed=False)
    items = parse(str(raw)) if raw else None
    out = []
    for c in items if isinstance(items, list) else []:
      if not isinstance(c, list) or len(c) < 2:
        continue
      vals = [str(v) for v in c]
      out.append({
          "type": vals[0], "name": vals[1],
          "comment": vals[2] if len(vals) > 2 else "",
          "protected": int("protected" in vals[3:5]),
          "variability": next((v for v in vals[2:] if v in ("parameter", "constant", "discrete", "unspecified")), ""),
          "causality": next((v for v in vals[2:] if v in ("input", "output")), ""),
          # array sizes can be expressions such as size(A, 1): store them as text
          "dims": next((json.dumps(v, default=str) for v in c[2:] if isinstance(v, list) and v), ""),
      })
    return out

  def imports(self, cls: str) -> Dict[str, str]:
    """alias -> full path for `import A = B;` / `import B;`, and '*' -> [packages] for `import B.*;`."""
    out: Dict[str, Any] = {"*": []}
    n = self.q(f"getImportCount({cls})") or 0
    for i in range(1, int(n) + 1 if isinstance(n, (int, float)) else 1):
      imp = self.q(f"getNthImport({cls}, {i})")
      if not isinstance(imp, (list, tuple)) or len(imp) < 3:
        continue
      path, alias, kind = str(imp[0]), str(imp[1] or ""), str(imp[2])
      if kind == "unqualified":
        out["*"].append(path)
      else:
        out[alias or path.split(".")[-1]] = path
    return out

  def extract(self, cls: str) -> Dict[str, Any]:
    kind = str(self.q(f"getClassRestriction({cls})") or "")
    rec: Dict[str, Any] = {
        "id": cls, "kind": kind, "partial": int(bool(self.q(f"isPartial({cls})"))),
        "description": str(self.q(f"getClassComment({cls})") or ""), "unit": "", "docs": "", "source": "",
        "extends": [str(x) for x in (self.q(f"getInheritedClasses({cls})") or ())],
        "components": [], "imports": self.imports(cls) if kind == "package" or kind in SOURCE_KINDS else {"*": []},
        "replaceable": [], "redeclare": [],
    }
    if kind == "type":
      unit = self.q(f'getDerivedClassModifierValue({cls}, unit)')
      rec["unit"] = str(unit or "").strip().strip('="').strip()
      return rec
    if kind == "package":
      return rec
    rec["components"] = self.components(cls)
    doc = self.q(f"getDocumentationAnnotation({cls})")
    if isinstance(doc, (list, tuple)) and doc:
      rec["docs"] = _strip_html(str(doc[0]))[:DOC_CHARS]
    if kind in SOURCE_KINDS:
      src = str(self.q(f"list({cls})") or "")
      top = _top_level(src)
      for m in re.finditer(r"\breplaceable\s+(package|function|model|block|record)\s+(\w+)\s*=\s*([\w.]+)", top):
        rec["replaceable"].append({"kind": m.group(1), "name": m.group(2), "default": m.group(3)})
      for m in re.finditer(r"\bredeclare\s+(?:replaceable\s+)?(package|function|model|block|record)\s+(\w+)\s*=\s*([\w.]+)", top):
        rec["redeclare"].append({"kind": m.group(1), "name": m.group(2), "target": m.group(3)})
      if ".Examples." in cls and kind == "model":
        rec["source"] = src[:EXAMPLE_SOURCE_CHARS]
    return rec


def _top_level(src: str) -> str:
  """The class body without its nested class definitions (so a nested class's replaceables aren't attributed
  to the outer class). Approximation: drop text between a nested `<kind> X` header and its `end X;`."""
  lines, out, skip = src.splitlines(), [], None
  for i, line in enumerate(lines):
    if i == 0:
      out.append(line)
      continue
    if skip:
      if re.match(rf"\s*end\s+{re.escape(skip)}\s*;", line):
        skip = None
      continue
    m = re.match(r"\s*(?:encapsulated\s+|partial\s+|replaceable\s+|redeclare\s+|final\s+)*"
                 r"(?:model|block|record|function|package|connector|type)\s+(\w+)\b(?!\s*=)", line)
    if m and not re.search(rf"\bend\s+{re.escape(m.group(1))}\s*;", line):
      skip = m.group(1)
      continue
    out.append(line)
  return "\n".join(out)


# ---------------------------------------------------------------------- name resolution
class Resolver:
  """Resolves a type name as written in a class (relative, imported or qualified) to an indexed class id,
  following Modelica's lookup: the class itself and its enclosing scopes, their imports, then the top level."""

  def __init__(self, records: Dict[str, Dict[str, Any]]):
    self.ids = set(records)
    self.recs = records

  def scopes(self, cls: str) -> List[str]:
    parts = cls.split(".")
    return [".".join(parts[:i]) for i in range(len(parts), 0, -1)]

  def resolve(self, name: str, cls: str) -> Optional[str]:
    if not name or name in ("Real", "Integer", "Boolean", "String"):
      return None
    if name in self.ids:
      return name
    first, _, rest = name.partition(".")
    for scope in self.scopes(cls):
      cand = f"{scope}.{name}"
      if cand in self.ids:
        return cand
      imps = (self.recs.get(scope) or {}).get("imports") or {}
      if first in imps:
        cand = imps[first] + (f".{rest}" if rest else "")
        if cand in self.ids:
          return cand
      for pkg in imps.get("*", []):
        cand = f"{pkg}.{name}"
        if cand in self.ids:
          return cand
    return None


# ----------------------------------------------------------------------------- writing
SCHEMA = """
DROP TABLE IF EXISTS nodes; DROP TABLE IF EXISTS components; DROP TABLE IF EXISTS edges;
DROP TABLE IF EXISTS meta; DROP TABLE IF EXISTS search;
CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT, partial INTEGER, description TEXT, unit TEXT,
                    docs TEXT, source TEXT);
CREATE TABLE components (cls TEXT, name TEXT, type TEXT, type_id TEXT, variability TEXT, causality TEXT,
                         protected INTEGER, is_connector INTEGER, dims TEXT, comment TEXT);
CREATE TABLE edges (src TEXT, type TEXT, dst TEXT, name TEXT, info TEXT);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX c_cls ON components(cls);
CREATE INDEX e_src ON edges(src, type);
CREATE INDEX e_dst ON edges(dst, type);
CREATE VIRTUAL TABLE search USING fts5(id, leaf, description, docs, tokenize='porter unicode61');
"""


def write(records: Dict[str, Dict[str, Any]], path: str, version: str) -> Dict[str, int]:
  r = Resolver(records)
  connector_ids = {i for i, rec in records.items() if "connector" in rec["kind"]}
  tmp = path + ".building"
  if os.path.exists(tmp):
    os.remove(tmp)
  os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
  db = sqlite3.connect(tmp)
  db.executescript(SCHEMA)
  n_edges = n_comp = 0
  for cid, rec in records.items():
    db.execute("INSERT INTO nodes VALUES (?,?,?,?,?,?,?)",
               (cid, rec["kind"], rec["partial"], rec["description"], rec["unit"], rec["docs"], rec["source"]))
    db.execute("INSERT INTO search VALUES (?,?,?,?)",
               (cid, re.sub(r"([a-z])([A-Z])", r"\1 \2", cid.split(".")[-1]), rec["description"], rec["docs"][:600]))
    for base in rec["extends"]:
      db.execute("INSERT INTO edges VALUES (?,?,?,?,?)", (cid, "extends", r.resolve(base, cid) or base, "", ""))
      n_edges += 1
    for c in rec["components"]:
      tid = r.resolve(c["type"], cid)
      db.execute("INSERT INTO components VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (cid, c["name"], c["type"], tid, c["variability"], c["causality"], c["protected"],
                  int(tid in connector_ids), c["dims"], c["comment"]))
      n_comp += 1
      if ".Examples." in cid and tid and tid.startswith("Modelica.") and ".Examples." not in tid:
        db.execute("INSERT INTO edges VALUES (?,?,?,?,?)", (cid, "uses", tid, c["name"], ""))
        n_edges += 1
    for rp in rec["replaceable"]:
      d = r.resolve(rp["default"], cid)
      info = {"kind": rp["kind"], "default": d or rp["default"],
              "default_partial": bool(d and records.get(d, {}).get("partial"))}
      db.execute("INSERT INTO edges VALUES (?,?,?,?,?)", (cid, "replaceable", d or rp["default"], rp["name"], json.dumps(info)))
      n_edges += 1
    for rd in rec["redeclare"]:
      t = r.resolve(rd["target"], cid)
      db.execute("INSERT INTO edges VALUES (?,?,?,?,?)", (cid, "redeclare", t or rd["target"], rd["name"], json.dumps(rd)))
      n_edges += 1
  meta = {"msl_version": version, "built_at": time.strftime("%Y-%m-%d %H:%M:%S"), "classes": str(len(records)),
          "components": str(n_comp), "edges": str(n_edges), "schema": "1"}
  db.executemany("INSERT INTO meta VALUES (?,?)", meta.items())
  db.commit()
  db.close()
  os.replace(tmp, path)  # atomic: a half-built index is never used
  return {"classes": len(records), "components": n_comp, "edges": n_edges}


def build(omc, path: str = INDEX_PATH, root: str = "Modelica", log=print) -> Dict[str, Any]:
  b = Builder(omc)
  t0 = time.time()
  if not omc.sendExpression("loadModel(Modelica)"):
    raise RuntimeError("OpenModelica could not load the Modelica Standard Library")
  version = str(omc.sendExpression("getVersion(Modelica)") or "?")
  todo = b.classes(root) if root == "Modelica" else ["Modelica"] + [c for c in b.classes("Modelica") if c.startswith(root)]
  # packages enclosing the classes are needed for import resolution even when only one package is built
  log(f"MSL index: {len(todo)} classes in {root} (MSL {version})")
  records: Dict[str, Dict[str, Any]] = {}
  for i, cls in enumerate(todo, 1):
    try:
      records[cls] = b.extract(cls)
    except Exception as e:  # one odd class must not stop the build
      log(f"  skipped {cls}: {e}")
    if i % 250 == 0:
      log(f"  {i}/{len(todo)} classes, {time.time() - t0:.0f}s")
  counts = write(records, path, version)
  counts.update(seconds=round(time.time() - t0), omc_calls=b.calls, msl_version=version, path=path)
  log(f"MSL index ready: {counts}")
  return counts


def main(argv=None):
  ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
  ap.add_argument("--out", default=INDEX_PATH)
  ap.add_argument("--limit", default="Modelica", help="only index this package (e.g. Modelica.Fluid)")
  a = ap.parse_args(argv)
  sys.path.insert(0, os.path.abspath("."))
  from OMPython import OMCSessionLocal
  build(OMCSessionLocal(), a.out, a.limit)


if __name__ == "__main__":
  main()
