"""Runs simulations as a separate process that Studio can watch and stop.

OMC's simulate() is one blocking call that holds the shared OMC session and cannot be cancelled. Here:
  1. buildModel() compiles the model (the only step that needs the OMC lock); skipped when the
     executable is newer than the .mo file.
  2. The generated executable runs as a child process, with start/stop time, intervals, solver and
     parameter overrides passed as flags, writing to a temporary result file.
  3. A watchdog polls it: progress (simulated time, read from the growing result file), wall-clock
     timeout, result-size cap, and a stop request from the user.
  4. Only a finished, successful run replaces <name>_res.mat; stopped or failed runs are deleted, so the
     last good results are never lost.

One simulation runs at a time (Studio-wide); `status()` describes it for the progress bar.
"""
import os
import re
import signal
import struct
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from typing import Any, Callable, Dict, List, Optional

# ---------------------------------------------------------------- settings (changed from the UI)
CONFIG = {
    "timeout_s": float(os.getenv("SIM_TIMEOUT_S", "120")),   # 0 = no limit
    "max_mb": float(os.getenv("MAX_RESULT_MB", "300")),       # result-size cap and preflight threshold
}
FAIL_RE = re.compile(r"simulation terminated by an assert|initialization failed|integrator failed|solver failed|"
                     r"division by zero|nonlinear solver failed|error", re.I)

_state_lock = threading.Lock()
_active: Optional["Run"] = None
_last: Optional[Dict[str, Any]] = None


class Busy(Exception):
  pass


class NeedsConfirm(Exception):
  def __init__(self, estimate_mb: float, n_vars: int, points: int):
    self.estimate_mb, self.n_vars, self.points = estimate_mb, n_vars, points
    super().__init__(f"This run would write about {estimate_mb:.0f} MB of results ({n_vars} variables x {points} points), "
                     f"above the {CONFIG['max_mb']:.0f} MB limit.")


class Run:
  def __init__(self, model: str, source: str, start: float, stop: float, timeout_s: float):
    self.model, self.source, self.start, self.stop = model, source, start, stop
    self.timeout_s = timeout_s
    self.phase = "building"           # building -> running -> done
    self.t = start                    # simulated time reached
    self.bytes = 0
    self.started = time.time()
    self.stop_requested = False
    self.proc: Optional[subprocess.Popen] = None
    self.reason: Optional[str] = None  # finished | failed | user | timeout | too_large

  def info(self) -> Dict[str, Any]:
    span = (self.stop - self.start) or 1.0
    return {"running": self.phase != "done", "model": self.model, "source": self.source, "phase": self.phase,
            "t": self.t, "start": self.start, "stop": self.stop,
            "progress": max(0.0, min(1.0, (self.t - self.start) / span)) if self.phase == "running" else 0.0,
            "elapsed": round(time.time() - self.started, 1), "timeout_s": self.timeout_s,
            "mb": round(self.bytes / 1e6, 1), "max_mb": CONFIG["max_mb"], "reason": self.reason}


def status() -> Dict[str, Any]:
  with _state_lock:
    if _active is not None:
      return _active.info()
    return {"running": False, "last": _last}


def request_stop(model: Optional[str] = None) -> bool:
  with _state_lock:
    run = _active
  if run is None or (model and run.model != model):
    return False
  run.stop_requested = True
  _kill(run)
  return True


def _kill(run: Run) -> None:
  p = run.proc
  if p is not None and p.poll() is None:
    try:
      os.killpg(p.pid, signal.SIGKILL)  # the executable runs in its own process group
    except Exception:
      try:
        p.kill()
      except Exception:
        pass


# ------------------------------------------------------------------ result file (OMC MAT v4) reading
_ELEM = {0: ("d", 8), 1: ("f", 4), 2: ("i", 4), 3: ("h", 2), 4: ("H", 2), 5: ("B", 1)}


