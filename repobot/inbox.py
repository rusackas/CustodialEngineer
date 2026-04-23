"""Attention-ranked cross-queue stream for the inbox view.

Rank groups (top = most urgent):
  0  needs_human          — skill bailed, a human call is required
  1  interrupted          — server restart; click Continue or re-run
  2  error / unparsed     — something broke, user likely needs to look
  3  triage verdict ready — has proposal, awaiting the user's click
  4  idle action session  — completed first turn, can follow up
  5  running action       — in flight, just watch
  6  triaging             — no proposal yet, spinner visible
  7  awaiting update      — parked
  8  done                 — terminal (hidden by default)

Within each group, most-recently-updated first — so the freshest item
in each rank floats to the top of that rank.
"""
from typing import Iterable


# Sub-grouping within each state column: every card falls into one of
# these buckets based on its attention rank. Used by _queue_body.html
# to render collapsible sub-sections inside each kanban column.
BUCKETS = [
    # (bucket_key, label, covers_ranks)
    ("attention", "needs attention", {0, 1, 2, 3}),
    ("progress",  "in progress",     {4, 5, 6}),
    ("queue",     "in queue",        {7}),
    ("done",      "done",            {8}),
]
_RANK_TO_BUCKET = {rank: key for key, _, ranks in BUCKETS for rank in ranks}


def rank_bucket(item: dict, queue_cfg: dict) -> str:
    """Return a bucket key for the item — used to sub-group cards
    inside a state column."""
    return _RANK_TO_BUCKET.get(attention_rank(item, queue_cfg), "attention")


_RANK_NAMES = [
    "needs_human",
    "interrupted",
    "error",
    "verdict",
    "idle_session",
    "running",
    "triaging",
    "awaiting",
    "done",
]


def attention_rank(item: dict, queue_cfg: dict) -> int:
    """Return the rank group (0..8) for one item. See module docstring."""
    lr = item.get("last_result") or {}
    status = lr.get("status")
    state = item.get("state")
    done_state = queue_cfg.get("done_state", "done")
    awaiting_state = queue_cfg.get("awaiting_state")
    initial_state = queue_cfg.get("initial_state")

    if state == done_state:
        return 8
    if status == "needs_human":
        return 0
    if status == "interrupted":
        return 1
    if status in ("error", "unparsed"):
        return 2
    if state == awaiting_state:
        return 7
    if status in ("running", "starting"):
        return 5
    if status == "queued":
        return 6
    # No last_result but has a proposal: verdict ready, waiting on click.
    if item.get("proposal"):
        return 3
    # No last_result, no proposal: still triaging.
    if state == initial_state or (
        isinstance(initial_state, list) and state in initial_state
    ):
        return 6
    # Fallback: verdict-ready bucket if we can't place it.
    return 3


def _updated_at(item: dict) -> str:
    raw = item.get("raw") or {}
    return (item.get("last_result_at") or raw.get("updatedAt")
            or item.get("state_changed_at") or "")


def attention_stream(
    queues_cfg: list[dict],
    state: dict,
    *,
    include_done: bool = False,
    queue_ids: Iterable[str] | None = None,
    rank_names: Iterable[str] | None = None,
) -> list[dict]:
    """Flatten all items across queues into one list, sorted by
    attention rank + recency. Each entry is annotated with `_rank`,
    `_rank_name`, and `_queue_id` for rendering.

    Filters:
    - `queue_ids` limits the set of queues considered
    - `rank_names` limits to specific rank buckets (e.g. {'verdict'})
    - `include_done` adds the `done` rank to the output (hidden by default)
    """
    by_id = {q["id"]: q for q in queues_cfg}
    selected_queues = set(queue_ids) if queue_ids else set(by_id)
    selected_ranks = (
        {_RANK_NAMES.index(n) for n in rank_names if n in _RANK_NAMES}
        if rank_names else None
    )

    out = []
    for qid, bucket in state.get("queues", {}).items():
        if qid not in selected_queues:
            continue
        qcfg = by_id.get(qid, {})
        for item in bucket.get("items", []):
            rank = attention_rank(item, qcfg)
            if not include_done and rank == 8:
                continue
            if selected_ranks is not None and rank not in selected_ranks:
                continue
            annotated = dict(item)
            annotated["_rank"] = rank
            annotated["_rank_name"] = _RANK_NAMES[rank]
            annotated["_queue_id"] = qid
            annotated["_queue_title"] = qcfg.get("title", qid)
            annotated["_updated_at"] = _updated_at(item)
            out.append(annotated)

    # Stable sort: first by rank ascending (0 = most urgent at top),
    # then by updated_at descending (newest first within each rank).
    out.sort(key=lambda it: it["_updated_at"], reverse=True)
    out.sort(key=lambda it: it["_rank"])
    return out
