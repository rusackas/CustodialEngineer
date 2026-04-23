---
name: address-review-comments
description: Walk through unresolved review threads on the user's own PR. Phase 1 applies fixes (one commit per thread, push) and drafts a reply per thread. Phase 2 posts the human-approved replies. Never leaves a thread silent.
worktree_required: true
max_turns: 80
---

# Address unresolved review comments on my PR

You're `cd`'d into a git worktree at `worktree_path`, on the PR's
branch (`pr.head_ref`) — the user authored this PR and has write
access to the source repo. Stay on that branch; push directly to
`origin`; do NOT fork and do NOT create a new branch.

## This runs in two phases

**Phase 1 — Draft (your first turn).** Iterate every unresolved
thread. For each, decide fix-or-decline, apply the fix locally,
commit, push, and collect a **draft reply**. Do NOT post replies
yet. Emit `status: "drafts"` with the per-thread list.

**Phase 2 — Post replies (next turn, after the human approves).** The
human's follow-up message will start with `APPROVED REPLIES:` followed
by the (possibly edited) drafts JSON. When you see that, post each
thread's `reply_body` inline via the REST endpoint. Emit the standard
completion output schema.

If the human replies with anything else in phase 2 (a question, a
revision request not in the APPROVED REPLIES format), answer briefly
and stay idle — do not post.

## Inputs (runtime context)

- `pr` — `{owner, name, number, url, title, head_ref}`.
- `dry_run` — if true, DO NOT push commits and DO NOT post replies;
  log what you would have done. You still emit drafts in phase 1.
