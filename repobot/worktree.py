"""Per-PR git worktree management.

Worktrees live under workspace/worktrees/pr-{number} and share .git with
the main clone at workspace/{repo_name}. Actions that need to modify the
PR's branch (rebase, lockfile regen) run inside one of these.
"""
import subprocess
from pathlib import Path

from .config import PROJECT_ROOT, load_config

WORKSPACE_DIR = PROJECT_ROOT / "workspace"
WORKTREES_DIR = WORKSPACE_DIR / "worktrees"


def repo_path() -> Path:
    cfg = load_config()
    return WORKSPACE_DIR / cfg["repo"]["name"]


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else str(repo_path()),
        capture_output=True,
        text=True,
        check=True,
    )


def worktree_path_for(pr_number: int) -> Path:
    return WORKTREES_DIR / f"pr-{pr_number}"


def ensure_worktree(pr_number: int, head_ref: str) -> Path:
    """Create (or reuse) a worktree for the PR.

    Fetches the PR's head via GitHub's `refs/pull/{N}/head` ref instead
    of the branch name. That ref is created for every PR (fork or
    in-repo) and is reachable from `origin` regardless of where the
    branch itself lives, so cross-fork PRs work the same as maintainer
    PRs. The worktree is checked out on a local branch named
    `{head_ref}` so pushes still target that name on origin for PRs
    whose branch does live in origin; for fork PRs, push-back is a
    separate concern handled by `fix-precommit-review`-class actions.
    """
    target = worktree_path_for(pr_number)
    # Namespaced local ref so the PR head doesn't clash with any
    # local branch named the same.
    pr_ref = f"refs/ce-pr/{pr_number}"
    _git("fetch", "origin",
         f"+refs/pull/{pr_number}/head:{pr_ref}")

    if (target / ".git").exists():
        _git("reset", "--hard", pr_ref, cwd=target)
        # Make sure the local branch tracks the fetched ref.
        _git("checkout", "-B", head_ref, pr_ref, cwd=target)
        return target

    WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
    _git("worktree", "add", "--force", "-B", head_ref,
         str(target), pr_ref)
    return target


def remove_worktree(pr_number: int) -> None:
    target = worktree_path_for(pr_number)
    if not target.exists():
        return
    try:
        _git("worktree", "remove", "--force", str(target))
    except subprocess.CalledProcessError:
        pass


def existing_worktree_numbers() -> list[int]:
    """Scan workspace/worktrees/pr-*/ and return the PR numbers on disk."""
    if not WORKTREES_DIR.exists():
        return []
    nums = []
    for child in WORKTREES_DIR.iterdir():
        if child.is_dir() and child.name.startswith("pr-"):
            try:
                nums.append(int(child.name[3:]))
            except ValueError:
                continue
    return nums


def prune_orphan_worktrees(live_pr_numbers: set[int]) -> list[int]:
    """Remove any worktree whose PR number isn't in the live set.
    Returns the list of PR numbers removed."""
    removed = []
    for n in existing_worktree_numbers():
        if n not in live_pr_numbers:
            remove_worktree(n)
            removed.append(n)
    # Also run `git worktree prune` to drop stale admin entries for
    # anything that was already removed from disk.
    try:
        _git("worktree", "prune")
    except subprocess.CalledProcessError:
        pass
    return removed
