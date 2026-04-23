"""FastAPI app serving the kanban UI and action dispatch endpoints."""
import threading
import time

import json

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import github, icons as _icons, inbox as _inbox, markdown as md, sessions, worktree
from .actions import (
    CONTINUE_NUDGE,
    approve_drafts,
    approve_plan,
    continue_action,
    dispatch,
)
from .config import PROJECT_ROOT, load_config
from .queues import (
    _mutate,
    _now,
    delete_item,
    find_item,
    get_global_setting,
    get_queue_setting,
    get_queues_config,
    load_state,
    queue_items,
    set_item_drafts,
    set_item_drafts_status,
    set_item_parked_at,
    set_item_plan,
    set_item_plan_status,
    set_item_result,
    set_item_state,
    update_global_setting,
    update_queue_setting,
)
from .runner import refresh_one_item, retriage_item, run_queue

DEFAULT_AUTO_REFRESH_SECONDS = 30

TEMPLATES_DIR = PROJECT_ROOT / "repobot" / "templates"
STATIC_DIR = PROJECT_ROOT / "repobot" / "static"

app = FastAPI(title="repobot")


@app.middleware("http")
async def htmx_swallow_redirects(request: Request, call_next):
    """With hx-boost on the body, every form submit is XHR. A 303
    redirect back to `/` would trigger HTMX to fetch `/` and swap the
    whole page — defeating the point of the migration. Convert those
    same-origin 303s into a 204 No Content when the request is
    HTMX-driven; the client's afterRequest hook nudges the polling
    loop to re-fetch the changed region instead."""
    response = await call_next(request)
    if (request.headers.get("HX-Request")
            and response.status_code == 303):
        return Response(status_code=204)
    return response

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
from markupsafe import Markup as _Markup
templates.env.globals["icon"] = lambda name, **kw: _Markup(_icons.render(name, **kw))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _sweep_stale_session_state() -> None:
    """Sessions live in memory; on startup that's empty. So:
    - Strip session_id pointers (nothing to chat with anymore).
    - Any item with last_result.status == 'running' was interrupted —
      demote back to the queue's initial_state and mark the result
      'interrupted' so the user knows to retry.
    - If `auto_resume_on_boot` is enabled, call continue_action for
      each interrupted item that has an SDK session id we can resume
      from.
    - Prune any worktree on disk whose PR number isn't referenced by
      a live item (e.g. from a previous session the user deleted while
      the server was running, or leftovers from an older run)."""
    auto_resume = bool(get_global_setting("auto_resume_on_boot", False))
    queues_by_id = {q["id"]: q for q in get_queues_config()}
    now = _now()
    live_numbers: set[int] = set()
    resume_candidates: list[tuple[str, object]] = []

    def _m(state):
        for qid, bucket in state.get("queues", {}).items():
            q = queues_by_id.get(qid)
            for item in bucket.get("items", []):
                item.pop("session_id", None)
                item.pop("triage_session_id", None)
                lr = item.get("last_result") or {}
                if lr.get("status") in ("running", "queued", "starting") and q:
                    item["state"] = q["initial_state"]
                    item["state_changed_at"] = now
                    item["last_result"] = {
                        **lr,
                        "status": "interrupted",
                        "message": "Action interrupted by server restart. Retry from the buttons.",
                    }
                    item["last_result_at"] = now
                    sdk_sid = (lr.get("meta") or {}).get("session_id")
                    if auto_resume and sdk_sid and item.get("id") is not None:
                        resume_candidates.append((qid, item["id"]))
                num = item.get("number")
                if isinstance(num, int):
                    live_numbers.add(num)
    _mutate(_m)

    try:
        removed = worktree.prune_orphan_worktrees(live_numbers)
        if removed:
            print(f"[startup] pruned {len(removed)} orphan worktree(s): {removed}")
    except Exception as exc:
        print(f"[startup] worktree prune failed: {exc}")

    if resume_candidates:
        from . import actions as _actions
        resumed = 0
        for qid, item_id in resume_candidates:
            try:
                _actions.continue_action(qid, item_id)
                resumed += 1
            except Exception as exc:
                print(f"[startup] auto-resume failed for {qid}/{item_id}: {exc}")
        print(f"[startup] auto-resumed {resumed}/{len(resume_candidates)} "
              "interrupted action(s)")


