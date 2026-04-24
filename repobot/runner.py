"""Top-level orchestration: fetch items for a queue, then triage them.

Slot accounting uses non-done items only — a queue with max_in_flight=10
and 4 items still in-progress will pull 6 more on the next refresh.

Triage is now skill-driven (invokes the Claude Agent SDK), so each item
takes ~20s. We fan out triage across one thread per newly-added item.
CLI callers pass `wait_for_triage=True` so the command blocks until
done; the web endpoint runs `run_queue` itself in a background thread
and doesn't care.

`refresh_existing=True` additionally re-fetches each non-done item
and, when the PR's `updatedAt` is newer than our last triage, clears
the stored proposal so the normal triage pass picks it up again.
(GitHub bumps `updatedAt` on any meaningful change — new commit,
comment, review, CI result — so this is a cheap and accurate signal.)
Auto-refresh on a timer uses the default (False) — it just tops up
the hopper. The Update button uses True.
"""
import threading

from . import github, sessions
from .queues import (
    _mutate,
    _now,
    count_non_done,
    get_queue_config,
    get_queue_setting,
    load_state,
    queue_items,
    set_triage,
    upsert_items,
)
from .triage import (
    triage_dependabot_pr,
    triage_my_pr,
    triage_review_requested_pr,
)


_LIVE_TRIAGE_STATES = {"queued", "starting", "running", "idle"}


def _items_with_live_triage(queue_id: str) -> set:
    """IDs of items that already have a live triage session — used to
    prevent auto-refresh from spawning duplicate triage threads every
    tick (which previously caused the queued backlog to grow
    unboundedly when triage ran slower than the 30s refresh)."""
    live: set = set()
    with sessions._SESSIONS_LOCK:
        for s in sessions.SESSIONS.values():
            if (s.kind == "triage" and s.queue_id == queue_id
                    and s.item_id is not None
                    and s.status in _LIVE_TRIAGE_STATES):
                live.add(s.item_id)
    return live


FETCHERS = {
    "failing-dependabot-prs": lambda: github.fetch_dependabot_prs(limit=50),
    "my-prs": lambda: github.fetch_my_prs(limit=50),
    "review-requested": lambda: github.fetch_review_requested_prs(limit=50),
}

TRIAGERS = {
    "failing-dependabot-prs": triage_dependabot_pr,
    "my-prs": triage_my_pr,
    "review-requested": triage_review_requested_pr,
}


# Optional pre-triage bucketer: turns a fetched PR dict into the target
# state for intake. Used by queues like `review-requested` that have
# multiple pre-triage columns. Default (no bucketer) → queue's
# `initial_state`.
def _bucket_review_requested(pr: dict) -> str:
    if pr.get("has_conflicts"):
        return "triage: blocked"
    ci = (pr.get("ci_status") or "").lower()
    if ci == "failing":
        return "triage: blocked"
    mss = (pr.get("mergeStateStatus") or "").upper()
    if mss in ("DIRTY", "BLOCKED", "BEHIND"):
        return "triage: blocked"
    return "triage: mergeable"


BUCKETERS = {
    "review-requested": _bucket_review_requested,
}


def _initial_states(q: dict) -> list[str]:
    """Valid pre-triage states for a queue. Multi-bucket queues list
    `initial_states`; single-bucket ones use `initial_state`."""
    states = q.get("initial_states")
    if states:
        return list(states)
    return [q["initial_state"]]


def _pick_initial_state(queue_id: str, q: dict, pr: dict) -> str:
    bucketer = BUCKETERS.get(queue_id)
    if bucketer is not None:
        try:
            return bucketer(pr)
        except Exception:
            pass
    return q["initial_state"]


def _triage_one(queue_id: str, item: dict, triage) -> None:
    extra: dict = {}
    try:
        result = triage(item, queue_id=queue_id)
        if len(result) == 3:
            proposal, actions, extra = result
        else:
            proposal, actions = result
    except Exception as exc:
        proposal = f"Triage failed: {exc}"
        actions = ["close", "prompt"]
        extra = {"triage_source": "error", "triage_error": str(exc)}
    set_triage(queue_id, item["id"], proposal, actions, extra=extra)


