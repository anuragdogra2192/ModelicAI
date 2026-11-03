"""Persistent model memory: runs, revisions, notes and pending proposals.

Lives in the same SQLite file as the chat history (api/chat_store.py), so one
mounted ./data volume keeps everything. The LangGraph checkpointer uses its own
file next to it (see agents/manager.py).
"""
import json
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from api.chat_store import DB_PATH, _conn as _chat_conn

_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
  c = _chat_conn()  # creates the file and the messages table
  c.executescript("""
    CREATE TABLE IF NOT EXISTS runs (
      id INTEGER PRIMARY KEY AUTOINCREMENT, model TEXT NOT NULL,
      settings TEXT NOT NULL, overrides TEXT NOT NULL, status TEXT NOT NULL,
      metrics TEXT NOT NULL DEFAULT '{}', wall REAL, source TEXT, created REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS revisions (
      id INTEGER PRIMARY KEY AUTOINCREMENT, model TEXT NOT NULL, code TEXT NOT NULL,
      summary TEXT NOT NULL, source TEXT, created REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS notes (
      id INTEGER PRIMARY KEY AUTOINCREMENT, model TEXT NOT NULL, text TEXT NOT NULL,
      created REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS proposals (
      id TEXT PRIMARY KEY, model TEXT, kind TEXT NOT NULL, payload TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending', created REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS model_meta (
      model TEXT PRIMARY KEY, origin_prompt TEXT, purpose TEXT, created REAL);
    CREATE TABLE IF NOT EXISTS drafts (
      id TEXT PRIMARY KEY, first_prompt TEXT, outcome TEXT NOT NULL DEFAULT 'open', model TEXT,
      error_category TEXT, last_span_id TEXT, turns INTEGER NOT NULL DEFAULT 0,
      created REAL NOT NULL, updated REAL NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_runs_model ON runs(model, id);
    CREATE INDEX IF NOT EXISTS idx_rev_model ON revisions(model, id);
  """)
  try:  # older databases: model_meta without session_id
    c.execute("ALTER TABLE model_meta ADD COLUMN session_id TEXT")
  except sqlite3.OperationalError:
    pass
  return c


def _rows(sql: str, args=()) -> List[sqlite3.Row]:
  with _lock, _conn() as c:
    c.row_factory = sqlite3.Row
    return c.execute(sql, args).fetchall()


def _exec(sql: str, args=()) -> int:
  with _lock, _conn() as c:
    return c.execute(sql, args).lastrowid


# ------------------------------------------------------------------ runs
def add_run(model, settings, overrides, status, metrics=None, wall=None, source="gui") -> int:
  return _exec("INSERT INTO runs (model, settings, overrides, status, metrics, wall, source, created) "
               "VALUES (?,?,?,?,?,?,?,?)",
               (model, json.dumps(settings), json.dumps(overrides), status,
                json.dumps(metrics or {}), wall, source, time.time()))


def runs(model: str, limit: int = 5) -> List[Dict[str, Any]]:
  out = []
  for r in _rows("SELECT * FROM runs WHERE model=? ORDER BY id DESC LIMIT ?", (model, limit)):
    out.append({"id": r["id"], "settings": json.loads(r["settings"]), "overrides": json.loads(r["overrides"]),
                "status": r["status"], "metrics": json.loads(r["metrics"]), "wall": r["wall"],
                "source": r["source"], "created": r["created"]})
  return out


# ------------------------------------------------------------- revisions
def add_revision(model: str, code: str, summary: str, source: str = "agent") -> int:
  return _exec("INSERT INTO revisions (model, code, summary, source, created) VALUES (?,?,?,?,?)",
               (model, code, summary, source, time.time()))


def revisions(model: str, limit: int = 8) -> List[Dict[str, Any]]:
  return [{"id": r["id"], "summary": r["summary"], "source": r["source"], "created": r["created"]}
          for r in _rows("SELECT id, summary, source, created FROM revisions WHERE model=? "
                         "ORDER BY id DESC LIMIT ?", (model, limit))]


def revision_code(model: str, rev_id: int) -> Optional[str]:
  r = _rows("SELECT code FROM revisions WHERE model=? AND id=?", (model, rev_id))
  return r[0]["code"] if r else None


# ------------------------------------------------------------------ notes
def add_note(model: str, text: str) -> int:
  return _exec("INSERT INTO notes (model, text, created) VALUES (?,?,?)", (model, text, time.time()))


def notes(model: str) -> List[str]:
  return [r["text"] for r in _rows("SELECT text FROM notes WHERE model=? ORDER BY id", (model,))]


# ------------------------------------------------------------ model meta
def set_meta(model: str, origin_prompt: str = None, purpose: str = None) -> None:
  with _lock, _conn() as c:
    c.execute("INSERT INTO model_meta (model, origin_prompt, purpose, created) VALUES (?,?,?,?) "
              "ON CONFLICT(model) DO UPDATE SET origin_prompt=COALESCE(excluded.origin_prompt, origin_prompt), "
              "purpose=COALESCE(excluded.purpose, purpose)",
              (model, origin_prompt, purpose, time.time()))


