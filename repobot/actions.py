"""Action registry + dispatcher.

Clicking an action button in the UI POSTs here. We:

1. Look up the action spec (skill name, worktree requirement, state transitions).
2. If it's instantaneous (skip), move straight to the terminal state.
3. Otherwise set `in_progress_state`, spawn a long-lived Claude session via
   `sessions.start_session`, and return to the UI immediately. The session
   stays open for user follow-ups; we wire `on_first_turn_complete` to flip
   the item to its terminal state on success. Non-success results (error,
   unparsed, needs_human) keep the item at `in_progress_state` so the chat
   button stays visible and the user can intervene. Worktrees are NOT torn
   down when the session closes — they live until the user deletes the card
   (so a post-timeout resume can still cd into them).
"""
import traceback
from typing import Any

from . import github, sessions, worktree
from .config import load_config
from .queues import (
    _now,
    find_item,
    load_state,
    set_item_assessment,
    set_item_diff_summary,
    set_item_drafts,
    set_item_drafts_status,
    set_item_parked_at,
    set_item_plan,
    set_item_plan_status,
    set_item_result,
    set_item_session_id,
    set_item_state,
)

# Statuses returned by a skill's JSON block that we consider "successful
# enough to land in the terminal state". Anything else leaves the card in
# `in_progress_state` for human follow-up via chat.
SUCCESS_STATUSES = frozenset({"completed", "skipped_dry_run", "skipped"})


