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
    """Create (or reuse) a worktree checked out at origin/{head_ref}."""
    target = worktree_path_for(pr_number)
    if (target / ".git").exists():
        _git("fetch", "origin", head_ref)
        _git("reset", "--hard", f"origin/{head_ref}", cwd=target)
        return target

    WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
    _git("fetch", "origin", head_ref)
    _git("worktree", "add", "--force", str(target), f"origin/{head_ref}")
    # Put the worktree on a local branch matching head_ref so pushes are natural.
    _git("checkout", "-B", head_ref, f"origin/{head_ref}", cwd=target)
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
