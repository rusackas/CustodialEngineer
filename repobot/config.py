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
EDITABLE_QUEUE_FIELDS = {"title", "max_in_flight", "query", "repo",
                         "hydrate", "filter",
                         "states", "initial_state", "initial_states",
                         "done_state", "awaiting_state",
                         # Internal-use only — see _state_machine_renames
                         # docstring. Carries the {old_name: new_name}
                         # mapping so the helper can migrate items.
                         "_state_renames"}
EDITABLE_QUERY_KEYS = {"author", "state", "review_requested", "labels",
                       "milestone", "head", "base", "assignee", "search"}
EDITABLE_HYDRATE_KEYS = {"ci_status", "merge_state", "review_threads"}
EDITABLE_FILTER_KEYS = {"attention_only", "non_draft"}


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
        if repo_val is None or repo_val == "":
            target.pop("repo", None)
        elif isinstance(repo_val, str):
            # Registry id reference (e.g. `repo: superset`) — preferred
            # form now that the top-level `repos:` registry exists.
            target["repo"] = repo_val
        elif isinstance(repo_val, dict) and repo_val.get("owner") and repo_val.get("name"):
            # Legacy inline form, still supported for back-compat.
            target["repo"] = {"owner": repo_val["owner"], "name": repo_val["name"]}
        else:
            raise ValueError(
                "repo must be a registry id string, {owner, name} dict, or None")
    if "query" in updates:
        q = target.get("query") or {}
        for k, v in query_updates.items():
            if v is None or v == "" or v == []:
                q.pop(k, None)
            else:
                q[k] = v
        target["query"] = q
    if "hydrate" in updates:
        h = updates["hydrate"]
        if not h:
            target.pop("hydrate", None)
        else:
            bad_h = set(h) - EDITABLE_HYDRATE_KEYS
            if bad_h:
                raise ValueError(
                    f"not editable hydrate keys: {', '.join(sorted(bad_h))}")
            target["hydrate"] = {k: bool(v) for k, v in h.items() if v}
    if "filter" in updates:
        f = updates["filter"]
        if not f:
            target.pop("filter", None)
        else:
            bad_f = set(f) - EDITABLE_FILTER_KEYS
            if bad_f:
                raise ValueError(
                    f"not editable filter keys: {', '.join(sorted(bad_f))}")
            target["filter"] = {k: bool(v) for k, v in f.items() if v}

    # State machine edits. The form sends `states` (the ordered list)
    # plus the role assignments (initial / done / awaiting). For
    # multi-bucket queues, `initial_states` carries the list of
    # pre-triage buckets and supersedes `initial_state` for the runner;
    # we still write `initial_state` as the first bucket so single-state
    # call sites have a fallback. Renames migrate every affected item
    # in SQL so we don't orphan rows that were sitting in a renamed
    # state.
    if "states" in updates:
        new_states = list(updates["states"] or [])
        if not new_states:
            raise ValueError("states must have at least one entry")
        seen: set[str] = set()
        for s in new_states:
            if not isinstance(s, str) or not s.strip():
                raise ValueError("every state must be a non-empty string")
            if s in seen:
                raise ValueError(f"duplicate state name: {s!r}")
            seen.add(s)
        old_states = list(target.get("states") or [])
        # Caller-supplied rename map: {old_name: new_name}. The form
        # tracks each row's original name client-side and passes it
        # through so we can distinguish a rename from a delete + add.
        renames = updates.get("_state_renames") or {}
        if not isinstance(renames, dict):
            raise ValueError("_state_renames must be a mapping")
        # Refuse to delete a state that still has items in it. Renames
        # are fine — we'll migrate them next.
        renamed_old = set(renames.keys())
        deleted = [s for s in old_states
                   if s not in new_states and s not in renamed_old]
        if deleted:
            from . import db as _db
            occupied = _db.items_in_states(queue_id, deleted)
            if occupied:
                msg = ", ".join(f"{s} ({n})" for s, n in occupied.items())
                raise ValueError(
                    "Can't delete states with items in them: "
                    f"{msg}. Move those items to another column first.")
        # Apply renames to items in SQL.
        if renames:
            from . import db as _db
            _db.rename_item_states(queue_id, renames)
        target["states"] = new_states

    if "initial_states" in updates:
        ss = updates.get("initial_states")
        if ss is None:
            target.pop("initial_states", None)
        else:
            ss = list(ss)
            avail = set(target.get("states") or updates.get("states") or [])
            for s in ss:
                if s not in avail:
                    raise ValueError(
                        f"initial_states[{s!r}] is not in `states`")
            target["initial_states"] = ss
    if "initial_state" in updates:
        s = updates["initial_state"]
        avail = set(target.get("states") or updates.get("states") or [])
        if s not in avail:
            raise ValueError(
                f"initial_state {s!r} is not in `states`")
        target["initial_state"] = s
    if "done_state" in updates:
        s = updates["done_state"]
        if s is None:
            target.pop("done_state", None)
        else:
            avail = set(target.get("states") or updates.get("states") or [])
            if s not in avail:
                raise ValueError(
                    f"done_state {s!r} is not in `states`")
            target["done_state"] = s
    if "awaiting_state" in updates:
        s = updates["awaiting_state"]
        if s is None:
            target.pop("awaiting_state", None)
        else:
            avail = set(target.get("states") or updates.get("states") or [])
            if s not in avail:
                raise ValueError(
                    f"awaiting_state {s!r} is not in `states`")
            target["awaiting_state"] = s

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


