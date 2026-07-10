# Design Document

This document specifies the simulated asset, the daily operating loop, the optimization, the integrity rules, and the metrics. The [README](README.md) explains why the project exists; this explains how it works.

## 1. The simulated asset

A merchant battery in ERCOT, defined entirely in `config.yaml`:

- 100 MW maximum charge and discharge power
- 200 MWh energy capacity (2-hour duration, the typical ERCOT spec)
- 90 percent round-trip efficiency, applied on the charge leg: buying 1 MWh from the grid stores 0.9 MWh
- State of charge starts and ends every operating day at zero
- Maximum one full cycle per day (discharge throughput capped at 200 MWh)
- Throughput cost field in $/MWh for degradation, zero in v1
- Settlement location: ERCOT North Hub (HB_NORTH), configurable

None of these values are hardcoded in source.

## 2. The daily loop

**Commit job (day-ahead).** ERCOT publishes day-ahead market (DAM) hourly settlement point prices for the next operating day in the early afternoon Central Time. After publication, the job pulls tomorrow's 24 hourly DAM prices for the configured hub, solves the optimal charge and discharge schedule against them, and commits the schedule, the prices used, and the solver metadata to `data/commits/` as a dated JSON. The git commit timestamp is the public proof the schedule existed before the operating day.

**Settlement job (day after).** Once the operating day completes, the job computes the DAM P&L (the committed schedule settled at the DAM prices it was optimized against, which is deterministic and replicates what a self-scheduled day-ahead position earns), pulls the operating day's realized real-time prices, computes the perfect-foresight optimum (the same optimization solved against realized real-time prices, the theoretical maximum a clairvoyant operator could have earned), and appends one row to `data/ledger.csv`. The hypothetical real-time settlement of the committed schedule is recorded as a diagnostic column.

**Why this design is honest.** Optimizing against known DAM prices and settling at DAM is not forecasting; it replicates a real battery self-scheduling in the day-ahead market. The uncertainty, and therefore the track record, lives in the capture rate: how much of the clairvoyant real-time optimum a committed day-ahead strategy captures varies daily with how well DAM predicted real time. Raw dollars alone would flatter the strategy; capture against perfect foresight is the honest benchmark and the headline metric.

## 3. The optimization

A linear program (cvxpy, solved with HiGHS). Decision variables are charge and discharge megawatts for each hour, bounded by the power limit. The objective maximizes revenue: the sum over hours of price times net discharge, less any throughput cost. Constraints encode the physics:

- State of charge follows the hourly flows, gaining 0.9 MWh per MWh bought and losing 1 MWh per MWh sold
- State of charge stays within 0 to 200 MWh, so the battery cannot discharge energy it never stored
- State of charge starts and ends the day at zero, so no value hides in carried-over charge
- Total discharge is capped at 200 MWh, one full cycle

Round-trip efficiency creates a minimum profitable spread: at 90 percent, the sale price must exceed the purchase price by roughly 11 percent before a cycle earns anything.

A sort-based heuristic (match cheapest charge hours to priciest later discharge hours) serves as a fallback if the solver fails, and each day's record logs which path produced the schedule. Unit tests prove the constraints hold on synthetic curves: flat prices produce zero trading, a single price spike attracts a full charge-discharge pair, and sub-threshold spreads are left alone.

## 4. Integrity rules

Violating any of these destroys the project's value, so they are absolute:

1. Append-only ledgers. A committed schedule or settled row is never edited, rewritten, or deleted. Corrections are new rows flagged as corrections, with the original left intact.
2. No backfilling. If the commit job fails and the operating day passes without a committed schedule, that day is a SKIP row forever. A schedule is never generated for a day whose prices are already known.
3. The commit must precede the operating day. If a late run means the day has begun, the job logs a SKIP and does not commit.
4. Missing price data is never fabricated or interpolated. If a pull fails after retries, the run logs the failure and the day stays pending or skips.
5. All stored timestamps are UTC, with operating-day logic handled explicitly in Central Time. Both daylight saving transitions are unit-tested, since timezone bugs are the most likely source of silent corruption.

## 5. Repository architecture

```
virtual-battery/
  config.yaml            # asset specs, hub, solver, schedule times
  src/prices.py          # DAM and RT price pulls, retries, validation
  src/optimize.py        # LP + heuristic fallback
  src/commit_job.py      # daily schedule commit (entrypoint 1)
  src/settle_job.py      # daily settlement + benchmarks (entrypoint 2)
  data/commits/          # one JSON per operating day, never modified
  data/ledger.csv        # append-only settled results
  docs/                  # project context; later, the static dashboard
  .github/workflows/     # two scheduled jobs: commit and settlement
  tests/                 # LP constraints, timezone handling, ledger immutability
```

Engineering rules: the data library version is pinned and its schemas were verified against live pulls at build time; both jobs are idempotent, so re-running a completed day changes nothing; data pulls retry with backoff; every run logs whether it succeeded, skipped, or failed; scheduled runners are best-effort, so jobs tolerate late starts within a defined window and skip cleanly beyond it.

## 6. Ledger schema

One row per operating day: operating_date, commit_timestamp_utc, schedule_source (lp or heuristic), dam_pnl_usd, rt_settled_pnl_usd (diagnostic), pf_rt_optimum_usd, capture_rate, pnl_per_mw_day, top_bottom_spread_usd, cycles_used, status (SETTLED, SKIP, CORRECTION), notes.

## 7. Metrics

Headline metrics: cumulative DAM P&L, average capture rate, P&L per MW-day, and annualized $/kW-year, which is comparable to published battery revenue benchmarks and provides an external sanity check. Capture rate can exceed 1.0 on days when the day-ahead market offered a wider spread than real time delivered; settling at DAM collected a premium those days.

## 8. Capex economics (planned)

Once the ledger holds enough rows to annualize with a stated confidence interval, a capex module tests whether the observed arbitrage revenue would justify building the asset. Inputs (installed $/kWh, fixed and variable O&M, project life, capacity fade, augmentation, discount rate) live in config, get verified against current public sources at build time, and feed a simple project cash flow. Outputs lead with three numbers: breakeven revenue in $/kW-year for zero NPV, the implied daily spread required to produce it at the observed capture rate, and the gap between realized and breakeven revenue in plain dollars. Sensitivity analysis replaces any single-point IRR. If simulated arbitrage revenue falls short of breakeven, that finding is published as is.

## 9. Scope limits

Energy arbitrage only: no ancillary services, no capacity value, no bid-ask dynamics, no market impact from the asset's own dispatch, price-taking self-schedule rather than price-quantity bids, simple degradation treatment. Simulation only; the asset does not exist and no bids are submitted to any market.
