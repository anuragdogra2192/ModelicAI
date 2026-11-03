"""Stop request for the running Modi turn (one turn runs at a time, see api/agent_api.py _lock).

The turn's loop checks it between graph steps; long tools check it too (the design loop before each
attempt). A running simulation is stopped through utils.sim_runner.
"""
import threading

STOP = threading.Event()


class Cancelled(Exception):
  """Raised inside tools when the user pressed Stop."""


def check() -> None:
  if STOP.is_set():
    raise Cancelled("Stopped by the user.")
