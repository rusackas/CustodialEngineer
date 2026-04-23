from .config import load_config
from .workspace import ensure_repo


def initialize():
    """Prepare the bot's local workspace: clone the configured repo if needed."""
    cfg = load_config()
    repo = cfg["repo"]
    path = ensure_repo(repo["owner"], repo["name"])
    return path
