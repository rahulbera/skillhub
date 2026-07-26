# Memory Renaming via a Value-File Rendezvous: Implementation and Full-Suite Evaluation

**Date:** 2026-07-26 · **Branch:** `rbdev` · **Authors:** Rahul Bera and Claude Fable 5

---

## 1. Key Idea

Loads that repeatedly communicate with an identifiable producer — a store to
the same address, or their own previous value — can have that communication
predicted at rename and delivered through registers, bypassing the memory
round-trip. Our previous MRN alias path predicted the producing store's PC
and then *searched* for the store instance in the store queue at the load's
rename. That search is structurally doomed for the tight recurrences it
targets: a store enters the store queue at dispatch, `renameToIEWDelay`
(2 cycles) after its own rename, so the same-iteration producer is **never**
visible when the load renames — the search always returns a prior iteration's
instance, whose captured data physreg is stale for any changing value.

The fix is to stop searching and **rendezvous** instead: the store *deposits*
its identity into a named cell at its own (in-order) rename; the load *reads*
the cell at its rename. In-order rename guarantees the deposit precedes the
read and belongs to the program-order-youngest earlier instance. This report
covers a faithful re-implementation of memory renaming in this style, its
gem5 O3 integration, and a 190-checkpoint evaluation.

## 2. Mechanism

A faithful implementation of hardware memory renaming after Tyson & Austin
[1], using the concrete three-table organization described by Reinman,
Calder, Tullsen, Tyson & Austin [2] (§2.3; see also the Calder & Reinman
survey [3] for the value/tag consumption semantics), adapted to gem5's O3
pipeline. Dependence distance 1 is the explicit design point ([2] §4 states
the same limitation).

### 2.1 Data Structures

- **Value File (VF)** — the rendezvous cells (1024, global LRU). Cell:
  `{gen, ptrValid, PhysRegIdPtr ptr, InstSeqNum ptrSeq, valueValid,
  RegVal value, lru}`. `gen` is a **generation tag**: incremented only when
  LRU reallocates the cell to a new owner; all references into the VF carry
  `{vfIdx, gen}`, so a reference into a recycled cell reads as "no binding"
  instead of silently consuming another PC's channel (a dangling-reference
  guard the original papers do not need to discuss, but which matters in
  practice).
- **Store/Load Cache (SLC)** — PC-indexed, 4096 × 4-way LRU, shared by store
  and load PCs. Entry: `{tag, vfIdx, gen, conf, selfBound, lru}`. `conf` is a
  saturating confidence counter (4 bits) on load entries.
- **Store Cache (SC)** — address-indexed, 4096 × 4-way LRU at 8 B
  granularity. Entry: `{tag, vfIdx, gen, storeSeq, lru}`. Writes are
  **program-order guarded**: a publish only overwrites an occupant whose
  `storeSeq` is older, closing the out-of-order address-resolution race.

### 2.2 Store Algorithm

