"""Triage logic for queue items.

`triage_dependabot_pr` is the entry point. It invokes the
`triage-failing-dependabot-pr` Skill via the Claude Agent SDK, which reads
CI logs and diff context to produce a well-informed proposal. If the
session fails (SDK error, unparsed output, empty actions list) we fall
back to `mechanical_triage`, which only looks at fields already on the
item — mergeable status, updatedAt.

The mechanical function is kept separately so tests can exercise it
without the SDK and the live path can fall back to it.
"""
from datetime import datetime, timezone

from . import sessions, worktree
from .config import load_config


STALE_DAYS = 7


def _parse_iso(ts: str | None):
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _age_days(raw: dict) -> float | None:
    dt = _parse_iso(raw.get("updatedAt"))
    if dt is None:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400


def mechanical_triage(item: dict) -> tuple[str, list[str]]:
    """Fallback triage using only fields already on the item."""
    raw = item.get("raw") or {}
    mergeable = (raw.get("mergeable") or "").upper()
    ci = (raw.get("ci_status") or "").lower()
    age = _age_days(raw)

    if mergeable == "CONFLICTING":
        msg = "Merge conflicts detected. Post `@dependabot rebase`; if that fails, rebase manually."
        return msg, ["dependabot-rebase", "rebase", "close"]

    if ci == "passing" and mergeable == "MERGEABLE":
        # `mergeable == MERGEABLE` only means no textual conflicts — the PR
        # could still be BLOCKED / BEHIND / UNSTABLE. The approve-merge
        # skill re-verifies mergeStateStatus before acting, so clicking
        # this is safe. We still flag the caveat in the proposal.
        msg = ("CI is green and no textual conflicts — approve-merge will "
               "re-verify mergeStateStatus before approving.")
        return msg, ["approve-merge", "prompt", "close"]

    if ci == "pending":
        msg = "Checks are still running. Re-triage on the next refresh."
        return msg, ["retrigger-ci", "prompt", "close"]

    if age is not None and age > STALE_DAYS:
        msg = (
            f"Stale PR (~{age:.0f}d since update) with failing CI. "
            "Consider `@dependabot recreate` or close if obsolete."
        )
        return msg, ["dependabot-recreate", "close", "prompt"]

    msg = "CI is failing. Likely a lockfile drift or flaky test — inspect logs."
    return msg, ["retrigger-ci", "update-lockfile", "rebase", "close", "prompt"]


def _build_context(item: dict, extra_pr_fields: dict | None = None) -> dict:
    """Build the skill session's runtime context. `pr.owner`/`pr.name`
    are derived from (in order): the item's stamped repo, the queue's
    configured repo, then top-level config.yaml `repo`. This is how
    cross-repo queues keep their skill calls pointed at the right
    project."""
    cfg = load_config()
    raw = item.get("raw") or {}
    item_repo = raw.get("repo") if isinstance(raw.get("repo"), dict) else None
    if item_repo and item_repo.get("owner") and item_repo.get("name"):
        owner, name = item_repo["owner"], item_repo["name"]
    else:
        owner = cfg["repo"]["owner"]
        name = cfg["repo"]["name"]
    pr = {
        "owner": owner,
        "name": name,
        "number": item.get("number"),
        "url": item.get("url"),
        "title": item.get("title"),
        "head_ref": raw.get("headRefName"),
        "mergeable": raw.get("mergeable"),
        "updated_at": raw.get("updatedAt"),
        "is_draft": raw.get("isDraft"),
        "ci_status": raw.get("ci_status"),
    }
    if extra_pr_fields:
        pr.update(extra_pr_fields)
    return {"pr": pr, "identity": (cfg.get("identity") or {})}


def _skill_triage(skill: str, item: dict, queue_id: str | None = None,
                  extra_pr_fields: dict | None = None
                  ) -> tuple[str, list[str], dict] | None:
    context = _build_context(item, extra_pr_fields)
    session_id, result = sessions.run_session_blocking(
        skill, context,
        cwd=str(worktree.repo_path()),
        kind="triage",
        queue_id=queue_id,
        item_id=item.get("id"),
    )
    proposal = result.get("proposal")
    actions = result.get("actions")
    if not proposal or not isinstance(actions, list) or not actions:
        return None
    # Carry every informational top-level field forward as triage_notes
    # so the UI can render `suggested_comment`, `blockers`, `concerns`,
    # `tests_needed`, etc. without each call-site needing its own
    # extraction. Only the control fields (proposal, actions, status,
    # meta, action) are excluded — the skill's `notes` dict is merged
    # in last so it wins on key collisions.
    _EXCLUDE = {"proposal", "actions", "status", "action", "meta"}
    notes = {k: v for k, v in result.items() if k not in _EXCLUDE and k != "notes"}
    nested = result.get("notes")
    if isinstance(nested, dict):
        notes.update(nested)
    notes["session_id"] = session_id
    return proposal, [str(a) for a in actions], notes


def triage_dependabot_pr(item: dict, queue_id: str | None = None
                         ) -> tuple[str, list[str], dict]:
    """Return (proposal, actions, extra). `extra` always includes
    `triage_source` ∈ {"skill", "mechanical"}."""
    try:
        skill_result = _skill_triage(
            "triage-dependabot-pr", item, queue_id=queue_id)
        if skill_result is not None:
            proposal, actions, notes = skill_result
            extra = {"triage_source": "skill", "triage_notes": notes}
            if notes.get("session_id"):
                extra["triage_session_id"] = notes["session_id"]
            return proposal, actions, extra
    except Exception as exc:
        msg, actions = mechanical_triage(item)
        return msg, actions, {"triage_source": "mechanical", "triage_error": str(exc)}
    msg, actions = mechanical_triage(item)
    return msg, actions, {"triage_source": "mechanical"}


