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
_SCHEMA_VERSION = 3


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
    if current < 2:
        _migrate_v2(c)
        c.execute("INSERT INTO schema_version (version) VALUES (2)")
        # One-shot import from the legacy JSON state file. Runs only
        # at v1→v2 since `queue_items` is empty at that point; future
        # migrations don't touch this.
        _maybe_migrate_from_queues_json(c)
    if current < 3:
        _migrate_v3(c)
        c.execute("INSERT INTO schema_version (version) VALUES (3)")


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


def _migrate_v2(c: sqlite3.Connection) -> None:
    """Phase 2: queue items, tasks, and runtime settings migrate out
    of state/queues.json. Hybrid schema — identity columns for
    indexability, JSON column for the rest of the dict so the
    in-memory shape round-trips losslessly without a per-field
    migration."""
    c.executescript("""
    CREATE TABLE IF NOT EXISTS queue_items (
      queue_id TEXT NOT NULL,
      item_id INTEGER NOT NULL,
      state TEXT,
      data_json TEXT NOT NULL,
      PRIMARY KEY (queue_id, item_id)
    );
    CREATE INDEX IF NOT EXISTS idx_queue_items_state
      ON queue_items(queue_id, state);

    CREATE TABLE IF NOT EXISTS tasks (
      id INTEGER PRIMARY KEY,
      status TEXT,
      data_json TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_tasks_status
      ON tasks(status);

    CREATE TABLE IF NOT EXISTS tasks_meta (
      next_id INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS settings (
      scope TEXT NOT NULL,        -- 'global' or 'queue:<id>'
      key TEXT NOT NULL,
      value_json TEXT NOT NULL,
      PRIMARY KEY (scope, key)
    );
    """)
    c.execute(
        "INSERT OR IGNORE INTO tasks_meta (rowid, next_id) VALUES (1, 1)"
    )


def _migrate_v3(c: sqlite3.Connection) -> None:
    """Phase 3: append-only audit tables for actions and state
    transitions. Both small, both indexed for the common queries
    ("what happened to PR #X" / "how often does action Y get used"
    / "how long do PRs sit in column Z")."""
    c.executescript("""
    CREATE TABLE IF NOT EXISTS actions_log (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts TEXT NOT NULL,
      queue_id TEXT,
      item_id INTEGER,
      action_id TEXT,
      status TEXT,
      message TEXT,
      session_id TEXT,
      meta_json TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_actions_log_item
      ON actions_log(queue_id, item_id, ts);
    CREATE INDEX IF NOT EXISTS idx_actions_log_ts
      ON actions_log(ts);
    CREATE INDEX IF NOT EXISTS idx_actions_log_action_ts
      ON actions_log(action_id, ts);

    CREATE TABLE IF NOT EXISTS state_transitions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts TEXT NOT NULL,
      queue_id TEXT NOT NULL,
      item_id INTEGER NOT NULL,
      from_state TEXT,
      to_state TEXT NOT NULL,
      reason TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_transitions_item
      ON state_transitions(queue_id, item_id, ts);
    CREATE INDEX IF NOT EXISTS idx_transitions_ts
      ON state_transitions(ts);
    """)


def _maybe_migrate_from_queues_json(c: sqlite3.Connection) -> None:
    """One-shot importer: read state/queues.json (the legacy JSON
    state file) into the new SQL tables, then archive the JSON file
    so subsequent boots don't re-import. No-op if the file's missing
    or the tables already have data."""
    n = c.execute("SELECT COUNT(*) FROM queue_items").fetchone()[0]
    n += c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    n += c.execute("SELECT COUNT(*) FROM settings").fetchone()[0]
    if n > 0:
        return
    json_path = PROJECT_ROOT / "state" / "queues.json"
    if not json_path.exists():
        return
    try:
        with open(json_path) as f:
            state = json.load(f)
    except Exception as exc:
        print(f"[db] migration read of queues.json failed: {exc}")
        return
    try:
        _flush_state_to_conn(c, state)
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        archive = json_path.with_name(f"queues.json.migrated-{ts}")
        json_path.rename(archive)
        n_q = sum(len((b or {}).get("items") or [])
                  for b in (state.get("queues") or {}).values())
        n_t = len(((state.get("tasks") or {}).get("items")) or [])
        print(f"[db] migrated state/queues.json → SQL "
              f"({n_q} queue items, {n_t} tasks). "
              f"Archived as {archive.name}")
    except Exception as exc:
        print(f"[db] migration write to SQL failed: {exc}")


