"""Tests for the fetch-and-triage orchestrator, specifically slot math."""
from repobot import queues, runner, sessions


def _fake_prs(n):
    return [{"number": i, "title": f"pr {i}", "url": "u",
             "mergeable": "MERGEABLE", "updatedAt": None, "headRefName": f"b{i}"}
            for i in range(1, n + 1)]


def test_refresh_pulls_only_remaining_slots(monkeypatch):
    monkeypatch.setitem(runner.FETCHERS, "failing-dependabot-prs",
                        lambda: _fake_prs(20))
    monkeypatch.setitem(runner.TRIAGERS, "failing-dependabot-prs",
                        lambda item, queue_id=None: ("msg", ["close"]))

    runner.run_queue("failing-dependabot-prs", wait_for_triage=True)
    items = queues.queue_items(queues.load_state(), "failing-dependabot-prs")
    assert len(items) == 10  # max_in_flight

    for i in items[:6]:
        queues.set_item_state("failing-dependabot-prs", i["id"], "done")

    runner.run_queue("failing-dependabot-prs", wait_for_triage=True)
    items = queues.queue_items(queues.load_state(), "failing-dependabot-prs")
    # 6 done + 4 in triage + 6 new = 16 total
    assert len(items) == 16
    assert queues.count_non_done(queues.load_state(), "failing-dependabot-prs") == 10


def test_triage_populates_proposal_and_actions(monkeypatch):
    monkeypatch.setitem(runner.FETCHERS, "failing-dependabot-prs",
                        lambda: _fake_prs(2))
    monkeypatch.setitem(runner.TRIAGERS, "failing-dependabot-prs",
                        lambda item, queue_id=None: ("go", ["close"]))

    runner.run_queue("failing-dependabot-prs", wait_for_triage=True)
    for item in queues.queue_items(queues.load_state(), "failing-dependabot-prs"):
        assert item["proposal"] == "go"
        assert item["actions"] == ["close"]


def test_pending_ci_prs_are_skipped(monkeypatch):
    """PRs whose CI is still in flight shouldn't be added to the queue;
    we wait until CI settles (passing or failing) so triage has signal."""
    mixed = [
        {"number": 1, "title": "pending pr", "url": "u",
         "ci_status": "pending", "mergeable": "MERGEABLE"},
        {"number": 2, "title": "passing pr", "url": "u",
         "ci_status": "passing", "mergeable": "MERGEABLE"},
        {"number": 3, "title": "failing pr", "url": "u",
         "ci_status": "failing", "mergeable": "MERGEABLE"},
    ]
    monkeypatch.setitem(runner.FETCHERS, "failing-dependabot-prs",
                        lambda: mixed)
    monkeypatch.setitem(runner.TRIAGERS, "failing-dependabot-prs",
                        lambda item, queue_id=None: ("msg", ["close"]))

    runner.run_queue("failing-dependabot-prs", wait_for_triage=True)
    items = queues.queue_items(queues.load_state(), "failing-dependabot-prs")
    numbers = sorted(i["number"] for i in items)
    assert numbers == [2, 3]  # pending #1 was skipped


def test_stale_done_demote_clears_last_result_and_aborts_triage(monkeypatch):
    """When the PR is touched after we marked it done, the card should
    demote back to initial_state, drop the now-misleading last_result,
    and kill any lingering idle triage session so a fresh one can spawn."""
    monkeypatch.setitem(runner.FETCHERS, "failing-dependabot-prs",
                        lambda: _fake_prs(1))
    monkeypatch.setitem(runner.TRIAGERS, "failing-dependabot-prs",
                        lambda item, queue_id=None: ("msg", ["close"]))

    runner.run_queue("failing-dependabot-prs", wait_for_triage=True)
    item = queues.queue_items(queues.load_state(), "failing-dependabot-prs")[0]
    queues.set_item_state("failing-dependabot-prs", item["id"], "done")
    queues.set_item_result("failing-dependabot-prs", item["id"], {
        "action": "skip", "status": "skipped", "message": "Skipped by user.",
    })

    aborts: list = []
    monkeypatch.setattr(sessions, "abort_sessions_for_item",
                        lambda qid, iid, kind=None: aborts.append((qid, iid, kind)) or 1)

    fresh_raw = dict(item["raw"], updatedAt="2099-01-01T00:00:00Z")
    monkeypatch.setitem(runner.FETCHERS, "failing-dependabot-prs",
                        lambda: [fresh_raw])
    runner.run_queue("failing-dependabot-prs", refresh_existing=True,
                     wait_for_triage=True)

    reloaded = queues.queue_items(queues.load_state(), "failing-dependabot-prs")[0]
    assert reloaded["state"] == "in triage"
    assert "last_result" not in reloaded
    assert ("failing-dependabot-prs", item["id"], "triage") in aborts


