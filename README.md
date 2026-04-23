# repobot

A bot to help keep up an open source GitHub repository.

## Target repository

- **Repo:** [apache/superset](https://github.com/apache/superset)
- Configured in `config.yaml` (`repo.owner` / `repo.name`).

## Authentication

- **GitHub:** `gh` CLI reads `GITHUB_TOKEN` / `GH_TOKEN` from the env.
  No token is committed.
- **Claude:** the spawned `claude` CLI uses its OAuth session (Max /
  Pro subscription). The executor scrubs `ANTHROPIC_API_KEY` and
  `ANTHROPIC_AUTH_TOKEN` from the subprocess env to keep it on the
  subscription regardless of shell state.

## Configuration

`config.yaml`:

- `repo.owner` / `repo.name` — the target repo.
- `auth.token_env` — env var name for the GitHub token.
- `actions.dry_run` — when true (default), side-effecting steps
  (push, force-push, PR comment, PR close) are *logged* instead of
  executed. The Claude session still runs everything else, so you
  can review what it *would* have done. Flip to false when you trust
  the flow.
- `sessions.max_concurrent` — global cap on live Claude sessions
  (triage + action combined). Extra dispatches enter a `queued`
  state and start as slots free up. Default: 4.
- `queues` — array of work queues.

### Queues

| Field            | Meaning                                               |
| ---------------- | ----------------------------------------------------- |
| `id`             | Stable identifier (used by the API / state file).     |
| `title`          | Human label shown in the UI.                          |
| `max_in_flight`  | Cap counted against *non-done* items.                 |
| `initial_state`  | State assigned to freshly-fetched items.              |
| `done_state`     | State treated as terminal (default `done`).           |
| `states`         | Ordered columns shown in the UI.                      |
| `query`          | Fetch parameters.                                     |

Slot accounting: Refresh pulls `max_in_flight − non_done_count` items.
10 slots, 4 `in triage` + 6 `done` → next refresh grabs 6 more.

First queue: **Dependabot PRs** (id `failing-dependabot-prs`, kept
for backward compat) — up to 10 open Dependabot PRs, oldest-first.
Triage branches on CI status: green PRs get an approve-merge proposal
(after re-verifying `mergeStateStatus == CLEAN` and scanning reviews);
failing PRs get a root-cause analysis from the failing logs.

## Stack

- **Python** — FastAPI + Jinja2 (UI), PyYAML (config), `gh` CLI
  (GitHub ops), `claude-agent-sdk` (headless Claude Code sessions).
- Queue state at `state/queues.json`.
- Per-PR git worktrees at `workspace/worktrees/pr-N/`.

## Layout

```
repobot/
├── config.yaml
├── pyproject.toml
├── state/                              # gitignored; queue state
├── workspace/                          # gitignored
│   ├── superset/                       # main clone
│   └── worktrees/                      # per-PR worktrees
├── .claude/skills/
│   ├── triage-dependabot-pr/           # triage playbook (CI + reviews)
│   ├── approve-and-merge-pr/
│   ├── close-pr/
│   ├── comment-on-pr/
│   ├── rebase-pr/
│   ├── update-pr-lockfile/
│   ├── retrigger-pr-ci/
│   ├── dependabot-rebase-comment/
│   └── dependabot-recreate-comment/
└── repobot/
    ├── __init__.py     # initialize() — clones the target repo
    ├── __main__.py     # CLI: init / fetch / serve
    ├── config.py
    ├── workspace.py    # initial clone
    ├── worktree.py     # per-PR worktrees
    ├── queues.py       # state storage (locked)
    ├── github.py       # `gh` CLI wrappers
    ├── triage.py       # mechanical triage stub
    ├── runner.py       # fetch + triage orchestration
    ├── actions.py      # action registry + dispatch (threaded)
    ├── executor.py     # spawns headless Claude sessions (OAuth)
    ├── api.py          # FastAPI app
    ├── templates/index.html
    └── static/style.css
```

## Running

```bash
# 1. Clone apache/superset into workspace/
.venv/bin/python -m repobot init

# 2. Populate a queue (fetches + mechanically triages up to max_in_flight)
.venv/bin/python -m repobot fetch failing-dependabot-prs

# 3. Kanban UI at http://127.0.0.1:8000
.venv/bin/python -m repobot serve
```

## Actions

Each card in a non-terminal state shows buttons for the actions its
triage step produced, plus a `skip` button. Clicking a button POSTs
to `/queues/{q}/items/{id}/actions/{a}`, which:

1. Moves the item to `in progress` (instant for `skip`).
2. Spawns a headless Claude Code session in a background thread, with
   the matching Skill's procedure injected as the prompt.
3. Worktree-requiring actions (`rebase`, `update-lockfile`) get a fresh
   worktree at `workspace/worktrees/pr-N/`.
4. On completion, the session's JSON result is stored as
   `item.last_result` and the item lands in its terminal state.

| Action                | Skill                             | Worktree | Mutates (in non-dry-run) |
| --------------------- | --------------------------------- | -------- | ------------------------ |
| `skip`                | —                                 | —        | —                        |
| `close`               | `close-pr`                        | no       | `gh pr close`            |
| `prompt`              | `prompt-on-pr`                    | **yes**  | varies (user instruction) |
| `rebase`              | `rebase-pr`                       | **yes**  | force-push               |
| `update-lockfile`     | `update-pr-lockfile`              | **yes**  | force-push               |
| `attempt-fix`         | `attempt-fix-pr`                  | **yes**  | force-push               |
| `retrigger-ci`        | `retrigger-pr-ci`                 | no       | `gh run rerun`           |
| `approve-merge`       | `approve-and-merge-pr`            | no       | `gh pr review --approve` + `gh pr merge --squash --auto` |
| `dependabot-rebase`   | `dependabot-rebase-comment`       | no       | `@dependabot rebase`     |
| `dependabot-recreate` | `dependabot-recreate-comment`     | no       | `@dependabot recreate`   |

## To-dos

- [x] Python backend, clone, config, gitignore.
- [x] Config schema for queues; `failing-dependabot-prs` defined.
- [x] Mechanical fetch + triage + JSON state storage.
- [x] Kanban UI with non-done slot math and refresh.
- [x] Claude Agent SDK executor with OAuth auth.
- [x] Action registry, per-PR worktrees, threaded dispatch.
- [x] Skip button; `dry_run` default; all 8 action skills written.
- [x] Skill-driven triage (SDK-backed, parallel threads per item) with
      mechanical fallback.
- [x] Delete button on `done` cards (removes item + worktree).
- [x] Unit tests (pytest): triage / queues / runner / actions.
- [ ] Server-sent events / polling hook so the UI auto-refreshes
      while background actions run (currently: meta-refresh every 10s).
- [ ] Flip `dry_run` to false and run a real `rebase` / `close`.
- [ ] Additional queues (stale issues, stale PRs, PR reviews, …).
- [ ] Scheduled / event-driven runs.
- [ ] `git fetch` update flow for the main clone.
- [x] **Finer-grained session transcript**: emit `ToolUseBlock` (name +
      short arg summary), `ToolResultBlock` (truncated), and
      `ThinkingBlock` as distinct transcript rows so the modal reads
      like real Claude Code.
- [x] **Header stats bar**: active session count pill and aggregate
      token pill on `/` (also `/stats` JSON endpoint). Per-session
      context % and tokens in the modal header.
- [x] **Per-card token count**: cards show `N turns · Xk tok · Ys` on
      `last_result` once a session produces a result.
- [x] **Orphan worktree cleanup**: on startup, worktrees for PRs not
      represented in state are removed from disk. Per-card delete still
      cleans up its own worktree synchronously.
- [x] **Unified Dependabot queue with approve-merge**: one queue pulls
      all open Dependabot PRs oldest-first; triage branches on CI status
      (green → `approve-merge` after re-verifying `mergeStateStatus ==
      CLEAN` and scanning reviewer signals; failing → log-based
      root-cause analysis). `approve-and-merge-pr` skill runs
      `gh pr review --approve` + `gh pr merge --squash --auto` and bails
      with `needs_human` if any reviewer/comment gate fires.

## Notes

- Project initialized 2026-04-20.
- `apache/superset` cloned at `workspace/superset/` (branch `master`).
- SDK smoke test: dispatched `dependabot-rebase` (dry run) on PR
  #39479; session verified PR + printed the command it *would* have
  posted; item landed in `done` with `status: skipped_dry_run`
  after ~20s / 2 turns.