def _flush_state_to_conn(c: sqlite3.Connection, state: dict) -> None:
    """Internal: rewrite the four state tables from a dict, on the
    given connection (no separate locking — caller holds the lock).
    Used both by the JSON migrator and by the public flush_state_dict.
    """
    c.execute("BEGIN")
    try:
        c.execute("DELETE FROM queue_items")
        c.execute("DELETE FROM tasks")
        c.execute("DELETE FROM settings")

        for qid, bucket in (state.get("queues") or {}).items():
            for item in (bucket or {}).get("items") or []:
                iid = item.get("id")
                if iid is None:
                    continue
                c.execute(
                    """INSERT INTO queue_items
                       (queue_id, item_id, state, data_json)
                       VALUES (?, ?, ?, ?)""",
                    (qid, int(iid), item.get("state"),
                     json.dumps(item, default=str)),
                )

        tasks_block = state.get("tasks") or {}
        for task in tasks_block.get("items") or []:
            tid = task.get("id")
            if tid is None:
                continue
            c.execute(
                """INSERT INTO tasks (id, status, data_json)
                   VALUES (?, ?, ?)""",
                (int(tid), task.get("status"),
                 json.dumps(task, default=str)),
            )
        next_id = int(tasks_block.get("next_id") or 1)
        c.execute("UPDATE tasks_meta SET next_id = ?", (next_id,))

        settings = state.get("settings") or {}
        for k, v in (settings.get("global") or {}).items():
            c.execute(
                """INSERT INTO settings (scope, key, value_json)
                   VALUES (?, ?, ?)""",
                ("global", k, json.dumps(v, default=str)),
            )
        for qid, kv in (settings.get("queues") or {}).items():
            for k, v in (kv or {}).items():
                c.execute(
                    """INSERT INTO settings (scope, key, value_json)
                       VALUES (?, ?, ?)""",
                    (f"queue:{qid}", k, json.dumps(v, default=str)),
                )
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise


# ============================================================== state IO

def load_state_dict() -> dict:
    """Read the entire queue + task + settings state from SQL into
    the same dict shape `queues.load_state()` previously returned
    from queues.json. Callers downstream of `queues.load_state` see
    no structural change.
    """
    state: dict = {"queues": {}}
    with _LOCK:
        c = conn()
        for r in c.execute(
            "SELECT queue_id, data_json FROM queue_items"
        ).fetchall():
            try:
                item = json.loads(r["data_json"])
            except json.JSONDecodeError:
                continue
            bucket = state["queues"].setdefault(
                r["queue_id"], {"items": []})
            bucket["items"].append(item)

        task_rows = c.execute(
            "SELECT data_json FROM tasks ORDER BY id"
        ).fetchall()
        next_id_row = c.execute(
            "SELECT next_id FROM tasks_meta"
        ).fetchone()
        state["tasks"] = {
            "items": [json.loads(r["data_json"]) for r in task_rows],
            "next_id": int(next_id_row["next_id"]) if next_id_row else 1,
        }

        settings_rows = c.execute(
            "SELECT scope, key, value_json FROM settings"
        ).fetchall()
    settings: dict = {"global": {}, "queues": {}}
    for r in settings_rows:
        try:
            v = json.loads(r["value_json"])
        except json.JSONDecodeError:
            continue
        scope = r["scope"]
        if scope == "global":
            settings["global"][r["key"]] = v
        elif scope.startswith("queue:"):
            qid = scope[len("queue:"):]
            settings["queues"].setdefault(qid, {})[r["key"]] = v
    state["settings"] = settings
    return state


def flush_state_dict(state: dict) -> None:
    """Write the full state dict back to SQL in a single transaction.
    Same write semantics as the legacy "rewrite the JSON file" path,
    just atomic — partial writes can't happen, so no more
    `.corrupt-` quarantined files."""
    with _LOCK:
        _flush_state_to_conn(conn(), state)


# ============================================================ audit log
# actions_log + state_transitions are append-only — every write is a
# single INSERT. No mutation, no deletion. The append-only shape is
# the whole point: it's the durable record of what happened, when.