def _refresh_existing_items(queue_id: str, fetched: list[dict],
                            initial_state: str, done_state: str,
                            awaiting_state: str | None = None,
                            q: dict | None = None) -> None:
    """Update `raw` on all items from a fresh fetch and act on staleness:

    - Non-done items whose PR `updatedAt` is newer than our `triaged_at`
      get their proposal cleared so triage re-runs on the next pass.
    - Done items whose PR `updatedAt` is newer than our `triaged_at` get
      demoted back to `initial_state` (proposal cleared too) so the user
      sees them again — "done" is a cache, not a verdict.
    - Awaiting-update items whose PR `updatedAt` is newer than their
      `parked_at` auto-unpark back to `initial_state` — the external
      signal the user was waiting on has arrived.
    - Items mid-action (non-initial, non-done state) still get `raw`
      refreshed, but we don't clear their proposal; the user is already
      acting on it.
    """
    fetched_by_number = {pr["number"]: pr for pr in fetched}
    now = _now()
    demoted_ids: list = []

    def _m(state):
        bucket = state.get("queues", {}).get(queue_id)
        if not bucket:
            return
        for item in bucket.get("items", []):
            fresh = fetched_by_number.get(item.get("number"))
            if not fresh:
                continue
            item["raw"] = fresh
            updated_at = fresh.get("updatedAt")

            target_state = (_pick_initial_state(queue_id, q, fresh)
                            if q is not None else initial_state)

            if awaiting_state and item.get("state") == awaiting_state:
                parked_at = item.get("parked_at")
                if parked_at and updated_at and updated_at > parked_at:
                    item["state"] = target_state
                    item["state_changed_at"] = now
                    item.pop("parked_at", None)
                    item.pop("last_result", None)
                    item.pop("last_result_at", None)
                    item.pop("proposal", None)
                    item.pop("actions", None)
                    item.pop("triaged_at", None)
                    demoted_ids.append(item.get("id"))
                continue

            triaged_at = item.get("triaged_at")
            stale = bool(triaged_at and updated_at and updated_at > triaged_at)
            if not stale:
                continue
            if item.get("state") == done_state:
                item["state"] = target_state
                item["state_changed_at"] = now
                # Also drop last_result — a stale "skipped by user" on a
                # card being re-triaged is actively misleading.
                item.pop("last_result", None)
                item.pop("last_result_at", None)
                demoted_ids.append(item.get("id"))
            if item.get("proposal"):
                item.pop("proposal", None)
                item.pop("actions", None)
                item.pop("triaged_at", None)
    _mutate(_m)

    # Idle triage sessions from the pre-demote triage still count as
    # "live" in `_items_with_live_triage`, which blocks run_queue from
    # spawning fresh triage. Kill them so the next pass re-triages.
    for iid in demoted_ids:
        sessions.abort_sessions_for_item(queue_id, iid, kind="triage")


