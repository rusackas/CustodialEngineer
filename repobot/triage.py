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
        from .github import default_repo_slug
        owner, name = default_repo_slug().split("/", 1)
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


# Default skill name used by `triage_generic_pr` when a queue doesn't
# pin one via its `triage_skill` config field. Resolves to
# `.claude/skills/triage-generic-pr/SKILL.md`.
DEFAULT_GENERIC_TRIAGE_SKILL = "triage-generic-pr"


def _resolve_triage_skill(queue_id: str | None) -> str:
    """Return the skill name to invoke for a queue's generic triage.
    Queue YAML may pin one via `triage_skill: …`; otherwise we fall
    back to the bundled `triage-generic-pr` skill."""
    if not queue_id:
        return DEFAULT_GENERIC_TRIAGE_SKILL
    cfg = load_config()
    for q in cfg.get("queues") or []:
        if q.get("id") == queue_id:
            skill = (q.get("triage_skill") or "").strip()
            return skill or DEFAULT_GENERIC_TRIAGE_SKILL
    return DEFAULT_GENERIC_TRIAGE_SKILL


def _can_push_back(raw: dict) -> bool:
    """True when we can push commits back to the PR's head branch —
    either an in-repo PR (we already have origin push) or a fork PR
    where the author opted into maintainer edits. Skills like
    `rebase`, `fix-precommit`, and `attempt-fix` need this to actually
    do anything; without it they bail to `needs_human` and the user
    wastes a click."""
    if not raw.get("is_cross_repository"):
        return True
    return bool(raw.get("maintainer_can_modify"))


