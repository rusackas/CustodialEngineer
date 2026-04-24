"""Thin wrappers around the `gh` CLI. Auth comes from GITHUB_TOKEN / GH_TOKEN.

GitHub's GraphQL gets slow when asking for `statusCheckRollup` across many PRs
at once, so we fetch the list with lightweight fields first, then hydrate
check status per-PR. A small retry loop absorbs transient 5xx responses.
"""
import json
import subprocess
import time
from contextlib import contextmanager
from contextvars import ContextVar

from .config import load_config

LIST_FIELDS = "number,title,url,mergeable,createdAt,updatedAt,headRefName,isDraft,author"
CHECK_FIELDS = "statusCheckRollup"


# `_current_repo_slug` is set by `repo_scope()` at the entry point of
# queue-scoped work (run_queue, action dispatch, API endpoints that
# know their queue). Every gh helper reads from it. Falls back to
# config.yaml's top-level `repo` when unset.
_current_repo_slug: ContextVar[str | None] = ContextVar(
    "custodial.repo_slug", default=None)


def _normalize_repo_entry(raw: dict) -> dict:
    """Fill in the derived `slug` and default `id` fields on a raw
    `repos:` list entry. `id` defaults to the slug so unadorned
    entries still work as first-class citizens."""
    owner = (raw.get("owner") or "").strip()
    name = (raw.get("name") or "").strip()
    if not owner or not name:
        return {}
    slug = f"{owner}/{name}"
    return {
        "id": (raw.get("id") or slug).strip(),
        "owner": owner,
        "name": name,
        "display_name": raw.get("display_name") or raw.get("title"),
        "slug": slug,
    }


def list_repos() -> list[dict]:
    """Return the configured repo registry as a list of normalized
    dicts `{id, owner, name, display_name, slug}`.

    Prefers the top-level `repos:` list. Falls back to synthesizing a
    single entry from the legacy top-level `repo:` dict so older
    config files keep working unchanged. Always returns at least one
    entry; raises if the config has neither shape set.
    """
    cfg = load_config()
    raw_list = cfg.get("repos")
    out: list[dict] = []
    if isinstance(raw_list, list) and raw_list:
        for r in raw_list:
            if isinstance(r, dict):
                entry = _normalize_repo_entry(r)
                if entry:
                    out.append(entry)
    if out:
        return out
    legacy = cfg.get("repo") or {}
    entry = _normalize_repo_entry(legacy) if isinstance(legacy, dict) else None
    if entry:
        return [entry]
    raise RuntimeError(
        "No repos configured — set `repos:` (a list of "
        "{id, owner, name}) or the legacy top-level `repo:` in "
        "config.yaml."
    )


def repo_by_id(repo_id: str) -> dict | None:
    """Look up a repo registry entry by its `id` (or its slug, since
    bare entries have `id == slug`). Returns None if unknown."""
    if not repo_id:
        return None
    for r in list_repos():
        if r["id"] == repo_id or r["slug"] == repo_id:
            return r
    return None


def default_repo_slug() -> str:
    """The fallback repo slug when nothing more specific is set.

    Honors `default_repo_id:` at the top level of config.yaml if it
    points at a valid registry entry; otherwise returns the first
    entry in the registry (or the legacy `repo:` if that's all that's
    set)."""
    cfg = load_config()
    did = cfg.get("default_repo_id")
    if did:
        r = repo_by_id(did)
        if r:
            return r["slug"]
    return list_repos()[0]["slug"]


# Kept for any external callers (tests / imports elsewhere) that
# still reach for the underscore-prefixed name. New code should call
# `default_repo_slug()`.
_default_repo_slug = default_repo_slug


def _repo_slug() -> str:
    explicit = _current_repo_slug.get()
    return explicit if explicit else default_repo_slug()


@contextmanager
def repo_scope(slug: str | None):
    """Context manager that pins `_repo_slug()` for the duration of
    a block. Pass None to let the default config-level repo win (no-
    op). Nested scopes stack cleanly thanks to contextvars."""
    if slug:
        token = _current_repo_slug.set(slug)
        try:
            yield
        finally:
            _current_repo_slug.reset(token)
    else:
        yield


