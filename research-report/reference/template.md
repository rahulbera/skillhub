# Report skeleton

Copy this shape; replace guidance comments with content. Keep every header
in Title Case. See `examples/` for a filled-in instance.

```markdown
# <Specific, Claim-Bearing Title: What Was Built and How It Was Judged>

**Date:** YYYY-MM-DD · **Branch:** `<branch>` · **Authors:** <User Name> and <Model Name>

---

## 1. Key Idea

<!-- The problem, the insight, and why the prior approach (if any) failed.
     2-3 paragraphs. A reader should get the whole point from this section.
     End with one sentence scoping what the report covers. -->

## 2. Mechanism

<!-- One paragraph: what was implemented, with citations [n] to the prior
     art it follows, and the design point/limitations stated up front. -->

### 2.1 Data Structures

<!-- Bullet per structure: name, geometry (entries × assoc, policy), exact
     field list in braces, and one sentence on any non-obvious field. -->

### 2.2 <Actor A> Algorithm

<!-- Numbered steps keyed to pipeline events. State invariants inline
     ("by construction, X holds because Y"). -->

### 2.3 <Actor B> Algorithm

<!-- Same. After the algorithms, one paragraph listing deliberate
     deviations from the cited prior art, so faithfulness is auditable. -->

### 2.4 Files Touched

<!-- Bullet per file (or tight group): path — one-line role. -->

### 2.5 Commits Covered

<!-- Every hash this report describes, oldest→newest, each with a 2-4 word
     label. This section makes the report auditable against git history. -->

## 3. Evaluation Methodology

<!-- Bullets: simulator/platform + core config; workloads + how driven
     (warmup/measurement windows); configurations swept; metric
     DEFINITIONS (exact formulas where ambiguity is possible); any
     canonical-configuration decision taken, with its date. -->

## 4. Key Results

![<alt text>](figs/<chart>.png)

*(figure: `figs/<chart>.{png,pdf}`, numbers in `figs/<chart>_numbers.csv`)*
*<One short line describing the shape of the figure.>*

<!-- Enumerated list, DESCENDING order of importance. Each item:
     **Bold claim sentence with the headline number.** Supporting numbers
     and one or two sentences of interpretation. Include negative results
     and words of caution (outliers, single-checkpoint traps) as items —
     they are records too. -->

## 5. Next Steps

<!-- Enumerated, DESCENDING order of importance. Each: bold name, what it
     targets, one-line why it is ranked where it is. -->

## References

[1] <Authors, "Title," Venue, Year — verified, not from memory.>
```