# Action spec:
#   label              — display label (matches the button text)
#   skill              — SKILL.md directory name under .claude/skills/ (None = no session)
#   worktree_required  — True → run the session inside a PR worktree
#   in_progress_state  — state the item moves to while running (None = instant)
#   terminal_state     — state on success
#   failure_state      — state on failure (typically "in triage" so it stays visible)
ACTIONS: dict[str, dict[str, Any]] = {
    "skip": {
        "label": "skip",
        "skill": None,
        "worktree_required": False,
        "in_progress_state": None,
        "terminal_state": "done",
        "failure_state": "in triage",
    },
    "await-update": {
        # Park the card — user is waiting on someone else (review, CI
        # rerun, external response). The refresh loop auto-unparks when
        # the PR's updatedAt passes the parked_at timestamp.
        "label": "await-update",
        "skill": None,
        "worktree_required": False,
        "in_progress_state": None,
        "terminal_state": "awaiting update",
        "failure_state": "in triage",
    },
    "close": {
        "label": "close",
        "skill": "close-pr",
        "worktree_required": False,
        "in_progress_state": "in progress",
        "terminal_state": "done",
        "failure_state": "in triage",
    },
    "prompt": {
        "label": "prompt",
        "skill": "prompt-on-pr",
        "worktree_required": True,
        "in_progress_state": "in progress",
        "terminal_state": "done",
        "failure_state": "in triage",
    },
    "rebase": {
        "label": "rebase",
        "skill": "rebase-pr",
        "worktree_required": True,
        "in_progress_state": "in progress",
        "terminal_state": "done",
        "failure_state": "in triage",
    },
    "update-lockfile": {
        "label": "update-lockfile",
        "skill": "update-pr-lockfile",
        "worktree_required": True,
        "in_progress_state": "in progress",
        "terminal_state": "done",
        "failure_state": "in triage",
    },
    "address-comments": {
        # Walks unresolved review threads on one of the user's own PRs,
        # applying fixes (one commit per thread) or replying with a
        # decline rationale. Every thread gets a reply; none are silently
        # dropped.
        "label": "address-comments",
        "skill": "address-review-comments",
        "worktree_required": True,
        "in_progress_state": "in progress",
        "terminal_state": "done",
        "failure_state": "in triage",
    },
    "attempt-fix": {
        "label": "attempt-fix",
        "skill": "attempt-fix-pr",
        "worktree_required": True,
        "in_progress_state": "in progress",
        "terminal_state": "done",
        "failure_state": "in triage",
    },
    "fix-precommit": {
        # Dedicated path for PRs whose ONLY red check is pre-commit.
        # Runs pre-commit on the changed files, commits the auto-fixes,
        # pushes. Narrower (and cheaper) than attempt-fix — no code
        # reasoning, just formatter / lint hygiene.
        "label": "fix-precommit",
        "skill": "fix-precommit-pr",
        "worktree_required": True,
        "in_progress_state": "in progress",
        "terminal_state": "done",
        "failure_state": "in triage",
    },
    "plan-fix": {
        # Phase-1 partner to attempt-fix: investigates and emits a plan
        # (status: "plan"), does NOT execute. The card stays in_progress
        # with item.plan populated until the human approves (or discards).
        "label": "plan-fix",
        "skill": "plan-pr-fix",
        "worktree_required": True,
        "in_progress_state": "in progress",
        "terminal_state": "in progress",
        "failure_state": "in triage",
    },
    "retrigger-ci": {
        "label": "retrigger-ci",
        "skill": "retrigger-pr-ci",
        "worktree_required": False,
        "in_progress_state": "in progress",
        "terminal_state": "done",
        "failure_state": "in triage",
    },
    "approve-merge": {
        "label": "approve-merge",
        "skill": "approve-and-merge-pr",
        "worktree_required": False,
        "in_progress_state": "in progress",
        "terminal_state": "done",
        "failure_state": "in triage",
    },
    "dependabot-rebase": {
        "label": "dependabot-rebase",
        "skill": "dependabot-rebase-comment",
        "worktree_required": False,
        "in_progress_state": "in progress",
        "terminal_state": "done",
        "failure_state": "in triage",
    },
    "dependabot-recreate": {
        "label": "dependabot-recreate",
        "skill": "dependabot-recreate-comment",
        "worktree_required": False,
        "in_progress_state": "in progress",
        "terminal_state": "done",
        "failure_state": "in triage",
    },
    # ---- review-requested queue actions ----
    "add-review-comment": {
        # Post a top-level comment on a PR being reviewed. Body is
        # pre-filled from triage.suggested_comment and edited by the
        # human in the modal before it lands here.
        "label": "add-review-comment",
        "skill": "add-pr-review-comment",
        "worktree_required": False,
        "in_progress_state": "in progress",
        "terminal_state": "done",
        "failure_state": "in triage",
    },
    "approve-review": {
        # Formal "Approve" review (no merge). Body optional.
        "label": "approve-review",
        "skill": "pr-review-approve",
        "worktree_required": False,
        "in_progress_state": "in progress",
        "terminal_state": "done",
        "failure_state": "in triage",
    },
    "request-changes-review": {
        # Formal "Request changes" review. Stronger signal — requires a
        # human-edited body.
        "label": "request-changes-review",
        "skill": "pr-review-request-changes",
        "worktree_required": False,
        "in_progress_state": "in progress",
        "terminal_state": "done",
        "failure_state": "in triage",
    },
    "request-reviewers": {
        # Pick reviewers from a candidate list (mechanically computed
        # from git history of touched files). Button click opens a
        # checkbox modal; submit POSTs to a dedicated endpoint that
        # calls the GH API directly — no Claude session.
        "label": "request-reviewers",
        "skill": None,
        "worktree_required": False,
        "in_progress_state": None,
        "terminal_state": "awaiting update",
        "failure_state": "in triage",
    },
    "ping-reviewers": {
        # Polite @-mention comment on your own PR to nudge currently-
        # requested reviewers for an update. Body pre-filled from
        # triage.notes.ping_comment and edited by the human in the modal.
        "label": "ping-reviewers",
        "skill": "ping-pr-reviewers",
        "worktree_required": False,
        "in_progress_state": "in progress",
        "terminal_state": "awaiting update",
        "failure_state": "in triage",
    },
    "fix-precommit-review": {
        # Pre-commit fix for a PR on the review-requested queue —
        # usually a fork PR. Same skill as my-prs' fix-precommit, but
        # action dispatch sets up a fork remote for push when the PR
        # has maintainerCanModify: true. Bails needs_human otherwise.
        "label": "fix-precommit-review",
        "skill": "fix-precommit-pr",
        "worktree_required": True,
        "in_progress_state": "in progress",
        "terminal_state": "awaiting update",
        "failure_state": "in triage",
    },
    "rebase-review": {
        # Rebase for a review-requested PR. Same dispatch behavior as
        # fix-precommit-review — fork-aware push-remote setup.
        "label": "rebase-review",
        "skill": "rebase-pr",
        "worktree_required": True,
        "in_progress_state": "in progress",
        "terminal_state": "awaiting update",
        "failure_state": "in triage",
    },
    "nudge-author": {
        # Polite nudge comment on a PR where CI is red and/or feedback
        # from other reviewers is unaddressed. Body pre-filled from
        # triage.nudge_comment and edited by the human in the modal.
        "label": "nudge-author",
        "skill": "nudge-pr-author",
        "worktree_required": False,
        "in_progress_state": "in progress",
        "terminal_state": "awaiting update",
        "failure_state": "in triage",
    },
    "dismiss-review-request": {
        # Silently remove the user from the PR's requested-reviewers
        # list. No comment, no mutations beyond the API call.
        "label": "dismiss-review-request",
        "skill": "dismiss-review-request",
        "worktree_required": False,
        "in_progress_state": "in progress",
        "terminal_state": "done",
        "failure_state": "in triage",
    },
    "summarize-diff": {
        # Read the PR diff, emit 3 bullets. `status: "summary"` is
        # handled specially in _on_first_turn — the output is stashed
        # on item.diff_summary and the card does NOT move state.
        "label": "summarize-diff",
        "skill": "summarize-pr-diff",
        "worktree_required": False,
        "in_progress_state": None,
        "terminal_state": None,
        "failure_state": None,
    },
    "assess-on-worktree": {
        # Deep PR assessment with the branch checked out in a worktree.
        # `status: "assessment"` is handled specially in _on_first_turn —
        # output stashed on item.assessment, card state stays put.
        "label": "assess-on-worktree",
        "skill": "assess-pr-on-worktree",
        "worktree_required": True,
        "in_progress_state": None,
        "terminal_state": None,
        "failure_state": None,
    },
}


