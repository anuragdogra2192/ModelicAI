"""Build a diagram scene from OpenModelica's graphical API (what OMEdit uses).

  getComponents / getComponentAnnotations -> instances and Placement
  getNthConnection / getNthConnectionAnnotation -> wires and their routes
  getIconAnnotation (+ getInheritedClasses)  -> Icon(graphics={...}) primitives

Icons are rendered to SVG fragments in Modelica coordinates (y up). The
frontend flips y once at the root, so every transform here stays in Modelica
convention. Text keeps %name / %param placeholders in data-t for the client to
substitute per instance.
"""
import math
import re
from html import escape
from typing import Any, Dict, List, Optional, Tuple

BUILTIN = {"Real", "Integer", "Boolean", "String"}
Matrix = Tuple[float, float, float, float, float, float]  # SVG a b c d e f


# ------------------------------------------------------------------ parser
class _Call:
  def __init__(self, name: str, args: list):
    self.name, self.args = name, args

  def __repr__(self):
    return f"{self.name}{self.args}"


class _Dyn:
  """DynamicSelect(static, dynamic): static value plus the Modelica expression evaluated during playback."""
  def __init__(self, static, expr: str):
    self.static, self.expr = static, expr

  def __repr__(self):
    return f"Dyn({self.static!r}, {self.expr!r})"


def _split_top_args(s: str):
  """Split 'a, b' at the top-level comma (ignores commas inside (), {}, [] and strings)."""
  depth, i, start, out = 0, 0, 0, []
  while i < len(s):
    c = s[i]
    if c == '"':
      j = i + 1
      while j < len(s) and s[j] != '"':
        j += 2 if s[j] == "\\" else 1
      i = j
    elif c in "({[":
      depth += 1
    elif c in ")}]":
      depth -= 1
    elif c == "," and depth == 0:
      out.append(s[start:i])
      start = i + 1
    i += 1
  out.append(s[start:])
  return out


def _extract_dynamic(text: str):
  """Replace DynamicSelect(a, b) with __DYN("k", a) and remember b's source text."""
  exprs, out, i = [], [], 0
  key = "DynamicSelect("
  while True:
    j = text.find(key, i)
    if j < 0:
      out.append(text[i:])
      break
    out.append(text[i:j])
    k, depth = j + len(key) - 1, 0
    while k < len(text):  # find the matching ')'
      if text[k] == '"':
        k += 1
        while k < len(text) and text[k] != '"':
          k += 2 if text[k] == "\\" else 1
      elif text[k] == "(":
        depth += 1
      elif text[k] == ")":
        depth -= 1
        if depth == 0:
          break
      k += 1
    inner = text[j + len(key):k]
    parts = _split_top_args(inner)
    static = parts[0]
    exprs.append(parts[1].strip() if len(parts) > 1 else "")
    out.append(f'__DYN("{len(exprs) - 1}", {static})')
    i = k + 1
  return "".join(out), exprs


_TOK = re.compile(r'\s*(?:(?P<str>"(?:[^"\\]|\\.)*")|(?P<num>[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)'
                  r'|(?P<id>[A-Za-z_][\w.]*)|(?P<sym>[{}(),=\-]))')


def parse(text: str) -> Any:
  """Parses OMC's unparsed output: {lists}, Calls(...), numbers, strings, idents, '-'."""
  toks, pos = [], 0
  text, dyn_exprs = _extract_dynamic(text.strip())
  while pos < len(text):
    m = _TOK.match(text, pos)
    if not m or m.end() == pos:
      pos += 1
      continue
    pos = m.end()
    for kind in ("str", "num", "id", "sym"):
      if m.group(kind) is not None:
        toks.append((kind, m.group(kind)))
        break
  i = 0

  def value():
    nonlocal i
    if i >= len(toks):
      return None
    kind, v = toks[i]
    i += 1
    if kind == "str":
      return bytes(v[1:-1], "utf-8").decode("unicode_escape", "ignore")
    if kind == "num":
      return float(v)
    if kind == "sym" and v == "{":
      return seq("}")
    if kind == "sym" and v == "-":
      return None  # OMC's "unset" marker inside Placement(...)
    if kind == "id":
      if v == "true":
        return True
      if v == "false":
        return False
      if i < len(toks) and toks[i] == ("sym", "("):
        i += 1
        args = seq(")")
        if v == "__DYN" and len(args) >= 2:
          return _Dyn(args[1], dyn_exprs[int(args[0])])
        if v == "DynamicSelect" and args:
          return args[0]
        return _Call(v, args)
      return v
    return None

  def seq(close):
    nonlocal i
    out = []
    while i < len(toks) and toks[i] != ("sym", close):
      if toks[i] == ("sym", ","):
        i += 1
        continue
      # named argument  name = value
      if toks[i][0] == "id" and i + 1 < len(toks) and toks[i + 1] == ("sym", "="):
        i += 2
      out.append(value())
    i += 1
    return out

  return value()


