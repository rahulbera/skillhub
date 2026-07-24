---
name: git-commit
description: Prepare a clean, convention-following git commit in any repo. Detects the repo's commit conventions (message format, formatter, trailers, review/test gates) once and persists them to a .commit-profile, then formats, verifies, stages, and commits. Use when creating commits or when the user asks to commit changes.
---

# Git commit workflow (generic)

Make a well-formed commit that follows **this** repo's conventions. The workflow
is fixed; the repo-specific bits live in a `.commit-profile` at the repo root,
learned once and reused.

> This is the portable template. A repo may keep a **specialized fork** of this
> skill (e.g. Hermes's Lore-format `git-commit`). Do NOT `sync.sh` this generic
> version over a repo that has its own — it would clobber the specialization.

## Workflow

Create a todo per step; work them in order.

### 1. Orient
- Look for `.commit-profile` at the repo root (`git rev-parse --show-toplevel`).
- If present, load it and go to step 2.
- If absent, follow `reference/detect.md` to infer the repo's conventions,
  **echo the inferred profile to the user for confirmation/correction**, then
  write it to `<repo-root>/.commit-profile` (copy `reference/profile_template.yml`
  and fill it). See `examples/hermes-lore.commit-profile.yml` for a filled one.

### 2. Survey the change
```bash
git diff --name-only
git diff --cached --name-only
```
Understand what changed, and whether it is one coherent change. If it is two
unrelated changes, split them into separate commits.

### 3. Format  (only if `formatter.cmd` is set)
Run the repo's formatter on the changed files, then re-stage anything it
rewrote so the formatting lands in this commit. Skip cleanly if `formatter.cmd`
is empty.

### 4. Review  (only if `review.cmd` is set)
Run the configured review step. Skip if null.

### 5. Verify  (only if `verify.cmd` is set and code changed)
Run the configured verification (tests / lint) when code or executable behavior
changed. Skip for docs-only changes, or when null.

### 6. Stage
Stage only the intended files — including anything the formatter rewrote. Never
stage paths matched by `staging.exclude` (build output, logs, local machine
state).

### 7. Review the staged diff
```bash
git diff --cached
```
Confirm every staged hunk belongs in this commit.

### 8. Commit
Write the message in `message.format`:
- **conventional** — `type(scope): summary` (imperative, ≤ `subject_max` chars),
  blank line, body explaining what and why.
- **plain** — imperative subject ≤ `subject_max`, blank line, rationale body.
- **custom** — follow `message.template` verbatim (e.g. a Lore block).

Apply `trailers`: add any `require`d trailers; add AI co-author lines **only if**
`trailers.co_author` is true.

## Rules
- One coherent change per commit.
- Never stage generated artifacts, build output, or local machine state.
- Commit or push only when the user asked for it.
- The message states what changed and why; record real constraints and rejected
  alternatives when they mattered.