def _mechanical_my_pr_triage(item: dict) -> tuple[str, list[str]]:
    """Fallback triage for my-prs. Used when the skill fails — picks
    one primary action from the three signals and lists the rest as
    fallbacks."""
    raw = item.get("raw") or {}
    has_conflicts = bool(raw.get("has_conflicts"))
    ci = (raw.get("ci_status") or "").lower()
    threads = raw.get("unresolved_threads") or []
    reasons = []
    actions: list[str] = []
    if has_conflicts:
        reasons.append("merge conflicts")
        actions.append("resolve-conflicts")
    if ci == "failing":
        reasons.append("failing CI")
        actions.extend(["attempt-fix", "fix-precommit", "update-lockfile"])
    if threads:
        reasons.append(f"{len(threads)} unresolved review thread"
                       + ("s" if len(threads) != 1 else ""))
        actions.append("address-comments")
    if not actions:
        return ("No blocking signal detected — manual triage.",
                ["prompt"])
    actions.append("prompt")
    # Dedup keeping order.
    seen: set = set()
    ordered = [a for a in actions if not (a in seen or seen.add(a))]
    return f"Needs attention: {', '.join(reasons)}.", ordered


def _mechanical_review_requested_triage(item: dict) -> tuple[str, list[str]]:
    """Fallback triage when the skill fails. Pulls the fetcher-bucketed
    classification and proposes safe defaults."""
    raw = item.get("raw") or {}
    has_conflicts = bool(raw.get("has_conflicts"))
    ci = (raw.get("ci_status") or "").lower()
    threads = raw.get("unresolved_threads") or []
    others = [t for t in threads if t.get("first_author")
              and t["first_author"] != (load_config().get("identity") or {})
              .get("github_username")]
    reasons: list[str] = []
    actions: list[str] = []
    if has_conflicts:
        reasons.append("merge conflicts")
        actions.append("await-update")
    if ci == "failing":
        reasons.append("failing CI")
        actions.append("nudge-author")
    if others:
        reasons.append(
            f"{len(others)} unresolved thread"
            + ("s" if len(others) != 1 else "")
            + " from others")
        actions.append("nudge-author")
    if not reasons:
        msg = "No blockers on signal check — safe to review."
        actions = ["approve-merge", "add-review-comment", "await-update",
                   "prompt", "skip"]
    else:
        msg = "Blockers: " + ", ".join(reasons) + "."
        actions.extend(["add-review-comment", "await-update",
                        "prompt", "skip"])
    seen: set = set()
    ordered = [a for a in actions if not (a in seen or seen.add(a))]
    return msg, ordered


def triage_review_requested_pr(item: dict, queue_id: str | None = None
                                ) -> tuple[str, list[str], dict]:
    """Triage one PR where the user has been asked to review. Passes
    the signal fields to the skill so it can reason about classification
    without re-fetching."""
    raw = item.get("raw") or {}
    extra_pr = {
        "has_conflicts": bool(raw.get("has_conflicts")),
        "merge_state_status": raw.get("mergeStateStatus"),
        "unresolved_threads": raw.get("unresolved_threads") or [],
    }
    try:
        skill_result = _skill_triage(
            "triage-review-requested", item, queue_id=queue_id,
            extra_pr_fields=extra_pr)
        if skill_result is not None:
            proposal, actions, notes = skill_result
            extra = {"triage_source": "skill", "triage_notes": notes}
            if notes.get("session_id"):
                extra["triage_session_id"] = notes["session_id"]
            return proposal, actions, extra
    except Exception as exc:
        msg, actions = _mechanical_review_requested_triage(item)
        return msg, actions, {"triage_source": "mechanical",
                              "triage_error": str(exc)}
    msg, actions = _mechanical_review_requested_triage(item)
    return msg, actions, {"triage_source": "mechanical"}


def triage_my_pr(item: dict, queue_id: str | None = None
                 ) -> tuple[str, list[str], dict]:
    """Triage one of the user's own open PRs. The skill gets the three
    signal fields (has_conflicts, ci_status, unresolved_threads) so it
    can reason about priority without re-fetching."""
    raw = item.get("raw") or {}
    extra_pr = {
        "has_conflicts": bool(raw.get("has_conflicts")),
        "merge_state_status": raw.get("mergeStateStatus"),
        "unresolved_threads": raw.get("unresolved_threads") or [],
    }
    try:
        skill_result = _skill_triage(
            "triage-my-pr", item, queue_id=queue_id,
            extra_pr_fields=extra_pr)
        if skill_result is not None:
            proposal, actions, notes = skill_result
            extra = {"triage_source": "skill", "triage_notes": notes}
            if notes.get("session_id"):
                extra["triage_session_id"] = notes["session_id"]
            return proposal, actions, extra
    except Exception as exc:
        msg, actions = _mechanical_my_pr_triage(item)
        return msg, actions, {"triage_source": "mechanical",
                              "triage_error": str(exc)}
    msg, actions = _mechanical_my_pr_triage(item)
    return msg, actions, {"triage_source": "mechanical"}