# --------------------------------------------------------------- matrices
def mul(m: Matrix, n: Matrix) -> Matrix:
  a, b, c, d, e, f = m
  A, B, C, D, E, F = n
  return (a * A + c * B, b * A + d * B, a * C + c * D, b * C + d * D,
          a * E + c * F + e, b * E + d * F + f)


def apply(m: Matrix, x: float, y: float) -> Tuple[float, float]:
  a, b, c, d, e, f = m
  return a * x + c * y + e, b * x + d * y + f


def placement_matrix(origin, extent, rotation, coord) -> Matrix:
  """Maps a class's icon coordinates into its parent's coordinates."""
  (x1, y1), (x2, y2) = extent
  cx1, cy1, cx2, cy2 = coord
  sx = (x2 - x1) / ((cx2 - cx1) or 1)
  sy = (y2 - y1) / ((cy2 - cy1) or 1)
  ccx, ccy = (cx1 + cx2) / 2, (cy1 + cy2) / 2
  ex, ey = (x1 + x2) / 2, (y1 + y2) / 2
  r = math.radians(rotation or 0)
  cos, sin = math.cos(r), math.sin(r)
  m: Matrix = (1, 0, 0, 1, origin[0], origin[1])
  m = mul(m, (cos, sin, -sin, cos, 0, 0))
  m = mul(m, (1, 0, 0, 1, ex, ey))
  m = mul(m, (sx, 0, 0, sy, 0, 0))
  return mul(m, (1, 0, 0, 1, -ccx, -ccy))


def parse_placement(call: Optional[_Call]):
  """Placement(visible, ox, oy, x1, y1, x2, y2, rot, [icon: ox, oy, x1, y1, x2, y2, rot])."""
  if not isinstance(call, _Call) or len(call.args) < 8:
    return None, None
  a = call.args

  def block(k):
    if len(a) < k + 7 or a[k + 2] is None:
      return None
    return {"origin": (a[k] or 0.0, a[k + 1] or 0.0),
            "extent": ((a[k + 2], a[k + 3]), (a[k + 4], a[k + 5])),
            "rotation": a[k + 6] or 0.0}

  return block(1), block(8)


# ---------------------------------------------------------------- renderer
def _rgb(c, default="0,0,0"):
  if isinstance(c, list) and len(c) >= 3 and all(isinstance(x, float) for x in c[:3]):
    return f"rgb({int(c[0])},{int(c[1])},{int(c[2])})"
  return f"rgb({default})"


def _stroke(color, pattern, thickness):
  if isinstance(pattern, str) and pattern.endswith("None"):
    return 'stroke="none"'
  dash = {"Dash": "6 3", "Dot": "1.5 3", "DashDot": "6 3 1.5 3", "DashDotDot": "6 3 1.5 3 1.5 3"}
  pat = (pattern or "").split(".")[-1]
  w = max(1.0, (thickness or 0.25) * 4)
  s = f'stroke="{_rgb(color)}" stroke-width="{w:.2f}" vector-effect="non-scaling-stroke" stroke-linejoin="round" stroke-linecap="round"'
  if pat in dash:
    s += f' stroke-dasharray="{dash[pat]}"'
  return s


def _fill(fill_color, fill_pattern):
  pat = (fill_pattern or "FillPattern.None").split(".")[-1]
  return 'fill="none"' if pat == "None" else f'fill="{_rgb(fill_color, "255,255,255")}"'


def _pts(points):
  return [(p[0], p[1]) for p in points or [] if isinstance(p, list) and len(p) >= 2]