def test_retriage_clears_verdict_aborts_session_and_reruns(monkeypatch):
    """retriage should blow away the existing verdict, abort any live
    triage session, and run a fresh triage pass."""
    calls = {"n": 0}

    def _counting_triager(item, queue_id=None):
        calls["n"] += 1
        return (f"verdict {calls['n']}", ["close"])

    monkeypatch.setitem(runner.FETCHERS, "failing-dependabot-prs",
                        lambda: _fake_prs(1))
    monkeypatch.setitem(runner.TRIAGERS, "failing-dependabot-prs",
                        _counting_triager)

    runner.run_queue("failing-dependabot-prs", wait_for_triage=True)
    item = queues.queue_items(queues.load_state(), "failing-dependabot-prs")[0]
    assert item["proposal"] == "verdict 1"
    queues.set_item_result("failing-dependabot-prs", item["id"], {
        "action": "skip", "status": "skipped", "message": "Skipped by user.",
    })

    aborts: list = []
    monkeypatch.setattr(sessions, "abort_sessions_for_item",
                        lambda qid, iid, kind=None: aborts.append((qid, iid, kind)) or 1)

    runner.retriage_item("failing-dependabot-prs", item["id"], wait=True)
    reloaded = queues.queue_items(queues.load_state(), "failing-dependabot-prs")[0]
    assert reloaded["proposal"] == "verdict 2"
    assert "last_result" not in reloaded
    assert ("failing-dependabot-prs", item["id"], "triage") in aborts


def test_awaiting_cards_dont_count_against_max_in_flight(monkeypatch):
    """Parked cards can pile up indefinitely; they shouldn't consume
    column slots or starve fresh triage."""
    monkeypatch.setitem(runner.FETCHERS, "failing-dependabot-prs",
                        lambda: _fake_prs(10))
    monkeypatch.setitem(runner.TRIAGERS, "failing-dependabot-prs",
                        lambda item, queue_id=None: ("msg", ["close"]))

    runner.run_queue("failing-dependabot-prs", wait_for_triage=True)
    items = queues.queue_items(queues.load_state(), "failing-dependabot-prs")
    assert len(items) == 10

    # Park all 10. They should all move out of the "slot-eligible" pool,
    # so the next fetch can pull in 10 new PRs.
    for it in items:
        queues.set_item_state("failing-dependabot-prs", it["id"], "awaiting update")
        queues.set_item_parked_at("failing-dependabot-prs", it["id"], queues._now())

    monkeypatch.setitem(runner.FETCHERS, "failing-dependabot-prs",
                        lambda: _fake_prs(20))
    runner.run_queue("failing-dependabot-prs", wait_for_triage=True)
    items = queues.queue_items(queues.load_state(), "failing-dependabot-prs")
    assert len(items) == 20  # original 10 parked + 10 new


def test_awaiting_card_unparks_when_pr_updates(monkeypatch):
    """External signal arrives → card auto-demotes to initial_state,
    clears any stale verdict, triage re-runs."""
    monkeypatch.setitem(runner.FETCHERS, "failing-dependabot-prs",
                        lambda: _fake_prs(1))
    monkeypatch.setitem(runner.TRIAGERS, "failing-dependabot-prs",
                        lambda item, queue_id=None: ("msg", ["close"]))

    runner.run_queue("failing-dependabot-prs", wait_for_triage=True)
    item = queues.queue_items(queues.load_state(), "failing-dependabot-prs")[0]
    queues.set_item_state("failing-dependabot-prs", item["id"], "awaiting update")
    queues.set_item_parked_at("failing-dependabot-prs", item["id"],
                              "2026-01-01T00:00:00Z")

    fresh_raw = dict(item["raw"], updatedAt="2099-01-01T00:00:00Z")
    monkeypatch.setitem(runner.FETCHERS, "failing-dependabot-prs",
                        lambda: [fresh_raw])
    runner.run_queue("failing-dependabot-prs", refresh_existing=True,
                     wait_for_triage=True)

    reloaded = queues.queue_items(queues.load_state(), "failing-dependabot-prs")[0]
    assert reloaded["state"] == "in triage"
    assert "parked_at" not in reloaded
    assert reloaded["proposal"] == "msg"  # fresh triage ran


