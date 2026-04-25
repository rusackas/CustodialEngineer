"""SQLite-backed durable store for session transcripts + token events.

Phase 1 of the storage migration. Strictly additive — the in-memory
`SESSIONS` dict in `sessions.py` remains the source of truth for live
UI; the DB is a write-through shadow that survives restarts so the
session-button on a card from before reboot can still open its
transcript modal, and so token analytics outlive the process.

Design notes:

- One shared connection. SQLite serializes writes anyway via its
  per-database lock, so multiple connections wouldn't help. We pass
  `check_same_thread=False` and protect the connection with our own
  RLock so the asyncio session loop and the random ad-hoc threads
  that call `start_session` can both write safely.
- WAL mode + `synchronous = NORMAL`. Concurrent readers can keep
  going while a writer commits; we lose at most the last few millis
  on a sudden power loss, which is fine for a personal tool.
- Schema is bootstrapped lazily on first connection; migrations are
  idempotent and recorded in `schema_version`. Bump `_SCHEMA_VERSION`
  and add a `_migrate_vN` function for each new migration.
- Every public function swallows DB errors and prints a warning
  rather than raising — the live UI shouldn't break if the DB write
  fails. The in-memory state is still authoritative.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .config import PROJECT_ROOT

DB_PATH = PROJECT_ROOT / "state" / "repobot.db"

_LOCK = threading.RLock()
_CONN: Optional[sqlite3.Connection] = None
_SCHEMA_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(
        str(DB_PATH),
        check_same_thread=False,
        # Autocommit; we manage transactions explicitly when batching.
        isolation_level=None,
        # Wait up to 10s for a lock before raising — generous since
        # writes are tiny and the lock-hold window is short.
        timeout=10.0,
    )
    c.row_factory = sqlite3.Row
    # WAL: many readers + one writer can coexist without blocking.
    c.execute("PRAGMA journal_mode = WAL")
    c.execute("PRAGMA synchronous = NORMAL")
    c.execute("PRAGMA foreign_keys = ON")
    return c


def conn() -> sqlite3.Connection:
    """Lazy-init the shared connection on first use. Subsequent calls
    return the same connection; bootstrap runs once."""
    global _CONN
    if _CONN is not None:
        return _CONN
    with _LOCK:
        if _CONN is None:
            c = _connect()
            _bootstrap(c)
            _CONN = c
    return _CONN


# ============================================================== migrations

def _bootstrap(c: sqlite3.Connection) -> None:
    c.executescript(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER PRIMARY KEY)"
    )
    cur = c.execute("SELECT MAX(version) FROM schema_version")
    current = cur.fetchone()[0] or 0
    if current < 1:
        _migrate_v1(c)
        c.execute("INSERT INTO schema_version (version) VALUES (1)")


def _migrate_v1(c: sqlite3.Connection) -> None:
    c.executescript("""
    CREATE TABLE IF NOT EXISTS sessions (
      session_id TEXT PRIMARY KEY,
      skill TEXT NOT NULL,
      kind TEXT,             -- triage / action / task
      queue_id TEXT,
      item_id TEXT,          -- stored as text so PR-numbers and
                             -- task-ids both fit
      action_id TEXT,
      sdk_session_id TEXT,
      started_at TEXT NOT NULL,
      closed_at TEXT,
      status TEXT,           -- last known status snapshot
      final_result_json TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_sessions_started
      ON sessions(started_at);
    CREATE INDEX IF NOT EXISTS idx_sessions_kind_started
      ON sessions(kind, started_at);
    CREATE INDEX IF NOT EXISTS idx_sessions_queue_item
      ON sessions(queue_id, item_id);

    CREATE TABLE IF NOT EXISTS turns (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      session_id TEXT NOT NULL REFERENCES sessions(session_id)
                              ON DELETE CASCADE,
      ts TEXT NOT NULL,
      role TEXT NOT NULL,    -- system / user / assistant / tool / error
      kind TEXT,             -- text / tool_use / tool_result /
                             -- thinking / error
      text TEXT,             -- the visible body (or summary for
                             -- tool_use)
      meta_json TEXT         -- usage, tool_input, tool_output, etc.
    );
    CREATE INDEX IF NOT EXISTS idx_turns_session
      ON turns(session_id, id);

    CREATE TABLE IF NOT EXISTS token_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      session_id TEXT,
      skill TEXT,
      ts TEXT NOT NULL,
      input_tokens INTEGER NOT NULL DEFAULT 0,
      output_tokens INTEGER NOT NULL DEFAULT 0,
      cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
      cache_read_input_tokens INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_token_events_ts
      ON token_events(ts);
    CREATE INDEX IF NOT EXISTS idx_token_events_skill_ts
      ON token_events(skill, ts);
    """)


# ================================================================ writes
# Every write is wrapped in a try/except — DB hiccups must never break
# the live UI. The in-memory SESSIONS dict is still authoritative.

def _safe_exec(sql: str, params: tuple) -> None:
    try:
        with _LOCK:
            conn().execute(sql, params)
    except sqlite3.Error as exc:
        print(f"[db] {sql.split()[0].lower()} failed: {exc}")


def record_session_start(session_id: str, skill: str, *,
                         kind: Optional[str] = None,
                         queue_id: Optional[str] = None,
                         item_id: Any = None,
                         action_id: Optional[str] = None,
                         started_at: Optional[str] = None) -> None:
    """Insert (or replace) a sessions row. Called from start_session
    BEFORE the coroutine actually runs, so the row exists by the time
    the first turn lands."""
    _safe_exec(
        """INSERT OR REPLACE INTO sessions
           (session_id, skill, kind, queue_id, item_id, action_id,
            started_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (session_id, skill, kind, queue_id,
         str(item_id) if item_id is not None else None,
         action_id, started_at or _now()),
    )


def record_session_close(session_id: str, *,
                         status: Optional[str] = None,
                         final_result: Optional[dict] = None,
                         sdk_session_id: Optional[str] = None) -> None:
    """Patch the closed_at + status + final_result on a sessions row.
    Tolerates being called multiple times; the COALESCE on
    final_result_json + sdk_session_id means a later call without
    those fields doesn't clobber the earlier values."""
    final_json = (json.dumps(final_result, default=str)
                  if final_result is not None else None)
    _safe_exec(
        """UPDATE sessions SET closed_at = ?, status = ?,
               final_result_json = COALESCE(?, final_result_json),
               sdk_session_id = COALESCE(?, sdk_session_id)
           WHERE session_id = ?""",
        (_now(), status, final_json, sdk_session_id, session_id),
    )


def record_sdk_session_id(session_id: str, sdk_session_id: str) -> None:
    """Patch the SDK session id once the SDK has issued one. Allows
    resume-after-restart by way of the SDK's own resume mechanism."""
    _safe_exec(
        "UPDATE sessions SET sdk_session_id = ? WHERE session_id = ?",
        (sdk_session_id, session_id),
    )


def record_turn(session_id: str, *,
                role: str,
                ts: Optional[str] = None,
                kind: Optional[str] = None,
                text: Optional[str] = None,
                meta: Optional[dict] = None) -> None:
    """Insert one transcript turn. Called from _append."""
    meta_json = (json.dumps(meta, default=str)
                 if meta is not None else None)
    _safe_exec(
        """INSERT INTO turns
           (session_id, ts, role, kind, text, meta_json)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (session_id, ts or _now(), role, kind, text, meta_json),
    )


def record_token_event(*, session_id: Optional[str],
                       skill: Optional[str],
                       usage: Optional[dict]) -> None:
    """Append a token-usage row. No-op if usage is empty/zero."""
    if not usage:
        return
    keys = ("input_tokens", "output_tokens",
            "cache_creation_input_tokens", "cache_read_input_tokens")
    snap = {k: int(usage.get(k) or 0) for k in keys}
    if not any(snap.values()):
        return
    _safe_exec(
        """INSERT INTO token_events
           (session_id, skill, ts,
            input_tokens, output_tokens,
            cache_creation_input_tokens, cache_read_input_tokens)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (session_id, skill, _now(),
         snap["input_tokens"], snap["output_tokens"],
         snap["cache_creation_input_tokens"],
         snap["cache_read_input_tokens"]),
    )


# ================================================================ reads

def load_session_meta(session_id: str) -> Optional[dict]:
    """Return the sessions row as a plain dict, or None."""
    try:
        with _LOCK:
            row = conn().execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (session_id,)
            ).fetchone()
    except sqlite3.Error as exc:
        print(f"[db] load_session_meta failed: {exc}")
        return None
    if not row:
        return None
    out = dict(row)
    if out.get("final_result_json"):
        try:
            out["final_result"] = json.loads(out["final_result_json"])
        except json.JSONDecodeError:
            out["final_result"] = None
    return out


