"""Entrypoint 2: settle completed operating days and append to the ledger.

Runs daily after the operating day fully closes on ERCOT's clock. For each
pending day from ledger_start_date through yesterday (Central Time):

- With a commit file: recompute DAM P&L (the committed schedule settled at
  the DAM prices it was optimized against, deterministic by construction),
  pull realized real-time prices, settle the same schedule at them as a
  diagnostic, solve the same LP against them for the perfect-foresight
  optimum, and compute capture rate = DAM P&L / perfect-foresight optimum.
- Without a commit file: the day is a SKIP row, forever. No backfilling.
- If the real-time pull fails, the day stays pending and tomorrow's run
  retries it. A late settlement is harmless; a fabricated one is not.

The ledger is append-only. This job never reads-modifies-writes existing
rows; it opens the file in append mode and adds new ones. Corrections, if
ever needed, are new rows flagged CORRECTION with the original left intact.

Why capture rate is the honest headline: raw dollars scale with how volatile
the week happened to be, which the strategy does not control. Capture rate
divides by what a clairvoyant operator could have earned from the same asset
on the same day, so it isolates the quality of the day-ahead commitment. It
can exceed 1.0 on days when day-ahead prices offered a wider spread than
real time delivered; settling at DAM collected a premium those days.
"""

import csv
import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from src.optimize import solve_schedule
from src.prices import PriceDataError, get_rt_prices, load_config, rt_hourly_average

logger = logging.getLogger("settle_job")

REPO_ROOT = Path(__file__).resolve().parent.parent
COMMITS_DIR = REPO_ROOT / "data" / "commits"
LEDGER_PATH = REPO_ROOT / "data" / "ledger.csv"

LEDGER_COLUMNS = [
    "operating_date",
    "commit_timestamp_utc",
    "schedule_source",
    "dam_pnl_usd",
    "rt_settled_pnl_usd",
    "pf_rt_optimum_usd",
    "capture_rate",
    "pnl_per_mw_day",
    "top_bottom_spread_usd",
    "cycles_used",
    "status",
    "notes",
]


def pending_days(now_utc, config, commits_dir, ledger_path):
    """Operating days from ledger_start_date through yesterday (Central Time)
    that have no ledger row yet, oldest first."""
    tz = config["market"]["timezone"]
    start = pd.Timestamp(config["jobs"]["ledger_start_date"]).date()
    today_ct = now_utc.tz_convert(tz).date()
    if today_ct <= start:
        return []
    done = set()
    if Path(ledger_path).exists():
        with open(ledger_path) as f:
            done = {row["operating_date"] for row in csv.DictReader(f)}
    days = pd.date_range(start, today_ct - pd.Timedelta(days=1), freq="D").date
    return [d for d in days if str(d) not in done]


def settle_day(record, rt_hourly, config):
    """One SETTLED ledger row from a commit record and realized RT prices."""
    asset = record["asset"]
    hours = record["hours"]
    dam_prices = np.array([h["dam_price_usd_per_mwh"] for h in hours])
    charge = np.array([h["charge_mw"] for h in hours])
    discharge = np.array([h["discharge_mw"] for h in hours])
    throughput_cost = asset.get("throughput_cost_usd_per_mwh", 0)

    # the committed hours and the realized RT hours must line up exactly
    commit_starts = [pd.Timestamp(h["interval_start_utc"]) for h in hours]
    rt_starts = list(rt_hourly["interval_start_utc"])
    if commit_starts != rt_starts:
        raise PriceDataError(
            f"hour misalignment for {record['operating_date']}: "
            f"{len(commit_starts)} committed vs {len(rt_starts)} realized"
        )
    rt_prices = rt_hourly["spp_usd_per_mwh"].values

    net = discharge - charge
    dam_pnl = float(dam_prices @ net - throughput_cost * discharge.sum())
    rt_settled = float(rt_prices @ net - throughput_cost * discharge.sum())
    pf = solve_schedule(rt_prices, config)
    pf_pnl = pf["pnl_usd"]

    if pf_pnl > 1e-6:
        capture = round(dam_pnl / pf_pnl, 4)
        note = ""
    else:
        capture = ""  # undefined when perfect foresight earns nothing
        note = "capture_rate undefined: perfect-foresight optimum is zero"

    return {
        "operating_date": record["operating_date"],
        "commit_timestamp_utc": record["generated_at_utc"],
        "schedule_source": record["schedule_source"],
        "dam_pnl_usd": round(dam_pnl, 2),
        "rt_settled_pnl_usd": round(rt_settled, 2),
        "pf_rt_optimum_usd": round(pf_pnl, 2),
        "capture_rate": capture,
        "pnl_per_mw_day": round(dam_pnl / asset["power_mw"], 2),
        "top_bottom_spread_usd": round(float(dam_prices.max() - dam_prices.min()), 2),
        "cycles_used": round(float(discharge.sum() / asset["energy_mwh"]), 4),
        "status": "SETTLED",
        "notes": note,
    }


def skip_row(operating_date, reason):
    return {
        "operating_date": str(operating_date),
        "commit_timestamp_utc": "",
        "schedule_source": "",
        "dam_pnl_usd": "",
        "rt_settled_pnl_usd": "",
        "pf_rt_optimum_usd": "",
        "capture_rate": "",
        "pnl_per_mw_day": "",
        "top_bottom_spread_usd": "",
        "cycles_used": "",
        "status": "SKIP",
        "notes": reason,
    }


def append_rows(ledger_path, rows):
    """Append-only by construction: existing bytes are never rewritten."""
    if not rows:
        return
    path = Path(ledger_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=LEDGER_COLUMNS)
        if is_new:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)


def run(now_utc=None, config=None, commits_dir=COMMITS_DIR, ledger_path=LEDGER_PATH):
    cfg = config or load_config()
    now_utc = now_utc or pd.Timestamp.now(tz="UTC")
    rows = []
    for day in pending_days(now_utc, cfg, commits_dir, ledger_path):
        commit_path = Path(commits_dir) / f"{day}.json"
        if not commit_path.exists():
            logger.warning("SKIP operating_date=%s reason=no committed schedule", day)
            rows.append(skip_row(day, "no committed schedule; day passed without a commit"))
            continue
        try:
            rt = get_rt_prices(str(day), cfg)
        except PriceDataError as e:
            logger.warning("PENDING operating_date=%s rt pull failed, will retry: %s", day, e)
            continue  # stays pending; never fabricate
        record = json.load(open(commit_path))
        row = settle_day(record, rt_hourly_average(rt), cfg)
        logger.warning(
            "SETTLED operating_date=%s dam_pnl=%.2f pf_optimum=%.2f capture=%s",
            day, row["dam_pnl_usd"], row["pf_rt_optimum_usd"], row["capture_rate"],
        )
        rows.append(row)
    append_rows(ledger_path, rows)
    return rows


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    try:
        run()
    except Exception:
        logger.exception("FAILED settle job")
        sys.exit(1)
    sys.exit(0)
