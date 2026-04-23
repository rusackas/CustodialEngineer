"""Queue state storage — persisted as JSON in state/queues.json.

All reads/writes go through a single lock so background worker threads
can safely update item state while the web handler reads it.
"""
import json
import os
import threading
from datetime import datetime, timezone
from typing import Iterable

from .config import PROJECT_ROOT, load_config

STATE_PATH = PROJECT_ROOT / "state" / "queues.json"
_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(state: dict) -> None:
    """Write state to a tmp file then rename — a crash mid-write leaves
    the original file untouched instead of truncating it."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE_PATH)


def load_state() -> dict:
    with _LOCK:
        if not STATE_PATH.exists():
            return {"queues": {}}
        with open(STATE_PATH) as f:
            return json.load(f)


def save_state(state: dict) -> None:
    with _LOCK:
        _atomic_write(state)


def _mutate(mutator):
    """Read-modify-write under lock."""
    with _LOCK:
        state = json.load(open(STATE_PATH)) if STATE_PATH.exists() else {"queues": {}}
        mutator(state)
        _atomic_write(state)
        return state


def get_queues_config() -> list[dict]:
    return load_config().get("queues", []) or []


def get_queue_config(queue_id: str) -> dict:
    for q in get_queues_config():
        if q["id"] == queue_id:
            return q
    raise KeyError(f"Unknown queue: {queue_id}")


def queue_items(state: dict, queue_id: str) -> list[dict]:
    return state.get("queues", {}).get(queue_id, {}).get("items", [])


def find_item(state: dict, queue_id: str, item_id) -> dict | None:
    for item in queue_items(state, queue_id):
        if item["id"] == item_id:
            return item
    return None


def count_non_done(state: dict, queue_id: str, done_state: str = "done",
                   awaiting_state: str | None = None) -> int:
    """Count items that occupy a "card slot" in the column. Done and
    awaiting-update items don't count — done is a terminal cache,
    awaiting may pile up indefinitely waiting on external input."""
    excluded = {done_state}
    if awaiting_state:
        excluded.add(awaiting_state)
    return sum(1 for i in queue_items(state, queue_id)
               if i.get("state") not in excluded)


def upsert_items(queue_id: str, new_items: Iterable[dict], initial_state: str) -> dict:
    def _m(state):
        bucket = state["queues"].setdefault(queue_id, {"items": []})
        by_id = {item["id"]: item for item in bucket["items"]}
        for incoming in new_items:
            item = by_id.get(incoming["id"])
            if item is None:
                incoming.setdefault("state", initial_state)
                incoming.setdefault("fetched_at", _now())
                bucket["items"].append(incoming)
            else:
                item["title"] = incoming.get("title", item.get("title"))
                item["raw"] = incoming.get("raw", item.get("raw"))
    return _mutate(_m)


def set_triage(queue_id: str, item_id, proposal: str, actions: list[str],
               extra: dict | None = None) -> None:
    def _m(state):
        for item in queue_items(state, queue_id):
            if item["id"] == item_id:
                item["proposal"] = proposal
                item["actions"] = actions
                item["triaged_at"] = _now()
                if extra:
                    for k, v in extra.items():
                        item[k] = v
                break
    _mutate(_m)


def set_item_state(queue_id: str, item_id, new_state: str) -> None:
    def _m(state):
        for item in queue_items(state, queue_id):
            if item["id"] == item_id:
                item["state"] = new_state
                item["state_changed_at"] = _now()
                break
    _mutate(_m)


def delete_item(queue_id: str, item_id) -> None:
    def _m(state):
        bucket = state.get("queues", {}).get(queue_id)
        if bucket is None:
            return
        bucket["items"] = [i for i in bucket["items"] if i["id"] != item_id]
    _mutate(_m)


def set_item_parked_at(queue_id: str, item_id, when: str | None) -> None:
    """Stamp (or clear) when an item was parked into `awaiting update`.
    Used to detect fresh activity on the PR — when its `updatedAt`
    passes `parked_at`, the card auto-demotes back to triage."""
    def _m(state):
        for item in queue_items(state, queue_id):
            if item["id"] == item_id:
                if when is None:
                    item.pop("parked_at", None)
                else:
                    item["parked_at"] = when
                break
    _mutate(_m)


def set_item_result(queue_id: str, item_id, result: dict) -> None:
    def _m(state):
        for item in queue_items(state, queue_id):
            if item["id"] == item_id:
                item["last_result"] = result
                item["last_result_at"] = _now()
                break
    _mutate(_m)


def add_item_tokens(queue_id: str, item_id, usage: dict) -> None:
    """Accumulate token usage onto an item's lifetime counter.
    Called once per turn-complete (ResultMessage) for sessions bound to
    an item, so per-card totals survive beyond the session's lifetime."""
    if not usage:
        return
    keys = ("input_tokens", "output_tokens",
            "cache_creation_input_tokens", "cache_read_input_tokens")

    def _m(state):
        for item in queue_items(state, queue_id):
            if item["id"] == item_id:
                tl = item.setdefault("tokens_lifetime", {})
                for k in keys:
                    v = usage.get(k)
                    if isinstance(v, (int, float)):
                        tl[k] = tl.get(k, 0) + int(v)
                break
    _mutate(_m)


