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
from .config import (
    PROJECT_ROOT,
    add_queue_block,
    get_queue_block_yaml,
    load_config,
    new_queue_template,
    replace_queue_block,
    update_queue_definition,
)
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

# A 30s fetch tick hammered the GitHub REST API — each queue's fetch
# is ~1 list call + 1 checks call per PR (50 PRs × ~2 calls × N queues
# every 30s = ~N × 12k calls/hr, eating the 5k/hr budget with just a
# couple of queues). 180s still feels responsive for a maintenance
# tool while leaving plenty of headroom for manual Updates and auto-
# unparking of awaiting-update cards.
DEFAULT_AUTO_REFRESH_SECONDS = 180

TEMPLATES_DIR = PROJECT_ROOT / "repobot" / "templates"
STATIC_DIR = PROJECT_ROOT / "repobot" / "static"

app = FastAPI(title="repobot")


def _reload_or_redirect(request: Request) -> Response:
    """Success response for settings / config-write endpoints. HTMX
    clients get a 204 + HX-Refresh so the whole page re-renders
    (tabs, header, queue-meta can all be affected by the write);
    non-HTMX clients get the usual 303 back to `/`."""
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Refresh": "true"})
    return RedirectResponse(url="/", status_code=303)


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
from datetime import datetime, timezone
from markupsafe import Markup as _Markup