CONTINUE_NUDGE = (
    "Continue from where you left off. Finish the task per the skill's "
    "documented procedure and emit a single ```json fenced block matching "
    "the skill's output schema as your final message."
)


def _approve_plan_message(plan: dict) -> str:
    """Format an approved (possibly edited) plan as the phase-2 trigger
    message for the plan-pr-fix skill. The leading `APPROVED PLAN:` is
    the sentinel the skill checks for."""
    import json as _json
    return (
        "APPROVED PLAN:\n\n"
        "```json\n" + _json.dumps(plan, indent=2) + "\n```\n\n"
        "Execute this plan exactly per the skill's phase-2 procedure. "
        "Emit the standard ```json output when you're done."
    )


async def approve_plan(queue_id: str, item_id, edited_plan: dict) -> bool:
    """Send an approved plan into the live plan-fix session. Returns
    True if delivered, False if the session is closed (caller should
    fall back to SDK-resume)."""
    item = find_item(load_state(), queue_id, item_id)
    if item is None:
        raise LookupError(f"Item {item_id} not in queue {queue_id}")
    sid = item.get("session_id")
    if not sid:
        return False
    msg = _approve_plan_message(edited_plan)
    delivered = await sessions.send_user_message(sid, msg)
    if delivered:
        set_item_plan(queue_id, item_id, edited_plan)
        set_item_plan_status(queue_id, item_id, "executing")
        set_item_result(queue_id, item_id, {
            "action": "execute-plan",
            "status": "running",
            "message": "Executing approved plan…",
        })
    return delivered


