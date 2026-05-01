# Memory — learned-pattern facts

Curated, stable lessons distilled from incidents, feedback PRs,
and validated judgment calls. Distinct from `CLAUDE.md` (stable
architecture / invariants — what's true by design) and
`BACKLOG.md` (running list of drive-by ideas — what's
unfinished).

This file is the right place for things like "we tried X and it
caused Y, so we learned Z." Each entry is a durable rule plus a
brief **Why** and (when not obvious) **How to apply**, so future
sessions can judge edge cases instead of blindly following the
rule.

## How this gets written

- When a `?` feedback PR merges and the underlying lesson is
  durable (i.e., generalizes beyond the one card that triggered
  it), add a one-line entry here as part of the same PR.
- Any session that's about to recommend something it suspects has
  been previously walked back should grep this file first.
- Entries get amended (not deleted) when superseded — leave a
  trail.

---

## Triage & action menu

### Failing CI is failing CI — never propose approve-merge over red checks

Don't recommend `approve-merge` while any check is in a failure
state (`FAILURE`, `TIMED_OUT`, `ACTION_REQUIRED`,
`STARTUP_FAILURE`), regardless of:
- whether the failing checks are branch-protected / required,
- the `mergeStateStatus` (UNSTABLE means GitHub considers it
  mergeable; that's not the bar),
- the skill's judgment that failures are unrelated drift / flake,
- whether the bump is trivial.

**Why:** PR #39759 (baseline-browser-mapping bump) — skill saw
`UNSTABLE` and concluded "Safe to approve-merge" in the proposal
narrative while the mechanical menu correctly excluded it. The
mismatch eroded trust in the menu/narrative alignment. Maintainer
policy: make CI green before merging, period.

**How to apply:** in `triage-dependabot-pr` and any other triage
skill that branches on CI state. The cross-cutting hard rule is
section 1c of `triage-dependabot-pr/SKILL.md`.

### The action menu reflects current GitHub signals — refetch before rebuild

Any code path that rebuilds the mechanical action menu must first
refetch the PR's `raw` from GitHub. `retriage_item` and
`refresh_one_item` both do this; new entry points must follow.

**Why:** PRs #39703 / #39794 — both showed `Unblock → fix-precommit`
in the action menu while the skill (which always fetches fresh
via `gh pr view`) correctly read CI as green. Cached `ci_status:
failing` was poisoning the menu. Stale signals are a recurring
root cause; refetching is cheap insurance.

### When the skill narrative contradicts the action menu, fix the skill

The mechanical menu is authoritative (see CLAUDE.md "Mechanical-
first triage"). If the skill's prose recommends an action the
menu doesn't include, the skill is the bug — tighten its prompt.

**Why:** PRs #39759 / #39703 / #39794 — three separate cases of
narrative diverging from menu. Users read the prose first; a
divergence reads as a broken tool.

### Hold-labels short-circuit the menu

PRs labeled `hold` / `wip` / `do-not-merge` / `do-not-merge/hold`
get a fixed action menu of `[await-update, prompt, close, skip]`
regardless of other signals.

**Why:** Maintainers use these labels to communicate "don't
act." Triage that ignores them generates noise the user has to
suppress manually. Implementation: `_held_short_circuit` in
`repobot/triage.py`.

### Bot review threads have three flavors; treat them differently

When deciding whether to propose `resolve-bot-threads`:
- **Boilerplate** (process violations, lockfile drift, coverage
  thresholds, CLA, "LGTM", changelog summaries) — safe to
  auto-resolve via the modal.
- **Substantive** (vulnerabilities, regressions, null deref,
  CVE references, missing test coverage flagged with specifics)
  — never auto-resolve; surface for human review.
- **Ambiguous** — show in the modal but pre-uncheck.

**Why:** bito / sonarcloud / dosu / coderabbitai produce a mix
of useful and non-actionable threads. Treating them as
homogeneous either over-blocks merges (boilerplate counted as
substantive) or hides real concerns (substantive auto-resolved).
Implementation: `classify_bot_thread` in `repobot/triage.py`.

### Two-phase stale-PR soft-close

For stale non-draft PRs that aren't bot-authored: first action
is `mark-as-draft` (soft warning + comment naming what's
needed). `close` only after a later sweep with no movement.

**Why:** Hard-closing on the first pass burns goodwill with
contributors who can still revive their work. Two-phase gives
them a clear signal + chance to respond.

### Substantive-only unpark

Awaiting-update items unpark only when substantive signals
change (new commit, new comment, review state change, CI flip,
conflict change), not on `updatedAt` bumps.

**Why:** Label churn, reviewer assignment shuffles, and bot
status check polls bump `updatedAt` constantly. Unpark on every
bump caused re-triage thrash on cards that hadn't actually
changed. Implementation: `should_unpark` + `park_signals` in
`repobot/queues.py`.

## Editorial control & voice

### Every public side-effect routes through a review modal

Comments, approval review bodies, close comments, PR
descriptions, replies to review threads — all of it. Skill
drafts, modal lets the user edit, action skill posts verbatim.
No exceptions, no "just this once" bypasses.

**Why:** Architectural invariant — the user is the maintainer,
not a passive consumer. Bypasses (even harmless-looking ones)
break the trust contract that lets `dry_run: false` stay safe.
This includes `attempt-fix-issue` PR descriptions, added in the
last sweep — PR bodies are public and live forever, same gate
as comments.

### First-person voice when posting as the user

Skills that post comments as the user must address other people
in second/third person and refer to the user in first person
("I'd love…", "I noticed…") — never "@rusackas thinks…" or
"rusackas is coordinating…".

**Why:** Reading "the bot is talking about you" rather than "to
you" is jarring and immediately reveals the post is bot-authored.
Even the dependabot triage path matters here because Dependabot
PRs often have substantive maintainer-driven coordination in the
comment thread.

### Push to existing head_ref on Apache Superset PRs; don't fork

When fixing a Superset PR as @rusackas (push-allowed on
apache/superset), push to the existing head ref. No fork, no
new branch unless one's needed.

**Why:** @rusackas is a maintainer with push access; forking
creates noise and detaches the fix from the original PR
narrative. Reply in first person too.

## Architecture & implementation

### Mechanical-first: action menu in Python, narrative in skills

If a card has the wrong action menu, fix `repobot/triage.py`
(or its callers in `runner.py`). Don't tweak a skill prompt to
"add" or "suppress" an action — skills don't decide menus.

**Why:** We tried the opposite (skills emit menus) and the
prompt fragility caused critical actions to disappear (approve-
merge missing on clean PRs, fix-precommit missing on lint
failures). Mechanical menus are testable, deterministic, and
fixable in one place.

### Custom-flow modal pattern for actions without a skill

For actions that need a confirmation modal but no Claude session
(request-reviewers, resolve-bot-threads, bulk-approve, track-pr,
spawn-feedback-task): register in `actions.ACTIONS_REGISTRY`
with `skill: None`, add a dedicated endpoint, add modal markup +
JS, route via `data-<flag>` submit-intercept.

**Why:** The pattern keeps the action button in the standard
menu loop (so the card UI stays uniform) while letting the
endpoint handle whatever bespoke flow it needs. Examples in the
repo cover the variations; new custom-flow actions should pick
the closest one and copy.

## Working with Evan

### Plan-then-build for non-trivial changes

Propose a 2–3 sentence plan with the main tradeoff. Wait for
"yes." Then execute.

**Why:** Evan reviews every PR. A misaligned implementation is
wasted work for both sides. Exploratory questions ("what could
we do about X?") get a recommendation, not a started
implementation. Direct asks ("yes please / go for it") are the
green light.

### Terse responses; lead with the answer

No pre-amble ("Great question!"), no trailing summaries that
restate the diff. File paths with line numbers (`repobot/triage.py:182`)
so Evan can jump there. End-of-turn summary is one or two
sentences max.

**Why:** Evan reads tool output directly; restatement is noise.
Maintained even on long sessions where the urge to recap is
strongest.

### Only commit when explicitly asked

Don't auto-commit because work looks done. Don't push without
the user saying so. Confirm before destructive operations
(`git reset --hard`, force-push, deleting branches, dropping
state).

**Why:** Standard safety; doubly so for an app whose state
includes live GitHub side-effects.