def load_turns(session_id: str) -> list[dict]:
    """Replay every transcript turn for a session in insertion order.
    Used to rehydrate the in-memory SESSIONS dict on boot, and as the
    backing read for the transcript modal once we flip the read path."""
    try:
        with _LOCK:
            rows = conn().execute(
                """SELECT ts, role, kind, text, meta_json
                   FROM turns WHERE session_id = ? ORDER BY id""",
                (session_id,)
            ).fetchall()
    except sqlite3.Error as exc:
        print(f"[db] load_turns failed: {exc}")
        return []
    out = []
    for r in rows:
        entry: dict = {"ts": r["ts"], "role": r["role"]}
        if r["kind"]:
            entry["kind"] = r["kind"]
        if r["text"] is not None:
            entry["text"] = r["text"]
        # Flatten meta back onto the entry so the transcript-modal
        # renderer (which expects flat keys: tool, summary, is_error,
        # etc.) doesn't have to know the row was loaded from disk.
        if r["meta_json"]:
            try:
                meta = json.loads(r["meta_json"])
                if isinstance(meta, dict):
                    for k, v in meta.items():
                        entry.setdefault(k, v)
            except json.JSONDecodeError:
                pass
        out.append(entry)
    return out


def list_recent_sessions(limit: int = 100,
                         kind: Optional[str] = None) -> list[dict]:
    """Sessions ordered newest-first. Used for boot rehydration so
    the most recently active sessions get their state restored."""
    sql = ("SELECT session_id, skill, kind, queue_id, item_id, "
           "action_id, sdk_session_id, started_at, closed_at, status, "
           "final_result_json FROM sessions")
    params: tuple = ()
    if kind:
        sql += " WHERE kind = ?"
        params = (kind,)
    sql += " ORDER BY started_at DESC LIMIT ?"
    params = params + (limit,)
    try:
        with _LOCK:
            rows = conn().execute(sql, params).fetchall()
    except sqlite3.Error as exc:
        print(f"[db] list_recent_sessions failed: {exc}")
        return []
    out = []
    for r in rows:
        d = dict(r)
        if d.get("final_result_json"):
            try:
                d["final_result"] = json.loads(d["final_result_json"])
            except json.JSONDecodeError:
                d["final_result"] = None
        out.append(d)
    return out


def tokens_in_window(seconds: int) -> dict:
    """Sum every token-event in the last `seconds` and return a dict
    with the four token-bucket keys. Used by the header readout. Pure
    SQL aggregate — way cheaper than walking the in-memory list once
    the table grows."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=seconds)
              ).isoformat()
    try:
        with _LOCK:
            row = conn().execute(
                """SELECT
                     COALESCE(SUM(input_tokens),0)  AS input_tokens,
                     COALESCE(SUM(output_tokens),0) AS output_tokens,
                     COALESCE(SUM(cache_creation_input_tokens),0)
                       AS cache_creation_input_tokens,
                     COALESCE(SUM(cache_read_input_tokens),0)
                       AS cache_read_input_tokens
                   FROM token_events WHERE ts >= ?""",
                (cutoff,)
            ).fetchone()
    except sqlite3.Error as exc:
        print(f"[db] tokens_in_window failed: {exc}")
        return {}
    return dict(row) if row else {}