def refresh_one_item(queue_id: str, item_id) -> dict:
    """Per-card refresh: re-fetch this PR from GitHub, update `raw`, and
    if its `updatedAt` is newer than our `triaged_at`, clear the
    proposal so triage re-runs. Mirrors the column-level Update button's
    staleness logic but scoped to a single item. Done items that are
    stale get demoted back to the queue's initial_state. Mid-action
    items (non-initial, non-done) just get `raw` refreshed so the UI
    shows fresh CI state without stomping on work in flight.

    Returns a summary: {"stale": bool, "state": <post-refresh state>}.
    """
    q = get_queue_config(queue_id)
    initial_state = q["initial_state"]
    done_state = q.get("done_state", "done")
    awaiting_state = q.get("awaiting_state")

    state = load_state()
    item = None
    for i in queue_items(state, queue_id):
        if i.get("id") == item_id:
            item = i
            break
    if item is None:
        raise LookupError(f"Item {item_id} not in queue {queue_id}")

    number = item.get("number")
    if not number:
        raise RuntimeError("Item has no PR number to refresh.")

    # Prefer the item's stamped repo (for cross-repo queues it's
    # authoritative), fall back to the queue's configured repo.
    slug = github.item_repo_slug(item) or github.queue_repo_slug(q)
    with github.repo_scope(slug):
        fresh = github.fetch_one_pr(number)
    triager = TRIAGERS.get(queue_id)
    now = _now()
    result = {"stale": False, "state": item.get("state")}

    demoted = False

    def _m(state):
        nonlocal demoted
        bucket = state.get("queues", {}).get(queue_id)
        if not bucket:
            return
        for it in bucket.get("items", []):
            if it.get("id") != item_id:
                continue
            it["raw"] = fresh
            updated_at = fresh.get("updatedAt")

            target_state = _pick_initial_state(queue_id, q, fresh)

            # Awaiting-update unpark: the external signal landed.
            if awaiting_state and it.get("state") == awaiting_state:
                parked_at = it.get("parked_at")
                unpark = bool(parked_at and updated_at and updated_at > parked_at)
                result["stale"] = unpark
                if not unpark:
                    return
                it["state"] = target_state
                it["state_changed_at"] = now
                it.pop("parked_at", None)
                it.pop("last_result", None)
                it.pop("last_result_at", None)
                it.pop("proposal", None)
                it.pop("actions", None)
                it.pop("triaged_at", None)
                demoted = True
                result["state"] = target_state
                return

            triaged_at = it.get("triaged_at")
            stale = bool(triaged_at and updated_at and updated_at > triaged_at)
            result["stale"] = stale
            if not stale:
                return
            if it.get("state") == done_state:
                it["state"] = target_state
                it["state_changed_at"] = now
                it.pop("last_result", None)
                it.pop("last_result_at", None)
                demoted = True
            if it.get("proposal"):
                it.pop("proposal", None)
                it.pop("actions", None)
                it.pop("triaged_at", None)
            result["state"] = it.get("state")
            return
    _mutate(_m)

    if demoted:
        sessions.abort_sessions_for_item(queue_id, item_id, kind="triage")

    if result["stale"] and triager is not None:
        if item_id not in _items_with_live_triage(queue_id):
            refreshed = load_state()
            target = None
            for i in queue_items(refreshed, queue_id):
                if i.get("id") == item_id:
                    target = i
                    break
            if target is not None \
                    and target.get("state") in set(_initial_states(q)) \
                    and not target.get("proposal"):
                threading.Thread(
                    target=_triage_one, args=(queue_id, target, triager),
                    daemon=True,
                ).start()
    return result


def retriage_item(queue_id: str, item_id, wait: bool = False) -> None:
    """Force a fresh triage on a single item: aborts any in-flight or
    idle triage session, clears the existing verdict, and spawns a new
    triage thread. Used by the retriage button (user disagrees with
    triage, or it got stuck). The item stays in `initial_state` — no
    state transition is implied by retriage itself."""
    q = get_queue_config(queue_id)
    triage = TRIAGERS.get(queue_id)
    if triage is None:
        raise ValueError(f"No triager registered for queue: {queue_id}")

    sessions.abort_sessions_for_item(queue_id, item_id, kind="triage")

    def _m(state):
        bucket = state.get("queues", {}).get(queue_id)
        if not bucket:
            return
        for it in bucket.get("items", []):
            if it.get("id") != item_id:
                continue
            for k in ("proposal", "actions", "triaged_at", "triage_source",
                      "triage_notes", "last_result", "last_result_at",
                      "triage_session_id"):
                it.pop(k, None)
            it["state"] = _pick_initial_state(queue_id, q, it.get("raw") or {})
            it["state_changed_at"] = _now()
            return
    _mutate(_m)

    initial_state = q["initial_state"]  # kept for downstream references below

    refreshed = load_state()
    item = None
    for i in queue_items(refreshed, queue_id):
        if i.get("id") == item_id:
            item = i
            break
    if item is None:
        raise LookupError(f"Item {item_id} not in queue {queue_id}")
    t = threading.Thread(
        target=_triage_one, args=(queue_id, item, triage), daemon=True,
    )
    t.start()
    if wait:
        t.join()


