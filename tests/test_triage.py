"""Tests for the mechanical-triage fallback."""
from datetime import datetime, timedelta, timezone

from repobot.triage import mechanical_triage


def _item(**raw):
    return {"id": 1, "number": 1, "raw": raw}


def test_conflicting_proposes_rebase_chain():
    msg, actions = mechanical_triage(_item(mergeable="CONFLICTING"))
    assert "conflict" in msg.lower()
    assert actions[0] == "dependabot-rebase"
    assert "rebase" in actions and "close" in actions


def test_stale_proposes_recreate():
    old = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    msg, actions = mechanical_triage(_item(mergeable="MERGEABLE", updatedAt=old))
    assert "stale" in msg.lower()
    assert actions[0] == "dependabot-recreate"


def test_recent_failing_proposes_retrigger_and_lockfile():
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    _, actions = mechanical_triage(_item(mergeable="MERGEABLE", updatedAt=recent))
    assert "retrigger-ci" in actions
    assert "update-lockfile" in actions


def test_missing_updated_at_falls_through_to_default():
    # No updatedAt → age is None → neither conflicting nor stale branch fires.
    _, actions = mechanical_triage(_item(mergeable="MERGEABLE"))
    assert "retrigger-ci" in actions
