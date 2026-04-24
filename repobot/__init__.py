from .workspace import ensure_repo


def initialize():
    """Prepare the bot's local workspace: clone every configured repo
    if needed. Returns the path of the default repo for call sites
    that care about just one."""
    from .github import list_repos, default_repo_slug
    repos = list_repos()
    for r in repos:
        ensure_repo(r["owner"], r["name"])
    default_owner, default_name = default_repo_slug().split("/", 1)
    from .workspace import WORKSPACE_DIR
    return WORKSPACE_DIR / default_name