_REPO_ID_RE = __import__("re").compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")


def add_repo_block(entry: dict) -> dict:
    """Append a new entry to the top-level `repos:` registry. Validates
    id is slug-shaped and unique; owner + name are required strings.
    Returns the normalized entry on success."""
    rid = (entry.get("id") or "").strip()
    if not _REPO_ID_RE.match(rid):
        raise ValueError(
            f"id `{rid}` must be slug-shaped (a-z 0-9 _ -, 1-64 chars).")
    owner = (entry.get("owner") or "").strip()
    name = (entry.get("name") or "").strip()
    if not owner or not name:
        raise ValueError("owner and name are required")

    y = _yaml_roundtrip()
    with open(CONFIG_PATH) as f:
        doc = y.load(f)
    repos = doc.get("repos") or []
    if any((r or {}).get("id") == rid for r in repos):
        raise ValueError(f"repo id `{rid}` already exists")
    new_entry = {"id": rid, "owner": owner, "name": name}
    display = (entry.get("display_name") or "").strip()
    if display:
        new_entry["display_name"] = display
    repos.append(new_entry)
    doc["repos"] = repos

    import os
    tmp = CONFIG_PATH.with_suffix(".yaml.tmp")
    with open(tmp, "w") as f:
        y.dump(doc, f)
    os.replace(tmp, CONFIG_PATH)
    return dict(new_entry)


def delete_repo_block(repo_id: str) -> None:
    """Remove a repo from the top-level registry. Refuses if it's the
    current default OR if any queue references it via `repo: <id>`."""
    y = _yaml_roundtrip()
    with open(CONFIG_PATH) as f:
        doc = y.load(f)
    repos = doc.get("repos") or []
    if not any((r or {}).get("id") == repo_id for r in repos):
        raise KeyError(repo_id)
    if doc.get("default_repo_id") == repo_id:
        raise ValueError(
            f"`{repo_id}` is the default repo — pick a different "
            f"default before removing it.")
    queues = doc.get("queues") or []
    in_use = [q.get("id") for q in queues if q.get("repo") == repo_id]
    if in_use:
        raise ValueError(
            f"`{repo_id}` is referenced by queues: "
            f"{', '.join(in_use)}. Repoint or remove those queues first.")
    doc["repos"] = [r for r in repos if (r or {}).get("id") != repo_id]

    import os
    tmp = CONFIG_PATH.with_suffix(".yaml.tmp")
    with open(tmp, "w") as f:
        y.dump(doc, f)
    os.replace(tmp, CONFIG_PATH)


def set_default_repo(repo_id: str) -> None:
    """Update the top-level `default_repo_id`. The id must exist in
    the registry."""
    y = _yaml_roundtrip()
    with open(CONFIG_PATH) as f:
        doc = y.load(f)
    repos = doc.get("repos") or []
    if not any((r or {}).get("id") == repo_id for r in repos):
        raise KeyError(repo_id)
    doc["default_repo_id"] = repo_id

    import os
    tmp = CONFIG_PATH.with_suffix(".yaml.tmp")
    with open(tmp, "w") as f:
        y.dump(doc, f)
    os.replace(tmp, CONFIG_PATH)


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
