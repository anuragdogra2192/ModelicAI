"""Per-model chat threads, persisted in SQLite (data/studio.db by default).

Each model name is a thread; "_workspace" holds messages sent while no model
was open that did not produce one. Mount ./data as a volume in Docker to keep
history across container restarts.
"""
import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

DB_PATH = os.getenv("STUDIO_DB", os.path.join("data", "studio.db"))
WORKSPACE = "_workspace"
_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
  os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
  c = sqlite3.connect(DB_PATH)
  c.execute("""CREATE TABLE IF NOT EXISTS messages (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      model TEXT NOT NULL,
      role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
      content TEXT NOT NULL,
      meta TEXT NOT NULL DEFAULT '{}',
      created REAL NOT NULL)""")
  c.execute("CREATE INDEX IF NOT EXISTS idx_messages_model ON messages(model, id)")
  return c


def add(model: str, role: str, content: str, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
  now = time.time()
  with _lock, _conn() as c:
    cur = c.execute(
        "INSERT INTO messages (model, role, content, meta, created) VALUES (?, ?, ?, ?, ?)",
        (model or WORKSPACE, role, content, json.dumps(meta or {}), now))
    return {"id": cur.lastrowid, "model": model or WORKSPACE, "role": role,
            "content": content, "meta": meta or {}, "created": now}


def history(model: str, limit: int = 200) -> List[Dict[str, Any]]:
  with _lock, _conn() as c:
    rows = c.execute(
        "SELECT id, model, role, content, meta, created FROM messages "
        "WHERE model = ? ORDER BY id DESC LIMIT ?", (model or WORKSPACE, limit)).fetchall()
  return [{"id": r[0], "model": r[1], "role": r[2], "content": r[3],
           "meta": json.loads(r[4] or "{}"), "created": r[5]} for r in reversed(rows)]


def move(from_model: str, to_model: str, since_id: int) -> None:
  """Re-files messages (e.g. a prompt sent from the workspace that created a model)."""
  with _lock, _conn() as c:
    c.execute("UPDATE messages SET model = ? WHERE model = ? AND id >= ?",
              (to_model, from_model or WORKSPACE, since_id))


def copy(from_model: str, to_model: str) -> int:
  """Copies a conversation (a draft that produced a model) into another thread. The draft keeps its own."""
  with _lock, _conn() as c:
    cur = c.execute("INSERT INTO messages (model, role, content, meta, created) "
                    "SELECT ?, role, content, meta, created FROM messages WHERE model = ? ORDER BY id",
                    (to_model, from_model))
    return cur.rowcount


def context_block(model: str, turns: int = 6) -> str:
  """Recent conversation as plain text, for prompts to agents without chat memory."""
  msgs = [m for m in history(model, turns) if m["role"] in ("user", "assistant")]
  if not msgs:
    return ""
  lines = [f'{"User" if m["role"] == "user" else "Assistant"}: {m["content"]}' for m in msgs]
  return "Earlier conversation about this model:\n" + "\n".join(lines)