1. **Rename** (in-order): find-or-allocate the store PC's SLC entry and VF
   cell; deposit `ptr := renamed data-source physreg` (at the store's own
   rename this is by construction the correct instance's physreg),
   `ptrSeq := seqNum`; deposit `value` too iff the data physreg is already
   scoreboard-ready, otherwise **invalidate** any stale value (a leftover
   value is the previous instance's data). Carry `{vfIdx, gen}` on the
   DynInst.
2. **Address resolution** (LSQ execute): publish
   `SC[physEffAddr] := {vfIdx, gen, seqNum}` from the carried reference —
   never a re-lookup — under the program-order guard.

### 2.3 Load Algorithm

1. **Rename**: look up the load PC in the SLC. On a gen-valid, confident
   binding, consume by priority: producer physreg **not** ready → **alias**
   the load's destination to it (existing MRN alias enforcement/verify);
   producer ready → **value-forward** its value; value-only cell (self-bound)
   → **last-value forward**. Below confidence: snapshot a shadow prediction
   instead (no consumption).
2. **Address resolution**: probe `SC[physEffAddr]`. Hit on a different
   channel → **rebind** the SLC entry to it and reset confidence; miss →
   **self-bind**: allocate an own VF cell (this is what makes last-value
   prediction of stable loads fall out of the same structure).
3. **Writeback**: if self-bound, write the loaded value into the cell.
   Verify consumed predictions against the true loaded value (mismatch →
   squash from the load, inclusive); train confidence from consumed *and*
   shadow outcomes. Squash-before-validation is accounted separately, so
   `made = correct + wrong + squashed` holds exactly per mode.

Deliberate deviations from [1,2]: all front-end events at rename rather than
decode; physical addresses for the SC; generation tags; the program-order
publish guard; uniform LRU allocation for the self-bind fallback (the paper
PC-indexes it); shadow training so confidence can rise without consumption.

### 2.4 Files Touched

- `src/cpu/o3/mem_rename_valuefile.{hh,cc}` — params-free core tables
  (VF/SLC/SC + confidence); `mem_rename_valuefile.test.cc` — 13 unit tests.
- `src/cpu/o3/mem_rename_predictor.hh`, `mem_rename_predictor_sim.cc`,
  `MemRenamePredictor.py` — SimObject wrapper, params
  (`mrnCorrelation=value_file`, sizes, `vfForwardProducerValue/LastValue`),
  per-mode stats.
- `src/cpu/o3/dyn_inst.hh` — carried `{vfIdx, gen}`, consumption mode,
  shadow snapshot.
- `src/cpu/o3/rename.cc` — store deposit, load lookup + consumption decision.
- `src/cpu/o3/lsq_unit.cc` — SC publish/probe, last-value capture, verify
  hooks, shadow training.
- `src/cpu/o3/rob.cc`, `src/cpu/o3/cpu.cc` — squashed-before-validation
  accounting.
- `src/cpu/o3/SConscript`, `configs/garfield/arm/sim_opts.py` — build + CLI.

### 2.5 Commits Covered

`71ee8e18a7` (design spec) · `d8844e205f` (implementation plan) ·
`20718d20ff` (core tables + tests) · `9ea6c4d507` (test hardening) ·
`8a3c52f019` (params/stats/CLI) · `a741c12f04` (rename integration) ·
`e046f9fd6d` (LSQ integration) · `428f07d488` (review polish) ·
`fcf6bd1c70` (research-log update).

## 3. Evaluation Methodology

- **Simulator:** gem5 FS, ARM, Neoverse-V2-like O3 core
  (`configs/garfield/arm/neoverse_v2.py`), 8-wide, 512-ROB class.
- **Workloads:** 190 SPEC26 Rate checkpoints (26 benchmarks × SimPoints,
  `tracezoo` FS checkpoints), restored with 10 M instruction warmup + 50 M
  detailed per checkpoint. Baseline IPCs reused from the July-19 campaign
  (no-MRN runs are bit-identical across these commits; verified on three
  checkpoints).
- **Configurations:** value-file rendezvous, alias enabled, legacy value
  path disabled, confidence threshold swept {6, 8, 10, 12, 14, 15}
  (1,140 runs). Directed microbenchmarks `mrnrec` (changing distance-1
  recurrence) and `mrncomm` (stable value) as functional gates.
- **Metrics:** per-checkpoint IPC speedup vs no-MRN; per-mode prediction
  accounting with the exact identity `made = correct + wrong + squashed`;
  accuracy reported both as correct/made and validated-only
  correct/(correct+wrong).
- **Canonical configuration:** threshold 14 is "baseline MRN" for all future
  comparisons (decision 2026-07-26).

## 4. Key Results

![Per-checkpoint speedup S-curve, MRN th14 over no-MRN](figs/scurve_mrn_th14.png)

*(figure: `figs/scurve_mrn_th14.{png,pdf}`, numbers in
`figs/scurve_mrn_th14_numbers.csv`)*
*Shape: long flat middle near 1.0; shallow left tail to 0.960; steep right
tail to 1.278.*

1. **First net-positive MRN configuration: geomean +0.92% over 190
   checkpoints at threshold 14** (th12: +0.87%). Prior MRN results on the
   same suite: legacy value path 1.0001 (break-even), legacy alias path
   0.985 (net loss). Right tail is substantial: sqlite +27.8%, vpr +22.5%,
   marian +13.8%; top-10 geomean 1.166.
2. **The benefit is dominated by last-value (stable/constant-load)
   forwarding, not store→load communication.** On gcc: alias-only +0.66%
   (44.8 K aliases) vs full model +9.35% with 3.6 M last-value forwards —
   the self-bound fallback of [2] doing the heavy lifting. This is the same
   load population Constable [4] characterizes as likely-stable (34.2% of
   dynamic loads on average; lowest in SPEC-like suites — i.e., SPEC
   *undersells* the technique), and the old value path structurally could
   not reach it: its address-matched store training found no store for
   73.8% of loads on gcc, precisely the population self-binding captures.
3. **Accuracy beats coverage everywhere.** Geomean rises monotonically with
   the confidence threshold up to 14 (coverage drops only 10% from
   threshold 6 to 15 while wrong forwards drop 6×); 107/190 checkpoints
   individually prefer the two strictest thresholds. Validated accuracy is
   99.4–99.8% at every threshold; the correct/made accuracy at th14 is
   93.5% suite-wide, with the gap almost entirely squashed-before-validation
   (ambient, coverage-proportional), not mispredicts.
4. **The rendezvous eliminates the alias path's structural failure.** On the
   changing-recurrence microbenchmark (`mrnrec`) the legacy path managed
   **one** alias in 2 M iterations (staleness-rejected every iteration);
   the rendezvous captures one alias per iteration (2.0 M) with **zero**
   mispredicts, +125% IPC. On gcc, enforced aliases rose 26× (1,702 →
   44,831) at higher verify accuracy (95.1% vs 89.5%).
5. **The loser tail is small, clustered, and mechanistically bimodal.** Only
   10/190 checkpoints lose > 1% (worst −3.95%); 80% sit in four workloads
   (cppcheck, vpr, cpython, ntest); only cppcheck and ntest are net-negative
   as workloads. Two distinct failure modes: (a) *flush-dominated* — up to
   6.5% of instructions burned at 41–121 insts/mispredict (avg 68),
   addressable by store-write invalidation of self-bound cells (the
   Constable Condition-2 analog: our SC already observes every resolved
   store address); (b) *perturbation-dominated* — e.g. cppcheck.0.0 at
   99.6% accuracy and 0.3% flushed instructions still loses 1.75%: the
   *correct* forwards perturb issue scheduling while the load still
   executes. Mode (b) is the structural gap between value speculation and
   execution elimination that Constable [4] targets; no confidence
   threshold fixes it.
6. **No meaningful Int/FP bias, by construction and by measurement.**
   INT 1.0102 vs FP 1.0076 (empirical categorization by committed FP-op
   fraction, 10% cutoff). The model forwards only to integer-destination
   loads, so all FP-category benefit flows through integer address/index
   loads — and still nets +0.76%.
7. **Single-checkpoint results mislead in both directions.** gcc's +9.35%
   compresses 10× at suite scale; conversely nest — the legacy value path's
   worst loser (0.959) — flips to the best FP gainer (+2.7%) under
   writeback-trained self-binding. Sweep before believing.

## 5. Next Steps

1. **Store-write invalidation of self-bound cells** (Constable Condition-2
   analog): the SC already observes every resolved store address, so a store
   publish can proactively kill stale last-value bindings at that address —
   attacks the flush-dominated losers and the residual wrong forwards
   without the coverage tax of a higher threshold.
2. **Fold the legacy value path into the model and delete it**, along with
   the `lsq_forward` correlator: the last-value mode already subsumes the
   legacy path's role at strictly better suite-wide results; consolidation
   unblocks all further work on a single mechanism.
3. **Criticality-gated forwarding** for the perturbation-dominated losers
   (forward only when dependents actually stall), or accept that cohort as
   the structural speculation-vs-elimination gap that Constable [4] targets.
4. **FP-destination last-value forwarding** (guard relaxation + an FP
   register write path at rename): closes the by-construction Int-only
   limitation; ranked last because FP workloads already net +0.76% through
   their integer loads.

## References

[1] G. S. Tyson and T. M. Austin, "Improving the Accuracy and Performance of
Memory Communication Through Renaming," MICRO-30, 1997.
[2] G. Reinman, B. Calder, D. Tullsen, G. Tyson, and T. Austin, "Classifying
Load and Store Instructions for Memory Renaming," Int'l Conference on
Supercomputing (ICS), 1999.
[3] B. Calder and G. Reinman, "A Comparative Survey of Load Speculation
Architectures," J. Instruction-Level Parallelism (JILP), 2000.
[4] R. Bera, A. Ranganathan, J. Rakshit, S. Mahto, A. V. Nori, J. Gaur,
A. Olgun, K. Kanellopoulos, M. Sadrosadati, S. Subramoney, and O. Mutlu,
"Constable: Improving Performance and Power Efficiency by Safely Eliminating
Load Instruction Execution," ISCA, 2024.