def test_queue_setting_overrides_max_in_flight(monkeypatch):
    """The UI-editable per-queue override should shrink the column cap
    below what's declared in config.yaml."""
    monkeypatch.setitem(runner.FETCHERS, "failing-dependabot-prs",
                        lambda: _fake_prs(20))
    monkeypatch.setitem(runner.TRIAGERS, "failing-dependabot-prs",
                        lambda item, queue_id=None: ("msg", ["close"]))
    queues.update_queue_setting("failing-dependabot-prs", "max_in_flight", 3)

    runner.run_queue("failing-dependabot-prs", wait_for_triage=True)
    items = queues.queue_items(queues.load_state(), "failing-dependabot-prs")
    assert len(items) == 3


def test_intake_paused_blocks_new_cards_but_not_refresh(monkeypatch):
    monkeypatch.setitem(runner.TRIAGERS, "failing-dependabot-prs",
                        lambda item, queue_id=None: ("msg", ["close"]))
    monkeypatch.setitem(runner.FETCHERS, "failing-dependabot-prs",
                        lambda: _fake_prs(5))
    runner.run_queue("failing-dependabot-prs", wait_for_triage=True)
    baseline = len(queues.queue_items(
        queues.load_state(), "failing-dependabot-prs"))
    assert baseline == 5

    queues.update_queue_setting("failing-dependabot-prs",
                                "intake_paused", True)
    monkeypatch.setitem(runner.FETCHERS, "failing-dependabot-prs",
                        lambda: _fake_prs(15))
    runner.run_queue("failing-dependabot-prs", wait_for_triage=True)
    # No new cards should have landed while paused.
    assert len(queues.queue_items(
        queues.load_state(), "failing-dependabot-prs")) == baseline


def test_worker_slots_caps_triage_fanout(monkeypatch):
    """With 5 pending cards and worker_slots=2, a single `run_queue` tick
    should only triage 2. The next tick picks up the rest."""
    # Pre-seed the column so we don't rely on the fetcher — this keeps
    # the test synchronous (threads complete via wait_for_triage=True)
    # and avoids leaking half-done threads into the real state file
    # after monkeypatch reverts STATE_PATH on teardown.
    monkeypatch.setitem(runner.FETCHERS, "failing-dependabot-prs",
                        lambda: [])
    call_count = 0

    def counting_triage(item, queue_id=None):
        nonlocal call_count
        call_count += 1
        return ("msg", ["close"])

    monkeypatch.setitem(runner.TRIAGERS, "failing-dependabot-prs",
                        counting_triage)

    seed = [{**pr, "id": pr["number"], "raw": pr} for pr in _fake_prs(5)]
    queues.upsert_items("failing-dependabot-prs", seed, "in triage")
    queues.update_queue_setting("failing-dependabot-prs", "worker_slots", 2)

    runner.run_queue("failing-dependabot-prs", wait_for_triage=True)
    # Only 2 of the 5 pending should have been triaged on this tick.
    assert call_count == 2
    triaged = [i for i in queues.queue_items(
        queues.load_state(), "failing-dependabot-prs") if i.get("proposal")]
    assert len(triaged) == 2


def test_failing_triage_still_sets_a_proposal(monkeypatch):
    monkeypatch.setitem(runner.FETCHERS, "failing-dependabot-prs",
                        lambda: _fake_prs(1))

    def _boom(item, queue_id=None):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(runner.TRIAGERS, "failing-dependabot-prs", _boom)
    runner.run_queue("failing-dependabot-prs", wait_for_triage=True)
    item = queues.queue_items(queues.load_state(), "failing-dependabot-prs")[0]
    assert "kaboom" in item["proposal"]
    assert item["actions"]  # not empty