def _mechanical_generic_triage(item: dict) -> tuple[str, list[str]]:
    """Signal-based fallback for arbitrary user-defined queues. We don't
    know whether the user is the author, reviewer, or just watching —
    so we propose safe, low-commitment actions and always include
    `prompt` as the human-escape hatch.

    Action priority:
    - Needs CI approval → `approve-ci` primary. One-click unblock.
    - Conflicts → `rebase` if push-allowed, else `nudge-author` /
      `await-update`.
    - Failing CI → `fix-precommit` / `attempt-fix` if push-allowed,
      else `nudge-author` / `await-update`.
    - Unresolved threads → `await-update`.
    - Stale + nothing actionable → `nudge-author`, then `close`.
    - Clean (no blockers) → `approve-merge` as the obvious primary;
      the action dispatcher re-verifies merge state before acting,
      so it's safe to surface even on a fast-path read.
    """
    from .identity import current_user_id
    raw = item.get("raw") or {}
    has_conflicts = bool(raw.get("has_conflicts"))
    ci = (raw.get("ci_status") or "").lower()
    threads = raw.get("unresolved_threads") or []
    is_draft = bool(raw.get("isDraft"))
    age = _age_days(raw)
    author_login = (raw.get("author") or {}).get("login") if isinstance(raw.get("author"), dict) else None
    is_bot = (raw.get("author") or {}).get("is_bot") if isinstance(raw.get("author"), dict) else False
    needs_ci_approval = bool(raw.get("needs_ci_approval"))
    push_allowed = _can_push_back(raw)
    # Self-merge feasibility check. `reviewDecision` reflects GitHub's
    # branch-protection verdict for the PR — null on repos that don't
    # require reviews, REVIEW_REQUIRED / CHANGES_REQUESTED when an
    # external approver is needed, APPROVED when greenlit. We can't
    # propose approve-merge to the bot's operator on a PR they wrote
    # themselves if branch protection still wants a separate reviewer
    # — GitHub blocks self-approval and would also block the merge.
    me = current_user_id()
    self_authored = (me != "self" and author_login and author_login == me)
    review_decision = (raw.get("reviewDecision") or "").upper()
    review_pending = review_decision in ("REVIEW_REQUIRED", "CHANGES_REQUESTED")

    reasons: list[str] = []
    actions: list[str] = []

    # Highest priority: CI is gated waiting for our click. One button
    # unblocks everything downstream.
    if needs_ci_approval:
        reasons.append("CI awaiting approval")
        actions.append("approve-ci")

    if has_conflicts:
        reasons.append("merge conflicts")
        if push_allowed:
            actions.append("rebase")
        if not is_bot:
            actions.append("nudge-author")
        actions.append("await-update")
    if ci == "failing":
        reasons.append("failing CI")
        if push_allowed:
            # Offer all three CI-fix paths so the user picks based on
            # failure shape: attempt-fix patches in place, plan-fix
            # plans first then patches, fix-precommit handles
            # formatter drift. Skills inspect the rollup themselves
            # and bail cleanly when their assumption doesn't fit.
            actions.extend(["attempt-fix", "plan-fix", "fix-precommit"])
        if not is_bot:
            actions.append("nudge-author")
        actions.append("await-update")
    if threads:
        reasons.append(
            f"{len(threads)} unresolved review thread"
            + ("s" if len(threads) != 1 else ""))
        actions.append("await-update")
    if not reasons and age is not None and age > 30:
        reasons.append(f"stale (~{age:.0f}d since update)")
        if not is_bot:
            actions.append("nudge-author")
        actions.append("close")

    # Draft PR shortcut — for someone else's draft that hasn't moved
    # in a while, the kind thing is to close with thanks and a
    # "feel free to reopen" door. We don't auto-detect the "hasn't
    # moved in a while" part here (the queue's sort:updated-asc
    # surface order does that for us — only stale ones surface);
    # the skill prompt has a richer judgment, but mechanical-side
    # we just propose `close` primary on drafts so the user has the
    # button when they need it.
    if is_draft and not self_authored:
        reasons.append("draft, not authored by you")
        actions.append("close")  # close-pr skill drafts thankful body

    # Clean PR (no blockers, not a draft, CI not pending) → propose
    # approve-merge as the obvious next click. The dispatcher
    # re-verifies mergeStateStatus before acting, so this is safe
    # even when our cached signals are slightly stale.
    #
    # Exception: self-authored PR on a repo with branch protection
    # requiring reviews. The bot can't approve its operator's own PR
    # (GitHub blocks self-approval), so proposing approve-merge would
    # only lead to a needs_human bounce. Surface await-update instead
    # — once another reviewer approves, the card auto-unparks and the
    # next triage can propose merge cleanly.
    is_clean = (not reasons
                and not is_draft
                and not has_conflicts
                and ci in ("passing", "")
                and not threads)
    self_merge_blocked = is_clean and self_authored and review_pending
    if is_clean and not self_merge_blocked:
        actions.append("approve-merge")
        msg = "Clean — no blockers detected. Approve-merge is safe to click."
    elif self_merge_blocked:
        actions.append("await-update")
        msg = ("Clean signals, but you're the author and branch "
               "protection requires another reviewer's approval — "
               "parking until someone else greenlights it.")
    elif not reasons:
        msg = "No blocking signal detected — manual triage."
    else:
        msg = "Needs attention: " + ", ".join(reasons) + "."

    # For non-draft PRs that have any blocker AND are someone else's
    # work, mark-as-draft is a soft-warning option that gives the
    # author a clear "you need to move this" without slamming the
    # door. Place it after the stronger fix actions but before
    # close-style escape hatches.
    if (reasons and not is_draft and not is_bot and not self_authored
            and (has_conflicts or ci == "failing" or threads)):
        actions.append("mark-as-draft")

    # Universal options always offered. `prompt` is the human escape
    # hatch; `summarize-diff` / `assess-on-worktree` give the user a
    # cheap way to ask for more context before deciding.
    actions.extend(["summarize-diff", "assess-on-worktree",
                    "prompt", "skip"])
    seen: set = set()
    ordered = [a for a in actions if not (a in seen or seen.add(a))]
    return msg, ordered


def triage_generic_pr(item: dict, queue_id: str | None = None
                      ) -> tuple[str, list[str], dict]:
    """Generic triager for user-defined queues. Tries the queue's
    configured triage skill (or `triage-generic-pr` by default), falls
    back to mechanical signal-based triage when the skill errors or
    returns an unparseable result.

    This is the registry fallback wired in `runner._triager_for_queue`
    — without it, queues outside the built-in TRIAGERS registry would
    have items stuck in their initial state forever.
    """
    raw = item.get("raw") or {}
    extra_pr = {
        "has_conflicts": bool(raw.get("has_conflicts")),
        "merge_state_status": raw.get("mergeStateStatus"),
        "unresolved_threads": raw.get("unresolved_threads") or [],
        "maintainer_can_modify": bool(raw.get("maintainer_can_modify")),
        "is_cross_repository": bool(raw.get("is_cross_repository")),
        "needs_ci_approval": bool(raw.get("needs_ci_approval")),
        # Branch-protection signal — feeds the self-merge feasibility
        # branch in the priority ladder.
        "review_decision": raw.get("reviewDecision"),
        "author_login": (raw.get("author") or {}).get("login")
                        if isinstance(raw.get("author"), dict) else None,
    }
    skill = _resolve_triage_skill(queue_id)
    try:
        skill_result = _skill_triage(
            skill, item, queue_id=queue_id, extra_pr_fields=extra_pr)
        if skill_result is not None:
            proposal, actions, notes = skill_result
            extra = {"triage_source": "skill",
                     "triage_skill": skill,
                     "triage_notes": notes}
            if notes.get("session_id"):
                extra["triage_session_id"] = notes["session_id"]
            return proposal, actions, extra
    except Exception as exc:
        msg, actions = _mechanical_generic_triage(item)
        return msg, actions, {"triage_source": "mechanical",
                              "triage_skill": skill,
                              "triage_error": str(exc)}
    msg, actions = _mechanical_generic_triage(item)
    return msg, actions, {"triage_source": "mechanical",
                          "triage_skill": skill}


