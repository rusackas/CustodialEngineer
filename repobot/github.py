"""Thin wrappers around the `gh` CLI. Auth comes from GITHUB_TOKEN / GH_TOKEN.

GitHub's GraphQL gets slow when asking for `statusCheckRollup` across many PRs
at once, so we fetch the list with lightweight fields first, then hydrate
check status per-PR. A small retry loop absorbs transient 5xx responses.
"""
import json
import subprocess
import time

from .config import load_config

LIST_FIELDS = "number,title,url,mergeable,updatedAt,headRefName,isDraft,author"
CHECK_FIELDS = "statusCheckRollup"


def _repo_slug() -> str:
    cfg = load_config()
    return f"{cfg['repo']['owner']}/{cfg['repo']['name']}"


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
    return pr


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
        out.append(pr)
    return out


# Legacy alias — kept so any external caller referencing the old name
# still works. New code should use `fetch_dependabot_prs`.
def fetch_failing_dependabot_prs(limit: int = 50) -> list[dict]:
    return [p for p in fetch_dependabot_prs(limit=limit)
            if p.get("ci_status") == "failing"]


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
    cfg = load_config()
    owner = cfg["repo"]["owner"]
    name = cfg["repo"]["name"]
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
            out.append(pr)
    return out


_BOT_SUFFIX = "[bot]"


_COLLABS_TTL_SEC = 600  # 10 min
_collabs_cache: dict = {"at": 0.0, "slug": "", "logins": set()}


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
    out: set[str] = set()
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
                out.add(login.lower())
        if len(batch) < 100:
            break
        page += 1
    _collabs_cache.update({"at": time.time(), "slug": slug, "logins": out})
    return out


def suggest_reviewers(pr_number: int, limit: int = 8) -> list[dict]:
    """Rank candidate reviewers for a PR by who's committed to the
    touched files recently. Excludes the PR author, bots, anyone
    already requested, and anyone who has already reviewed.

    Returns a list of dicts: {login, commits, files, last_touched,
    avatar_url}. Sorted by commit count desc, then recency desc.
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
    )[:limit]
    # Annotate each candidate with whether they can be requested as
    # a reviewer — i.e., whether they're a repo collaborator. Callers
    # (the modal UI) use this to decide which of the two checkboxes
    # next to each name is available.
    try:
        collabs = collaborator_logins()
    except Exception:
        collabs = set()
    for e in ranked:
        e["files"] = sorted(e["files"])
        # If we couldn't fetch collaborators at all, don't block the
        # request checkbox for anyone — let the server reject it if
        # it must.
        e["can_request"] = (not collabs) or (e["login"].lower() in collabs)
    return ranked


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
