"""Claude Agent SDK session management.

Every non-skip action and every skill-driven triage runs as a session
here. Sessions are:

- **Long-lived**: after the initial skill prompt finishes, the session
  stays idle for up to IDLE_TIMEOUT_SEC so the user can open the modal
  and send follow-up messages.
- **Inspectable**: the full transcript (user/assistant/system turns) is
  kept in memory and served as JSON to the UI for polling.
- **Bidirectional**: user follow-ups are delivered to the live
  ClaudeSDKClient via an asyncio.Queue and appended to the transcript.

## Threading model

All session coroutines run in a dedicated asyncio loop on a daemon
thread (`sessions-loop`). Callers from any thread create / interact with
sessions via the sync helpers here; FastAPI async handlers bridge in
via `asyncio.wrap_future(asyncio.run_coroutine_threadsafe(...))`.

The claude-agent-sdk caveat ("cannot use a ClaudeSDKClient across
different async runtime contexts") is respected — each client is
created, used, and closed inside a single coroutine running in the
session loop.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from .config import PROJECT_ROOT, load_config

SKILLS_DIR = PROJECT_ROOT / ".claude" / "skills"
IDLE_TIMEOUT_SEC = 30 * 60
DEFAULT_MAX_CONCURRENT = 4


def _max_concurrent() -> int:
    """Effective session cap. Reads the UI override from state first; falls
    back to config.yaml's `sessions.max_concurrent`, then DEFAULT."""
    cfg_default = DEFAULT_MAX_CONCURRENT
    try:
        cfg_default = int((load_config().get("sessions") or {})
                          .get("max_concurrent", DEFAULT_MAX_CONCURRENT))
    except Exception:
        pass
    try:
        from .queues import get_global_setting
        override = get_global_setting("max_concurrent", None)
        if override is not None:
            return max(1, int(override))
    except Exception:
        pass
    return cfg_default


def resize_semaphore(new_cap: int) -> int:
    """Resize the live session semaphore. Called after the user edits the
    global cap in the UI so the change takes effect without a restart.

    Semaphores don't natively resize, but the counter is just an int —
    we can bump it up by `release()`-ing the delta, or shrink it by
    `acquire_nowait()`-ing. If we can't shrink immediately (all slots in
    use), that's fine: existing holders won't be pre-empted, but new
    acquires will see the smaller cap once holders release.
    Returns the delta actually applied.
    """
    loop = _ensure_loop()
    sem = _session_semaphore
    if sem is None:
        return 0
    new_cap = max(1, int(new_cap))

    async def _apply() -> int:
        # asyncio.Semaphore exposes the counter as `_value` — undocumented
        # but stable across Python versions we support. If that ever
        # changes, fall back to "no live resize, restart to apply".
        current = getattr(sem, "_value", None)
        if current is None:
            return 0
        delta = new_cap - current
        if delta > 0:
            for _ in range(delta):
                sem.release()
        elif delta < 0:
            for _ in range(-delta):
                if not sem.locked() and getattr(sem, "_value", 0) > 0:
                    try:
                        await asyncio.wait_for(sem.acquire(), timeout=0)
                    except asyncio.TimeoutError:
                        break
        return delta

    fut = asyncio.run_coroutine_threadsafe(_apply(), loop)
    try:
        return fut.result(timeout=2.0)
    except Exception:
        return 0

_API_KEY_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
)


def _oauth_env() -> dict[str, str]:
    env = dict(os.environ)
    for k in _API_KEY_VARS:
        env.pop(k, None)
    return env


DEFAULT_MAX_TURNS = 40


def load_skill(name: str) -> str:
    path = SKILLS_DIR / name / "SKILL.md"
    text = path.read_text()
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            return text[end + 4:].lstrip()
    return text


def _skill_frontmatter(name: str) -> dict:
    path = SKILLS_DIR / name / "SKILL.md"
    text = path.read_text()
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end == -1:
        return {}
    fm: dict = {}
    for line in text[4:end].splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        fm[k.strip()] = v.strip()
    return fm