def record_action_event(*,
                        queue_id: Optional[str] = None,
                        item_id: Any = None,
                        action_id: Optional[str] = None,
                        status: Optional[str] = None,
                        message: Optional[str] = None,
                        session_id: Optional[str] = None,
                        meta: Optional[dict] = None) -> None:
    """Append one row to actions_log. Called from every place that
    currently writes `last_result` so we keep the full history of
    statuses, not just the latest."""
    meta_json = (json.dumps(meta, default=str)
                 if meta is not None else None)
    _safe_exec(
        """INSERT INTO actions_log
           (ts, queue_id, item_id, action_id, status, message,
            session_id, meta_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (_now(), queue_id,
         int(item_id) if item_id is not None else None,
         action_id, status, message, session_id, meta_json),
    )


def record_state_transition(*,
                            queue_id: str,
                            item_id: Any,
                            from_state: Optional[str],
                            to_state: str,
                            reason: Optional[str] = None) -> None:
    """Append one row to state_transitions. Called from set_item_state
    after the mutation when the state actually changed."""
    if item_id is None:
        return
    _safe_exec(
        """INSERT INTO state_transitions
           (ts, queue_id, item_id, from_state, to_state, reason)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (_now(), queue_id, int(item_id), from_state, to_state, reason),
    )


def actions_for_item(queue_id: str, item_id: Any,
                     limit: int = 50) -> list[dict]:
    """Recent actions on a specific item, newest first. Used by the
    drawer's history pane (Phase 4 UI work)."""
    try:
        with _LOCK:
            rows = conn().execute(
                """SELECT ts, action_id, status, message, session_id
                   FROM actions_log
                   WHERE queue_id = ? AND item_id = ?
                   ORDER BY id DESC LIMIT ?""",
                (queue_id,
                 int(item_id) if item_id is not None else None,
                 limit),
            ).fetchall()
    except sqlite3.Error as exc:
        print(f"[db] actions_for_item failed: {exc}")
        return []
    return [dict(r) for r in rows]


def transitions_for_item(queue_id: str, item_id: Any,
                         limit: int = 50) -> list[dict]:
    """State transition history for one item, oldest first so a
    timeline view reads naturally."""
    try:
        with _LOCK:
            rows = conn().execute(
                """SELECT ts, from_state, to_state, reason
                   FROM state_transitions
                   WHERE queue_id = ? AND item_id = ?
                   ORDER BY id LIMIT ?""",
                (queue_id,
                 int(item_id) if item_id is not None else None,
                 limit),
            ).fetchall()
    except sqlite3.Error as exc:
        print(f"[db] transitions_for_item failed: {exc}")
        return []
    return [dict(r) for r in rows]


def time_in_state_summary(queue_id: Optional[str] = None,
                          since_iso: Optional[str] = None) -> list[dict]:
    """For each (queue_id, from_state) pair, return the average time
    items spent in that state before transitioning out. Uses the
    LAG window function to look up each transition's predecessor.
    """
    where: list[str] = []
    params: list[Any] = []
    if queue_id:
        where.append("queue_id = ?")
        params.append(queue_id)
    if since_iso:
        where.append("ts >= ?")
        params.append(since_iso)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
    WITH ordered AS (
      SELECT
        queue_id, item_id, ts, from_state, to_state,
        LAG(ts) OVER (
          PARTITION BY queue_id, item_id ORDER BY id
        ) AS prev_ts,
        LAG(to_state) OVER (
          PARTITION BY queue_id, item_id ORDER BY id
        ) AS prev_to
      FROM state_transitions{where_sql}
    )
    SELECT
      queue_id,
      COALESCE(from_state, prev_to) AS state,
      COUNT(*) AS n,
      AVG(
        (julianday(ts) - julianday(prev_ts)) * 86400.0
      ) FILTER (WHERE prev_ts IS NOT NULL) AS avg_seconds
    FROM ordered
    GROUP BY queue_id, COALESCE(from_state, prev_to)
    ORDER BY queue_id, avg_seconds DESC
    """
    try:
        with _LOCK:
            rows = conn().execute(sql, params).fetchall()
    except sqlite3.Error as exc:
        print(f"[db] time_in_state_summary failed: {exc}")
        return []
    return [dict(r) for r in rows]


def recent_actions(limit: int = 50,
                   action_id: Optional[str] = None,
                   status: Optional[str] = None) -> list[dict]:
    """Recent rows in actions_log, newest first. Optional filter on
    action_id (e.g. only `attempt-fix` invocations) or status (e.g.
    only `error`)."""
    where: list[str] = []
    params: list[Any] = []
    if action_id:
        where.append("action_id = ?")
        params.append(action_id)
    if status:
        where.append("status = ?")
        params.append(status)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)
    try:
        with _LOCK:
            rows = conn().execute(
                f"""SELECT ts, queue_id, item_id, action_id, status,
                           message, session_id
                    FROM actions_log{where_sql}
                    ORDER BY id DESC LIMIT ?""",
                params,
            ).fetchall()
    except sqlite3.Error as exc:
        print(f"[db] recent_actions failed: {exc}")
        return []
    return [dict(r) for r in rows]


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