def _path(points, smooth, closed):
  pts = _pts(points)
  if len(pts) < 2:
    return ""
  if isinstance(smooth, str) and smooth.endswith("Bezier") and len(pts) > 2:
    # OMEdit-style: quadratic curves through segment midpoints.
    if closed:
      pts = pts + [pts[0]]
    mid = [((pts[k][0] + pts[k + 1][0]) / 2, (pts[k][1] + pts[k + 1][1]) / 2) for k in range(len(pts) - 1)]
    d = f"M{pts[0][0]:g} {pts[0][1]:g} L{mid[0][0]:g} {mid[0][1]:g}"
    for k in range(1, len(mid)):
      d += f" Q{pts[k][0]:g} {pts[k][1]:g} {mid[k][0]:g} {mid[k][1]:g}"
    d += f" L{pts[-1][0]:g} {pts[-1][1]:g}"
  else:
    d = "M" + " L".join(f"{x:g} {y:g}" for x, y in pts)
  return d + (" Z" if closed else "")


def _wrap(origin, rotation, body):
  ox, oy = (origin + [0, 0])[:2] if isinstance(origin, list) else (0, 0)
  if not ox and not oy and not rotation:
    return body
  return f'<g transform="translate({ox:g} {oy:g}) rotate({rotation or 0:g})">{body}</g>'


def _dyn_attrs(dyn: Dict[int, str], fields: Dict[int, str]) -> str:
  """data-dyn-<field>="<Modelica expression>" for animated fields (evaluated in the browser)."""
  return "".join(f' data-dyn-{name}="{escape(dyn[i], quote=True)}"' for i, name in fields.items() if dyn.get(i))


def render_primitive(p: Any) -> str:
  if not isinstance(p, _Call):
    return ""
  dyn = {i: v.expr for i, v in enumerate(p.args) if isinstance(v, _Dyn)}
  args = [v.static if isinstance(v, _Dyn) else v for v in p.args]
  p = _Call(p.name, args)
  a = args + [None] * 16
  hidden = ""
  if a[0] is False:
    if not dyn.get(0):
      return ""
    hidden = ' style="display:none"'  # becomes visible when its DynamicSelect says so
  try:
    if p.name == "Line":
      # visible, origin, rotation, points, color, pattern, thickness, arrow, arrowSize, smooth
      d = _path(a[3], a[9], False)
      extra = _dyn_attrs(dyn, {0: "visible", 3: "points", 4: "stroke"}) + hidden
      return _wrap(a[1], a[2], f'<path d="{d}" fill="none" {_stroke(a[4], a[5], a[6])}{extra}/>') if d else ""
    if p.name == "Polygon":
      # visible, origin, rotation, lineColor, fillColor, pattern, fillPattern, thickness, points, smooth
      d = _path(a[8], a[9], True)
      extra = _dyn_attrs(dyn, {0: "visible", 3: "stroke", 4: "fill", 8: "points"}) + ' data-closed="1"' + hidden
      return _wrap(a[1], a[2], f'<path d="{d}" {_fill(a[4], a[6])} {_stroke(a[3], a[5], a[7])}{extra}/>') if d else ""
    if p.name == "Rectangle":
      # ..., thickness, borderPattern, extent, radius
      (x1, y1), (x2, y2) = _pts(a[9])[:2]
      r = a[10] or 0
      body = (f'<rect x="{min(x1, x2):g}" y="{min(y1, y2):g}" width="{abs(x2 - x1):g}" height="{abs(y2 - y1):g}" '
              f'rx="{r:g}" {_fill(a[4], a[6])} {_stroke(a[3], a[5], a[7])}'
              f'{_dyn_attrs(dyn, {0: "visible", 3: "stroke", 4: "fill", 9: "extent"})}{hidden}/>')
      return _wrap(a[1], a[2], body)
    if p.name == "Ellipse":
      # ..., thickness, extent, startAngle, endAngle
      (x1, y1), (x2, y2) = _pts(a[8])[:2]
      cx, cy, rx, ry = (x1 + x2) / 2, (y1 + y2) / 2, abs(x2 - x1) / 2, abs(y2 - y1) / 2
      s, e = (a[9] if isinstance(a[9], float) else 0), (a[10] if isinstance(a[10], float) else 360)
      if abs((e - s) % 360) < 1e-6 or e - s >= 360:
        body = (f'<ellipse cx="{cx:g}" cy="{cy:g}" rx="{rx:g}" ry="{ry:g}" {_fill(a[4], a[6])} {_stroke(a[3], a[5], a[7])}'
                f'{_dyn_attrs(dyn, {0: "visible", 3: "stroke", 4: "fill", 8: "extent"})}{hidden}/>')
      else:
        sx, sy = cx + rx * math.cos(math.radians(s)), cy + ry * math.sin(math.radians(s))
        ex, ey = cx + rx * math.cos(math.radians(e)), cy + ry * math.sin(math.radians(e))
        large = 1 if (e - s) % 360 > 180 else 0
        body = (f'<path d="M{sx:g} {sy:g} A{rx:g} {ry:g} 0 {large} 1 {ex:g} {ey:g}" '
                f'{_fill(a[4], a[6])} {_stroke(a[3], a[5], a[7])}/>')
      return _wrap(a[1], a[2], body)
    if p.name == "Text":
      # ..., thickness, extent, textString, fontSize, [textColor], fontName, textStyle, hAlign
      pts = _pts(a[8])
      if len(pts) < 2:
        return ""
      (x1, y1), (x2, y2) = pts[:2]
      txt_i = next((i for i, v in enumerate(p.args) if i >= 9 and isinstance(v, str)
                    and not v.startswith(("TextStyle", "TextAlignment"))), None)
      txt = p.args[txt_i] if txt_i is not None else ""
      color = next((v for v in p.args[10:] if isinstance(v, list) and len(v) == 3), a[3])
      h, w = abs(y2 - y1), abs(x2 - x1)
      fs = a[10] if isinstance(a[10], float) and a[10] > 0 else min(h * 0.8, w / max(len(txt), 1) * 1.7)
      cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
      # Flip locally so glyphs read upright inside the y-up scene.
      body = (f'<text data-t="{escape(txt)}" transform="translate({cx:g} {cy:g}) scale(1 -1)" '
              f'font-size="{fs:g}" text-anchor="middle" dominant-baseline="central" '
              f'fill="{_rgb(color, "0,0,255")}" font-family="Arial, sans-serif"'
              f'{_dyn_attrs(dyn, {0: "visible", txt_i if txt_i is not None else -1: "text"})}{hidden}>{escape(txt)}</text>')
      return _wrap(a[1], a[2], body)
  except (TypeError, ValueError, IndexError):
    return ""
  return ""