def run_queue(queue_id: str, wait_for_triage: bool = False,
              refresh_existing: bool = False) -> dict:
    q = get_queue_config(queue_id)
    # UI can override `max_in_flight` (column card cap), `worker_slots`
    # (concurrent triage fan-out per tick), and `intake_paused` (freeze
    # new-card intake without stopping refresh/triage on existing ones).
    max_in_flight = int(get_queue_setting(
        queue_id, "max_in_flight", q["max_in_flight"]))
    worker_slots = int(get_queue_setting(
        queue_id, "worker_slots", q.get("worker_slots", max_in_flight)))
    intake_paused = bool(get_queue_setting(queue_id, "intake_paused", False))
    initial_state = q["initial_state"]
    done_state = q.get("done_state", "done")
    awaiting_state = q.get("awaiting_state")

    # Per-queue fetcher first (for queues that need bespoke hydration
    # like merge state or unresolved threads). Otherwise fall through
    # to the generic search-driven fetcher, which translates the
    # queue's `query:` block to a `gh pr list --search` invocation
    # — the same syntax you'd type into GitHub's search bar.
    # Per-queue fetcher first (for queues that need bespoke hydration
    # like merge state or unresolved threads). Otherwise fall through
    # to the generic search-driven fetcher, which translates the
    # queue's `query:` block to a `gh pr list --search` invocation
    # — the same syntax you'd type into GitHub's search bar.
    fetch = FETCHERS.get(queue_id)
    if fetch is None:
        query_block = q.get("query") or {}
        hydrate_block = q.get("hydrate") or {}
        filter_block = q.get("filter") or {}

        def fetch():
            return github.fetch_search(query_block,
                                       limit=max(50, max_in_flight),
                                       hydrate=hydrate_block,
                                       post_filter=filter_block)
    triage = TRIAGERS.get(queue_id)

    state = load_state()
    non_done = count_non_done(state, queue_id, done_state=done_state,
                              awaiting_state=awaiting_state)
    slots = 0 if intake_paused else max(0, max_in_flight - non_done)

    fetched: list[dict] | None = None
    if slots > 0 or refresh_existing:
        # Pin the repo slug for this queue's duration so every github
        # helper inside `fetch()` and its callees reads the correct
        # owner/name. The fetcher also stamps `raw.repo` on each PR,
        # so downstream per-item actions can find their way back.
        with github.repo_scope(github.queue_repo_slug(q)):
            fetched = fetch()

    if refresh_existing and fetched is not None:
        _refresh_existing_items(queue_id, fetched, initial_state,
                                done_state, awaiting_state, q=q)

    if slots > 0 and fetched is not None:
        existing_ids = {i["id"] for i in queue_items(load_state(), queue_id)}
        # Don't triage PRs with CI still in flight — wait until it
        # settles (pass or fail) so the triage skill has signal to act
        # on. `_refresh_existing_items` above still runs for pending
        # PRs, so cards already on the board stay up to date.
        settled = [pr for pr in fetched if pr.get("ci_status") != "pending"]
        fresh = [pr for pr in settled if pr["number"] not in existing_ids]
        to_add = fresh[:slots]

        # Group by per-item target state so multi-bucket queues
        # (review-requested) land items in the right pre-triage column.
        by_state: dict[str, list[dict]] = {}
        for pr in to_add:
            target = _pick_initial_state(queue_id, q, pr)
            by_state.setdefault(target, []).append({
                "id": pr["number"],
                "number": pr["number"],
                "title": pr["title"],
                "url": pr["url"],
                "raw": pr,
            })
        for state_name, items in by_state.items():
            upsert_items(queue_id, items, state_name)

    if triage is None:
        return load_state()["queues"].get(queue_id, {"items": []})

    state = load_state()
    already_triaging = _items_with_live_triage(queue_id)
    valid_initial_states = set(_initial_states(q))
    pending = [
        item for item in queue_items(state, queue_id)
        if item.get("state") in valid_initial_states
        and not item.get("proposal")
        and item["id"] not in already_triaging
    ]
    # Cap triage fan-out to worker_slots minus what's already in flight —
    # so a queue "deprioritized" to 2 slots won't stampede the semaphore
    # when 10 new cards land at once.
    free_slots = max(0, worker_slots - len(already_triaging))
    pending = pending[:free_slots]

    threads = [
        threading.Thread(
            target=_triage_one, args=(queue_id, item, triage), daemon=True,
        )
        for item in pending
    ]
    for t in threads:
        t.start()
    if wait_for_triage:
        for t in threads:
            t.join()

    return load_state()["queues"].get(queue_id, {"items": []})