def queue_repo_slug(queue_cfg: dict) -> str:
    """Derive the repo slug for a queue's config block. Three shapes
    are accepted for the queue's `repo` field, in precedence order:

    1. A registry id string (e.g. `repo: superset`) — resolves via
       `repo_by_id()` against the top-level `repos:` list.
    2. A bare slug string (e.g. `repo: apache/superset`) — used
       as-is.
    3. A dict (`repo: {owner: apache, name: superset}`) — legacy
       inline form, still supported for back-compat.

    Falls back to `default_repo_slug()` when none of the above match.
    """
    r = queue_cfg.get("repo") if queue_cfg else None
    if isinstance(r, dict) and r.get("owner") and r.get("name"):
        return f"{r['owner']}/{r['name']}"
    if isinstance(r, str):
        if "/" in r:
            return r
        # Registry-id reference — resolve via `repos:` list.
        entry = repo_by_id(r)
        if entry:
            return entry["slug"]
    return default_repo_slug()


def item_repo_slug(item: dict) -> str | None:
    """Derive the repo slug for a fetched item. Items get their repo
    stamped on `raw.repo` at fetch time; older items without the stamp
    return None, and the caller should fall back to the queue's repo."""
    raw = item.get("raw") or {}
    r = raw.get("repo")
    if isinstance(r, dict) and r.get("owner") and r.get("name"):
        return f"{r['owner']}/{r['name']}"
    if isinstance(r, str) and "/" in r:
        return r
    return None


def _gh_json(cmd: list[str], retries: int = 2) -> list | dict:
    last_err = None
    for attempt in range(retries + 1):
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return json.loads(result.stdout)
        last_err = result.stderr.strip()
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"gh failed: {' '.join(cmd)}\n{last_err}")


# Rate-limit snapshot. `gh api rate_limit` is itself exempt from the
# rate limit, so polling it is free. Cached for 30s anyway to keep
# per-tick work cheap and steady.
_rate_limit_cache: dict = {"at": 0.0, "data": None}
_RATE_LIMIT_TTL = 30.0