# --------------------------------------------------------- OMC scene builder
class DiagramBuilder:
  def __init__(self, omc):
    self.omc = omc
    self._icons: Dict[str, Tuple[tuple, str]] = {}
    self._ports: Dict[str, Dict[str, Matrix]] = {}
    self._port_types: Dict[str, Dict[str, str]] = {}

  def raw(self, expr: str) -> str:
    try:
      return str(self.omc.sendExpression(expr, parsed=False) or "")
    except Exception:
      return ""

  def q(self, expr: str) -> Any:
    return parse(self.raw(expr))

  def exists(self, cls: str) -> bool:
    return self.raw(f"isType({cls})").strip() != "true" and self.raw(f"existClass({cls})").strip() == "true"

  def resolve(self, name: str, context: str, imports: Dict[str, str] = None) -> Optional[str]:
    """Turns a possibly relative / imported type name into a fully qualified one."""
    if name in BUILTIN:
      return None
    head, _, rest = name.partition(".")
    cands = []
    if imports:
      if head in imports:
        cands.append(imports[head] + ("." + rest if rest else ""))
      cands += [f"{pkg}.{name}" for pkg in imports.get("*", [])]
    parts = context.split(".")
    cands += [".".join(parts[:k] + [name]) for k in range(len(parts), 0, -1)]
    cands.append(name)
    for c in cands:
      if self.raw(f"existClass({c})").strip() == "true":
        return c
    return None

  # -- class level -------------------------------------------------------
  def class_components(self, cls: str):
    comps = self.q(f"getComponents({cls})") or []
    anns = self.q(f"getComponentAnnotations({cls})") or []
    out = []
    for idx, c in enumerate(comps):
      if not isinstance(c, list) or len(c) < 2:
        continue
      ann = anns[idx] if idx < len(anns) and isinstance(anns[idx], list) else []
      placement = next((x for x in ann if isinstance(x, _Call) and x.name == "Placement"), None)
      out.append({"type": c[0], "name": c[1], "comment": c[2] if len(c) > 2 else "",
                  "placement": placement})
    return out

  def lineage(self, cls: str, depth=0) -> List[str]:
    """cls plus all inherited classes, base classes first."""
    if depth > 8:
      return [cls]
    out = []
    for base in self.q(f"getInheritedClasses({cls})") or []:
      full = self.resolve(str(base), cls)
      if full:
        out += self.lineage(full, depth + 1)
    return out + [cls]

  def icon(self, cls: str, depth=0) -> Tuple[tuple, str]:
    """Returns (coordinate extent, svg fragment) for a class icon incl. connectors."""
    if cls in self._icons:
      return self._icons[cls]
    coord, parts = (-100.0, -100.0, 100.0, 100.0), []
    for c in self.lineage(cls):
      ann = self.q(f"getIconAnnotation({c})")
      if isinstance(ann, list) and len(ann) >= 4:
        if all(isinstance(v, float) for v in ann[:4]) and c == cls:
          coord = tuple(ann[:4])
        graphics = ann[-1] if isinstance(ann[-1], list) else []
        parts += [render_primitive(g) for g in graphics]
    # Connector icons drawn at their iconTransformation (e.g. MSL blue pin squares).
    ports = {}
    if depth < 2:
      for base in self.lineage(cls):
        for comp in self.class_components(base):
          t = self.resolve(comp["type"], base)
          if not t or self.raw(f"isConnector({t})").strip() != "true":
            continue
          diag, ico = parse_placement(comp["placement"])
          pl = ico or diag
          if not pl:
            continue
          sub_coord, sub_svg = self.icon(t, depth + 1)
          m = placement_matrix(pl["origin"], pl["extent"], pl["rotation"], sub_coord)
          ports[comp["name"]] = m
          self._port_types.setdefault(cls, {})[comp["name"]] = t
          parts.append(f'<g transform="matrix({" ".join(f"{v:.4g}" for v in m)})">{sub_svg}</g>')
    self._ports[cls] = ports
    self._icons[cls] = (coord, "".join(parts))
    return self._icons[cls]

  # -- model level -------------------------------------------------------
  def scene(self, model: str, source: str = "") -> Dict[str, Any]:
    imports = parse_imports(source)
    comps, missing, out_dirs = [], 0, {}
    for comp in self.class_components(model):
      if comp["type"] in BUILTIN:
        continue
      t = self.resolve(comp["type"], model, imports)
      if not t or self.raw(f"isType({t})").strip() == "true":
        continue  # plain variables / SI types are not blocks
      coord, svg = self.icon(t)
      diag, _ = parse_placement(comp["placement"])
      if not diag:
        missing += 1
        diag = {"origin": (0.0, 0.0), "rotation": 0.0,
                "extent": ((-10.0 + 40 * missing, -10.0), (10.0 + 40 * missing, 10.0))}
      m = placement_matrix(diag["origin"], diag["extent"], diag["rotation"], coord)
      ports = {name: (apply(mul(m, pm), 0, 0)) for name, pm in self._ports.get(t, {}).items()}
      center = apply(m, (coord[0] + coord[2]) / 2, (coord[1] + coord[3]) / 2)
      for pname, (px, py) in ports.items():
        dx, dy = px - center[0], py - center[1]
        out_dirs[f'{comp["name"]}.{pname}'] = (
            (1 if dx > 0 else -1, 0) if abs(dx) >= abs(dy) else (0, 1 if dy > 0 else -1))
      modifiers, defaults = {}, {}
      mnames = self.q(f"getComponentModifierNames({model}, {comp['name']})")
      for mname in (mnames if isinstance(mnames, list) else []):
        val = self.raw(f"getComponentModifierValue({model}, {comp['name']}.{mname})").strip().strip('"')
        if val and not val.startswith("Error"):
          modifiers[str(mname)] = val.lstrip("= ").strip()
      for key in set(re.findall(r"%([A-Za-z_]\w*)", svg)) - {"name", "class"} - set(modifiers):
        val = self.raw(f'getParameterValue({t}, "{key}")').strip().strip('"')
        if val:
          defaults[key] = val
      comps.append({"name": comp["name"], "type": t, "comment": comp["comment"],
                    "modifiers": modifiers, "defaults": defaults,
                    "matrix": m, "coord": coord, "svg": svg,
                    "ports": {k: [round(x, 3), round(y, 3)] for k, (x, y) in ports.items()},
                    "port_types": dict(self._port_types.get(t, {})),
                    "auto_placed": comp["placement"] is None})
    port_xy = {f'{c["name"]}.{p}': xy for c in comps for p, xy in c["ports"].items()}
    wires = []
    count = self.raw(f"getConnectionCount({model})").strip()
    for i in range(1, int(float(count)) + 1 if count.replace(".", "").isdigit() else 1):
      conn = self.q(f"getNthConnection({model}, {i})") or []
      if len(conn) < 2:
        continue
      a, b = (re.sub(r"\[.*?\]", "", str(x)) for x in conn[:2])
      ann = self.q(f"getNthConnectionAnnotation({model}, {i})") or []
      line = next((x for x in ann if isinstance(x, _Call) and x.name == "Line"), None)
      pts, color = [], [0.0, 0.0, 255.0]
      if line:
        la = line.args + [None] * 6
        pts, color = _pts(la[3]), la[4] if isinstance(la[4], list) else color
      if len(pts) < 2:
        pa, pb = port_xy.get(a), port_xy.get(b)
        if pa and pb:
          pts = route(pa, out_dirs.get(a, (1, 0)), pb, out_dirs.get(b, (-1, 0)))
      wires.append({"from": a, "to": b, "points": [list(p) for p in pts], "color": _rgb(color)})
    coord = self.q(f"getDiagramAnnotation({model})")
    extent = tuple(coord[:4]) if isinstance(coord, list) and len(coord) >= 4 and all(
        isinstance(v, float) for v in coord[:4]) else (-100.0, -100.0, 100.0, 100.0)
    # The model's own Diagram-layer graphics (rectangles, labels, sketches drawn in the model).
    background = ""
    if isinstance(coord, list) and coord and isinstance(coord[-1], list):
      background = "".join(render_primitive(g) for g in coord[-1])
    # Equation-based models have no instances: show the model's icon so the canvas is not empty.
    icon_coord, icon_svg = (None, "")
    if not comps:
      self._icons.pop(model, None)
      icon_coord, icon_svg = self.icon(model)
    return {"components": comps, "wires": wires, "extent": extent, "background": background,
            "model_icon": {"coord": icon_coord, "svg": icon_svg} if icon_svg else None,
            "equation_based": not comps}


