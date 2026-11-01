"""Write parameter values into Modelica source ("Write into model").

Handles the three shapes found in models:
  top-level      parameter SI.Mass m = 1 "Mass";        name "m"
  existing mod   Damper damper(d=d) annotation(...)      name "damper.d"
  missing mod    Force force annotation(...)             name "force.f_nominal" -> force(f_nominal=v)
Modifier lists are parsed with paren/string awareness, so nested modifiers such
as mass(m=m, s(start=0)) and annotations are left untouched. Formatting,
comments and annotations elsewhere are preserved.
"""
import re
from typing import Dict, List, Optional, Tuple

_VALUE = re.compile(r"^(?:[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?|true|false)$")
# Parameter expressions such as 2*k, R*C, sqrt(k/m): identifiers, numbers, + - * / ^, parentheses, commas.
_EXPR = re.compile(r"^[\w\s.+\-*/^(),]+$")


def _valid_value(v: str) -> bool:
  if _VALUE.match(v):
    return True
  if not _EXPR.match(v) or v.count("(") != v.count(")"):
    return False
  depth = 0
  for ch in v:
    depth += ch == "("
    depth -= ch == ")"
    if depth < 0:
      return False
  return True


def _match_paren(s: str, i: int) -> int:
  """Index of the ')' matching s[i] == '(' (skips strings)."""
  depth, j, n = 0, i, len(s)
  while j < n:
    c = s[j]
    if c == '"':
      j = s.index('"', j + 1) if '"' in s[j + 1:] else n
    elif c == "(":
      depth += 1
    elif c == ")":
      depth -= 1
      if depth == 0:
        return j
    j += 1
  return -1


def _split_top(args: str) -> List[Tuple[int, int]]:
  """(start, end) spans of comma-separated top-level items in a modifier list."""
  spans, depth, start, j = [], 0, 0, 0
  while j < len(args):
    c = args[j]
    if c == '"':
      k = args.find('"', j + 1)
      j = k if k >= 0 else len(args)
    elif c in "({":
      depth += 1
    elif c in ")}":
      depth -= 1
    elif c == "," and depth == 0:
      spans.append((start, j))
      start = j + 1
    j += 1
  spans.append((start, len(args)))
  return spans


def _declaration(code: str, comp: str) -> Optional[re.Match]:
  """Component declaration line: <Type> <comp>[(...)] ..."""
  return re.search(rf"^(\s*(?:(?:inner|outer|final|replaceable)\s+)*[A-Za-z_][\w.]*\s+){re.escape(comp)}\b(?!\s*\.)",
                   code, re.M)


def set_top_level(code: str, name: str, value: str) -> Optional[str]:
  m = re.search(rf"^(\s*(?:final\s+)?parameter\s+[A-Za-z_][\w.]*(?:\s*\[[^\]]*\])?\s+{re.escape(name)}\b"
                rf"\s*(?:\((?:[^()]|\([^()]*\))*\))?\s*=\s*)([^;\"\s]+)", code, re.M)
  if not m:
    return None
  return code[:m.start(2)] + value + code[m.end(2):]  # value may be an expression (2*k)


def set_component_modifier(code: str, comp: str, par: str, value: str) -> Optional[str]:
  m = _declaration(code, comp)
  if not m:
    return None
  after = m.end()  # just after the component name
  rest = code[after:]
  ws = len(rest) - len(rest.lstrip(" \t"))
  if rest[ws:ws + 1] == "(":
    open_i = after + ws
    close_i = _match_paren(code, open_i)
    if close_i < 0:
      return None
    args = code[open_i + 1:close_i]
    for a, b in _split_top(args):
      item = args[a:b]
      # par = old    or    par(start=0) = old    -> replace the value
      mm = re.match(rf"^(\s*(?:final\s+|each\s+)*{re.escape(par)}\s*(?:\(.*\))?\s*=\s*)(.*?)(\s*)$", item, re.S)
      if mm:
        new_item = mm.group(1) + value + mm.group(3)
        return code[:open_i + 1] + args[:a] + new_item + args[b:] + code[close_i:]
      # par(start=0) without a value      -> par(start=0)=value (no duplicate modification)
      mm = re.match(rf"^(\s*(?:final\s+|each\s+)*{re.escape(par)}\s*\(.*\))(\s*)$", item, re.S)
      if mm:
        new_item = mm.group(1) + "=" + value + mm.group(2)
        return code[:open_i + 1] + args[:a] + new_item + args[b:] + code[close_i:]
    sep = ", " if args.strip() else ""
    return code[:close_i] + f"{sep}{par}={value}" + code[close_i:]
  # no modifier list yet: Type comp annotation(...)  ->  Type comp(par=value) annotation(...)
  return code[:after] + f"({par}={value})" + code[after:]


def bake(code: str, values: Dict[str, str]) -> Tuple[str, List[str], List[str]]:
  """Returns (new_code, applied_names, skipped_names)."""
  applied, skipped = [], []
  for name, value in values.items():
    value = str(value).strip()
    if not _valid_value(value):
      skipped.append(name)
      continue
    parts = name.split(".")
    new = None
    if len(parts) == 1:
      new = set_top_level(code, name, value)
    elif len(parts) == 2:
      new = set_component_modifier(code, parts[0], parts[1], value)
    if new is None:
      skipped.append(name)
    else:
      code = new
      applied.append(name)
  return code, applied, skipped