def _skill_max_turns(name: str) -> int:
    try:
        raw = _skill_frontmatter(name).get("max_turns")
        return int(raw) if raw else DEFAULT_MAX_TURNS
    except Exception:
        return DEFAULT_MAX_TURNS


_JSON_FENCE_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_result(text: str) -> dict:
    match = _JSON_FENCE_RE.search(text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    return {
        "status": "unparsed",
        "message": "Session produced no JSON block.",
        "raw_tail": (text or "")[-500:],
    }


# ---------- session state ----------


@dataclass
class SessionState:
    session_id: str
    skill: str
    context: dict
    cwd: str
    kind: str  # "action" | "triage"
    queue_id: Optional[str] = None
    item_id: Optional[Any] = None
    action_id: Optional[str] = None
    status: str = "starting"  # starting | running | idle | closed | error
    transcript: list[dict] = field(default_factory=list)
    final_result: Optional[dict] = None
    sdk_session_id: Optional[str] = None  # Claude SDK's session id, for resume
    resumed_from: Optional[str] = None  # our session_id that this resumes
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    # Cumulative token usage from ResultMessage.usage (input / output /
    # cache_creation / cache_read). Kept as a plain dict so we can sum
    # whatever keys the SDK hands us.
    tokens: dict = field(default_factory=dict)
    # Snapshot of the most recent client.get_context_usage() — only the
    # fields we surface in the UI (totalTokens, maxTokens, percentage).
    context_usage: Optional[dict] = None
    on_first_turn_complete: Optional[Callable[["SessionState"], None]] = None
    on_started: Optional[Callable[["SessionState"], None]] = None
    on_close: Optional[Callable[["SessionState"], None]] = None
    # Fires after every turn (including the first). Receives the parsed
    # JSON result dict for that turn. Used by multi-turn flows like
    # plan-fix where a later turn finalizes the item.
    on_turn_complete: Optional[Callable[["SessionState", dict], None]] = None
    _user_queue: Any = None
    _first_turn_done: Any = None


SESSIONS: dict[str, SessionState] = {}
_SESSIONS_LOCK = threading.Lock()

# Unique sentinel pushed into a session's _user_queue to ask it to shut
# down at its next idle check (e.g. when its owning card is deleted).
_ABORT_SENTINEL = object()

# Per-turn token events — (timestamp, usage_dict). Used to compute a
# rolling 24-hour token total without keeping dead-session state forever.
# Trimmed opportunistically when new events land.
_TOKEN_EVENTS: list[tuple[float, dict]] = []
_TOKEN_EVENTS_LOCK = threading.Lock()
_TOKEN_WINDOW_SEC = 24 * 3600


def _record_token_event(usage: Optional[dict]) -> None:
    if not usage:
        return
    keys = ("input_tokens", "output_tokens",
            "cache_creation_input_tokens", "cache_read_input_tokens")
    snap = {k: int(usage.get(k) or 0) for k in keys}
    if not any(snap.values()):
        return
    now = time.time()
    with _TOKEN_EVENTS_LOCK:
        _TOKEN_EVENTS.append((now, snap))
        cutoff = now - _TOKEN_WINDOW_SEC
        # Cheap amortized trim: events are append-ordered by time.
        idx = 0
        for idx, (ts, _) in enumerate(_TOKEN_EVENTS):
            if ts >= cutoff:
                break
        else:
            idx = len(_TOKEN_EVENTS)
        if idx:
            del _TOKEN_EVENTS[:idx]


def _tokens_in_last(window_sec: float) -> dict:
    cutoff = time.time() - window_sec
    totals: dict = {}
    with _TOKEN_EVENTS_LOCK:
        for ts, snap in _TOKEN_EVENTS:
            if ts < cutoff:
                continue
            for k, v in snap.items():
                totals[k] = totals.get(k, 0) + v
    return totals


# ---------- loop management ----------


_session_loop: Optional[asyncio.AbstractEventLoop] = None
_session_loop_thread: Optional[threading.Thread] = None
_session_semaphore: Optional[asyncio.Semaphore] = None
_LOOP_LOCK = threading.Lock()


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _session_loop, _session_loop_thread, _session_semaphore
    with _LOOP_LOCK:
        if _session_loop is None:
            loop = asyncio.new_event_loop()

            async def _init_loop_state():
                # Semaphore must be constructed on the loop that will use it.
                global _session_semaphore
                _session_semaphore = asyncio.Semaphore(_max_concurrent())

            def _run_loop():
                asyncio.set_event_loop(loop)
                loop.run_until_complete(_init_loop_state())
                loop.run_forever()

            t = threading.Thread(target=_run_loop, daemon=True, name="sessions-loop")
            t.start()
            _session_loop = loop
            _session_loop_thread = t
            # Wait briefly for the semaphore to be created before returning.
            deadline = time.time() + 2.0
            while _session_semaphore is None and time.time() < deadline:
                time.sleep(0.01)
    return _session_loop


# ---------- public sync API ----------


def start_session(
    skill: str,
    context: dict,
    cwd: str,
    *,
    kind: str = "action",
    queue_id: Optional[str] = None,
    item_id: Optional[Any] = None,
    action_id: Optional[str] = None,
    on_first_turn_complete: Optional[Callable[[SessionState], None]] = None,
    on_started: Optional[Callable[[SessionState], None]] = None,
    on_close: Optional[Callable[[SessionState], None]] = None,
    on_turn_complete: Optional[Callable[[SessionState, dict], None]] = None,
    sdk_resume: Optional[str] = None,
    initial_user_message: Optional[str] = None,
) -> str:
    """Start a session and return its id immediately. Safe from any thread.
    The session goes through `queued` → `starting` as it waits for a slot in
    the concurrency semaphore, then `running` / `idle` once it's live.

    `sdk_resume` resumes a previous SDK session (preserves its memory);
    when set, the skill body is NOT re-sent. Pass `initial_user_message`
    to send a nudge as the first turn of the resumed conversation."""
    loop = _ensure_loop()
    state = SessionState(
        session_id=uuid.uuid4().hex,
        skill=skill,
        context=context,
        cwd=cwd,
        kind=kind,
        queue_id=queue_id,
        item_id=item_id,
        action_id=action_id,
        status="queued",
        on_first_turn_complete=on_first_turn_complete,
        on_started=on_started,
        on_close=on_close,
        on_turn_complete=on_turn_complete,
    )
    if sdk_resume:
        state.resumed_from = sdk_resume
    with _SESSIONS_LOCK:
        SESSIONS[state.session_id] = state
    asyncio.run_coroutine_threadsafe(
        _run_session(state, sdk_resume=sdk_resume,
                     initial_user_message=initial_user_message), loop,
    )
    return state.session_id


def resume_session(old_session_id: str) -> Optional[str]:
    """Resume a closed session by starting a new client with the SDK's
    resume option. Transcript from the old session is copied forward so the
    UI sees continuity. Returns the new session_id, or None if the old
    session is missing / not resumable."""
    old = SESSIONS.get(old_session_id)
    if old is None or not old.sdk_session_id:
        return None

    loop = _ensure_loop()
    new = SessionState(
        session_id=uuid.uuid4().hex,
        skill=old.skill,
        context=old.context,
        cwd=old.cwd,
        kind=old.kind,
        queue_id=old.queue_id,
        item_id=old.item_id,
        action_id=old.action_id,
        status="queued",
        resumed_from=old_session_id,
    )
    new.transcript = list(old.transcript)
    new.transcript.append({
        "role": "system",
        "text": f"— Resumed from earlier session {old_session_id[:8]} —",
        "ts": time.time(),
    })
    with _SESSIONS_LOCK:
        SESSIONS[new.session_id] = new
    asyncio.run_coroutine_threadsafe(
        _run_session(new, sdk_resume=old.sdk_session_id), loop,
    )
    # Point the card's chat button at the new session so reopening the
    # modal later lands on the resumed conversation.
    if new.queue_id and new.item_id is not None:
        try:
            from .queues import set_item_session_id
            set_item_session_id(new.queue_id, new.item_id,
                                new.session_id, kind=new.kind)
        except Exception:
            pass
    return new.session_id


def run_session_blocking(
    skill: str,
    context: dict,
    cwd: str,
    *,
    kind: str = "triage",
    queue_id: Optional[str] = None,
    item_id: Optional[Any] = None,
) -> tuple[str, dict]:
    """Start a session; block until the first turn completes.
    Returns (session_id, final_result). The session keeps running after
    this returns (user can open it in the UI and send follow-ups).
    """
    sid = start_session(skill, context, cwd, kind=kind, queue_id=queue_id, item_id=item_id)
    state = SESSIONS[sid]
    loop = _ensure_loop()

    async def _wait() -> None:
        # _first_turn_done is created inside _run_session; wait briefly for it.
        for _ in range(200):
            if state._first_turn_done is not None:
                break
            await asyncio.sleep(0.05)
        if state._first_turn_done is not None:
            await state._first_turn_done.wait()

    fut = asyncio.run_coroutine_threadsafe(_wait(), loop)
    fut.result()
    return sid, dict(state.final_result or {})


def get_snapshot(session_id: str) -> Optional[dict]:
    state = SESSIONS.get(session_id)
    if state is None:
        return None
    return {
        "session_id": state.session_id,
        "status": state.status,
        "skill": state.skill,
        "kind": state.kind,
        "queue_id": state.queue_id,
        "item_id": state.item_id,
        "action_id": state.action_id,
        "transcript": list(state.transcript),
        "final_result": state.final_result,
        "created_at": state.created_at,
        "finished_at": state.finished_at,
        "resumable": bool(state.sdk_session_id),
        "resumed_from": state.resumed_from,
        "tokens": dict(state.tokens),
        "context_usage": state.context_usage,
    }


def list_sessions() -> list[dict]:
    with _SESSIONS_LOCK:
        return [
            {
                "session_id": s.session_id,
                "status": s.status,
                "kind": s.kind,
                "skill": s.skill,
                "queue_id": s.queue_id,
                "item_id": s.item_id,
                "action_id": s.action_id,
                "tokens": dict(s.tokens),
                "context_usage": s.context_usage,
            }
            for s in SESSIONS.values()
        ]


def stats() -> dict:
    """Aggregate snapshot for the header bar.

    - `working`: sessions actively holding a slot (starting / running).
      Idle sessions release their slot — they're parked waiting for a
      follow-up but not using resources.
    - `queued`: sessions waiting for a slot (status queued).
    - `idle`: sessions with first turn done, waiting for follow-up.
    - `active`: working + queued, for legacy callers.
    """
    working_states = {"starting", "running"}
    with _SESSIONS_LOCK:
        by_status: dict[str, int] = {}
        tokens_total: dict[str, int] = {}
        live_sessions = []
        for s in SESSIONS.values():
            by_status[s.status] = by_status.get(s.status, 0) + 1
            for k, v in s.tokens.items():
                tokens_total[k] = tokens_total.get(k, 0) + int(v)
            if s.status not in ("closed", "closing", "error"):
                live_sessions.append({
                    "session_id": s.session_id,
                    "kind": s.kind,
                    "skill": s.skill,
                    "queue_id": s.queue_id,
                    "item_id": s.item_id,
                    "status": s.status,
                    "context_percentage": (s.context_usage or {}).get("percentage"),
                    "tokens": dict(s.tokens),
                })
    working = sum(c for st, c in by_status.items() if st in working_states)
    queued = by_status.get("queued", 0)
    idle = by_status.get("idle", 0)
    try:
        from . import worktree as _wt
        worktrees = len(_wt.existing_worktree_numbers())
    except Exception:
        worktrees = 0
    return {
        "total": sum(by_status.values()),
        "active": working + queued,
        "working": working,
        "queued": queued,
        "idle": idle,
        "cap": _max_concurrent(),
        "by_status": by_status,
        "tokens_total": tokens_total,
        "tokens_24h": _tokens_in_last(_TOKEN_WINDOW_SEC),
        "live": live_sessions,
        "worktrees": worktrees,
    }


# ---------- async API (for FastAPI handlers) ----------


async def send_user_message(session_id: str, text: str) -> bool:
    """Deliver a user follow-up to a live session."""
    state = SESSIONS.get(session_id)
    if state is None or state.status in ("closed", "closing", "error"):
        return False
    if state._user_queue is None:
        return False
    loop = _ensure_loop()
    cf = asyncio.run_coroutine_threadsafe(state._user_queue.put(text), loop)
    await asyncio.wrap_future(cf)
    return True


def abort_sessions_for_item(queue_id: str, item_id,
                            kind: Optional[str] = None) -> int:
    """Signal every live session bound to this item to terminate at its
    next idle check. Best-effort: a session mid-turn will finish that
    turn first, then see the sentinel and exit cleanly. Safe from any
    thread. Returns the number of sessions signaled. `kind` narrows to
    just `"triage"` or `"action"` — omit to signal both."""
    loop = _ensure_loop()
    targets = []
    with _SESSIONS_LOCK:
        for s in SESSIONS.values():
            if (s.queue_id == queue_id and s.item_id == item_id
                    and s.status not in ("closed", "closing", "error")
                    and s._user_queue is not None
                    and (kind is None or s.kind == kind)):
                targets.append(s)
    for s in targets:
        # Flip status eagerly so callers checking "is anything live for
        # this item?" see the session as done immediately. The coroutine
        # will finish cleanup on its own when it picks up the sentinel.
        s.status = "closing"
        asyncio.run_coroutine_threadsafe(
            s._user_queue.put(_ABORT_SENTINEL), loop,
        )
    return len(targets)


# ---------- session runner (runs in the session loop) ----------


async def _run_session(state: SessionState,
                       sdk_resume: Optional[str] = None,
                       initial_user_message: Optional[str] = None) -> None:
    state._user_queue = asyncio.Queue()
    state._first_turn_done = asyncio.Event()

    sem = _session_semaphore
    acquired = False
    try:
        # Wait for a slot. While queued we stay in status='queued' so the
        # UI can show a dimmed / deferred card.
        state.status = "queued"
        _append(state, {"role": "system",
                        "text": f"Queued · waiting for session slot "
                                f"(cap {_max_concurrent()})"})
        if sem is not None:
            await sem.acquire()
            acquired = True

        state.status = "starting"
        if state.on_started:
            try:
                state.on_started(state)
            except Exception as exc:
                _append(state, {"role": "system",
                                "text": f"on_started hook errored: {exc}"})

        opts_kwargs = dict(
            cwd=state.cwd,
            env=_oauth_env(),
            allowed_tools=["Bash", "Read", "Grep", "Glob", "Edit", "Write"],
            permission_mode="bypassPermissions",
            max_turns=_skill_max_turns(state.skill),
            setting_sources=None,
        )
        if sdk_resume:
            opts_kwargs["resume"] = sdk_resume
        options = ClaudeAgentOptions(**opts_kwargs)

        async with ClaudeSDKClient(options=options) as client:
            if sdk_resume:
                _append(state, {"role": "system",
                                "text": f"Reconnected to session (resume={sdk_resume[:8]})."})
                if initial_user_message:
                    state.status = "running"
                    _append(state, {"role": "user", "text": initial_user_message})
                    await client.query(initial_user_message)
                    await _consume_turn(state, client, is_first=True)
                    if state.on_first_turn_complete:
                        try:
                            state.on_first_turn_complete(state)
                        except Exception as exc:
                            _append(state, {"role": "system",
                                            "text": f"on_first_turn_complete hook errored: {exc}"})
                else:
                    state.status = "idle"
                state._first_turn_done.set()
            else:
                skill_body = load_skill(state.skill)
                initial_prompt = (
                    f"{skill_body}\n\n"
                    f"## Runtime context\n\n"
                    f"```json\n{json.dumps(state.context, indent=2)}\n```\n\n"
                    "When you finish, print a single JSON object fenced as ```json ... ``` "
                    "matching the Output schema this Skill documents above.\n"
                )
                state.status = "running"
                _append(state, {"role": "system",
                                "text": f"Session started · skill `{state.skill}`"})
                _append(state, {"role": "user",
                                "text": f"[initial prompt for {state.skill}]"})

                await client.query(initial_prompt)
                await _consume_turn(state, client, is_first=True)

                if state.on_first_turn_complete:
                    try:
                        state.on_first_turn_complete(state)
                    except Exception as exc:
                        _append(state, {"role": "system",
                                        "text": f"on_first_turn_complete hook errored: {exc}"})
                state._first_turn_done.set()

            while True:
                # Release the concurrency slot while idle — idle sessions
                # don't use CPU/API, they're just parked waiting for a
                # user follow-up. A follow-up will re-acquire below.
                state.status = "idle"
                if acquired and sem is not None:
                    sem.release()
                    acquired = False
                try:
                    user_text = await asyncio.wait_for(
                        state._user_queue.get(), timeout=IDLE_TIMEOUT_SEC,
                    )
                except asyncio.TimeoutError:
                    _append(state, {"role": "system",
                                    "text": "Session closed (idle timeout)."})
                    break

                if user_text is _ABORT_SENTINEL:
                    _append(state, {"role": "system",
                                    "text": "Session aborted (card deleted)."})
                    break

                # User sent a follow-up — re-acquire a slot before running.
                state.status = "queued"
                if sem is not None:
                    await sem.acquire()
                    acquired = True
                state.status = "running"
                _append(state, {"role": "user", "text": user_text})
                await client.query(user_text)
                await _consume_turn(state, client, is_first=False)

    except Exception as exc:
        state.status = "error"
        _append(state, {"role": "system", "text": f"Session error: {exc}"})
        if state._first_turn_done is not None and not state._first_turn_done.is_set():
            state.final_result = {"status": "error", "message": str(exc)}
            state._first_turn_done.set()
    finally:
        if acquired and sem is not None:
            sem.release()
        state.status = "closed" if state.status != "error" else state.status
        state.finished_at = time.time()
        if state.on_close:
            try:
                state.on_close(state)
            except Exception:
                pass


def _summarize_tool_input(name: str, data: dict) -> str:
    """Produce a detailed tool-use summary. For Bash, show the full
    command (multi-line OK — the modal preserves whitespace). For other
    tools, prefer the most characteristic arg (file_path / pattern /
    url). Falls back to a compact key=value dump."""
    if not isinstance(data, dict):
        return ""
    # Bash: show the full command body; it's usually what you want to see.
    if name == "Bash":
        cmd = data.get("command")
        if isinstance(cmd, str) and cmd.strip():
            return cmd.strip()
    for key in ("file_path", "path", "pattern", "url", "query", "command"):
        val = data.get(key)
        if isinstance(val, str) and val:
            return val.strip()
    parts = []
    for k, v in data.items():
        sv = str(v)
        parts.append(f"{k}={sv[:80]}" + ("…" if len(sv) > 80 else ""))
        if sum(len(p) for p in parts) > 300:
            break
    return " ".join(parts)


def _flatten_tool_result(content) -> tuple[str, bool]:
    """ToolResultBlock.content is either a string or a list of content
    blocks (dicts with type='text'/'image'/...). Collapse to plain text."""
    if isinstance(content, str):
        return content, False
    if isinstance(content, list):
        chunks = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                chunks.append(part.get("text", ""))
            elif isinstance(part, dict):
                chunks.append(f"[{part.get('type', 'block')}]")
        return "\n".join(c for c in chunks if c), False
    return "", False


_TOOL_RESULT_MAX = 4000


def _accumulate_tokens(total: dict, usage: Optional[dict]) -> None:
    if not usage:
        return
    for key in ("input_tokens", "output_tokens",
                "cache_creation_input_tokens", "cache_read_input_tokens"):
        val = usage.get(key)
        if isinstance(val, (int, float)):
            total[key] = total.get(key, 0) + int(val)


async def _consume_turn(state: SessionState, client: ClaudeSDKClient,
                        *, is_first: bool) -> None:
    last_text = ""
    meta: dict = {}
    async for msg in client.receive_response():
        # SystemMessage (subtype=='init') fires at the very start of
        # each turn and carries the SDK session id. Capture it ASAP
        # so an interruption before first-turn-complete is still
        # resumable. For ACTION sessions, also persist it on
        # last_result.meta — the continue button and
        # auto-resume-on-boot read from there. Triage sessions track
        # their id separately via item.triage_session_id and do not
        # own last_result, so don't touch it for those.
        if isinstance(msg, SystemMessage):
            sid = (msg.data or {}).get("session_id")
            if sid and not state.sdk_session_id:
                state.sdk_session_id = sid
                if (state.kind == "action"
                        and state.queue_id
                        and state.item_id is not None):
                    try:
                        from .queues import find_item, load_state, set_item_result
                        cur = find_item(load_state(), state.queue_id, state.item_id) or {}
                        lr = cur.get("last_result")
                        if isinstance(lr, dict):
                            lr = dict(lr)
                            meta = dict(lr.get("meta") or {})
                            meta["session_id"] = sid
                            lr["meta"] = meta
                            set_item_result(state.queue_id, state.item_id, lr)
                    except Exception:
                        pass
            continue
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    last_text = block.text
                    _append(state, {"role": "assistant", "text": block.text})
                elif isinstance(block, ToolUseBlock):
                    _append(state, {
                        "role": "tool-use",
                        "tool": block.name,
                        "summary": _summarize_tool_input(block.name, block.input or {}),
                        "tool_use_id": block.id,
                    })
                elif isinstance(block, ThinkingBlock) and block.thinking.strip():
                    _append(state, {"role": "thinking", "text": block.thinking})
        elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
            # Tool results arrive wrapped in a UserMessage.
            for block in msg.content:
                if isinstance(block, ToolResultBlock):
                    text, _ = _flatten_tool_result(block.content)
                    truncated = text if len(text) <= _TOOL_RESULT_MAX \
                        else text[:_TOOL_RESULT_MAX] + f"\n… ({len(text) - _TOOL_RESULT_MAX} more chars)"
                    _append(state, {
                        "role": "tool-result",
                        "text": truncated,
                        "is_error": bool(block.is_error),
                        "tool_use_id": block.tool_use_id,
                    })
        elif isinstance(msg, ResultMessage):
            _accumulate_tokens(state.tokens, msg.usage)
            _record_token_event(msg.usage)
            if state.queue_id and state.item_id is not None and msg.usage:
                try:
                    from .queues import add_item_tokens
                    add_item_tokens(state.queue_id, state.item_id, msg.usage)
                except Exception:
                    pass
            meta = {
                "duration_ms": msg.duration_ms,
                "num_turns": msg.num_turns,
                "is_error": msg.is_error,
                "cost_usd": msg.total_cost_usd,
                "session_id": msg.session_id,
                "tokens": dict(state.tokens),
            }
            if msg.session_id:
                state.sdk_session_id = msg.session_id
            if msg.result and not last_text:
                last_text = msg.result
            _append(state, {
                "role": "system",
                "text": f"Turn complete · {msg.num_turns} turns · {msg.duration_ms}ms",
            })

    # After the turn, grab the current context window breakdown. It's a
    # best-effort snapshot — if the client is already torn down or the
    # call fails, we just skip.
    try:
        usage = await client.get_context_usage()
        state.context_usage = {
            "totalTokens": usage.get("totalTokens"),
            "maxTokens": usage.get("maxTokens"),
            "percentage": usage.get("percentage"),
            "model": usage.get("model"),
        }
    except Exception:
        pass

    parsed = _extract_result(last_text)
    parsed["meta"] = meta
    # Keep final_result as the latest turn's result. Multi-turn flows
    # like plan-fix need phase-2's execution result to land here; the
    # first-turn callback still reads state.final_result at turn 1.
    state.final_result = parsed
    if state.on_turn_complete:
        try:
            state.on_turn_complete(state, parsed)
        except Exception as exc:
            _append(state, {"role": "system",
                            "text": f"on_turn_complete hook errored: {exc}"})


def _append(state: SessionState, entry: dict) -> None:
    entry.setdefault("ts", time.time())
    state.transcript.append(entry)