def route(pa, da, pb, db, stub=6.0):
  """Orthogonal route that leaves each pin along its outward direction."""
  a1 = (pa[0] + da[0] * stub, pa[1] + da[1] * stub)
  b1 = (pb[0] + db[0] * stub, pb[1] + db[1] * stub)
  if da[0] != 0:  # leave horizontally, then vertical, then horizontal
    mid = [(a1[0], b1[1])] if db[1] != 0 else [((a1[0] + b1[0]) / 2, a1[1]), ((a1[0] + b1[0]) / 2, b1[1])]
  else:
    mid = [(b1[0], a1[1])] if db[0] != 0 else [(a1[0], (a1[1] + b1[1]) / 2), (b1[0], (a1[1] + b1[1]) / 2)]
  pts = [tuple(pa), a1, *mid, b1, tuple(pb)]
  out = []
  for p in pts:  # drop duplicates and collinear interior points
    if out and abs(out[-1][0] - p[0]) < 1e-9 and abs(out[-1][1] - p[1]) < 1e-9:
      continue
    if len(out) >= 2 and ((out[-2][0] == out[-1][0] == p[0]) or (out[-2][1] == out[-1][1] == p[1])):
      out[-1] = p
      continue
    out.append(p)
  return out


def parse_imports(source: str) -> Dict[str, Any]:
  out: Dict[str, Any] = {"*": []}
  for m in re.finditer(r"\bimport\s+([\w.]+)\s*(?:=\s*([\w.]+))?\s*(\.\*)?\s*;", source):
    a, b, star = m.groups()
    if b:  # import Short = Full.Path;
      out[a] = b
    elif star:
      out["*"].append(a)
    else:
      out[a.split(".")[-1]] = a
  return out