def rate_limit_snapshot(force: bool = False) -> dict | None:
    """Return a compact view of the GitHub REST + GraphQL rate limits.

    Shape: `{core: {used, limit, remaining, reset_at}, graphql: {...}}`.
    Returns None if the `gh` call fails (e.g., offline, missing token).
    Never raises — this is an observability helper, not load-bearing.
    """
    now = time.time()
    if (not force
            and _rate_limit_cache["data"] is not None
            and now - _rate_limit_cache["at"] < _RATE_LIMIT_TTL):
        return _rate_limit_cache["data"]
    try:
        result = subprocess.run(
            ["gh", "api", "rate_limit"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return _rate_limit_cache["data"]
        raw = json.loads(result.stdout).get("resources", {})
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return _rate_limit_cache["data"]

    def _pick(key: str) -> dict | None:
        r = raw.get(key)
        if not isinstance(r, dict):
            return None
        limit = r.get("limit") or 0
        remaining = r.get("remaining") or 0
        return {
            "used": r.get("used", max(limit - remaining, 0)),
            "limit": limit,
            "remaining": remaining,
            "reset_at": r.get("reset"),
        }

    data = {
        "core": _pick("core"),
        "graphql": _pick("graphql"),
        "search": _pick("search"),
    }
    _rate_limit_cache["at"] = now
    _rate_limit_cache["data"] = data
    return data


def list_prs(author: str, state: str = "open", limit: int = 50) -> list[dict]:
    return _gh_json([
        "gh", "pr", "list",
        "--repo", _repo_slug(),
        "--author", author,
        "--state", state,
        "--json", LIST_FIELDS,
        "--limit", str(limit),
    ])


def pr_checks(number: int) -> list[dict]:
    data = _gh_json([
        "gh", "pr", "view", str(number),
        "--repo", _repo_slug(),
        "--json", CHECK_FIELDS,
    ])
    return data.get("statusCheckRollup") or []


def _is_failure(check: dict) -> bool:
    # CANCELLED is intentionally NOT a failure. Superset has manual-gate
    # workflows (e.g. `check-hold-label`) that get cancelled as part of
    # normal operation; GitHub's own "all checks passed" banner ignores
    # CANCELLED for the same reason. If a cancellation represents a real
    # upstream problem, the upstream check will be in FAILURE / TIMED_OUT
    # / ACTION_REQUIRED and we'll catch it there.
    conclusion = (check.get("conclusion") or "").upper()
    state = (check.get("state") or "").upper()
    return conclusion in {"FAILURE", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"} or state == "FAILURE"


def ci_status(checks: list[dict]) -> str:
    """Collapse the statusCheckRollup to one of {passing, failing, pending}.

    Priority order matters:
    - pending: any check still in flight — we don't know the final verdict
      yet, so defer. A transient CANCELLED (e.g. Superset's
      check-hold-label) would otherwise flip this to "failing" and leak
      cards through the pending-CI triage filter.
    - failing: nothing in flight and at least one failure-terminal check.
    - passing: everything completed successfully (or no checks).
    """
    for c in checks:
        status = (c.get("status") or "").upper()
        state = (c.get("state") or "").upper()
        if status in {"QUEUED", "IN_PROGRESS", "PENDING", "WAITING"} or state == "PENDING":
            return "pending"
    if any(_is_failure(c) for c in checks):
        return "failing"
    return "passing"


def fetch_one_pr(number: int) -> dict:
    """Fetch a single PR with its check rollup in the same shape as
    `fetch_dependabot_prs` emits — used by the per-card refresh button."""
    fields = LIST_FIELDS + "," + CHECK_FIELDS
    pr = _gh_json([
        "gh", "pr", "view", str(number),
        "--repo", _repo_slug(),
        "--json", fields,
    ])
    checks = pr.get("statusCheckRollup") or []
    pr["ci_status"] = ci_status(checks)
    _stamp_repo(pr)
    return pr


def _stamp_repo(pr: dict) -> dict:
    """Stamp the current-scope repo onto a PR dict so downstream
    item-level operations know which repo this PR belongs to. Without
    this, cross-repo queues break at action time."""
    slug = _repo_slug()
    if slug and "/" in slug:
        owner, name = slug.split("/", 1)
        pr["repo"] = {"owner": owner, "name": name}
    return pr


def _build_search_query(query_block: dict) -> str:
    """Translate a queue's `query:` block into a GitHub search string
    suitable for `gh pr list --search`. If the block has a non-empty
    `search:` field, it's used verbatim — power-user mode lets you
    paste any filter you'd type into GitHub's search bar (operators
    like `updated:<90d`, `sort:updated-asc`, `no:draft`, etc.).
    Otherwise, build a basic search string from the structured
    fields the form already collects.

    `self` resolves to `@me` so search strings stay portable across
    accounts.
    """
    if not isinstance(query_block, dict):
        return "is:pr is:open"
    explicit = (query_block.get("search") or "").strip()
    if explicit:
        return explicit

    parts = ["is:pr"]
    state = (query_block.get("state") or "open").lower()
    if state in ("open", "closed", "merged"):
        parts.append(f"is:{state}")
    # state == "all" → no is:* filter

    def _resolve(login: str) -> str:
        if (login or "").lower() == "self":
            return _self_handle()
        return login

    if author := query_block.get("author"):
        parts.append(f"author:{_resolve(author)}")
    if rev := query_block.get("review_requested"):
        parts.append(f"review-requested:{_resolve(rev)}")
    if assignee := query_block.get("assignee"):
        parts.append(f"assignee:{_resolve(assignee)}")
    if milestone := query_block.get("milestone"):
        parts.append(f'milestone:"{milestone}"')
    for label in (query_block.get("labels") or []):
        parts.append(f'label:"{label}"')
    return " ".join(parts)


def fetch_search(query_block: dict, limit: int = 50,
                 hydrate_checks: bool = True) -> list[dict]:
    """Generic PR fetcher — runs `gh pr list --search` against the
    current repo scope using a search string built from the queue's
    `query:` block. Optionally hydrates each PR's `statusCheckRollup`
    + `ci_status` so triage skills downstream can read them like
    they do from the queue-specific fetchers.

    Set `hydrate_checks=False` for big result sets where CI status
    isn't load-bearing (e.g. a stale-PR-triage queue with hundreds
    of cards) — saves N+1 `gh pr view` calls.
    """
    search = _build_search_query(query_block)
    prs = _gh_json([
        "gh", "pr", "list",
        "--repo", _repo_slug(),
        # State is owned by the search query; stop `gh` from
        # double-filtering to open-only by default.
        "--state", "all",
        "--search", search,
        "--json", LIST_FIELDS,
        "--limit", str(limit),
    ])
    if not isinstance(prs, list):
        return []
    out: list[dict] = []
    for pr in prs:
        if hydrate_checks:
            try:
                checks = pr_checks(pr["number"])
            except Exception:
                checks = []
            pr["statusCheckRollup"] = checks
            pr["ci_status"] = ci_status(checks)
        _stamp_repo(pr)
        out.append(pr)
    return out


def fetch_dependabot_prs(limit: int = 50) -> list[dict]:
    """All open Dependabot PRs with hydrated check rollups, sorted oldest-
    update-first so long-languishing PRs get triage attention first."""
    prs = list_prs(author="app/dependabot", state="open", limit=limit)
    prs.sort(key=lambda p: p.get("updatedAt") or "")
    out = []
    for pr in prs:
        checks = pr_checks(pr["number"])
        pr["statusCheckRollup"] = checks
        pr["ci_status"] = ci_status(checks)
        _stamp_repo(pr)
        out.append(pr)
    return out


# Legacy alias — kept so any external caller referencing the old name
# still works. New code should use `fetch_dependabot_prs`.
def fetch_failing_dependabot_prs(limit: int = 50) -> list[dict]:
    return [p for p in fetch_dependabot_prs(limit=limit)
            if p.get("ci_status") == "failing"]


_RESOLVE_THREAD_MUTATION = """
mutation($threadId:ID!){
  resolveReviewThread(input:{threadId:$threadId}){
    thread{ id isResolved }
  }
}
"""


def resolve_review_thread(thread_node_id: str) -> None:
    """Mark a review thread resolved via GraphQL. Thread IDs are the
    GraphQL node ids (the `id` field on reviewThreads.nodes), not
    comment ids."""
    if not thread_node_id:
        raise ValueError("empty thread id")
    result = subprocess.run(
        ["gh", "api", "graphql",
         "-f", f"query={_RESOLVE_THREAD_MUTATION}",
         "-F", f"threadId={thread_node_id}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"gh api graphql (resolveReviewThread {thread_node_id[:8]}…) "
            f"failed: {result.stderr.strip()}"
        )


def pr_push_info(pr_number: int) -> dict:
    """Return the info needed to push back to a PR's head branch:
      - head_ref: the branch name
      - is_cross_repository: True iff the PR is from a fork
      - maintainer_can_modify: True if the author enabled "Allow
        edits and access to secrets by maintainers" (GitHub grants
        your token push access to the fork in that case)
      - head_repo: {owner, name} of the head repo (usually the fork)

    Used by action dispatch to decide whether we CAN push, and to
    set up the right push remote before the skill runs.
    """
    data = _gh_json([
        "gh", "pr", "view", str(pr_number),
        "--repo", _repo_slug(),
        "--json",
        "headRepository,headRepositoryOwner,headRefName,"
        "maintainerCanModify,isCrossRepository",
    ])
    head_repo = data.get("headRepository") or {}
    head_owner = (data.get("headRepositoryOwner") or {}).get("login") or ""
    return {
        "head_ref": data.get("headRefName"),
        "is_cross_repository": bool(data.get("isCrossRepository")),
        "maintainer_can_modify": bool(data.get("maintainerCanModify")),
        "head_repo": {
            "owner": head_owner,
            "name": head_repo.get("name"),
        },
    }


def ensure_push_remote(pr_number: int, worktree_path) -> tuple[str, str]:
    """Set up the right push target for a PR inside its worktree.
    Returns (remote_name, head_ref).

    - For in-repo PRs: returns ("origin", head_ref). Nothing to
      configure — the existing clone already has the branch in origin.
    - For fork PRs with maintainerCanModify: adds (or updates) a
      remote named `pr-fork-<N>` pointing at the fork, returns that
      name. Your GITHUB_TOKEN has push rights to the fork thanks to
      the maintainer-edits grant.
    - For fork PRs without maintainerCanModify: raises RuntimeError.
      The skill should bail with `needs_human` — the only legitimate
      recourse is to ask the PR author to push the fix themselves.
    """
    info = pr_push_info(pr_number)
    head_ref = info["head_ref"]
    if not info["is_cross_repository"]:
        return "origin", head_ref
    if not info["maintainer_can_modify"]:
        raise RuntimeError(
            "Maintainer edits are disabled on this fork PR — we can't "
            "push. Consider nudge-author instead."
        )
    fork_owner = info["head_repo"]["owner"]
    fork_name = info["head_repo"]["name"]
    remote = f"pr-fork-{pr_number}"
    url = f"https://github.com/{fork_owner}/{fork_name}.git"
    from pathlib import Path
    wt = str(worktree_path) if not isinstance(worktree_path, Path) else str(worktree_path)
    check = subprocess.run(
        ["git", "-C", wt, "remote", "get-url", remote],
        capture_output=True, text=True,
    )
    if check.returncode != 0:
        subprocess.run(
            ["git", "-C", wt, "remote", "add", remote, url],
            check=True, capture_output=True,
        )
    else:
        subprocess.run(
            ["git", "-C", wt, "remote", "set-url", remote, url],
            check=True, capture_output=True,
        )
    return remote, head_ref


def post_pr_comment(pr_number: int, body: str) -> None:
    """Post a top-level comment on a PR. Shells out to `gh pr comment`
    so the body can carry any multiline / markdown / @-mentions
    without shell quoting hell."""
    if not body or not body.strip():
        raise ValueError("empty comment body")
    result = subprocess.run(
        ["gh", "pr", "comment", str(pr_number),
         "--repo", _repo_slug(),
         "--body-file", "-"],
        input=body,
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh pr comment failed: {result.stderr.strip()}")


def post_review_reply(pr_number: int, first_comment_id: int, body: str) -> None:
    """Post an inline reply on an existing PR review thread. JSON-encoded
    body goes via stdin so newlines and quotes survive intact. Raises
    RuntimeError on non-zero exit."""
    endpoint = (f"/repos/{_repo_slug()}/pulls/{pr_number}"
                f"/comments/{first_comment_id}/replies")
    result = subprocess.run(
        ["gh", "api", "--method", "POST",
         "-H", "Accept: application/vnd.github+json",
         endpoint, "--input", "-"],
        input=json.dumps({"body": body}),
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"gh api reply (comment {first_comment_id}) failed: "
            f"{result.stderr.strip()}"
        )


# ---------- "my PRs" queue ----------
#
# Pulls the user's own open non-draft PRs that need attention:
# any of (merge conflicts, failing CI, unresolved review threads).
# Unresolved threads aren't exposed on `gh pr view --json`; the graphql
# call below is the authoritative source.

_REVIEW_THREADS_QUERY = """
query($owner:String!,$name:String!,$number:Int!){
  repository(owner:$owner,name:$name){
    pullRequest(number:$number){
      mergeStateStatus
      reviewThreads(first:50){
        nodes{
          id
          isResolved
          isOutdated
          path
          line
          comments(first:5){nodes{author{login} body createdAt}}
        }
      }
    }
  }
}
"""


def _review_threads(number: int) -> tuple[str, list[dict]]:
    """Returns (mergeStateStatus, list of thread dicts). Threads are
    pruned to the fields we actually consume downstream — the skill
    gets file, line, author, and body excerpt."""
    # Honor the ContextVar scope first — cross-repo queues pin the
    # slug there. Only fall through to the config default when
    # unscoped.
    owner, name = _repo_slug().split("/", 1)
    data = _gh_json([
        "gh", "api", "graphql",
        "-f", f"query={_REVIEW_THREADS_QUERY}",
        "-F", f"owner={owner}",
        "-F", f"name={name}",
        "-F", f"number={number}",
    ])
    pr = (data.get("data") or {}).get("repository", {}).get("pullRequest") or {}
    mss = pr.get("mergeStateStatus") or ""
    threads = (pr.get("reviewThreads") or {}).get("nodes") or []
    slim = []
    for t in threads:
        if t.get("isResolved"):
            continue
        comments = ((t.get("comments") or {}).get("nodes") or [])
        first = comments[0] if comments else {}
        slim.append({
            "id": t.get("id"),
            "path": t.get("path"),
            "line": t.get("line"),
            "is_outdated": bool(t.get("isOutdated")),
            "first_author": (first.get("author") or {}).get("login"),
            "first_body": (first.get("body") or "")[:2000],
            "comments_count": len(comments),
        })
    return mss, slim


def _self_handle() -> str:
    """The PR author we filter by. Prefers `identity.github_username`
    from config; falls back to `@me` (gh's authenticated user)."""
    cfg = load_config()
    handle = (cfg.get("identity") or {}).get("github_username")
    return handle or "@me"


def list_review_requested_prs(limit: int = 50) -> list[dict]:
    """Open PRs where the authenticated user is a requested reviewer.
    `gh pr list --search "review-requested:@me"` is equivalent to
    querying the user's review-request inbox and is the canonical
    source — it respects both individual and team review requests."""
    cfg = load_config()
    handle = (cfg.get("identity") or {}).get("github_username") or "@me"
    # `review-requested` accepts a handle or @me; we use the stored
    # handle so the filter still works when the server is auth'd as a
    # different account (e.g. a bot PAT).
    search = f"review-requested:{handle}"
    return _gh_json([
        "gh", "pr", "list",
        "--repo", _repo_slug(),
        "--state", "open",
        "--search", search,
        "--json", LIST_FIELDS,
        "--limit", str(limit),
    ])


def fetch_review_requested_prs(limit: int = 50) -> list[dict]:
    """Open PRs requesting review from the configured user, hydrated
    with CI status, mergeStateStatus, and the list of unresolved review
    threads (from any author) — the triage skill uses those to tell
    whether OTHERS' feedback is still pending before you approve.

    Returns PRs sorted oldest-update-first (longest-waiting gets
    triaged first). Draft PRs are excluded.
    """
    prs = list_review_requested_prs(limit=limit)
    prs = [p for p in prs if not p.get("isDraft")]
    prs.sort(key=lambda p: p.get("updatedAt") or "")

    out: list[dict] = []
    for pr in prs:
        number = pr["number"]
        try:
            checks = pr_checks(number)
        except Exception:
            checks = []
        pr["statusCheckRollup"] = checks
        pr["ci_status"] = ci_status(checks)
        try:
            mss, threads = _review_threads(number)
        except Exception:
            mss, threads = "", []
        pr["mergeStateStatus"] = mss
        pr["unresolved_threads"] = threads
        pr["has_conflicts"] = (pr.get("mergeable") == "CONFLICTING"
                               or mss == "DIRTY")
        _stamp_repo(pr)
        out.append(pr)
    return out


def fetch_my_prs(limit: int = 50) -> list[dict]:
    """Open, non-draft PRs authored by the configured user that need
    attention — any of: merge conflicts, failing CI, unresolved review
    threads. Each returned PR has:
      - ci_status: passing | failing | pending
      - unresolved_threads: list of {id, path, line, first_author, first_body, is_outdated}
      - has_conflicts: bool
      - mergeStateStatus: from graphql
    PRs with none of the three signals are excluded.
    """
    prs = list_prs(author=_self_handle(), state="open", limit=limit)
    prs = [p for p in prs if not p.get("isDraft")]
    prs.sort(key=lambda p: p.get("updatedAt") or "")

    out: list[dict] = []
    for pr in prs:
        number = pr["number"]
        try:
            checks = pr_checks(number)
        except Exception:
            checks = []
        pr["statusCheckRollup"] = checks
        pr["ci_status"] = ci_status(checks)
        try:
            mss, threads = _review_threads(number)
        except Exception:
            mss, threads = "", []
        pr["mergeStateStatus"] = mss
        pr["unresolved_threads"] = threads
        pr["has_conflicts"] = (pr.get("mergeable") == "CONFLICTING"
                               or mss == "DIRTY")
        if (pr["has_conflicts"] or pr["ci_status"] == "failing"
                or threads):
            _stamp_repo(pr)
            out.append(pr)
    return out


_BOT_SUFFIX = "[bot]"


_COLLABS_TTL_SEC = 600  # 10 min
_collabs_cache: dict = {"at": 0.0, "slug": "", "logins": set(), "records": []}


def _refresh_collaborators() -> None:
    """Fetch + cache the repo's write-access collaborator list. Stores
    both the lowercase-login set (for membership checks) and a list of
    `{login, avatar_url}` dicts (for UI rendering).
    """
    slug = _repo_slug()
    logins: set[str] = set()
    records: list[dict] = []
    page = 1
    while True:
        try:
            batch = _gh_json([
                "gh", "api",
                f"/repos/{slug}/collaborators",
                "-X", "GET",
                "-f", "per_page=100",
                "-f", f"page={page}",
                "-f", "affiliation=all",
            ])
        except Exception:
            break
        if not isinstance(batch, list) or not batch:
            break
        for c in batch:
            login = (c.get("login") or "").strip()
            perms = c.get("permissions") or {}
            if login and (perms.get("push") or perms.get("admin")
                          or perms.get("maintain")):
                logins.add(login.lower())
                records.append({
                    "login": login,
                    "avatar_url": c.get("avatar_url"),
                })
        if len(batch) < 100:
            break
        page += 1
    _collabs_cache.update({
        "at": time.time(), "slug": slug,
        "logins": logins, "records": records,
    })


def collaborator_logins(force: bool = False) -> set[str]:
    """Return the lowercase logins of anyone in the repo's collaborator
    list who has push or admin rights — i.e., the set of people who
    can legitimately be requested as reviewers. Cached for 10 min to
    avoid hammering the API; the cache is keyed by repo slug so a
    config change invalidates automatically.
    """
    slug = _repo_slug()
    if (not force
            and _collabs_cache["slug"] == slug
            and time.time() - _collabs_cache["at"] < _COLLABS_TTL_SEC):
        return _collabs_cache["logins"]
    _refresh_collaborators()
    return _collabs_cache["logins"]


def collaborator_records(force: bool = False) -> list[dict]:
    """List of `{login, avatar_url}` for each write-access collaborator.
    Same cache as `collaborator_logins()`."""
    slug = _repo_slug()
    if (not force
            and _collabs_cache["slug"] == slug
            and time.time() - _collabs_cache["at"] < _COLLABS_TTL_SEC):
        return list(_collabs_cache["records"])
    _refresh_collaborators()
    return list(_collabs_cache["records"])


def suggest_reviewers(pr_number: int, suggested_limit: int = 12) -> dict:
    """Return candidate reviewers grouped into two buckets:
    - `suggested`: people who've touched this PR's files recently,
      ranked by commit count / recency. Carries `commits`, `files`,
      `last_touched` metadata for UI context.
    - `others`: the rest of the repo's write-access collaborators,
      alpha-sorted. Avatar-only (no PR-specific stats).

    Both groups exclude the PR author, bots, anyone already requested
    as a reviewer, and anyone who has already reviewed. Every
    candidate has `can_request: True` (they're collaborators); the
    field is kept for UI compatibility.
    """
    pr = _gh_json([
        "gh", "pr", "view", str(pr_number),
        "--repo", _repo_slug(),
        "--json", "author,files,reviewRequests,reviews",
    ])
    author_login = ((pr.get("author") or {}).get("login") or "").lower()
    exclude = {author_login} if author_login else set()
    for r in pr.get("reviewRequests") or []:
        login = (r.get("login") or "").lower()
        if login:
            exclude.add(login)
    for rv in pr.get("reviews") or []:
        login = ((rv.get("author") or {}).get("login") or "").lower()
        if login:
            exclude.add(login)

    files = [f.get("path") for f in (pr.get("files") or []) if f.get("path")]
    # Keep the query budget bounded on huge PRs; the most-touched files
    # usually come first in GitHub's listing.
    files = files[:12]

    candidates: dict[str, dict] = {}
    for path in files:
        try:
            commits = _gh_json([
                "gh", "api",
                f"/repos/{_repo_slug()}/commits",
                "-X", "GET",
                "-f", f"path={path}",
                "-f", "per_page=10",
            ])
        except Exception:
            continue
        if not isinstance(commits, list):
            continue
        for c in commits:
            author = c.get("author") or {}
            login = (author.get("login") or "").strip()
            if not login or login.lower() in exclude:
                continue
            if login.endswith(_BOT_SUFFIX) or (author.get("type") or "") == "Bot":
                continue
            commit_date = ((c.get("commit") or {}).get("author") or {}).get("date") or ""
            entry = candidates.setdefault(login, {
                "login": login,
                "avatar_url": author.get("avatar_url"),
                "commits": 0,
                "files": set(),
                "last_touched": "",
            })
            entry["commits"] += 1
            entry["files"].add(path)
            if commit_date > (entry["last_touched"] or ""):
                entry["last_touched"] = commit_date

    ranked = sorted(
        candidates.values(),
        key=lambda e: (e["commits"], e["last_touched"]),
        reverse=True,
    )[:suggested_limit]
    try:
        collab_logins = collaborator_logins()
        collab_records = collaborator_records()
    except Exception:
        collab_logins = set()
        collab_records = []

    # `suggested` keeps the file-toucher ranking plus commit/file stats.
    # Every entry has `can_request` set — only collaborators end up in
    # the UI's "request" column; non-collaborators still appear (for
    # "nudge") but with the request checkbox disabled.
    suggested: list[dict] = []
    suggested_logins: set[str] = set()
    for e in ranked:
        e["files"] = sorted(e["files"])
        e["can_request"] = (not collab_logins) or (e["login"].lower() in collab_logins)
        suggested.append(e)
        suggested_logins.add(e["login"].lower())

    # `others` is everyone else with write access, minus the excluded
    # set (author/already-requested/already-reviewed) and anyone
    # already surfaced in `suggested`.
    others: list[dict] = []
    for rec in collab_records:
        login = (rec.get("login") or "").strip()
        if not login:
            continue
        low = login.lower()
        if low in exclude or low in suggested_logins:
            continue
        others.append({
            "login": login,
            "avatar_url": rec.get("avatar_url"),
            "commits": 0,
            "files": [],
            "last_touched": "",
            "can_request": True,
        })
    others.sort(key=lambda e: e["login"].lower())

    return {"suggested": suggested, "others": others}


_DRAWER_FIELDS = ",".join([
    "number", "title", "body", "url", "author",
    "state", "isDraft", "createdAt", "updatedAt",
    "headRefName", "baseRefName", "mergeable",
    "additions", "deletions", "changedFiles",
    "labels", "milestone",
    "reviewRequests", "reviews", "reviewDecision",
    "comments", "statusCheckRollup",
    "closingIssuesReferences", "assignees",
])


def fetch_pr_for_drawer(pr_number: int) -> dict:
    """Fetch a rich snapshot of a PR for the drawer view. One `gh pr
    view --json` call — no per-field round-trips."""
    return _gh_json([
        "gh", "pr", "view", str(pr_number),
        "--repo", _repo_slug(),
        "--json", _DRAWER_FIELDS,
    ])


def request_reviewers(pr_number: int, logins: list[str]) -> dict:
    """POST /repos/{owner}/{name}/pulls/{N}/requested_reviewers with
    the selected logins. Returns the raw gh API response."""
    if not logins:
        raise ValueError("no reviewers selected")
    cmd = ["gh", "api",
           "--method", "POST",
           "-H", "Accept: application/vnd.github+json",
           f"/repos/{_repo_slug()}/pulls/{pr_number}/requested_reviewers"]
    for login in logins:
        cmd.extend(["-f", f"reviewers[]={login}"])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"gh api request_reviewers failed: {result.stderr.strip()}"
        )
    try:
        return json.loads(result.stdout) if result.stdout else {}
    except json.JSONDecodeError:
        return {"raw": result.stdout}
