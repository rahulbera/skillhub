# skillhub

Canonical home for reusable Claude Code skills across my workflow. Each
subdirectory is one self-contained skill (a `SKILL.md` plus its `reference/`
and `examples/`). Skills are synced into consuming repos under
`.claude/skills/<name>/` via each skill's `sync.sh`.

## Skills

- **ggplot-house-style** — generate publication-quality charts in a consistent
  house style (ggplot2 + hrbrthemes, dual PNG/PDF export, numbers-CSV per
  chart). Orients to a project's data format once, then plots.
- **git-commit** — review + verification + Lore commit preparation. Hermes-specific
  (references `AGENTS.md` Lore format, C++ `.clang-format`, `code-review`/`testing`
  skills); adapt before using in another repo.