def _data2_layout(path: str):
  """Offset of the data_2 values, values per time point and element format; None while headers are incomplete."""
  try:
    with open(path, "rb") as f:
      pos = 0
      for _ in range(6):
        f.seek(pos)
        h = f.read(20)
        if len(h) < 20:
          return None
        typ, mrows, ncols, _imag, namlen = struct.unpack("<5i", h)
        if namlen <= 0 or mrows < 0 or ncols < 0:
          return None
        name = f.read(namlen).rstrip(b"\x00")
        fmt, size = _ELEM.get((typ % 100) // 10, ("d", 8))
        start = pos + 20 + namlen
        if name == b"data_2":
          return start, mrows, fmt, size
        pos = start + mrows * ncols * size
  except OSError:
    return None
  return None


def _sim_time(path: str, layout) -> Optional[float]:
  """Time of the last complete point in data_2 (first value of each column, OMC's transposed layout)."""
  start, per_point, fmt, size = layout
  try:
    n = (os.path.getsize(path) - start) // (per_point * size)
    if n <= 0:
      return None
    with open(path, "rb") as f:
      f.seek(start + (n - 1) * per_point * size)
      return struct.unpack("<" + fmt, f.read(size))[0]
  except (OSError, struct.error):
    return None


def _stored_vars(init_xml: str) -> int:
  """Number of values stored per output point (time + non-parameter variables), from <model>_init.xml."""
  try:
    root = ET.parse(init_xml).getroot()
  except (OSError, ET.ParseError):
    return 0
  n = 1
  for sv in root.iter("ScalarVariable"):
    if sv.get("variability") in ("continuous", "discrete") and sv.get("isProtected", "false") != "true" \
        and sv.get("aliasVariable") is None and sv.get("alias", "noAlias") == "noAlias":
      n += 1
  return n


# ----------------------------------------------------------------------------------------- the run
def run(client, omc_lock, path: str, name: str, settings: Dict[str, Any], overrides: Dict[str, Any],
        result_file: str, source: str = "gui", timeout_s: Optional[float] = None, force: bool = False,
        track: bool = True, on_progress: Callable[[Dict[str, Any]], None] = lambda i: None) -> Dict[str, Any]:
  """Builds (if needed) and runs one simulation. Returns {status, messages, reason, wall}.
  Raises Busy if another tracked run is active, NeedsConfirm if the preflight estimate is above the cap."""
  global _active, _last
  start, stop = float(settings["start_time"]), float(settings["stop_time"])
  n = int(settings["number_of_intervals"])
  timeout_s = CONFIG["timeout_s"] if timeout_s is None else float(timeout_s)
  r = Run(name, source, start, stop, timeout_s)
  if track:
    with _state_lock:
      if _active is not None:
        raise Busy(f"A simulation of {_active.model} is already running. Stop it first.")
      _active = r
  t0 = time.time()
  try:
    folder = os.path.dirname(os.path.abspath(path))
    exe = os.path.join(folder, name)
    # 1. build (only when the source changed since the last build)
    if not (os.path.exists(exe) and os.path.getmtime(exe) >= os.path.getmtime(path)):
      with omc_lock:
        client.omc.sendExpression(f'loadFile("{os.path.abspath(path)}")')
        client.omc.sendExpression(f'cd("{folder}")')
        try:
          built = client.omc.sendExpression(f"buildModel({name})")
        finally:
          client.omc.sendExpression(f'cd("{os.path.abspath(".")}")')
        err = str(client.omc.sendExpression("getErrorString()") or "")
      if not os.path.exists(exe):
        r.reason = "failed"
        return {"status": "FAILED", "reason": "build", "wall": round(time.time() - t0, 3),
                "messages": f"Build failed. {err or built}".strip()}
    if r.stop_requested:
      r.reason = "user"
      return {"status": "STOPPED", "reason": "user", "wall": round(time.time() - t0, 3), "messages": "Stopped by you."}

    # 2. preflight size estimate
    per_point = _stored_vars(os.path.join(folder, f"{name}_init.xml"))
    cap_mb = CONFIG["max_mb"]
    if per_point and cap_mb > 0:
      est_mb = per_point * (n + 1) * 8 / 1e6
      if est_mb > cap_mb:
        if not force:
          raise NeedsConfirm(est_mb, per_point, n + 1)
        cap_mb = est_mb * 2  # confirmed by the user: allow the estimate (+ room for event points)
        CONFIG["forced_mb"] = max(CONFIG.get("forced_mb", 0), cap_mb)

    # 3. run the executable with a watchdog
    tmp = os.path.join(folder, f"{name}_res.running.mat")
    log_path = os.path.join(folder, f"{name}_run.log")
    for f in (tmp,):
      if os.path.exists(f):
        os.remove(f)
    step = (stop - start) / max(1, n)
    # Run settings go into a copy of <name>_init.xml (DefaultExperiment), passed with -f. Recent OpenModelica
    # versions ignore startTime/stopTime/stepSize given through -override ("override variable name not found").
    run_xml = _run_init_xml(folder, name, start, stop, step, settings.get("tolerance"), settings.get("method", "dassl"))
    args = [exe, f"-r={tmp}", f"-s={settings.get('method', 'dassl')}"]
    if run_xml:
      args.append(f"-f={run_xml}")
    else:  # no init file found: fall back to overrides (older OpenModelica accepts them)
      args.append("-override=" + ",".join(f"{k}={v}" for k, v in {"startTime": start, "stopTime": stop, "stepSize": step}.items()))
    if overrides:
      args.append("-override=" + ",".join(f"{k}={v}" for k, v in overrides.items()))
    r.phase = "running"
    with open(log_path, "w") as log:
      r.proc = subprocess.Popen(args, cwd=folder, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
      layout = None
      while r.proc.poll() is None:
        time.sleep(0.25)
        if os.path.exists(tmp):
          r.bytes = os.path.getsize(tmp)
          layout = layout or _data2_layout(tmp)
          if layout:
            t = _sim_time(tmp, layout)
            if t is not None:
              r.t = t
        if r.stop_requested:
          r.reason = "user"
        elif timeout_s and time.time() - r.started > timeout_s:
          r.reason = "timeout"
        elif cap_mb > 0 and r.bytes > cap_mb * 1e6:
          r.reason = "too_large"
        if r.reason:
          _kill(r)
          r.proc.wait()
          break
        on_progress(r.info())
    full = _read_log(log_path)
    out = full[-4000:]
    wall = round(time.time() - t0, 3)
    chatter = re.search(r"Chattering detected[^\n]*", out)
    if r.reason:
      if os.path.exists(tmp):
        os.remove(tmp)
      why = {"user": "Stopped by you.",
             "timeout": f"Stopped: the run took longer than the {timeout_s:g} s timeout.",
             "too_large": f"Stopped: the result file passed the {cap_mb:g} MB limit."}[r.reason]
      if chatter:
        why += (" The model is chattering (" + chatter.group(0)[:200] + "). Events fire faster than time advances, "
                "e.g. a bouncing ball that never comes to rest: add a rest condition.")
      detail = explain_failure(exe, folder, name, run_xml, overrides, full, r.t) if r.reason == "timeout" else ""
      return {"status": "STOPPED", "reason": r.reason, "wall": wall,
              "messages": f"{why}\nReached t = {r.t:g} s.\n" + (detail + "\n" if detail else "") + summarize_log(full)}
    ok = r.proc.returncode == 0 and os.path.exists(tmp) and not FAIL_RE.search(
        "\n".join(l for l in out.splitlines() if not l.startswith("LOG_SUCCESS")))
    if ok:
      os.replace(tmp, result_file)  # atomic: the old results stay until a new run has fully succeeded
      r.t = stop
      r.reason = "finished"
    else:
      if os.path.exists(tmp):
        os.remove(tmp)
      r.reason = "failed"
    return {"status": "SUCCESS" if ok else "FAILED", "reason": r.reason, "wall": wall,
            "messages": (_failure_text(exe, folder, name, run_xml, overrides, full) if not ok else out[-1500:])
                        + (f"\nWarning: {chatter.group(0)}" if chatter and ok else "")}
  finally:
    r.phase = "done"
    if track:
      with _state_lock:
        _last = r.info()
        _active = None


_DETAIL = re.compile(r"violat|has value|allowed range|is negative|out of range|-->|division by zero", re.I)


def explain_failure(exe: str, folder: str, name: str, run_xml: Optional[str], overrides: Dict[str, Any],
                    log: str, t_fail: Optional[float], timeout_s: float = 20.0) -> str:
  """When the log says only 'an assertion failed at time t' (OpenModelica hides the details of assertions inside
  the nonlinear solver), re-run once with verbose solver logging up to just past t and extract the assertion
  lines that name the variable and its value. Returns '' if nothing more is found."""
  if _DETAIL.search(log) or not re.search(r"assertion failed|linear system|nonlinear|non-linear", log, re.I):
    return ""
  times = [float(x) for x in re.findall(r"at time ([-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?)", log)]
  t = max([x for x in [t_fail] + times[:1] if x] or [0.0]) or None  # run just past the first failure
  if not t or not run_xml or not os.path.exists(run_xml):
    return ""
  xml = open(run_xml, encoding="utf-8", errors="replace").read()
  dbg_xml = os.path.join(folder, f"{name}_debug_init.xml")
  with open(dbg_xml, "w", encoding="utf-8") as f:
    f.write(re.sub(r'\bstopTime\s*=\s*"[^"]*"', f'stopTime = "{t * 1.02 + 1e-3}"', xml, count=1))
  dbg_res, dbg_log = os.path.join(folder, f"{name}_debug_res.mat"), os.path.join(folder, f"{name}_debug.log")
  args = [exe, f"-r={dbg_res}", f"-f={dbg_xml}", "-lv=LOG_NLS_V"]
  if overrides:
    args.append("-override=" + ",".join(f"{k}={v}" for k, v in overrides.items()))
  try:
    with open(dbg_log, "w") as out:
      p = subprocess.Popen(args, cwd=folder, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
      try:
        p.wait(timeout=timeout_s)
      except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        p.wait()
  except Exception:
    return ""
  found: Dict[str, str] = {}  # message without numbers -> first full message
  try:
    with open(dbg_log, encoding="utf-8", errors="replace") as f:
      prev = ""
      for line in f:
        txt = re.sub(r"^(\|\s*)+|^LOG_\w+\s*\|\s*\w+\s*\|\s*", "", line.strip()).strip(" |")
        if _DETAIL.search(txt):
          item = (prev + " " + txt if "-->" in txt and len(txt) < 60 else txt)[:300]
          found.setdefault(re.sub(r"[-+]?\d[\d.eE+-]*", "#", item)[:160], item)
          if len(found) >= 8:
            break
        prev = txt
  finally:
    for f in (dbg_res, dbg_log, dbg_xml):
      try:
        os.remove(f)
      except OSError:
        pass
  if not found:
    return ""
  return f"DETAILS (verbose re-run up to t = {t:g} s):\n" + "\n".join(f"- {x}" for x in found.values())


def _failure_text(exe, folder, name, run_xml, overrides, full) -> str:
  detail = explain_failure(exe, folder, name, run_xml, overrides, full, None)
  return (detail + "\n" if detail else "") + summarize_log(full)


def _read_log(path: str, limit: int = 2_000_000) -> str:
  if not os.path.exists(path):
    return ""
  with open(path, encoding="utf-8", errors="replace") as f:
    data = f.read(limit + 1)
  return data if len(data) <= limit else data[:limit // 2] + "\n...\n" + data[-limit // 2:]


_KEY = re.compile(r"assert|violat|not in the allowed range|out of range|has value|is negative|negative|exceed|"
                  r"error|failed|division by zero|chattering|singular|terminated", re.I)
_NOISE = re.compile(r"could take some time|It could help to provide better start-values|For more information|"
                    r"simulate with -lv|override variable name not found", re.I)


def summarize_log(log: str, max_chars: int = 1800) -> str:
  """The distinct messages that explain a failed run (which variable, which value, at what time), first;
  OpenModelica repeats generic retry lines hundreds of times and they would push the real cause out."""
  entries: List[str] = []
  for line in log.splitlines():
    text = line.strip()
    if not text:
      continue
    if text.startswith("|") and entries:  # continuation line of the previous LOG_* message
      body = re.sub(r"^(\|\s*)+", "", text).strip(" |")
      if body:
        entries[-1] += " " + body
    else:
      entries.append(re.sub(r"^LOG_\w+\s*\|\s*\w+\s*\|\s*", "", text))
  seen: Dict[str, int] = {}
  order: List[str] = []
  for e in entries:
    e = re.sub(r"\s*(The non-linear solver tries[^.]*\.|It could help[^.]*\.|For more information[^.]*\.?)", "", e).strip()
    if not _KEY.search(e) or re.search(r"override variable name not found", e, re.I):
      continue
    key = re.sub(r"[-+]?\d[\d.eE+-]*", "#", e)[:200]  # same message at another time/value counts once
    if key not in seen:
      seen[key] = 0
      order.append(e)
    seen[key] += 1
  lines = []
  for e in order:
    n = seen[re.sub(r"[-+]?\d[\d.eE+-]*", "#", e)[:200]]
    lines.append(f"- {e[:400]}" + (f"  (x{n})" if n > 1 else ""))
  summary = ("KEY MESSAGES:\n" + "\n".join(lines)) if lines else ""
  tail = log[-600:] if len(summary) < max_chars - 600 else ""
  return (summary + ("\n\nLOG TAIL:\n" + tail if tail else ""))[:max_chars] or log[-1500:]


def _run_init_xml(folder: str, name: str, start: float, stop: float, step: float,
                  tolerance: Optional[float], method: str) -> Optional[str]:
  """Copy of <name>_init.xml with this run's start/stop/step/tolerance/solver in <DefaultExperiment>."""
  src = os.path.join(folder, f"{name}_init.xml")
  if not os.path.exists(src):
    return None
  xml = open(src, encoding="utf-8", errors="replace").read()
  m = re.search(r"<DefaultExperiment\b[^>]*>", xml)
  if not m:
    return None
  tag = m.group(0)
  for attr, val in (("startTime", start), ("stopTime", stop), ("stepSize", step),
                    ("tolerance", tolerance), ("solver", method)):
    if val is None:
      continue
    # OpenModelica writes `stopTime = "1.0"` (spaces around =); match with or without spaces
    pat = rf'\b{attr}\s*=\s*"[^"]*"'
    if re.search(pat, tag):
      tag = re.sub(pat, f'{attr} = "{val}"', tag)
    else:
      tag = tag.replace("<DefaultExperiment", f'<DefaultExperiment {attr}="{val}"', 1)
  dst = os.path.join(folder, f"{name}_run_init.xml")
  with open(dst, "w", encoding="utf-8") as f:
    f.write(xml[:m.start()] + tag + xml[m.end():])
  return dst


def estimate(folder: str, name: str, n: int) -> Optional[Dict[str, Any]]:
  """Preflight estimate from the last build (no build here). None if the model was never built."""
  per_point = _stored_vars(os.path.join(folder, f"{name}_init.xml"))
  if not per_point:
    return None
  mb = per_point * (n + 1) * 8 / 1e6
  return {"mb": round(mb, 1), "variables": per_point, "points": n + 1, "max_mb": CONFIG["max_mb"],
          "over_limit": CONFIG["max_mb"] > 0 and mb > CONFIG["max_mb"],
          "note": "Events add extra points, so the real file can be larger."}
