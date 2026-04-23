"""Tests for the action dispatcher.

We stub `sessions.start_session` so no Claude session is spawned; the
stub records its arguments and synchronously invokes the first-turn
callback to simulate the session landing its structured JSON result.
"""
import time
import uuid

from repobot import actions, queues


def _setup_item(item_id=1, head_ref="dep-bump"):
    queues.upsert_items(
        "failing-dependabot-prs",
        [{
            "id": item_id, "number": item_id, "title": "t", "url": "u",
            "raw": {"headRefName": head_ref},
        }],
        initial_state="in triage",
    )


def _wait_for_state(item_id, target, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        item = queues.find_item(queues.load_state(), "failing-dependabot-prs", item_id)
        if item and item.get("state") == target:
            return item
        time.sleep(0.02)
    raise AssertionError(f"item {item_id} never reached state {target}")


def _fake_sessions(final_result):
    """Return a fake start_session that fires the first-turn callback
    synchronously. Worktree cleanup is deferred to the delete endpoint
    (not a session callback), so we don't simulate on_close here."""
    calls = {}
    fr = final_result

    def fake_start_session(skill, context, cwd, *, kind="action",
                           queue_id=None, item_id=None, action_id=None,
                           on_started=None, on_first_turn_complete=None,
                           on_close=None, on_turn_complete=None,
                           **_unused):
        calls["skill"] = skill
        calls["context"] = context
        calls["cwd"] = cwd
        calls["kind"] = kind
        calls["action_id"] = action_id
        sid = uuid.uuid4().hex
        calls["session_id"] = sid

        class _FakeState:
            pass
        s = _FakeState()
        s.session_id = sid
        s.final_result = fr

        if on_started:
            on_started(s)
        if on_first_turn_complete:
            on_first_turn_complete(s)
        return sid

    return calls, fake_start_session


def test_skip_is_instant_and_lands_in_done():
    _setup_item(1)
    actions.dispatch("failing-dependabot-prs", 1, "skip")
    item = queues.find_item(queues.load_state(), "failing-dependabot-prs", 1)
    assert item["state"] == "done"
    assert item["last_result"]["status"] == "skipped"


def test_non_skip_action_spawns_session_and_completes(monkeypatch):
    calls, fake = _fake_sessions({"status": "completed", "message": "ok"})
    monkeypatch.setattr(actions.sessions, "start_session", fake)

    _setup_item(2)
    actions.dispatch("failing-dependabot-prs", 2, "close")

    item = _wait_for_state(2, "done")
    assert item["last_result"]["status"] == "completed"
    assert calls["skill"] == "close-pr"
    assert calls["context"]["pr"]["number"] == 2
    assert "dry_run" in calls["context"]


def test_worktree_action_creates_worktree_and_leaves_it(monkeypatch):
    """Worktrees live until the user deletes the card (see api.py delete
    endpoint). We no longer tear them down when the session closes, so a
    post-timeout resume can still cd into them."""
    created = []
    removed = []

    monkeypatch.setattr(actions.worktree, "ensure_worktree",
                        lambda n, ref: created.append((n, ref)) or "/tmp/fake")
    monkeypatch.setattr(actions.worktree, "remove_worktree",
                        lambda n: removed.append(n))
    _, fake = _fake_sessions({"status": "completed", "message": "ok"})
    monkeypatch.setattr(actions.sessions, "start_session", fake)

    _setup_item(3, head_ref="dep/bump-foo")
    actions.dispatch("failing-dependabot-prs", 3, "rebase")
    _wait_for_state(3, "done")

    assert created == [(3, "dep/bump-foo")]
    assert removed == []  # no automatic teardown anymore


def test_session_error_stays_in_progress_with_result(monkeypatch):
    """Non-success results (error / unparsed / needs_human) keep the card
    at in_progress_state so the chat button stays visible and the user
    can intervene via the session modal."""
    _, fake = _fake_sessions({"status": "error", "message": "nope"})
    monkeypatch.setattr(actions.sessions, "start_session", fake)

    _setup_item(4)
    actions.dispatch("failing-dependabot-prs", 4, "close")
    item = _wait_for_state(4, "in progress")
    assert item["last_result"]["status"] == "error"


def test_session_unparsed_stays_in_progress(monkeypatch):
    """Sessions that exhaust their turn budget without producing the JSON
    block land as 'unparsed' — those also stay in_progress for chat."""
    _, fake = _fake_sessions({"status": "unparsed",
                              "message": "Session produced no JSON block."})
    monkeypatch.setattr(actions.sessions, "start_session", fake)

    _setup_item(5)
    actions.dispatch("failing-dependabot-prs", 5, "close")
    item = _wait_for_state(5, "in progress")
    assert item["last_result"]["status"] == "unparsed"
