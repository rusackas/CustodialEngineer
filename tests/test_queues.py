"""Tests for the JSON-backed queue state."""
from repobot import queues


def _pr(n):
    return {"id": n, "number": n, "title": f"pr {n}", "url": "u", "raw": {}}


def test_upsert_adds_new_items_in_initial_state():
    queues.upsert_items("q", [_pr(1), _pr(2)], initial_state="in triage")
    state = queues.load_state()
    items = queues.queue_items(state, "q")
    assert [i["state"] for i in items] == ["in triage", "in triage"]
    assert [i["id"] for i in items] == [1, 2]


def test_upsert_is_idempotent():
    queues.upsert_items("q", [_pr(1)], "in triage")
    queues.upsert_items("q", [_pr(1)], "in triage")
    assert len(queues.queue_items(queues.load_state(), "q")) == 1


def test_set_item_state_transitions():
    queues.upsert_items("q", [_pr(1)], "in triage")
    queues.set_item_state("q", 1, "done")
    item = queues.find_item(queues.load_state(), "q", 1)
    assert item["state"] == "done"
    assert "state_changed_at" in item


def test_count_non_done_ignores_done():
    queues.upsert_items("q", [_pr(1), _pr(2), _pr(3)], "in triage")
    queues.set_item_state("q", 1, "done")
    assert queues.count_non_done(queues.load_state(), "q") == 2


def test_delete_item_removes_and_is_safe_when_missing():
    queues.upsert_items("q", [_pr(1), _pr(2)], "in triage")
    queues.delete_item("q", 1)
    ids = [i["id"] for i in queues.queue_items(queues.load_state(), "q")]
    assert ids == [2]
    queues.delete_item("q", 99)  # no-op, no exception
    queues.delete_item("nonexistent", 1)  # no-op, no exception


def test_set_triage_records_proposal_and_actions():
    queues.upsert_items("q", [_pr(1)], "in triage")
    queues.set_triage("q", 1, "go rebase", ["rebase", "close"])
    item = queues.find_item(queues.load_state(), "q", 1)
    assert item["proposal"] == "go rebase"
    assert item["actions"] == ["rebase", "close"]