_sweep_stale_session_state()


def _auto_refresh_interval() -> int:
    cfg = load_config()
    return int(cfg.get("auto_refresh", {}).get(
        "interval_seconds", DEFAULT_AUTO_REFRESH_SECONDS))


def _start_auto_refresh() -> None:
    """One daemon thread per queue. Each wakes every N seconds and calls
    `run_queue` (non-blocking triage fan-out) to keep the hopper full.
    Deep refresh — re-checking existing items' CI — is still manual via
    the Refresh button."""
    interval = _auto_refresh_interval()
    if interval <= 0:
        return
    for q in get_queues_config():
        def loop(qid=q["id"]):
            while True:
                try:
                    run_queue(qid, wait_for_triage=False)
                except Exception as exc:
                    print(f"[auto-refresh {qid}] {exc}")
                time.sleep(interval)
        threading.Thread(target=loop, daemon=True, name=f"auto-refresh-{q['id']}").start()


_start_auto_refresh()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    cfg = load_config()
    state = load_state()
    queues_cfg = get_queues_config()
    pending = False
    for q in queues_cfg:
        items = state.get("queues", {}).get(q["id"], {}).get("items", [])
        for item in items:
            if item.get("state") == q["initial_state"] and not item.get("proposal"):
                pending = True
                break
            if item.get("last_result", {}).get("status") == "running":
                pending = True
                break
        if pending:
            break

    # Fold UI overrides back into the queue dicts so the template can
    # render the currently-effective values without rummaging through
    # two sources of truth.
    for q in queues_cfg:
        q["effective_max_in_flight"] = int(get_queue_setting(
            q["id"], "max_in_flight", q["max_in_flight"]))
        q["effective_worker_slots"] = int(get_queue_setting(
            q["id"], "worker_slots",
            q.get("worker_slots", q["effective_max_in_flight"])))
        q["intake_paused"] = bool(get_queue_setting(
            q["id"], "intake_paused", False))

    stats_data = sessions.stats()
    stats_data["auto_resume_on_boot"] = bool(
        get_global_setting("auto_resume_on_boot", False))
    # Enrich live-session rows with PR number/title so the popover can
    # render a useful label without another round-trip.
    live_by_item: dict[str, dict] = {}
    for ls in stats_data.get("live", []):
        qid = ls.get("queue_id")
        iid = ls.get("item_id")
        if qid is None or iid is None:
            continue
        for item in state.get("queues", {}).get(qid, {}).get("items", []):
            if item.get("id") == iid:
                ls["number"] = item.get("number")
                ls["title"] = item.get("title")
                break
        live_by_item[f"{qid}:{iid}"] = ls

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "queues": queues_cfg,
            "state": state,
            "dry_run": bool(cfg.get("actions", {}).get("dry_run", True)),
            "pending": pending,
            "stats": stats_data,
            "live_by_item": live_by_item,
        },
    )


@app.post("/queues/{queue_id}/fetch")
def fetch_queue(queue_id: str):
    threading.Thread(
        target=run_queue, args=(queue_id,),
        kwargs={"wait_for_triage": True, "refresh_existing": True}, daemon=True,
    ).start()
    return RedirectResponse(url="/", status_code=303)


@app.post("/queues/{queue_id}/items/{item_id}/actions/{action_id}")
def act(queue_id: str, item_id: int, action_id: str,
        comment_body: str = Form("")):
    extra = {"comment_body": comment_body} if comment_body.strip() else None
    dispatch(queue_id, item_id, action_id, extra_context=extra)
    return RedirectResponse(url="/", status_code=303)


@app.post("/queues/{queue_id}/items/{item_id}/prompt")
def prompt(queue_id: str, item_id: int, instruction: str = Form(...)):
    instruction = instruction.strip()
    if not instruction:
        return RedirectResponse(url="/", status_code=303)
    dispatch(queue_id, item_id, "prompt", extra_context={"instruction": instruction})
    return RedirectResponse(url="/", status_code=303)