def _approve_drafts_message(edited_drafts: dict) -> str:
    """Format approved reply drafts as the phase-2 trigger message for
    the address-review-comments skill. `APPROVED REPLIES:` is the
    sentinel the skill checks for."""
    import json as _json
    return (
        "APPROVED REPLIES:\n\n"
        "```json\n" + _json.dumps(edited_drafts, indent=2) + "\n```\n\n"
        "Post each reply exactly per the skill's phase-2 procedure. "
        "Emit the standard ```json output when you're done."
    )


def _item_repo_slug_for(queue_id: str, item: dict) -> str:
    """Resolve the repo slug for card-level ops: item's stamped repo
    wins, queue's configured repo is the fallback. Tied together so
    every action-scoped gh call agrees."""
    from .github import item_repo_slug, queue_repo_slug
    slug = item_repo_slug(item)
    if slug:
        return slug
    try:
        from .queues import get_queue_config
        return queue_repo_slug(get_queue_config(queue_id))
    except Exception:
        return queue_repo_slug({})


async def approve_drafts(queue_id: str, item_id, edited_drafts: dict) -> dict:
    """Deliver approved reply drafts. Prefers the live phase-2 session; if
    it has closed (common after a reboot), posts each reply directly via
    `gh api` so the user doesn't have to re-run `address-comments` just to
    fire off the approved text.

    Returns `{"via": "session" | "direct" | "dry_run", ...}`. Raises
    `RuntimeError` if the direct fallback fails catastrophically."""
    item = find_item(load_state(), queue_id, item_id)
    if item is None:
        raise LookupError(f"Item {item_id} not in queue {queue_id}")
    sid = item.get("session_id")
    if sid:
        delivered = await sessions.send_user_message(
            sid, _approve_drafts_message(edited_drafts))
        if delivered:
            set_item_drafts(queue_id, item_id, edited_drafts)
            set_item_drafts_status(queue_id, item_id, "executing")
            set_item_result(queue_id, item_id, {
                "action": "post-replies",
                "status": "running",
                "message": "Posting approved replies…",
            })
            return {"via": "session"}

    # Session is gone — post directly. Every draft carries
    # `first_comment_id` + `reply_body`, so we have everything we need.
    cfg = load_config()
    dry_run = bool(cfg.get("actions", {}).get("dry_run", True))
    pr_number = item.get("number")
    if not pr_number:
        raise RuntimeError("item has no PR number; cannot post replies.")
    threads = edited_drafts.get("threads") or []
    # "Fix anyway" overrides need the live skill session to actually
    # apply the code change, commit, and push. Without a session we
    # can only post text replies — surface a clear error rather than
    # silently dropping the override intent.
    overrides = [t for t in threads if t.get("override_fix_anyway")]
    if overrides:
        set_item_drafts_status(queue_id, item_id, "error")
        set_item_result(queue_id, item_id, {
            "action": "post-replies",
            "status": "needs_human",
            "message": (
                f"{len(overrides)} thread(s) marked \"fix anyway\" need a "
                "live session to apply the code change. Re-run "
                "`address-comments` and approve again."),
        })
        raise RuntimeError(
            "fix-anyway overrides require a live session — re-run "
            "address-comments and approve again."
        )
    posted, resolved, skipped, errors = 0, 0, 0, []
    # Pin the repo for every post/resolve call in this batch. Item's
    # stamped repo wins, falls back to the queue's configured repo.
    _repo_token = github._current_repo_slug.set(
        _item_repo_slug_for(queue_id, item))
    try:
        for t in threads:
            body = (t.get("reply_body") or "").strip()
            fcid = t.get("first_comment_id")
            should_resolve = bool(t.get("should_resolve"))
            thread_id = t.get("thread_id") or t.get("id")
            if not body or not fcid:
                # No reply drafted — but user may still have asked to
                # resolve this thread (e.g., reviewer's concern was
                # already addressed in a prior commit). Honor that.
                if should_resolve and thread_id and not dry_run:
                    try:
                        github.resolve_review_thread(thread_id)
                        resolved += 1
                    except Exception as exc:
                        errors.append({"thread_id": thread_id, "error": str(exc)})
                else:
                    skipped += 1
                continue
            if dry_run:
                continue
            try:
                github.post_review_reply(int(pr_number), int(fcid), body)
                posted += 1
            except Exception as exc:
                errors.append({"first_comment_id": fcid, "error": str(exc)})
                continue
            if should_resolve and thread_id:
                try:
                    github.resolve_review_thread(thread_id)
                    resolved += 1
                except Exception as exc:
                    errors.append({"thread_id": thread_id, "error": str(exc)})
    finally:
        github._current_repo_slug.reset(_repo_token)

    set_item_drafts(queue_id, item_id, edited_drafts)
    if errors:
        set_item_drafts_status(queue_id, item_id, "error")
        set_item_result(queue_id, item_id, {
            "action": "post-replies",
            "status": "error",
            "message": (f"Direct post failed for {len(errors)} of "
                        f"{len(threads)} threads."),
            "errors": errors,
            "posted": posted,
        })
        raise RuntimeError(
            f"posted {posted}/{len(threads)}; {len(errors)} failures"
        )

    via = "dry_run" if dry_run else "direct"
    set_item_drafts_status(queue_id, item_id, "done")
    set_item_state(queue_id, item_id, "done")
    eligible = sum(
        1 for t in threads if (t.get("reply_body") or "").strip()
    )
    to_resolve = sum(1 for t in threads if t.get("should_resolve"))
    if dry_run:
        msg = (f"dry_run — would post {eligible} reply(ies) and resolve "
               f"{to_resolve} thread(s) directly.")
    else:
        msg_parts = [f"Posted {posted} reply(ies)"]
        if resolved:
            msg_parts.append(f"resolved {resolved} thread(s)")
        msg_parts.append("(session had closed)")
        msg = ", ".join(msg_parts[:-1]) + " " + msg_parts[-1] + "."
    set_item_result(queue_id, item_id, {
        "action": "post-replies",
        "status": "skipped_dry_run" if dry_run else "completed",
        "message": msg,
        "via": via,
        "posted": posted,
        "resolved": resolved,
        "skipped": skipped,
    })
    return {"via": via, "posted": posted, "resolved": resolved, "skipped": skipped}


