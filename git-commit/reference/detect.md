# Detecting a repo's commit conventions

Run this the first time the skill is used in a repo (no `.commit-profile` yet).
Infer each field, then **echo the whole inferred profile to the user for
confirmation** before writing it. Prefer explicit repo documentation over
inference from history; fall back to the conservative defaults below when a
signal is absent.

## `message.format`
1. Read the last ~20 commit subjects/bodies:
   ```bash
   git log -20 --format='%s%n%b%n----'
   ```
2. Classify:
   - Most subjects match `type(scope): summary` (feat/fix/docs/refactor/…) → **conventional**.
   - Bodies carry a consistent structured block (labeled lines like
     `Constraint:` / `Tested:`, sign-off schemes, etc.) → **custom**, and copy
     that block shape into `message.template`.
   - Otherwise → **plain**.
3. Cross-check `CONTRIBUTING.md`, `AGENTS.md`, `CLAUDE.md`, `.gitmessage` — a
   stated rule wins over inference.
4. `subject_max`: the max subject length seen in history, rounded to 50 or 72;
   default 72.

## `trailers`
- Scan recent commits for `Co-authored-by:` / `Signed-off-by:`:
  ```bash
  git log -30 --format='%(trailers)'
  ```
- `co_author`: true only if recent commits consistently include AI co-author
  lines AND no doc forbids them. Default **false** (many repos forbid them).
- `require`: any trailer that appears on essentially every commit (e.g.
  `Signed-off-by` in DCO repos).

## `formatter.cmd`
Look, in order, for a canonical format command the repo already exposes:
1. `Makefile` target `format`/`fmt` → `make format`.
2. `package.json` scripts `format`/`fmt`/`lint:fix` → `npm run <name>`.
3. `.pre-commit-config.yaml` → `pre-commit run --files <changed>`.
4. Language formatter configs present:
   `.clang-format`→clang-format, `.prettierrc*`→prettier, `pyproject.toml`
   `[tool.black]`/`[tool.ruff]`→black/ruff, `rustfmt.toml`/`Cargo.toml`→`cargo fmt`,
   `*.go`→`gofmt -w`.
5. None found → leave `cmd: null` (skip formatting).

## `review.cmd` / `verify.cmd`
- `verify.cmd`: the repo's test entrypoint — `make test`, `npm test`,
  `pytest`, `cargo test`, a CI script. null if none obvious.
- `review.cmd`: a review skill/command the repo expects before commit (often
  none). null by default.

## `staging.exclude`
Start from the repo's build/output dirs (from `.gitignore` and obvious
artifacts): `build/`, `dist/`, `target/`, `node_modules/`, `*.log`, plus any
local-state files. These are belt-and-suspenders on top of `.gitignore`.

## Conservative defaults (nothing detected)
`message.format: plain`, `subject_max: 72`, `trailers.co_author: false`,
`formatter.cmd: null`, `review.cmd: null`, `verify.cmd: null`.
