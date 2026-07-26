---
name: research-report
description: Write a research-log report that records a completed experiment or mechanism study for posterity, organized like a scientific paper (Key Idea, Mechanism, Evaluation Methodology, Key Results, Next Steps). Use this whenever the user wants to "record the learnings", "write up the experiment/sweep", "log this to the research log", "report this for posterity", or an experimental campaign (sweep, ablation, prototype evaluation) has just concluded and its insights would otherwise live only in the conversation — even if the user doesn't say "report".
---

# Research-log reports

Turn a finished piece of research — an implemented mechanism plus its
evaluation — into a permanent, self-contained markdown report that a reader
with no session context (including a future Claude) can trust and build on.
The organizing principle: **write it like a paper**, because the paper shape
forces the four questions a future reader has — what was the idea, what
exactly was built, how was it measured, what was learned.

## Where reports live

`docs/research-log/<Topic>/YYYY-MM-DD-<slug>.md`, with figures in a sibling
`figs/` directory. `<Topic>` is the mechanism/project area (e.g. `MRN`);
one file per campaign, dated. **Copy every chart (PNG + PDF + its
numbers-CSV) into `figs/`** — analysis directories usually live under
gitignored run trees, and a report whose figures die with a scratch
directory is not a record. The tracked tree must stand alone.

## Structure

Follow `reference/template.md` — read it before writing; it carries the
exact section skeleton with inline guidance. `examples/` holds a real,
complete report to calibrate tone and depth. The fixed shape:

1. **Title** — specific and claim-bearing, not generic.
2. **Byline** — `**Date:** … · **Branch:** … · **Authors:** <user's name>
   and <the assisting model's name>`. Both names: the human directs, the
   model implements — the record should say so.
3. **Key Idea** — the problem and the insight, including *why the previous
   approach failed* if there was one. A reader should understand the point
   of the whole report from this section alone.
4. **Mechanism** — what was actually built: data structures with exact
   fields, the algorithms per event/instruction type, deliberate deviations
   from any cited prior art. End with two inventory subsections:
   **Files Touched** (path + one-line role) and **Commits Covered** (every
   hash the report describes) — these make the report auditable against git.
5. **Evaluation Methodology** — simulator/platform, workloads, configs
   swept, metric definitions (define them exactly; ambiguous metrics are how
   results get misremembered), and any canonical-configuration decisions.
6. **Key Results** — the lead figure first, then an **enumerated list in
   descending order of importance**. Each item leads with the claim in bold
   followed by the numbers that support it. Include negative results and
   outlier warnings — they are often the most valuable entries.
7. **Next Steps** — enumerated, again in descending order of importance,
   each with a one-line why.
8. **References** — numbered, cited from the text as [n].

## Rules that came from getting it wrong

- **Title-case every header and subheader.**
- **Verify citations; never cite from memory.** Venue names and years get
  misremembered (a paper "from SC'03" turned out to be ICS 1999). If the
  user names a citation loosely, find the real one, cite it correctly, and
  tell them what you resolved it to.
- **Figure subtitles must not clip.** Keep chart-internal subtitles to two
  short lines (wrap with `\n` in the plot script and regenerate) and keep
  markdown captions to short lines. If a caption needs three clauses, it
  needs a sentence in the body instead.
- **Numbers in the report must be reproducible from the tracked tree**:
  every figure ships its numbers-CSV; every claim traces to a stat or a
  file in `figs/`.
- Charts follow the house plotting style — if a `ggplot-house-style` skill
  is installed, use it for any figure generated for the report.

## Workflow

1. Gather: the campaign's results (tables/notes from the session), chart
   artifacts, commit hashes (`git log --oneline` over the covered range).
2. Create the topic directory and `figs/`; copy chart artifacts in.
3. Write the report per the template; re-read it once with fresh eyes for
   clipped captions, header casing, and citation correctness.
4. **Present the draft to the user and wait for their corrections before
   committing** — byline, emphasis order, and figure cosmetics are theirs
   to call. Iterate in place; commit only on their signal, using the
   repo's commit conventions (commit-note files, message format — follow
   the repo's `git-commit` skill if present).

## Where this skill lives

Canonical source is `~/skillhub/research-report/`. A copy travels in each
consuming repo under `.claude/skills/research-report/`. When the canonical
version changes, re-sync with `bash sync.sh <target-repo>`.