def meta(model: str) -> Dict[str, Any]:
  r = _rows("SELECT * FROM model_meta WHERE model=?", (model,))
  return dict(r[0]) if r else {}


# ----------------------------------------------------------------- drafts
# A "+ New" conversation. Kept forever (until its model is deleted), whatever happens to it, so failed
# requests can be studied and turned into eval cases. Outcome: open -> created | design_failed |
# discarded | refused | abandoned (created always wins; abandoned only replaces open).
FINAL = ("created",)


def touch_draft(draft_id: str, prompt: str = "", span_id: Optional[str] = None) -> None:
  now = time.time()
  with _lock, _conn() as c:
    c.execute("INSERT INTO drafts (id, first_prompt, created, updated, turns, last_span_id) VALUES (?,?,?,?,1,?) "
              "ON CONFLICT(id) DO UPDATE SET updated=excluded.updated, turns=turns+1, "
              "last_span_id=COALESCE(excluded.last_span_id, last_span_id)",
              (draft_id, prompt[:2000], now, now, span_id))


def set_draft_outcome(draft_id: str, outcome: str, model: Optional[str] = None,
                      category: Optional[str] = None) -> bool:
  """Returns True when the outcome changed."""
  with _lock, _conn() as c:
    r = c.execute("SELECT outcome FROM drafts WHERE id=?", (draft_id,)).fetchone()
    if not r or r[0] in FINAL or (outcome == "abandoned" and r[0] != "open"):
      return False
    c.execute("UPDATE drafts SET outcome=?, model=COALESCE(?, model), error_category=COALESCE(?, error_category), "
              "updated=? WHERE id=?", (outcome, model, category, time.time(), draft_id))
    return True


def draft(draft_id: str) -> Dict[str, Any]:
  r = _rows("SELECT * FROM drafts WHERE id=?", (draft_id,))
  return dict(r[0]) if r else {}


def stale_drafts(older_than_s: float) -> List[Dict[str, Any]]:
  return [dict(r) for r in _rows("SELECT * FROM drafts WHERE outcome='open' AND updated < ?",
                                 (time.time() - older_than_s,))]


def link_model_session(model: str, session_id: str) -> None:
  """The model keeps the session of the draft it came from, so Phoenix shows one story."""
  set_meta(model)
  _exec("UPDATE model_meta SET session_id=? WHERE model=?", (session_id, model))


def session_of(model: Optional[str]) -> Optional[str]:
  """Tracing session for a thread: the model name, or the _new_... id while it is still a draft.
  A model made from a draft keeps a link to it (model_meta.session_id = origin draft), and the draft's
  traces carry modelica.model / draft.outcome, so both are easy to find from either side."""
  return model


def origin_draft(model: str) -> Optional[str]:
  r = _rows("SELECT session_id FROM model_meta WHERE model=?", (model,))
  return r[0]["session_id"] if r and r[0]["session_id"] else None


# -------------------------------------------------------------- proposals
def add_proposal(model: Optional[str], kind: str, payload: Dict[str, Any]) -> str:
  pid = uuid.uuid4().hex[:10]
  _exec("INSERT INTO proposals (id, model, kind, payload, created) VALUES (?,?,?,?,?)",
        (pid, model, kind, json.dumps(payload), time.time()))
  return pid


def proposal(pid: str) -> Optional[Dict[str, Any]]:
  r = _rows("SELECT * FROM proposals WHERE id=?", (pid,))
  if not r:
    return None
  r = r[0]
  return {"id": r["id"], "model": r["model"], "kind": r["kind"], "status": r["status"],
          "payload": json.loads(r["payload"]), "created": r["created"]}


def set_proposal_status(pid: str, status: str) -> None:
  _exec("UPDATE proposals SET status=? WHERE id=?", (status, pid))


def purge_model(model: str) -> dict:
  """Erases all memory of a model: chat, runs, revisions, notes, proposals, origin.
  Also clears rows left by earlier archive-style deletes (<name>@deleted-...)."""
  counts, proposal_ids = {}, []
  with _lock, _conn() as c:
    proposal_ids = [r[0] for r in c.execute(
        "SELECT id FROM proposals WHERE model=? OR model LIKE ?", (model, f"{model}@deleted-%"))]
    # drafts that led to this model: their chat goes too (the agent thread is removed by the delete hook)
    draft_ids = [r[0] for r in c.execute("SELECT id FROM drafts WHERE model=?", (model,))]
    for d in draft_ids:
      c.execute("DELETE FROM messages WHERE model=?", (d,))
    counts["drafts"] = c.execute("DELETE FROM drafts WHERE model=?", (model,)).rowcount
    for table in ("messages", "runs", "revisions", "notes", "proposals", "model_meta"):
      cur = c.execute(f"DELETE FROM {table} WHERE model=? OR model LIKE ?", (model, f"{model}@deleted-%"))
      counts[table] = cur.rowcount
  return {"deleted_rows": counts, "proposal_ids": proposal_ids, "draft_ids": draft_ids}
