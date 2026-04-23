import subprocess
from pathlib import Path

from .config import PROJECT_ROOT

WORKSPACE_DIR = PROJECT_ROOT / "workspace"


def ensure_repo(owner: str, name: str) -> Path:
    """Clone github.com/{owner}/{name} into workspace/{name}. No-op if already cloned."""
    target = WORKSPACE_DIR / name
    if (target / ".git").is_dir():
        print(f"[repobot] {owner}/{name} already cloned at {target}")
        return target

    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{owner}/{name}.git"
    print(f"[repobot] cloning {url} -> {target}")
    subprocess.run(["git", "clone", url, str(target)], check=True)
    return target