# ============================================================
# Issue triage (parallel to the PR functions above, with
# issue-flavored signals and an issue-specific action set).
# ============================================================

DEFAULT_GENERIC_ISSUE_TRIAGE_SKILL = "triage-generic-issue"

# Labels that, when present on an open issue, mean "the maintainers
# already decided this isn't getting fixed" — close-as-stale becomes
# the obvious primary even for relatively young issues.
_DECIDED_OUT_LABELS = {
    "wontfix", "won't fix", "wont-fix", "not-a-bug",
    "duplicate", "invalid", "cant-reproduce", "cannot-reproduce",
}

# Labels that mean "we asked the reporter for something and never
# heard back" — close-as-stale is appropriate after a timeout.
_AWAITING_REPORTER_LABELS = {
    "needs-info", "needs:info", "more-info-needed", "awaiting-response",
}

# Labels that mean "still open by design" — don't propose close.
_KEEP_OPEN_LABELS = {
    "good-first-issue", "good first issue", "help-wanted", "help wanted",
    "discussion", "rfc", "epic", "tracking",
}


def _label_set(raw: dict) -> set[str]:
    out: set[str] = set()
    for l in raw.get("labels") or []:
        name = (l.get("name") or "").strip().lower()
        if name:
            out.add(name)
    return out


def _mechanical_generic_issue_triage(item: dict) -> tuple[str, list[str]]:
    """Signal-based issue triage. The action menu mirrors what a
    human maintainer would reach for on a stale-issue sweep:

    - `close-as-stale` — close the issue with a polite explanatory
      comment.
    - `label-as-stale` — add a `stale` label as a 30-day warning shot
      before close. Useful when the issue might still be valid but
      needs reporter engagement.
    - `nudge-issue-author` — @-mention the reporter to revive the
      thread. Use when there's been recent maintainer engagement
      and the report itself is plausible.
    - `convert-to-discussion` — for "how do I…" / feature-request-
      shaped issues that fit better as discussions.
    - `prompt` / `skip` — universal escape hatches.

    Decision ladder (first match wins for primary):
    1. Decided-out labels (wontfix, duplicate, etc.) → `close-as-stale`.
    2. Awaiting-reporter labels + age > 30d → `close-as-stale`.
    3. Keep-open labels (good-first-issue, help-wanted, rfc) →
       `prompt` primary; offer label-as-stale only if very old.
    4. Stale (>180d, no recent comment) → `label-as-stale` primary
       on the first pass; `close-as-stale` if already labeled stale.
    5. Has recent comments + reporter is the most recent commenter →
       `prompt` primary (a maintainer should weigh in next).
    6. Has recent maintainer comment, no reporter follow-up → `nudge-
       issue-author` primary.
    7. Default (active discussion) → `prompt` primary.
    """
    raw = item.get("raw") or {}
    labels = _label_set(raw)
    age = _age_days(raw)
    last_comment_age = None
    if raw.get("last_comment_at"):
        try:
            dt = _parse_iso(raw["last_comment_at"])
            if dt:
                from datetime import datetime, timezone
                last_comment_age = (datetime.now(timezone.utc) - dt).total_seconds() / 86400
        except Exception:
            pass
    last_commenter = (raw.get("last_commenter") or "").strip()
    author_login = (raw.get("author") or {}).get("login") if isinstance(raw.get("author"), dict) else None
    state_reason = (raw.get("stateReason") or "").upper()

    has_decided_out = bool(labels & _DECIDED_OUT_LABELS)
    has_awaiting = bool(labels & _AWAITING_REPORTER_LABELS)
    has_keep_open = bool(labels & _KEEP_OPEN_LABELS)
    already_stale_labeled = "stale" in labels

    reasons: list[str] = []
    actions: list[str] = []

    if has_decided_out:
        decided_label = next(iter(labels & _DECIDED_OUT_LABELS))
        reasons.append(f"labeled `{decided_label}`")
        actions.append("close-as-stale")
    elif has_awaiting and age is not None and age > 30:
        reasons.append(f"awaiting reporter info for ~{age:.0f}d")
        actions.extend(["close-as-stale", "nudge-issue-author"])
    elif already_stale_labeled and age is not None and age > 60:
        reasons.append(f"labeled `stale`, ~{age:.0f}d old")
        actions.append("close-as-stale")
    elif has_keep_open:
        # Don't auto-close `good-first-issue`, `help-wanted`, etc. —
        # those are meant to stay open. Only suggest stale-labeling
        # if very old AND no recent activity.
        keep_label = next(iter(labels & _KEEP_OPEN_LABELS))
        reasons.append(f"labeled `{keep_label}` (kept open by design)")
        if last_comment_age is not None and last_comment_age > 180:
            actions.append("label-as-stale")
    elif age is not None and age > 180 and (
            last_comment_age is None or last_comment_age > 90):
        reasons.append(f"stale (~{age:.0f}d old, ~{last_comment_age:.0f}d since last comment)"
                       if last_comment_age else f"stale (~{age:.0f}d old, no recent comments)")
        actions.append("label-as-stale")
    elif (last_commenter and author_login
          and last_commenter == author_login
          and last_comment_age is not None and last_comment_age > 30):
        reasons.append(
            f"reporter @{author_login} last spoke ~{last_comment_age:.0f}d ago, no maintainer follow-up")
        # Maintainer should weigh in — keep menu light.
    elif (last_commenter and author_login
          and last_commenter != author_login
          and last_comment_age is not None and last_comment_age > 30):
        reasons.append(
            f"maintainer @{last_commenter} last spoke ~{last_comment_age:.0f}d ago, no reporter follow-up")
        actions.append("nudge-issue-author")

    if not reasons:
        msg = "Active issue — no stale signal."
    else:
        msg = "Triage: " + "; ".join(reasons) + "."

    # Universal options always offered last. Convert-to-discussion
    # is offered freely — it's a soft action and a maintainer can
    # always undo. Skip / prompt as escape hatches.
    actions.extend(["nudge-issue-author", "convert-to-discussion",
                    "label-as-stale", "prompt", "skip"])
    seen: set = set()
    ordered = [a for a in actions if not (a in seen or seen.add(a))]
    return msg, ordered


