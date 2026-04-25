---
name: convert-issue-to-discussion
description: Convert one open issue to a GitHub Discussion. Picks a discussion category (defaults to Q&A or General), runs the GraphQL mutation, reports the new discussion URL.
worktree_required: false
---

# Convert an issue to a discussion

**Inputs**: `issue.{owner, name, number}`, `dry_run`.

GitHub's `convertIssueToDiscussion` GraphQL mutation needs three
things: the issue's GraphQL node id, a discussion category id, and
optionally a body that overrides the issue body. We resolve these
in order.

## Procedure

### 1. Confirm the issue is still convertible

```
gh issue view {issue.number} --repo {issue.owner}/{issue.name} \
  --json state,id
```

- If `state != "OPEN"` → `status: skipped`, message: "issue is
  already closed; convert isn't available."

Capture `id` (the GraphQL node id) for step 3.

### 2. Resolve a discussion category

GitHub repos must have Discussions enabled and at least one
category. Try Q&A first (the most natural fit for "how do I…"
issues), fall back to General:

```
gh api graphql -F owner={issue.owner} -F name={issue.name} \
  -f query='query($owner:String!,$name:String!){
    repository(owner:$owner,name:$name){
      hasDiscussionsEnabled
      discussionCategories(first:25){nodes{id name slug}}
    }
  }'
```

- If `hasDiscussionsEnabled == false` → `status: needs_human`,
  message: "Discussions are not enabled on this repo. Enable them
  in repo settings, then retry."
- Pick by slug priority: `q-a` → `general` → first available
  non-announcement category. Capture its `id`.

### 3. **If `dry_run == true`**

Print: which category we'd use, the issue node id, the discussion
that *would* be created. Stop. Report `status: skipped_dry_run`.

### 4. Run the mutation

```
gh api graphql \
  -F issue_id={ISSUE_NODE_ID} \
  -F category_id={CATEGORY_NODE_ID} \
  -f query='mutation($issue_id:ID!,$category_id:ID!){
    convertPullRequestToDiscussion(input:{}) { ... }   # WRONG mutation
  }'
```

Wait — the correct mutation name is `convertIssueToDiscussion`. Use:

```
gh api graphql \
  -F issue_id={ISSUE_NODE_ID} \
  -F category_id={CATEGORY_NODE_ID} \
  -f query='mutation($issue_id:ID!,$category_id:ID!){
    convertIssueToDiscussion(input:{
      issueId:$issue_id, categoryId:$category_id
    }){
      discussion{ id number url title }
    }
  }'
```

Note: this mutation may be in a feature preview at any given time;
if `gh api` errors with a "feature not enabled" / "preview required"
message, bail with `status: needs_human` and the verbatim error so
the maintainer can do the convert from the GitHub UI instead.

### 5. Verify

The mutation response gives `discussion.url`. Capture and report.

## Output

```json
{
  "status": "completed | skipped | skipped_dry_run | needs_human | error",
  "message": "one-sentence summary",
  "discussion_url": "https://github.com/.../discussions/N or null",
  "category_used": "Q&A | General | …"
}
```

## Guardrails

- Don't write a comment on the original issue — the convert
  operation generates GitHub's own breadcrumb.
- If Discussions aren't enabled, surface that as `needs_human` and
  stop. Don't try to enable them automatically — that's a repo-
  settings change with permission implications.
