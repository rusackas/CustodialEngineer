"""Config loading + in-place edits.

`load_config()` uses PyYAML for the hot path (fast, every request). For
edits that need to round-trip back to disk without eating the file's
comments and structure, we use `ruamel.yaml` in round-trip mode.
"""
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


# Fields the UI is allowed to touch on a queue. Everything else is
# considered infrastructure (queue id, state machine, initial_state)
# and requires a config-file edit by hand. Keeps the editor tight and
# means accidental form submissions can't corrupt the core shape.
EDITABLE_QUEUE_FIELDS = {"title", "max_in_flight", "query"}
EDITABLE_QUERY_KEYS = {"author", "state", "review_requested", "labels",
                       "milestone", "head", "base", "assignee"}


def update_queue_definition(queue_id: str, updates: dict) -> dict:
    """Apply edits to a queue's definition in `config.yaml`, preserving
    comments and structure via `ruamel.yaml`. Returns the updated queue
    block. Raises KeyError if the queue doesn't exist, ValueError if
    updates touch fields outside the allowed set.
    """
    from ruamel.yaml import YAML

    bad = set(updates) - EDITABLE_QUEUE_FIELDS
    if bad:
        raise ValueError(
            f"not editable via UI: {', '.join(sorted(bad))} "
            f"(allowed: {', '.join(sorted(EDITABLE_QUEUE_FIELDS))})")

    query_updates = updates.get("query") or {}
    bad_query = set(query_updates) - EDITABLE_QUERY_KEYS
    if bad_query:
        raise ValueError(
            f"not editable query keys: {', '.join(sorted(bad_query))}")

    y = YAML()
    y.preserve_quotes = True
    y.width = 10_000   # don't reflow long lines
    y.indent(mapping=2, sequence=4, offset=2)
    with open(CONFIG_PATH) as f:
        doc = y.load(f)
    queues = doc.get("queues") or []
    target = next((q for q in queues if q.get("id") == queue_id), None)
    if target is None:
        raise KeyError(queue_id)

    if "title" in updates:
        target["title"] = updates["title"]
    if "max_in_flight" in updates:
        target["max_in_flight"] = int(updates["max_in_flight"])
    if "query" in updates:
        q = target.get("query") or {}
        for k, v in query_updates.items():
            if v is None or v == "" or v == []:
                q.pop(k, None)
            else:
                q[k] = v
        target["query"] = q

    # Atomic write: stage to .tmp then rename. Same pattern as the
    # state-file writer — avoids leaving a truncated config.yaml if
    # something interrupts the dump mid-stream.
    import os
    tmp = CONFIG_PATH.with_suffix(".yaml.tmp")
    with open(tmp, "w") as f:
        y.dump(doc, f)
    os.replace(tmp, CONFIG_PATH)
    return dict(target)