def continue_action(queue_id: str, item_id) -> str | None:
    """Resume a stuck session on an existing worktree. Requires the item
    to have an SDK session id persisted in `last_result.meta.session_id`
    (written when the original session completed its first turn). Same
    worktree, same skill, same action — just a fresh process with SDK
    resume so Claude keeps its memory of the work it did."""
    item = find_item(load_state(), queue_id, item_id)
    if item is None:
        raise LookupError(f"Item {item_id} not in queue {queue_id}")
    last_result = item.get("last_result") or {}
    action_id = last_result.get("action")
    if not action_id:
        raise RuntimeError("Item has no prior action to continue.")
    # "execute-plan" / "post-replies" are pseudo-actions written during
    # phase 2 (after the human approves). The underlying session is the
    # original skill — resume with that spec.
    if action_id == "execute-plan":
        action_id = "plan-fix"
    elif action_id == "post-replies":
        action_id = "address-comments"
    spec = ACTIONS.get(action_id)
    if spec is None or spec["skill"] is None:
        raise RuntimeError(f"Action {action_id!r} is not continuable.")
    sdk_sid = (last_result.get("meta") or {}).get("session_id")
    if not sdk_sid:
        raise RuntimeError(
            "No SDK session id on record — nothing to resume. "
            "Re-run the original action instead."
        )

    if spec["in_progress_state"]:
        set_item_state(queue_id, item_id, spec["in_progress_state"])
    set_item_result(queue_id, item_id, {
        "action": action_id,
        "status": "queued",
        "message": f"Resuming `{action_id}`…",
    })

    cfg = load_config()
    dry_run = bool(cfg.get("actions", {}).get("dry_run", True))
    wt_path = None
    if spec["worktree_required"]:
        head_ref = (item.get("raw") or {}).get("headRefName")
        if not head_ref:
            raise RuntimeError("PR has no headRefName; cannot create worktree.")
        wt_path = worktree.ensure_worktree(item_id, head_ref)
        cwd = str(wt_path)
    else:
        cwd = str(worktree.repo_path())

    context = {
        "pr": {
            "owner": cfg["repo"]["owner"],
            "name": cfg["repo"]["name"],
            "number": item.get("number"),
            "url": item.get("url"),
            "title": item.get("title"),
            "head_ref": (item.get("raw") or {}).get("headRefName"),
        },
        "triage": {
            "proposal": item.get("proposal"),
            "source": item.get("triage_source"),
            "notes": item.get("triage_notes"),
        },
        "identity": (cfg.get("identity") or {}),
        "dry_run": dry_run,
        "worktree_path": str(wt_path) if wt_path else None,
        "resumed": True,
    }

    def _on_started(s):
        set_item_result(queue_id, item_id, {
            "action": action_id,
            "status": "running",
            "message": f"Resuming `{action_id}`…",
        })

    def _on_first_turn(s):
        result = dict(s.final_result or {})
        result.setdefault("status", "completed")
        result["action"] = action_id
        status = result.get("status", "completed")
        if status in SUCCESS_STATUSES and spec["terminal_state"]:
            set_item_state(queue_id, item_id, spec["terminal_state"])
        set_item_result(queue_id, item_id, result)

    session_id = sessions.start_session(
        spec["skill"], context, cwd,
        kind="action",
        queue_id=queue_id,
        item_id=item_id,
        action_id=action_id,
        on_started=_on_started,
        on_first_turn_complete=_on_first_turn,
        sdk_resume=sdk_sid,
        initial_user_message=CONTINUE_NUDGE,
    )
    set_item_session_id(queue_id, item_id, session_id)
    return session_id