@app.post("/queues/{queue_id}/items/{item_id}/continue")
def cont(queue_id: str, item_id: int):
    try:
        continue_action(queue_id, item_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return RedirectResponse(url="/", status_code=303)


@app.post("/queues/{queue_id}/items/{item_id}/resume-live")
async def resume_live(queue_id: str, item_id: int):
    """Nudge a LIVE idle session to finish / emit its final JSON. Unlike
    the `continue` endpoint (which spins up a fresh SDK-resumed process
    for a closed session), this sends the nudge straight into the
    existing idle session's user queue."""
    item = find_item(load_state(), queue_id, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    sid = item.get("session_id") or item.get("triage_session_id")
    if not sid:
        raise HTTPException(status_code=400, detail="no live session on item")
    delivered = await sessions.send_user_message(sid, CONTINUE_NUDGE)
    if not delivered:
        raise HTTPException(status_code=409,
                            detail="session is closed — use the continue button instead")
    return RedirectResponse(url="/", status_code=303)


@app.post("/queues/{queue_id}/items/{item_id}/plan/approve")
async def approve_plan_endpoint(queue_id: str, item_id: int,
                                 plan_json: str = Form(...)):
    """Submit a (possibly edited) plan back to the live plan-fix session
    so it executes phase 2. The payload is the full edited plan dict as
    JSON — we round-trip it through the skill."""
    try:
        edited = json.loads(plan_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"invalid plan JSON: {exc}")
    if not isinstance(edited, dict):
        raise HTTPException(status_code=400, detail="plan must be a JSON object")
    delivered = await approve_plan(queue_id, item_id, edited)
    if not delivered:
        raise HTTPException(
            status_code=409,
            detail="plan session has closed — discard and re-run `plan-fix`.",
        )
    return RedirectResponse(url="/", status_code=303)


@app.post("/queues/{queue_id}/items/{item_id}/plan/discard")
def discard_plan(queue_id: str, item_id: int):
    set_item_plan(queue_id, item_id, None)
    set_item_plan_status(queue_id, item_id, "discarded")
    return RedirectResponse(url="/", status_code=303)


@app.post("/queues/{queue_id}/items/{item_id}/drafts/approve")
async def approve_drafts_endpoint(queue_id: str, item_id: int,
                                   drafts_json: str = Form(...)):
    """Submit (possibly edited) per-thread reply drafts. Prefers the
    live address-review-comments session so it can post each reply;
    falls back to direct `gh api` posts when the session has closed
    (e.g. after a reboot) so the click always "just proceeds"."""
    try:
        edited = json.loads(drafts_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"invalid drafts JSON: {exc}")
    if not isinstance(edited, dict):
        raise HTTPException(status_code=400, detail="drafts must be a JSON object")
    try:
        await approve_drafts(queue_id, item_id, edited)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return RedirectResponse(url="/", status_code=303)


@app.post("/queues/{queue_id}/items/{item_id}/drafts/discard")
def discard_drafts(queue_id: str, item_id: int):
    set_item_drafts(queue_id, item_id, None)
    set_item_drafts_status(queue_id, item_id, "discarded")
    return RedirectResponse(url="/", status_code=303)


@app.post("/queues/{queue_id}/clear-done")
def clear_done(queue_id: str):
    """Delete every item currently in the queue's done_state column.
    Reuses the per-item delete path so worktree pruning + session abort
    stay correct. Anything still in the upstream feed will come back on
    the next Update."""
    queues_by_id = {q["id"]: q for q in get_queues_config()}
    q = queues_by_id.get(queue_id)
    if q is None:
        raise HTTPException(status_code=404, detail="unknown queue")
    done_state = q.get("done_state", "done")
    state = load_state()
    done_ids = [i["id"] for i in queue_items(state, queue_id)
                if i.get("state") == done_state]
    for iid in done_ids:
        try:
            sessions.abort_sessions_for_item(queue_id, iid)
        except Exception:
            pass
        delete_item(queue_id, iid)
        try:
            worktree.remove_worktree(iid)
        except Exception:
            pass
    return RedirectResponse(url="/", status_code=303)


@app.post("/queues/{queue_id}/items/{item_id}/refresh")
def refresh_item(queue_id: str, item_id: int):
    """Per-card refresh: refetch this PR from GitHub, re-triage if the
    PR has been touched since last triage, otherwise leave alone."""
    try:
        refresh_one_item(queue_id, item_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return RedirectResponse(url="/", status_code=303)


@app.post("/queues/{queue_id}/items/{item_id}/retriage")
def retriage(queue_id: str, item_id: int):
    """Force a fresh triage on this item: clears the existing verdict
    and spawns a new triage session. Used when the human disagrees with
    the triage or the old session got stuck."""
    try:
        retriage_item(queue_id, item_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return RedirectResponse(url="/", status_code=303)


def _ctx_for_queue(request: Request, queue_id: str) -> dict:
    """Build the Jinja context needed to render _queue_body.html for one
    queue. Mirrors the shape the index page prepares so the partial
    renders identically whether it's embedded in the full page or
    returned as an HTMX fragment."""
    cfg = load_config()
    queues_cfg = get_queues_config()
    # Per-queue overrides (matches index endpoint).
    for q in queues_cfg:
        q["effective_max_in_flight"] = int(
            get_queue_setting(q["id"], "max_in_flight", q.get("max_in_flight", 0)))
        q["effective_worker_slots"] = int(
            get_queue_setting(q["id"], "worker_slots", q.get("max_in_flight", 0)))
        q["intake_paused"] = bool(get_queue_setting(
            q["id"], "intake_paused", False))
    queue = next((q for q in queues_cfg if q["id"] == queue_id), None)
    if queue is None:
        raise HTTPException(status_code=404, detail="unknown queue")
    state = load_state()
    stats_data = sessions.stats()
    stats_data["auto_resume_on_boot"] = bool(
        get_global_setting("auto_resume_on_boot", False))
    live_by_item: dict[str, dict] = {}
    for ls in stats_data.get("live", []):
        qid = ls.get("queue_id"); iid = ls.get("item_id")
        if qid is None or iid is None:
            continue
        for item in state.get("queues", {}).get(qid, {}).get("items", []):
            if item.get("id") == iid:
                ls["number"] = item.get("number")
                ls["title"] = item.get("title")
                break
        live_by_item[f"{qid}:{iid}"] = ls
    q_items = state.get("queues", {}).get(queue_id, {}).get("items", [])
    done_state = queue.get("done_state") or "done"
    awaiting_state = queue.get("awaiting_state")
    return {
        "request": request,
        "queue": queue,
        "state": state,
        "stats": stats_data,
        "live_by_item": live_by_item,
        "dry_run": bool(cfg.get("actions", {}).get("dry_run", True)),
        "q_items": q_items,
        "done_state": done_state,
        "awaiting_state": awaiting_state,
    }


@app.get("/fragments/inbox", response_class=HTMLResponse)
def inbox_stream(request: Request,
                 queue: str | None = None,
                 rank: str | None = None,
                 include_done: int = 0):
    """Attention-ranked cross-queue stream for the inbox view.
    Query params:
      `queue`  — comma-separated queue ids to include; default: all
      `rank`   — comma-separated rank names to include; default: all non-done
      `include_done` — 1 to surface done items too
    """
    queues_cfg = get_queues_config()
    state = load_state()
    queue_ids = [q.strip() for q in queue.split(",")] if queue else None
    rank_names = [r.strip() for r in rank.split(",")] if rank else None
    stream = _inbox.attention_stream(
        queues_cfg, state,
        include_done=bool(include_done),
        queue_ids=queue_ids,
        rank_names=rank_names,
    )
    return templates.TemplateResponse(
        request, "_inbox_stream.html",
        {"request": request, "stream": stream},
    )


@app.get("/inspect/{queue_id}/{item_id}", response_class=HTMLResponse)
def inspect(request: Request, queue_id: str, item_id: int):
    """Render one card fully, for the inspector panel. Uses the same
    _card.html the board uses — styling differentiates based on the
    `.inspector` ancestor context."""
    ctx = _ctx_for_queue(request, queue_id)
    item = next((i for i in ctx["q_items"] if i.get("id") == item_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    ctx["item"] = item
    return templates.TemplateResponse(request, "_inspector.html", ctx)


@app.get("/queues/{queue_id}/body", response_class=HTMLResponse)
def queue_body(request: Request, queue_id: str):
    """Return the state-columns fragment for one queue. Polled by
    HTMX every few seconds; morph-swap preserves DOM identity so open
    <details>, focused inputs, and scroll position survive."""
    ctx = _ctx_for_queue(request, queue_id)
    return templates.TemplateResponse(request, "_queue_body.html", ctx)


@app.get("/fragments/header-readout", response_class=HTMLResponse)
def header_readout(request: Request):
    """Return just the header stats readout. Polled by HTMX."""
    stats_data = sessions.stats()
    stats_data["auto_resume_on_boot"] = bool(
        get_global_setting("auto_resume_on_boot", False))
    tt = stats_data.get("tokens_24h") or {}
    ttl = (tt.get("input_tokens", 0) + tt.get("output_tokens", 0)
           + tt.get("cache_creation_input_tokens", 0)
           + tt.get("cache_read_input_tokens", 0))
    return templates.TemplateResponse(
        request, "_header_readout.html",
        {"request": request, "s": stats_data, "ttl": ttl},
    )


@app.get("/queues/{queue_id}/items/{item_id}/drawer", response_class=HTMLResponse)
def drawer(request: Request, queue_id: str, item_id: int):
    """Render a PR snapshot (title, body, CI, reviewers, comments,
    linked issues) as an HTML fragment for the in-app drawer."""
    item = find_item(load_state(), queue_id, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    pr_number = item.get("number")
    if not pr_number:
        raise HTTPException(status_code=400, detail="item has no PR number")
    try:
        pr = github.fetch_pr_for_drawer(int(pr_number))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    cfg = load_config()
    owner = cfg["repo"]["owner"]
    name = cfg["repo"]["name"]
    body_html = md.render(pr.get("body"), owner=owner, name=name)
    comments = []
    for c in pr.get("comments") or []:
        author_login = (c.get("author") or {}).get("login") or "unknown"
        comments.append({
            "author": author_login,
            "createdAt": c.get("createdAt"),
            "html": md.render(c.get("body"), owner=owner, name=name),
        })
    return templates.TemplateResponse(
        request, "drawer.html",
        {"pr": pr, "body_html": body_html, "comments": comments},
    )


@app.get("/queues/{queue_id}/items/{item_id}/reviewer-candidates")
def reviewer_candidates(queue_id: str, item_id: int):
    """Return ranked candidate reviewers for the modal. Mechanical
    computation — no Claude session involved."""
    item = find_item(load_state(), queue_id, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    pr_number = item.get("number")
    if not pr_number:
        raise HTTPException(status_code=400, detail="item has no PR number")
    try:
        candidates = github.suggest_reviewers(int(pr_number))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return JSONResponse({"candidates": candidates})


@app.post("/queues/{queue_id}/items/{item_id}/request-reviewers")
def submit_request_reviewers(queue_id: str, item_id: int,
                             reviewers: list[str] = Form(default=[])):
    """Called by the reviewer-picker modal after the user ticks boxes.
    Calls the GH API with the selected logins, parks the card in
    `awaiting update`, and records the result."""
    item = find_item(load_state(), queue_id, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    pr_number = item.get("number")
    if not pr_number:
        raise HTTPException(status_code=400, detail="item has no PR number")
    cleaned = [r.strip() for r in reviewers if r and r.strip()]
    if not cleaned:
        raise HTTPException(status_code=400,
                            detail="no reviewers selected")

    cfg = load_config()
    dry_run = bool(cfg.get("actions", {}).get("dry_run", True))
    qcfg = {q["id"]: q for q in get_queues_config()}.get(queue_id, {})
    awaiting_state = qcfg.get("awaiting_state", "awaiting update")

    if dry_run:
        set_item_result(queue_id, item_id, {
            "action": "request-reviewers",
            "status": "skipped_dry_run",
            "message": (f"dry_run — would request review from "
                        f"{', '.join('@' + r for r in cleaned)}."),
            "reviewers": cleaned,
        })
        return RedirectResponse(url="/", status_code=303)

    try:
        github.request_reviewers(int(pr_number), cleaned)
    except Exception as exc:
        set_item_result(queue_id, item_id, {
            "action": "request-reviewers",
            "status": "error",
            "message": str(exc),
        })
        raise HTTPException(status_code=502, detail=str(exc))

    set_item_state(queue_id, item_id, awaiting_state)
    set_item_parked_at(queue_id, item_id, _now())
    set_item_result(queue_id, item_id, {
        "action": "request-reviewers",
        "status": "completed",
        "message": (f"Requested review from "
                    f"{', '.join('@' + r for r in cleaned)}."),
        "reviewers": cleaned,
    })
    return RedirectResponse(url="/", status_code=303)


@app.post("/queues/{queue_id}/items/{item_id}/delete")
def delete(queue_id: str, item_id: int):
    try:
        sessions.abort_sessions_for_item(queue_id, item_id)
    except Exception:
        pass
    delete_item(queue_id, item_id)
    try:
        worktree.remove_worktree(item_id)
    except Exception:
        pass
    return RedirectResponse(url="/", status_code=303)


@app.get("/stats")
def stats():
    return JSONResponse(sessions.stats())


@app.get("/sessions/{session_id}")
def session_snapshot(session_id: str):
    snap = sessions.get_snapshot(session_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="session not found")
    return JSONResponse(snap)


@app.post("/sessions/{session_id}/send")
async def session_send(session_id: str, text: str = Form(...)):
    text = text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty message")
    delivered = await sessions.send_user_message(session_id, text)
    if not delivered:
        raise HTTPException(status_code=409, detail="session is closed")
    return JSONResponse({"ok": True})


@app.post("/sessions/{session_id}/resume")
def session_resume(session_id: str):
    new_id = sessions.resume_session(session_id)
    if new_id is None:
        raise HTTPException(status_code=409, detail="session is not resumable")
    return JSONResponse({"session_id": new_id})


@app.post("/settings/global")
def update_global(max_concurrent: int = Form(...),
                  auto_resume_on_boot: str = Form("")):
    """Bump (or trim) the global session cap. Applies live via semaphore
    resize — new/queued sessions pick up the new cap immediately; existing
    in-flight sessions finish their current turn at the old cap.
    Also persists `auto_resume_on_boot` — when true, the startup sweep
    resumes any interrupted action that left an SDK session id behind."""
    if max_concurrent < 1 or max_concurrent > 64:
        raise HTTPException(status_code=400,
                            detail="max_concurrent must be between 1 and 64")
    update_global_setting("max_concurrent", int(max_concurrent))
    update_global_setting(
        "auto_resume_on_boot",
        auto_resume_on_boot.lower() in ("1", "true", "on", "yes"),
    )
    try:
        sessions.resize_semaphore(int(max_concurrent))
    except Exception as exc:
        print(f"[settings] semaphore resize failed: {exc}")
    return RedirectResponse(url="/", status_code=303)


@app.post("/queues/{queue_id}/settings")
def update_queue_settings(queue_id: str,
                          max_in_flight: int = Form(...),
                          worker_slots: int = Form(...),
                          intake_paused: str = Form("")):
    """Per-queue settings. `worker_slots` is clamped to the global cap so
    a deprioritized queue can't accidentally monopolize all sessions."""
    queues_by_id = {q["id"]: q for q in get_queues_config()}
    if queue_id not in queues_by_id:
        raise HTTPException(status_code=404, detail="unknown queue")
    cfg = load_config()
    cfg_cap = int((cfg.get("sessions") or {}).get("max_concurrent", 8))
    global_cap = int(get_global_setting("max_concurrent", cfg_cap))
    if max_in_flight < 0 or max_in_flight > 500:
        raise HTTPException(status_code=400,
                            detail="max_in_flight out of range")
    worker_slots = max(1, min(int(worker_slots), global_cap))
    update_queue_setting(queue_id, "max_in_flight", int(max_in_flight))
    update_queue_setting(queue_id, "worker_slots", int(worker_slots))
    update_queue_setting(queue_id, "intake_paused",
                         intake_paused.lower() in ("1", "true", "on", "yes"))
    return RedirectResponse(url="/", status_code=303)