def _resolve_issue_triage_skill(queue_id: str | None) -> str:
    if not queue_id:
        return DEFAULT_GENERIC_ISSUE_TRIAGE_SKILL
    cfg = load_config()
    for q in cfg.get("queues") or []:
        if q.get("id") == queue_id:
            skill = (q.get("triage_skill") or "").strip()
            return skill or DEFAULT_GENERIC_ISSUE_TRIAGE_SKILL
    return DEFAULT_GENERIC_ISSUE_TRIAGE_SKILL


def triage_generic_issue(item: dict, queue_id: str | None = None
                         ) -> tuple[str, list[str], dict]:
    """Generic triager for issue queues. Tries the queue's configured
    triage skill (or `triage-generic-issue` by default), falls back
    to mechanical signal-based triage on skill error / unparseable
    result. Same shape as `triage_generic_pr` for symmetry."""
    raw = item.get("raw") or {}
    labels = _label_set(raw)
    extra_pr = {
        "labels": sorted(labels),
        "comments_count": raw.get("comments_count") or 0,
        "last_commenter": raw.get("last_commenter"),
        "last_comment_at": raw.get("last_comment_at"),
        "state_reason": raw.get("stateReason"),
        "author_login": (raw.get("author") or {}).get("login")
                        if isinstance(raw.get("author"), dict) else None,
        # Pass through trimmed comments + body so the skill can read
        # the discussion without a second round-trip.
        "comments": raw.get("comments") or [],
        "body": raw.get("body") or "",
    }
    skill = _resolve_issue_triage_skill(queue_id)
    try:
        skill_result = _skill_triage(
            skill, item, queue_id=queue_id, extra_pr_fields=extra_pr)
        if skill_result is not None:
            proposal, actions, notes = skill_result
            extra = {"triage_source": "skill",
                     "triage_skill": skill,
                     "triage_notes": notes}
            if notes.get("session_id"):
                extra["triage_session_id"] = notes["session_id"]
            return proposal, actions, extra
    except Exception as exc:
        msg, actions = _mechanical_generic_issue_triage(item)
        return msg, actions, {"triage_source": "mechanical",
                              "triage_skill": skill,
                              "triage_error": str(exc)}
    msg, actions = _mechanical_generic_issue_triage(item)
    return msg, actions, {"triage_source": "mechanical",
                          "triage_skill": skill}