def dispatch(queue_id: str, item_id, action_id: str,
             extra_context: dict | None = None) -> str | None:
    """Kick off an action. Returns the session_id (or None for skip)."""
    spec = ACTIONS.get(action_id)
    if spec is None:
        raise ValueError(f"Unknown action: {action_id}")

    item = find_item(load_state(), queue_id, item_id)
    if item is None:
        raise LookupError(f"Item {item_id} not in queue {queue_id}")

    # Instant (skip / await-update): no session. Abort any idle action
    # session for this item — otherwise it lingers in memory until its
    # 30-min idle timeout and blocks further actions.
    if spec["skill"] is None:
        sessions.abort_sessions_for_item(queue_id, item_id, kind="action")
        if spec["terminal_state"]:
            set_item_state(queue_id, item_id, spec["terminal_state"])
        if action_id == "await-update":
            set_item_parked_at(queue_id, item_id, _now())
            set_item_result(queue_id, item_id, {
                "action": action_id,
                "status": "parked",
                "message": "Awaiting PR activity — auto-unparks on next update.",
            })
        else:
            set_item_result(queue_id, item_id, {
                "action": action_id,
                "status": "skipped",
                "message": "Skipped by user.",
            })
        return None

    # Guard: at most one live action session per item. A second click
    # (or a stale double-submit) would otherwise orphan the first.
    with sessions._SESSIONS_LOCK:
        for s in sessions.SESSIONS.values():
            if (s.kind == "action" and s.queue_id == queue_id
                    and s.item_id == item_id
                    and s.status not in ("closed", "closing", "error")):
                return None

    if spec["in_progress_state"]:
        set_item_state(queue_id, item_id, spec["in_progress_state"])
    set_item_result(queue_id, item_id, {
        "action": action_id,
        "status": "queued",
        "message": f"Queued `{action_id}`…",
    })

    cfg = load_config()
    dry_run = bool(cfg.get("actions", {}).get("dry_run", True))

    try:
        wt_path = None
        if spec["worktree_required"]:
            head_ref = (item.get("raw") or {}).get("headRefName")
            if not head_ref:
                raise RuntimeError("PR has no headRefName; cannot create worktree.")
            wt_path = worktree.ensure_worktree(item_id, head_ref)
            cwd = str(wt_path)
        else:
            cwd = str(worktree.repo_path())

        # For write-action skills (rebase / fix-precommit / attempt-fix
        # / update-lockfile), the push path depends on whether the PR
        # is in-repo or a fork. Resolve up front so the skill doesn't
        # have to guess:
        #   - in-repo: push to origin
        #   - fork + maintainer_can_modify: push to a per-PR fork remote
        #   - fork + NOT modifiable: bail now with needs_human
        push_remote = None
        push_ref = (item.get("raw") or {}).get("headRefName")
        WRITE_ACTIONS = {
            "rebase", "rebase-review",
            "fix-precommit", "fix-precommit-review",
            "attempt-fix", "update-lockfile",
        }
        if action_id in WRITE_ACTIONS and wt_path:
            try:
                pr_num = int(item.get("number"))
                push_remote, push_ref = github.ensure_push_remote(pr_num, wt_path)
            except Exception as exc:
                # Fork without maintainer edits → graceful bail.
                if spec["failure_state"]:
                    set_item_state(queue_id, item_id, spec["failure_state"])
                set_item_result(queue_id, item_id, {
                    "action": action_id,
                    "status": "needs_human",
                    "message": f"cannot push: {exc}",
                })
                return None

        context = {
            "pr": {
                "owner": cfg["repo"]["owner"],
                "name": cfg["repo"]["name"],
                "number": item.get("number"),
                "url": item.get("url"),
                "title": item.get("title"),
                "head_ref": (item.get("raw") or {}).get("headRefName"),
                "push_remote": push_remote,
                "push_ref": push_ref,
            },
            "triage": {
                "proposal": item.get("proposal"),
                "source": item.get("triage_source"),
                "notes": item.get("triage_notes"),
            },
            "identity": (cfg.get("identity") or {}),
            "dry_run": dry_run,
            "worktree_path": str(wt_path) if wt_path else None,
        }
        context.update(extra_context or {})

        def _on_started(s):
            set_item_result(queue_id, item_id, {
                "action": action_id,
                "status": "running",
                "message": f"Running `{action_id}`…",
            })

        def _on_first_turn(s):
            result = dict(s.final_result or {})
            result.setdefault("status", "completed")
            result["action"] = action_id
            status = result.get("status", "completed")
            # plan-fix phase 1: stash the plan on the item, keep the card
            # in progress so the plan pane renders and the session stays
            # live for the approve/edit flow.
            if action_id == "plan-fix" and status == "plan":
                plan = {k: v for k, v in result.items()
                        if k not in ("status", "action", "meta", "message")}
                if "message" in result:
                    plan["message"] = result["message"]
                set_item_plan(queue_id, item_id, plan)
                set_item_plan_status(queue_id, item_id, "proposed")
                set_item_result(queue_id, item_id, result)
                return
            # address-comments phase 1: stash the per-thread reply drafts
            # on the item. The card stays in progress until the human
            # reviews/approves them in the drafts modal.
            if action_id == "address-comments" and status == "drafts":
                drafts = {k: v for k, v in result.items()
                          if k not in ("status", "action", "meta", "message")}
                if "message" in result:
                    drafts["message"] = result["message"]
                set_item_drafts(queue_id, item_id, drafts)
                set_item_drafts_status(queue_id, item_id, "proposed")
                set_item_result(queue_id, item_id, result)
                return
            # summarize-diff: stash the bullets on the card, DON'T move
            # state. Pure read-aid for the reviewer.
            if action_id == "summarize-diff" and status == "summary":
                summary = {k: v for k, v in result.items()
                           if k not in ("status", "action", "meta")}
                set_item_diff_summary(queue_id, item_id, summary)
                set_item_result(queue_id, item_id, result)
                return
            # assess-on-worktree: stash the richer assessment, DON'T move
            # state. The card stays in its triage column; the assessment
            # pane renders the new findings.
            if action_id == "assess-on-worktree" and status == "assessment":
                assessment = {k: v for k, v in result.items()
                              if k not in ("status", "action", "meta")}
                set_item_assessment(queue_id, item_id, assessment)
                set_item_result(queue_id, item_id, result)
                return
            if status in SUCCESS_STATUSES:
                if spec["terminal_state"]:
                    set_item_state(queue_id, item_id, spec["terminal_state"])
                # nudge-author parks the card in `awaiting update`; stamp
                # parked_at so the refresh loop auto-unparks it once the
                # PR author acts (push, comment, resolve thread).
                if action_id == "nudge-author":
                    set_item_parked_at(queue_id, item_id, _now())
            elif status == "needs_human":
                # The skill gave up and wants a human to look. Route back
                # to the queue's failure_state (usually "in triage") so the
                # card doesn't linger in the in-progress column.
                if spec["failure_state"]:
                    set_item_state(queue_id, item_id, spec["failure_state"])
            # Other non-success (error, unparsed, ...): leave the card at
            # `in_progress_state` so the chat button stays live and the
            # user can follow up with the session before idle-timeout.
            set_item_result(queue_id, item_id, result)

        # Phase-2 completion hook for skills that round-trip through the
        # UI (plan-fix after APPROVED PLAN, address-comments after
        # APPROVED REPLIES). Flips the card to done on success and
        # records the execution result either way.
        def _on_turn_done(s, result):
            if action_id not in ("plan-fix", "address-comments"):
                return
            if s._first_turn_done is None or not s._first_turn_done.is_set():
                return  # phase 1 handled by _on_first_turn
            status = (result or {}).get("status")
            if action_id == "plan-fix":
                if status == "plan":
                    return  # another plan iteration, not an execution
                execution = dict(result or {})
                execution["action"] = "execute-plan"
                if status in SUCCESS_STATUSES:
                    set_item_state(queue_id, item_id, "done")
                    set_item_plan_status(queue_id, item_id, "done")
                set_item_result(queue_id, item_id, execution)
            else:  # address-comments
                if status == "drafts":
                    return  # another drafts iteration, not an execution
                execution = dict(result or {})
                execution["action"] = "post-replies"
                if status in SUCCESS_STATUSES:
                    set_item_state(queue_id, item_id, "done")
                    set_item_drafts_status(queue_id, item_id, "done")
                set_item_result(queue_id, item_id, execution)

        session_id = sessions.start_session(
            spec["skill"], context, cwd,
            kind="action",
            queue_id=queue_id,
            item_id=item_id,
            action_id=action_id,
            on_started=_on_started,
            on_first_turn_complete=_on_first_turn,
            on_turn_complete=(_on_turn_done
                              if action_id in ("plan-fix", "address-comments")
                              else None),
        )
        set_item_session_id(queue_id, item_id, session_id)
        return session_id

    except Exception as exc:
        if spec["failure_state"]:
            set_item_state(queue_id, item_id, spec["failure_state"])
        set_item_result(queue_id, item_id, {
            "action": action_id,
            "status": "error",
            "message": str(exc),
            "traceback": traceback.format_exc(limit=3),
        })
        return None
