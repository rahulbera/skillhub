# House-style plotting conventions

This is the grammar every chart script follows. Steps 1-3, 8, 9, 10 are
**fixed** (the portable look-and-feel, provided by `style_template.R`). Steps
4-7 are **data-dependent** (filled from the project's `.plot-contract.yml`).

## The 10-step grammar

1. `#!/usr/bin/env Rscript` + a header comment documenting **Inputs** and **Outputs**.
2. `suppressPackageStartupMessages({ library(ggplot2); library(hrbrthemes); library(dplyr); library(tidyr); library(yaml); library(ragg); library(scales) })`.
3. `source(<project>_style.R)` — the single source of truth for color / order / labels (a filled copy of `style_template.R`).
4. **Read the rollup CSV** named in the contract, with its declared schema.
5. **Join per-row metadata** from the contract's metadata file (category / workload / weight), if any.
6. **Transform**: baseline = the contract's baseline row; `speedup = metric / baseline`; aggregate per the contract's default (`geomean` | `weighted_geomean` | `mean`).
7. **Relabel + order** raw series tokens to display names via `house_relabel()` and factor with `house_levels`.
8. **Write a `*_numbers.csv`** holding the exact numbers behind every bar/line — same basename as the chart, in the output dir. Non-negotiable (reproducibility).
9. **Plot**: `house_col()` / `geom_line()` + `house_fill_scale()` (or `house_colour_scale()`) + `theme_house()` + `house_hline1()` at y=1 + data labels on the summary group.
10. **Dual export** via `house_save(p, path_noext)` — PNG (ragg @200dpi) + PDF (cairo), identical geometry.

## Non-negotiable rules (the house look-and-feel)

- **No error bars** unless the user explicitly asks for them.
- **A numbers-CSV behind every chart** (step 8). If you cannot write it, the chart is not done.
- **Both PNG and PDF**, every time, via `house_save()`.
- **Color is tied to the series NAME**, not its position — defined once in `house_fill`. Reordering a chart never recolors a series. This is what makes a whole paper's figures cohere.
- **Data labels** go on the summary group only (the `GEOMEAN` / `AVG` bar), `angle = 90` when the chart is dense.
- **Benefit-of-doubt accuracy**: a series that makes no prediction counts as **100% accuracy / 0% coverage**, never as "wrong" (`benefit_of_doubt_accuracy()`).
- **Aggregation discipline**: within a group use the contract's aggregation (weighted geomean for per-workload speedup, arithmetic mean for precision/recall); across groups the summary is a non-weighted geomean/mean unless told otherwise. State which in the subtitle.
- **Palettes, not patterns**: prefer large-contrast sequential colors within a family. `ggpattern` is optional and may be unavailable (it was uninstallable in our sandbox) — never make a chart depend on it.

## Chart archetypes (see `examples/hermes/`)

- **Grouped bars** — `plot_speedup_membound146.R`: geomean/weighted-geomean by category/workload with a `GEOMEAN` group and value labels.
- **Line / S-curve** — `plot_scurve_rateint_hermes.R`: per-item metric sorted low→high, subtitle summary (`min: … ; max: … ; top-N: …`).
- **Sorted tornado** — `plot_gap_pcless_vs_unc_pythia.R`: signed per-item gap sorted, two-color by sign, `geom_hline` at 0.
- **2-facet** — `plot_accuracy_coverage_membound146.R`: two metrics (accuracy + coverage) side by side sharing the style.

Any other chart is composed from this grammar at request time — do not invent a new styling system.

## The full reference set

These four are curated archetypes. The complete campaign chart library
(~18 scripts) lives in `runs/tuning/analysis/scripts/` in the Hermes repo,
with the canonical `hermes_style.R` alongside — consult it for a less common
chart shape before building one from scratch.