def set_item_plan(queue_id: str, item_id, plan: dict | None) -> None:
    """Store (or clear) the proposed plan produced by the plan-pr-fix
    skill. Set to None to remove (e.g., when the user discards it)."""
    def _m(state):
        for item in queue_items(state, queue_id):
            if item["id"] == item_id:
                if plan is None:
                    item.pop("plan", None)
                else:
                    item["plan"] = plan
                break
    _mutate(_m)


def set_item_plan_status(queue_id: str, item_id, status: str | None) -> None:
    """Track where the plan is in its lifecycle: `proposed` → (user
    approves) → `executing` → `done`; or `discarded` if the user bails.
    None clears the field."""
    def _m(state):
        for item in queue_items(state, queue_id):
            if item["id"] == item_id:
                if status is None:
                    item.pop("plan_status", None)
                else:
                    item["plan_status"] = status
                break
    _mutate(_m)


def set_item_drafts(queue_id: str, item_id, drafts: dict | None) -> None:
    """Store (or clear) the proposed reply drafts produced by phase 1 of
    address-review-comments. None clears the field."""
    def _m(state):
        for item in queue_items(state, queue_id):
            if item["id"] == item_id:
                if drafts is None:
                    item.pop("drafts", None)
                else:
                    item["drafts"] = drafts
                break
    _mutate(_m)


def set_item_drafts_status(queue_id: str, item_id, status: str | None) -> None:
    """Lifecycle marker for drafts: `proposed` → `executing` → `done`; or
    `discarded` if the user bails. None clears."""
    def _m(state):
        for item in queue_items(state, queue_id):
            if item["id"] == item_id:
                if status is None:
                    item.pop("drafts_status", None)
                else:
                    item["drafts_status"] = status
                break
    _mutate(_m)


def set_item_assessment(queue_id: str, item_id, assessment: dict | None) -> None:
    """Store (or clear) the worktree-based PR assessment produced by
    `assess-pr-on-worktree`. Rendered as a pane on the card; doesn't move
    the card's state. None clears."""
    def _m(state):
        for item in queue_items(state, queue_id):
            if item["id"] == item_id:
                if assessment is None:
                    item.pop("assessment", None)
                else:
                    item["assessment"] = assessment
                break
    _mutate(_m)


def set_item_diff_summary(queue_id: str, item_id, summary: dict | None) -> None:
    """Store (or clear) the 3-bullet diff summary produced by
    `summarize-pr-diff`. Pure read-aid: rendered as a pane, no state
    change. None clears."""
    def _m(state):
        for item in queue_items(state, queue_id):
            if item["id"] == item_id:
                if summary is None:
                    item.pop("diff_summary", None)
                else:
                    item["diff_summary"] = summary
                break
    _mutate(_m)


def set_item_session_id(queue_id: str, item_id, session_id: str | None,
                        kind: str = "action") -> None:
    """Record the Claude session id on an item so the UI can open it."""
    key = "triage_session_id" if kind == "triage" else "session_id"
    def _m(state):
        for item in queue_items(state, queue_id):
            if item["id"] == item_id:
                if session_id is None:
                    item.pop(key, None)
                else:
                    item[key] = session_id
                break
    _mutate(_m)


# ---------- runtime settings (UI-editable overrides on top of config.yaml) ----------
#
# Defaults live in config.yaml. The UI writes overrides into state["settings"]
# so they persist across restarts without editing config. `get_*_setting`
# returns the override if present, else the config default.


def _settings(state: dict) -> dict:
    return state.setdefault("settings", {"global": {}, "queues": {}})


def get_global_setting(key: str, default):
    state = load_state()
    return state.get("settings", {}).get("global", {}).get(key, default)


def get_queue_setting(queue_id: str, key: str, default):
    """Per-queue override, falling back to the supplied default (normally
    the value from config.yaml)."""
    state = load_state()
    return (state.get("settings", {}).get("queues", {})
            .get(queue_id, {}).get(key, default))


def update_global_setting(key: str, value) -> None:
    def _m(state):
        s = _settings(state)
        s.setdefault("global", {})[key] = value
    _mutate(_m)


def update_queue_setting(queue_id: str, key: str, value) -> None:
    def _m(state):
        s = _settings(state)
        s.setdefault("queues", {}).setdefault(queue_id, {})[key] = value
    _mutate(_m)