templates.env.globals["icon"] = lambda name, **kw: _Markup(_icons.render(name, **kw))
templates.env.globals["rank_bucket"] = _inbox.rank_bucket
templates.env.globals["BUCKETS"] = _inbox.BUCKETS


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def _time_ago(iso: str | None) -> str:
    """Humanize an ISO timestamp into relative English. Empty on None.
    Granularity deliberately low — "3 weeks ago" is more scannable
    than "19 days ago" when you're looking at a card."""
    dt = _parse_iso(iso)
    if dt is None:
        return ""
    now = datetime.now(timezone.utc)
    delta = now - dt
    secs = delta.total_seconds()
    if secs < 0:
        return "just now"
    if secs < 60:
        return "just now"
    if secs < 3600:
        mins = int(secs // 60)
        return f"{mins} minute{'s' if mins != 1 else ''} ago"
    if secs < 86400:
        hours = int(secs // 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = int(secs // 86400)
    if days == 1:
        return "yesterday"
    if days < 14:
        return f"{days} days ago"
    if days < 60:
        weeks = days // 7
        return f"{weeks} week{'s' if weeks != 1 else ''} ago"
    if days < 365:
        months = days // 30
        return f"{months} month{'s' if months != 1 else ''} ago"
    years = days // 365
    return f"{years} year{'s' if years != 1 else ''} ago"


def _exact_time(iso: str | None) -> str:
    """Format an ISO timestamp as a tooltip-friendly local-ish string,
    e.g. 'Apr 21, 2026 · 14:30 UTC'. Always UTC to avoid timezone
    mismatches between the user and the server."""
    dt = _parse_iso(iso)
    if dt is None:
        return ""
    return dt.astimezone(timezone.utc).strftime("%b %d, %Y · %H:%M UTC")


templates.env.globals["time_ago"] = _time_ago
templates.env.globals["exact_time"] = _exact_time
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
    """Resolve the auto-refresh interval at boot. Global setting
    (user-editable) wins over config.yaml default so the user can
    tune without restarting. Lower bound of 30s so accidental '1'
    doesn't nuke the GitHub API budget."""
    cfg = load_config()
    cfg_default = int(cfg.get("auto_refresh", {}).get(
        "interval_seconds", DEFAULT_AUTO_REFRESH_SECONDS))
    setting = int(get_global_setting("auto_refresh_seconds", cfg_default))
    return max(30, setting) if setting > 0 else setting  # 0 still means off


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
    stats_data["auto_refresh_seconds"] = _auto_refresh_interval()
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

    # Per-queue count of "needs your attention" items. Ranks 0..3 are
    # the ones that actually want a click (needs_human / interrupted /
    # error / verdict-ready). Used for the tab badges.
    queue_attention_counts: dict[str, int] = {}
    for q in queues_cfg:
        qid = q["id"]
        n = 0
        for item in state.get("queues", {}).get(qid, {}).get("items", []):
            if _inbox.attention_rank(item, q) <= 3:
                n += 1
        queue_attention_counts[qid] = n

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
            "queue_attention_counts": queue_attention_counts,
            "rate_limit": github.rate_limit_snapshot(),
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
    stats_data["auto_refresh_seconds"] = _auto_refresh_interval()
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
    stats_data["auto_refresh_seconds"] = _auto_refresh_interval()
    tt = stats_data.get("tokens_24h") or {}
    ttl = (tt.get("input_tokens", 0) + tt.get("output_tokens", 0)
           + tt.get("cache_creation_input_tokens", 0)
           + tt.get("cache_read_input_tokens", 0))
    return templates.TemplateResponse(
        request, "_header_readout.html",
        {"request": request, "s": stats_data, "ttl": ttl,
         "rate_limit": github.rate_limit_snapshot()},
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
    # Resolve the repo for this item: stamped on `raw.repo` at fetch
    # time; fall back to the queue's configured repo.
    qcfg = {q["id"]: q for q in get_queues_config()}.get(queue_id, {})
    slug = github.item_repo_slug(item) or github.queue_repo_slug(qcfg)
    try:
        with github.repo_scope(slug):
            pr = github.fetch_pr_for_drawer(int(pr_number))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    owner, name = slug.split("/", 1)
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
        {"pr": pr, "body_html": body_html, "comments": comments,
         "queue_id": queue_id, "item_id": item_id},
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
    qcfg = {q["id"]: q for q in get_queues_config()}.get(queue_id, {})
    slug = github.item_repo_slug(item) or github.queue_repo_slug(qcfg)
    try:
        with github.repo_scope(slug):
            grouped = github.suggest_reviewers(int(pr_number))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    # Keep the flat `candidates` key around for any caller that hasn't
    # migrated yet — it's the concatenation of both groups in display
    # order (suggested first, then others).
    flat = list(grouped.get("suggested") or []) + list(grouped.get("others") or [])
    return JSONResponse({
        "candidates": flat,
        "suggested": grouped.get("suggested") or [],
        "others": grouped.get("others") or [],
    })


@app.post("/queues/{queue_id}/items/{item_id}/request-reviewers")
def submit_request_reviewers(queue_id: str, item_id: int,
                             reviewers: list[str] = Form(default=[]),
                             nudge: list[str] = Form(default=[]),
                             comment_body: str = Form(default="")):
    """Called by the reviewer-picker modal. Two independent slots of
    action per candidate:
      - `reviewers` — logins to request as formal reviewers (only
        valid for repo collaborators; handled via GH's
        requested_reviewers API).
      - `nudge` + `comment_body` — logins to @-mention in a freeform
        comment on the PR. Body was edited in the second modal before
        it arrived here.
    At least one must be non-empty. Both may be non-empty; if so, we
    request review first then post the nudge comment.
    """
    item = find_item(load_state(), queue_id, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    pr_number = item.get("number")
    if not pr_number:
        raise HTTPException(status_code=400, detail="item has no PR number")
    to_request = [r.strip() for r in reviewers if r and r.strip()]
    to_nudge = [r.strip() for r in nudge if r and r.strip()]
    comment = (comment_body or "").strip()
    if not to_request and not to_nudge:
        raise HTTPException(status_code=400,
                            detail="no reviewers or nudges selected")
    if to_nudge and not comment:
        raise HTTPException(status_code=400,
                            detail="nudge selected but no comment body")

    cfg = load_config()
    dry_run = bool(cfg.get("actions", {}).get("dry_run", True))
    qcfg = {q["id"]: q for q in get_queues_config()}.get(queue_id, {})
    awaiting_state = qcfg.get("awaiting_state", "awaiting update")

    actions_taken: list[str] = []
    errors: list[str] = []

    if dry_run:
        if to_request:
            actions_taken.append(
                "would request review from "
                + ", ".join("@" + r for r in to_request))
        if to_nudge:
            actions_taken.append(
                f"would post nudge comment ({len(comment)} chars)")
        set_item_result(queue_id, item_id, {
            "action": "request-reviewers",
            "status": "skipped_dry_run",
            "message": "dry_run — " + "; ".join(actions_taken),
            "reviewers": to_request,
            "nudged": to_nudge,
        })
        return RedirectResponse(url="/", status_code=303)

    qcfg_lookup = {q["id"]: q for q in get_queues_config()}.get(queue_id, {})
    slug = github.item_repo_slug(item) or github.queue_repo_slug(qcfg_lookup)
    if to_request:
        try:
            with github.repo_scope(slug):
                github.request_reviewers(int(pr_number), to_request)
            actions_taken.append(
                "requested review from "
                + ", ".join("@" + r for r in to_request))
        except Exception as exc:
            errors.append(f"request_reviewers: {exc}")

    if to_nudge and comment:
        try:
            with github.repo_scope(slug):
                github.post_pr_comment(int(pr_number), comment)
            actions_taken.append(
                "posted nudge comment (@"
                + ", @".join(to_nudge) + ")")
        except Exception as exc:
            errors.append(f"post_pr_comment: {exc}")

    if errors and not actions_taken:
        set_item_result(queue_id, item_id, {
            "action": "request-reviewers",
            "status": "error",
            "message": "; ".join(errors),
        })
        raise HTTPException(status_code=502, detail="; ".join(errors))

    set_item_state(queue_id, item_id, awaiting_state)
    set_item_parked_at(queue_id, item_id, _now())
    set_item_result(queue_id, item_id, {
        "action": "request-reviewers",
        "status": "completed" if not errors else "completed_with_errors",
        "message": "; ".join(actions_taken + (["(errors: " + "; ".join(errors) + ")"] if errors else [])),
        "reviewers": to_request,
        "nudged": to_nudge,
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
def update_global(request: Request,
                  max_concurrent: int = Form(...),
                  auto_resume_on_boot: str = Form(""),
                  auto_refresh_seconds: int = Form(180)):
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
    # 0 disables auto-refresh entirely; any other value clamped to
    # >= 30s inside _auto_refresh_interval(). The change takes effect
    # after restart (the refresh threads are started once at boot).
    refresh = int(auto_refresh_seconds)
    if refresh < 0 or refresh > 3600:
        raise HTTPException(status_code=400,
                            detail="auto_refresh_seconds must be 0–3600")
    update_global_setting("auto_refresh_seconds", refresh)
    try:
        sessions.resize_semaphore(int(max_concurrent))
    except Exception as exc:
        print(f"[settings] semaphore resize failed: {exc}")
    return _reload_or_redirect(request)


@app.get("/queues/{queue_id}/definition")
def queue_definition(queue_id: str):
    """Return one queue's current configuration (from config.yaml),
    for the edit-query form."""
    queues = get_queues_config()
    q = next((q for q in queues if q.get("id") == queue_id), None)
    if q is None:
        raise HTTPException(status_code=404, detail="unknown queue")
    return JSONResponse({
        "id": q.get("id"),
        "title": q.get("title"),
        "max_in_flight": q.get("max_in_flight"),
        "query": q.get("query") or {},
    })


@app.post("/queues/{queue_id}/definition")
def update_queue_definition_endpoint(
    request: Request,
    queue_id: str,
    title: str = Form(""),
    repo_owner: str = Form(""),
    repo_name: str = Form(""),
    q_author: str = Form(""),
    q_state: str = Form("open"),
    q_review_requested: str = Form(""),
    q_labels: str = Form(""),
    q_assignee: str = Form(""),
    q_milestone: str = Form(""),
    q_search: str = Form(""),
):
    """Rewrite one queue's query in config.yaml, preserving comments
    and structure. Labels are a comma-separated string in the form;
    stored as a list. On success, clear this queue's items so the
    next fetch tick repopulates against the new query — otherwise
    stale cards from the old query linger on the board."""
    queues_cfg = get_queues_config()
    if not any(q.get("id") == queue_id for q in queues_cfg):
        raise HTTPException(status_code=404, detail="unknown queue")

    labels_list = [l.strip() for l in q_labels.split(",") if l.strip()]
    query_updates: dict = {
        "author": q_author.strip() or None,
        "state": q_state.strip() or None,
        "review_requested": q_review_requested.strip() or None,
        "assignee": q_assignee.strip() or None,
        "milestone": q_milestone.strip() or None,
        "labels": labels_list or None,
        "search": q_search.strip() or None,
    }
    # Prune None so we don't write empty keys into yaml.
    query_updates = {k: v for k, v in query_updates.items() if v is not None}

    updates: dict = {"query": query_updates}
    if title.strip():
        updates["title"] = title.strip()
    # Per-queue repo override. Both fields must be filled in to set;
    # both empty clears any override (queue falls back to top-level).
    owner = repo_owner.strip()
    name = repo_name.strip()
    if owner and name:
        updates["repo"] = {"owner": owner, "name": name}
    elif not owner and not name:
        # Explicit clear (empty both fields) → remove the override.
        updates["repo"] = None
    # Mixed (one filled, one empty) is ambiguous — ignore.
    try:
        update_queue_definition(queue_id, updates)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Wipe this queue's items so the next fetch repopulates from the
    # new query. Without this, cards matching the OLD query stick
    # around until the user manually deletes or closes them.
    def _clear(state):
        bucket = state.get("queues", {}).get(queue_id)
        if bucket:
            bucket["items"] = []
    _mutate(_clear)

    # Kick an immediate fetch in a background thread so the user sees
    # new cards show up without waiting for the auto-refresh tick.
    threading.Thread(
        target=run_queue, args=(queue_id,),
        kwargs={"wait_for_triage": False},
        daemon=True,
    ).start()

    return _reload_or_redirect(request)


@app.post("/queues/compose")
def queue_compose(
    prompt: str = Form(...),
    current_yaml: str = Form(""),
):
    """Run the compose-queue skill on a natural-language prompt,
    return the generated YAML for the user to review. Does NOT
    write to config.yaml — that's still the explicit Save click.
    """
    if not prompt or not prompt.strip():
        raise HTTPException(status_code=400, detail="empty prompt")
    queues_cfg = get_queues_config()
    existing_ids = [q.get("id") for q in queues_cfg if q.get("id")]
    context = {
        "prompt": prompt.strip(),
        "current_yaml": current_yaml or "",
        "existing_ids": existing_ids,
    }
    try:
        session_id, result = sessions.run_session_blocking(
            "compose-queue", context,
            cwd=str(PROJECT_ROOT),
            kind="compose",
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"compose failed: {exc}")
    status = result.get("status")
    yaml_out = result.get("yaml")
    message = result.get("message") or ""
    if status == "error" or not yaml_out:
        raise HTTPException(
            status_code=400,
            detail=message or "compose skill returned no YAML"
        )
    return JSONResponse({
        "yaml": yaml_out,
        "message": message,
        "session_id": session_id,
    })


@app.get("/queues/new/template")
def queue_new_template():
    """Return a YAML template for a brand-new queue, used to seed the
    new-queue modal's Raw YAML editor."""
    return JSONResponse({"yaml": new_queue_template()})


def _post_add_queue(parsed: dict):
    """Shared body for the two new-queue endpoints. Writes to
    config.yaml, pre-creates the queue's state bucket, and kicks a
    fetch so the new queue shows up populated within a poll tick."""
    try:
        added = add_queue_block(parsed)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    qid = added["id"]

    # Pre-create the state bucket so downstream set_item_* calls have
    # something to insert into.
    def _ensure(state):
        state.setdefault("queues", {}).setdefault(
            qid, {"items": [], "created_at": _now()})
    _mutate(_ensure)

    threading.Thread(
        target=run_queue, args=(qid,),
        kwargs={"wait_for_triage": False},
        daemon=True,
    ).start()
    return qid


@app.post("/queues/new/form")
def queue_new_form(
    id: str = Form(...),
    title: str = Form(...),
    max_in_flight: int = Form(10),
    repo_owner: str = Form(""),
    repo_name: str = Form(""),
    q_author: str = Form(""),
    q_state: str = Form("open"),
    q_review_requested: str = Form(""),
    q_labels: str = Form(""),
    q_assignee: str = Form(""),
    q_milestone: str = Form(""),
    q_search: str = Form(""),
):
    """Create a new queue from the structured form. Uses sensible
    defaults for the state machine (in triage / in progress /
    awaiting update / done). For custom state machines, use the
    Raw YAML tab instead."""
    query: dict = {}
    if q_author.strip(): query["author"] = q_author.strip()
    if q_state.strip(): query["state"] = q_state.strip()
    if q_review_requested.strip():
        query["review_requested"] = q_review_requested.strip()
    if q_assignee.strip(): query["assignee"] = q_assignee.strip()
    if q_milestone.strip(): query["milestone"] = q_milestone.strip()
    labels = [l.strip() for l in q_labels.split(",") if l.strip()]
    if labels: query["labels"] = labels
    if q_search.strip(): query["search"] = q_search.strip()

    parsed = {
        "id": id.strip(),
        "title": title.strip(),
        "max_in_flight": int(max_in_flight),
        "initial_state": "in triage",
        "done_state": "done",
        "awaiting_state": "awaiting update",
        "states": ["in triage", "in progress", "awaiting update", "done"],
        "query": query,
    }
    if repo_owner.strip() and repo_name.strip():
        parsed["repo"] = {"owner": repo_owner.strip(),
                          "name": repo_name.strip()}
    _post_add_queue(parsed)
    return RedirectResponse(url="/", status_code=303)


@app.post("/queues/new/raw")
def queue_new_raw(yaml_text: str = Form(...)):
    """Create a new queue from raw YAML. Full control over state
    machine, query, labels, etc."""
    from ruamel.yaml import YAML
    y = YAML()
    try:
        parsed = y.load(yaml_text)
    except Exception as exc:
        raise HTTPException(status_code=400,
                            detail=f"YAML parse error: {exc}")
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400,
                            detail="YAML must describe a single queue mapping")
    _post_add_queue(parsed)
    return RedirectResponse(url="/", status_code=303)


@app.get("/queues/{queue_id}/definition/raw")
def queue_definition_raw(queue_id: str):
    """Return the queue's config.yaml block as a YAML string. Used
    by the settings modal's YAML tab to load the editor with the
    current content."""
    try:
        return JSONResponse({"yaml": get_queue_block_yaml(queue_id)})
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown queue")


@app.post("/queues/{queue_id}/definition/raw")
def update_queue_definition_raw(request: Request,
                                queue_id: str,
                                yaml_text: str = Form(...)):
    """Replace the queue's entire block in config.yaml from a raw
    YAML string. Validation: must parse, must be a mapping, must
    include the required keys, and the id must match the URL param
    (renaming via YAML save isn't allowed because the state file is
    keyed by id)."""
    try:
        replace_queue_block(queue_id, yaml_text)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown queue")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Same post-save behavior as the structured form: clear items
    # and kick a fresh fetch so the new definition takes effect.
    def _clear(state):
        bucket = state.get("queues", {}).get(queue_id)
        if bucket:
            bucket["items"] = []
    _mutate(_clear)
    threading.Thread(
        target=run_queue, args=(queue_id,),
        kwargs={"wait_for_triage": False},
        daemon=True,
    ).start()
    return _reload_or_redirect(request)


@app.post("/queues/{queue_id}/settings")
def update_queue_settings(request: Request,
                          queue_id: str,
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
    return _reload_or_redirect(request)


# ============================================================= tasks board
# Ad-hoc tasks: user submits a prompt + repo, we spawn a `do-task`
# session in a fresh worktree. Parallel track to the PR-triage queues.

from . import tasks as _tasks  # noqa: E402


@app.post("/tasks/new")
def new_task(request: Request,
             repo_id: str = Form(...),
             prompt: str = Form(...),
             task_type: str = Form(default=_tasks.TASK_TYPE_DEFAULT)):
    """Create a task and spawn the `do-task` session. Returns to the
    main page — the task appears in the In Progress column as soon
    as the session acks startup."""
    prompt = (prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    if not github.repo_by_id(repo_id):
        raise HTTPException(status_code=400, detail=f"unknown repo: {repo_id}")
    if task_type not in _tasks.TASK_TYPES:
        task_type = _tasks.TASK_TYPE_DEFAULT
    try:
        task = _tasks.create_task(repo_id=repo_id, prompt=prompt,
                                  task_type=task_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # Dispatch in a background thread so the POST returns quickly —
    # worktree setup can shell out to `git fetch` which is slow.
    def _dispatch():
        try:
            _tasks.dispatch_task(task["id"])
        except Exception as exc:
            _tasks.update_task(task["id"], status="stuck",
                               last_result={
                                   "status": "error",
                                   "message": f"spawn failed: {exc}",
                               })
    threading.Thread(target=_dispatch, daemon=True).start()
    return _reload_or_redirect(request)


@app.post("/tasks/{task_id}/delete")
def delete_task_endpoint(task_id: int):
    """Remove a task record, abort any in-flight session, and prune
    the worktree. Mirror of the per-queue-item delete path."""
    try:
        sessions.abort_sessions_for_item(None, task_id)
    except Exception:
        pass
    _tasks.remove_task_worktree(task_id)
    _tasks.delete_task(task_id)
    return RedirectResponse(url="/", status_code=303)


@app.post("/tasks/{task_id}/retry")
def retry_task_endpoint(request: Request, task_id: int):
    """Re-dispatch a task with its original prompt + repo + type.
    Aborts any live session, resets the worktree to a clean state on
    the same `ce/task-{N}` branch, and spawns a fresh `do-task`
    session. Useful for stuck tasks where the skill bailed and the
    user wants another go without re-typing the prompt."""
    task = _tasks.find_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    try:
        sessions.abort_sessions_for_item(None, task_id)
    except Exception:
        pass
    _tasks.update_task(task_id, status="in_progress",
                       session_id=None, last_result=None)

    def _dispatch():
        try:
            _tasks.dispatch_task(task_id)
        except Exception as exc:
            _tasks.update_task(task_id, status="stuck", last_result={
                "status": "error",
                "message": f"retry failed: {exc}",
            })
    threading.Thread(target=_dispatch, daemon=True).start()
    return _reload_or_redirect(request)


@app.post("/tasks/clear-done")
def clear_done_tasks(request: Request):
    """Bulk-delete every task in the Done column. Aborts sessions
    (none should be live for a done task, but be defensive) and
    prunes their worktrees. Mirror of the per-queue clear-done."""
    for t in _tasks.list_tasks():
        if t.get("status") != "done":
            continue
        tid = t.get("id")
        if tid is None:
            continue
        try:
            sessions.abort_sessions_for_item(None, tid)
        except Exception:
            pass
        _tasks.remove_task_worktree(tid)
        _tasks.delete_task(tid)
    return _reload_or_redirect(request)


@app.post("/tasks/{task_id}/create-pr")
def create_pr_from_task(request: Request, task_id: int):
    """Open a PR from a task's worktree branch. Used when a task
    produced commits but the skill didn't already open the PR
    (e.g. a question-mode task that incidentally fixed something).
    Pushes the branch + runs `gh pr create`; stamps pr_url on the
    task so subsequent renders link out instead of offering the
    button again."""
    task = _tasks.find_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if task.get("pr_url"):
        return _reload_or_redirect(request)
    repo = github.repo_by_id(task.get("repo_id"))
    if not repo:
        raise HTTPException(status_code=400, detail="task has no valid repo")
    wt_path = _tasks.task_worktree_path(task_id)
    if not (wt_path / ".git").exists():
        raise HTTPException(status_code=400,
                            detail="worktree is gone — can't open a PR for it")
    branch = task.get("branch") or f"ce/task-{task_id}"
    title = task.get("title") or task["prompt"][:60]
    body_lines = [
        f"_Opened from Custodial Engineer task-{task_id}._",
        "",
        "**Original prompt:**",
        "",
        f"> {task['prompt']}",
    ]
    body = "\n".join(body_lines)
    import subprocess as _sp
    try:
        _sp.run(
            ["git", "push", "--set-upstream", "origin",
             f"HEAD:{branch}"],
            cwd=str(wt_path), capture_output=True, text=True, check=True,
        )
        result = _sp.run(
            ["gh", "pr", "create",
             "--repo", repo["slug"],
             "--title", title,
             "--body", body,
             "--head", branch],
            cwd=str(wt_path), capture_output=True, text=True, check=True,
        )
        url = (result.stdout or "").strip().splitlines()[-1]
    except _sp.CalledProcessError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"PR creation failed: {exc.stderr or exc.stdout}",
        )
    last = dict(task.get("last_result") or {})
    last["pr_url"] = url
    _tasks.update_task(task_id, pr_url=url, last_result=last)
    return _reload_or_redirect(request)


@app.post("/tasks/{task_id}/open-issue")
def open_issue_endpoint(request: Request, task_id: int):
    """Open the drafted issue on GitHub. Uses the issue_title /
    issue_body emitted by the do-task skill on completion. Stamps
    the resulting issue URL onto the task record so the card can
    link out to it."""
    task = _tasks.find_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    last = task.get("last_result") or {}
    title = last.get("issue_title") or task.get("title")
    body = last.get("issue_body") or ""
    if not title:
        raise HTTPException(status_code=400,
                            detail="task has no drafted issue title")
    repo = github.repo_by_id(task.get("repo_id"))
    if not repo:
        raise HTTPException(status_code=400, detail="task has no valid repo")
    import subprocess as _sp
    try:
        with github.repo_scope(repo["slug"]):
            result = _sp.run(
                ["gh", "issue", "create",
                 "--repo", repo["slug"],
                 "--title", title,
                 "--body", body or "_(no body)_"],
                capture_output=True, text=True, check=True,
            )
        url = (result.stdout or "").strip().splitlines()[-1]
    except _sp.CalledProcessError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"gh issue create failed: {exc.stderr or exc.stdout}",
        )
    new_result = dict(last)
    new_result["issue_url"] = url
    _tasks.update_task(task_id, issue_url=url, last_result=new_result)
    return _reload_or_redirect(request)


@app.get("/fragments/tasks/body", response_class=HTMLResponse)
def tasks_body(request: Request):
    """HTML fragment for the tasks board — creation form + status
    columns. Polled by HTMX like the queue-body fragments."""
    items = _tasks.list_tasks()
    by_status: dict[str, list[dict]] = {s: [] for s in _tasks.TASK_STATUSES}
    for t in items:
        by_status.setdefault(t.get("status", "in_progress"), []).append(t)
    repos = github.list_repos()
    return templates.TemplateResponse(
        request, "_tasks_board.html",
        {
            "request": request,
            "tasks_by_status": by_status,
            "repos": repos,
            "task_types": _tasks.TASK_TYPES,
            "default_task_type": _tasks.TASK_TYPE_DEFAULT,
        },
    )
