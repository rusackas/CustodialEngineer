"""Identity shim — the contract for "who is this for?".

Today this is a single-user tool: the answer is always the user
configured at `identity.github_username` in config.yaml. The whole
file exists to make that a *helper call* rather than a scattered
read of config — so when a future multi-user mode lands (auth
cookies, per-user scoping), only this module's implementation
changes. Every caller that asked `current_user_id()` keeps
working; the value just starts varying per request.

Naming intent:
- `current_user_id()` is a *tenant id* — a stable string used to
  scope data to a person. Today it's the GitHub login, so it
  doubles as a human handle, but callers should treat it as opaque.
- This is conceptually distinct from the GitHub-search `@me`
  shorthand returned by `github._self_handle()`. That one resolves
  to a string GitHub recognizes; this one is for our own
  bookkeeping.
"""
from .config import load_config


_DEFAULT_USER_ID = "self"


def current_user_id() -> str:
    """Return a stable string identifying the current user.

    Today: pulls `identity.github_username` from config.yaml; falls
    back to `"self"` if unset.

    Future: when auth lands, reads the request's session cookie /
    token and returns the matching user record's id. Callers that
    asked through here keep working unchanged.
    """
    cfg = load_config()
    handle = (cfg.get("identity") or {}).get("github_username")
    return (handle or _DEFAULT_USER_ID).strip() or _DEFAULT_USER_ID
