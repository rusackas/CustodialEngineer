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
EDITABLE_QUEUE_FIELDS = {"title", "max_in_flight", "query", "repo"}
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
    if "repo" in updates:
        repo_val = updates["repo"]
        if repo_val is None:
            target.pop("repo", None)
        elif isinstance(repo_val, dict) and repo_val.get("owner") and repo_val.get("name"):
            target["repo"] = {"owner": repo_val["owner"], "name": repo_val["name"]}
        else:
            raise ValueError("repo must be {owner, name} or None")
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


def _yaml_roundtrip():
    """Shared ruamel.yaml instance for the roundtrip-preserving edits."""
    from ruamel.yaml import YAML
    y = YAML()
    y.preserve_quotes = True
    y.width = 10_000
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def get_queue_block_yaml(queue_id: str) -> str:
    """Serialize just one queue's block from config.yaml as a YAML
    string, preserving ordering + style. Used by the settings-modal's
    raw editor so the user sees the exact shape they can round-trip."""
    import io
    y = _yaml_roundtrip()
    with open(CONFIG_PATH) as f:
        doc = y.load(f)
    queues = doc.get("queues") or []
    target = next((q for q in queues if q.get("id") == queue_id), None)
    if target is None:
        raise KeyError(queue_id)
    buf = io.StringIO()
    y.dump(target, buf)
    return buf.getvalue()


_QUEUE_ID_RE = __import__("re").compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")


def add_queue_block(parsed: dict) -> dict:
    """Append a new queue block to config.yaml. Validates:
      - parsed is a mapping
      - `id` is slug-shaped and not already in use
      - required keys present (id, title, initial_state, states)
    Returns the added queue dict. Raises ValueError on validation
    failure. Uses ruamel.yaml so the rest of the file's formatting
    and comments are preserved."""
    if not isinstance(parsed, dict):
        raise ValueError("queue definition must be a mapping")
    required = {"id", "title", "initial_state", "states"}
    missing = required - set(parsed.keys())
    if missing:
        raise ValueError(
            "missing required keys: " + ", ".join(sorted(missing)))
    qid = str(parsed.get("id") or "").strip()
    if not qid:
        raise ValueError("id is required")
    if not _QUEUE_ID_RE.match(qid):
        raise ValueError(
            f"id `{qid}` must be lowercase alphanumeric with "
            f"dashes or underscores (e.g. `my-queue`, 1-64 chars).")

    y = _yaml_roundtrip()
    with open(CONFIG_PATH) as f:
        doc = y.load(f)
    queues = doc.get("queues") or []
    if any(q.get("id") == qid for q in queues):
        raise ValueError(f"queue id `{qid}` already exists")
    queues.append(parsed)
    doc["queues"] = queues

    import os
    tmp = CONFIG_PATH.with_suffix(".yaml.tmp")
    with open(tmp, "w") as f:
        y.dump(doc, f)
    os.replace(tmp, CONFIG_PATH)
    return dict(parsed)


def new_queue_template(qid: str = "my-queue",
                       title: str = "My Queue") -> str:
    """Serialize a sensible default queue block as YAML — used to
    seed the new-queue modal's Raw YAML tab."""
    import io
    y = _yaml_roundtrip()
    from ruamel.yaml.comments import CommentedMap, CommentedSeq
    block = CommentedMap()
    block["id"] = qid
    block["title"] = title
    block["max_in_flight"] = 10
    block["initial_state"] = "in triage"
    block["done_state"] = "done"
    block["awaiting_state"] = "awaiting update"
    states = CommentedSeq(["in triage", "in progress",
                           "awaiting update", "done"])
    block["states"] = states
    query = CommentedMap()
    query["author"] = "self"
    query["state"] = "open"
    block["query"] = query
    buf = io.StringIO()
    y.dump(block, buf)
    return buf.getvalue()


def replace_queue_block(queue_id: str, yaml_text: str) -> dict:
    """Parse a YAML string as one queue block and replace the matching
    entry in config.yaml. Validates:
      - parses as a mapping
      - required keys present (id, title, initial_state, states)
      - id matches the URL parameter (can't rename via YAML save)
    Raises ValueError with a human-readable explanation on any
    validation failure. Returns the new queue block on success.
    """
    y = _yaml_roundtrip()
    try:
        parsed = y.load(yaml_text)
    except Exception as exc:
        raise ValueError(f"YAML parse error: {exc}")
    if not isinstance(parsed, dict):
        raise ValueError("YAML must describe a single queue mapping")
    required = {"id", "title", "initial_state", "states"}
    missing = required - set(parsed.keys())
    if missing:
        raise ValueError(
            "missing required keys: " + ", ".join(sorted(missing)))
    if parsed.get("id") != queue_id:
        raise ValueError(
            f"id mismatch — YAML says `{parsed.get('id')}`, "
            f"expected `{queue_id}`. Queue IDs can't be renamed "
            f"via the settings UI (state file is keyed by id).")

    with open(CONFIG_PATH) as f:
        doc = y.load(f)
    queues = doc.get("queues") or []
    for i, q in enumerate(queues):
        if q.get("id") == queue_id:
            queues[i] = parsed
            break
    else:
        raise KeyError(queue_id)

    import os
    tmp = CONFIG_PATH.with_suffix(".yaml.tmp")
    with open(tmp, "w") as f:
        y.dump(doc, f)
    os.replace(tmp, CONFIG_PATH)
    return dict(parsed)