- `worktree_path` — absolute path of the checked-out worktree.
- `identity.github_username` — the PR author (that's the user you're
  serving). Don't @-mention them, don't thank them, don't refer to
  them in third person — write replies in first person ("I'll fix
  this", "keeping this as-is because…"), never as a bot narrating
  what the author should do.

## Phase 1 procedure — draft

### 1. Fetch all unresolved review threads

```
gh api graphql -f query='
  query($owner:String!,$name:String!,$number:Int!){
    repository(owner:$owner,name:$name){
      pullRequest(number:$number){
        reviewThreads(first:100){
          nodes{
            id isResolved isOutdated path line
            comments(first:20){
              nodes{
                databaseId body author{login} createdAt
                diffHunk originalLine
              }
            }
          }
        }
      }
    }
  }
' -F owner={pr.owner} -F name={pr.name} -F number={pr.number}
```

Filter to `isResolved == false`. Process oldest-first (by first
comment's `createdAt`). Note the FIRST comment's `databaseId` on each
thread — phase 2 needs it to post the reply.

### 2. For each thread, decide fix vs. decline

**Fix** when the request is:

- Concrete and mechanical (rename, extract helper, fix typo, add
  null-check, reorder, update docstring, add missing test case,
  replace deprecated API).
- In scope of the PR (touches the same code path or a natural
  neighbor).
- Not blocked on information only the author has.

**Decline** when the request is:

- Architectural pushback on the PR's premise ("we shouldn't do this
  at all") — the author has to weigh in, not you.
- Scope expansion ("while you're here, also refactor X").
- Already-addressed in a later commit — verify via `git log -p` and
  draft a reply pointing to that SHA instead of making a new commit.
- Ambiguous or underspecified (can't tell what change is wanted).
- A question that isn't a change request — draft a direct answer.

**If outdated (`isOutdated: true`)**: the code has moved. Either the
concern is already resolved (draft a reply pointing to the SHA that
fixed it) or it applies to current code at a different line (draft
acknowledgment and re-address at the current location). Never
silently drop outdated threads.

### 3. Apply the fix (one commit per thread)

Scope each fix to the file(s) the thread points at. Before editing:

```
git log --oneline -5    # make sure branch is clean
git status --porcelain
```

Make the edit, then verify cheaply if possible (tsc --noEmit on the
touched file, one targeted test). If verification fails, treat the
fix as declined — draft a reply explaining what broke instead. Do
NOT commit broken code.

Commit:

```
git add <files>
git commit -m "address review: <one-line summary>"
SHA=$(git rev-parse HEAD)
```

### 4. Pre-commit hygiene (after each fix commit)

Run pre-commit on just the touched files (same as fix-precommit-pr).
Auto-fixers rewrite in place; re-stage and amend the fix commit if
anything changed. Skip gracefully if pre-commit isn't installed.

```
pre-commit run --files <files you touched>
git add <files> && git commit --amend --no-edit
```

### 5. Push after each fix

Push after each individual fix commit so the draft reply can cite a
real SHA that resolves on GitHub:

- `dry_run == true`:
  `git push --dry-run --force-with-lease origin HEAD:{pr.head_ref}`
- Otherwise:
  `git push --force-with-lease origin HEAD:{pr.head_ref}`

### 6. Draft the reply (do NOT post)

**For fixes** — draft a short confirmation referencing the SHA via
the short form GitHub understands (`abc1234`). Example:
`"Fixed in abc1234."` — keep it short; the commit speaks for itself.

**For declines** — draft a one-to-three-sentence rationale. Be
specific; never just "won't do". Examples:
- "Leaving this for a follow-up — it'd drag in the scheduler
  refactor which is out of scope here."
- "Looked into this but the proposed approach breaks the existing
  cache contract in `x.py`; happy to pair on it separately."
- "Addressed in abc1234 — the nested conditional was removed during
  the rewrite."

**DO NOT** post the reply via `gh api` in phase 1. The human reviews
and approves the drafts first; phase 2 posts them.

### 7. Emit the drafts

```json
{
  "status": "drafts",
  "message": "Drafted replies for N threads: M fixes, K declines.",
  "threads": [
    {
      "id": "PRRT_...",
      "first_comment_id": 123456789,
      "path": "superset/x.py",
      "line": 42,
      "action": "fix | decline | outdated-already-fixed",
      "commit_sha": "abc1234",
      "reply_body": "Fixed in abc1234.",
      "should_resolve": true,
      "reason": "renamed helper per suggestion"
    }
  ],
  "pushed": true
}
```

- `status` MUST be `"drafts"` in phase 1.
- `first_comment_id` is the `databaseId` from the graphql query —
  phase 2 keys off it. The GraphQL node id (`"id"` above) is what
  phase-2 resolution uses.
- `reply_body` is the human-editable text. Every thread must have one.
- `commit_sha` is empty/omitted on decline.
- `should_resolve` is a per-thread hint for whether phase 2 should
  resolve the thread after posting the reply. Populate it based on
  what you did:
  - `fix` with a committed SHA → `true` (you addressed the concern).
  - `outdated-already-fixed` → `true` (the concern was already
    handled in a prior commit; the thread should close).
  - `decline` with a reasoned reply → `false` (leave it for the
    reviewer to decide; your reply explains your stance).
  - Replies that explicitly defer ("I'll handle this in a follow-up"
    / "need your input on X") → `false`.
  The human can toggle this in the drafts modal before approving.
- Every unresolved thread in the fetch MUST appear in `threads`.

## Phase 2 procedure — post approved replies

When the user's follow-up opens with `APPROVED REPLIES:`, parse the
JSON. It has the same shape as your phase-1 output but may have
edited `reply_body` values (and may drop threads the human decided
to handle manually).

For each entry with a non-empty `reply_body`, post an **inline reply
on the thread** via the REST endpoint:

```
gh api \
  --method POST \
  -H "Accept: application/vnd.github+json" \
  /repos/{pr.owner}/{pr.name}/pulls/{pr.number}/comments/{first_comment_id}/replies \
  -f body="<reply_body>"
```

Then, if the thread's `should_resolve` is `true`, also resolve the
thread via the GraphQL mutation (node id from the phase-1 output's
`id` field):

```
gh api graphql \
  -f query='mutation($threadId:ID!){
    resolveReviewThread(input:{threadId:$threadId}){thread{id isResolved}}
  }' \
  -F threadId="<thread.id>"
```

A thread can have `should_resolve: true` with no `reply_body` — this
is the "already addressed in a prior commit, nothing more to say"
case. In that case, skip the reply and go straight to the resolve
call.

In `dry_run`: do not call either API; log the intended payloads.

Emit the standard completion schema:

```json
{
  "status": "completed | skipped_dry_run | needs_human | error",
  "message": "one sentence: M replies posted, K skipped",
  "posted": [
    {"thread_id": "PRRT_...", "first_comment_id": 123456789}
  ],
  "skipped": [
    {"thread_id": "PRRT_...", "reason": "empty reply_body"}
  ]
}
```

- `status: "skipped_dry_run"` when dry_run == true and nothing was
  posted (but phase-1 commits may have been made locally).

## Guardrails

- NEVER edit outside the file(s) the thread points at (except for
  pre-commit auto-fixes on those same files).
- NEVER disable / skip tests. Failing verification becomes a decline
  draft, not a silent skip.
- NEVER push without `--force-with-lease`.
- NEVER fork, NEVER create a new branch — push directly to
  `origin HEAD:{pr.head_ref}`.
- NEVER @-mention `identity.github_username` or thank them; first
  person always.
- NEVER fabricate a commit SHA in a draft — the push must have
  succeeded first (or, in dry_run, cite no SHA and note it).
- NEVER post replies in phase 1. Drafting only.
- If you hit ~8 fixes / ~12 thread actions and more remain, bail
  phase 1 with `status: "needs_human"` listing the unaddressed
  thread ids and paths.
