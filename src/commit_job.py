"""Entrypoint 1: commit tomorrow's schedule, day-ahead.

Runs daily after ERCOT publishes DAM prices (about 13:30 Central). Pulls
tomorrow's 24 hourly prices, solves the schedule, writes one JSON to
data/commits/, and exits. The git commit and push happen outside this script
(GitHub Actions workflow or a manual git commit), so the file write stays
separate from the public timestamp that proves it.

Timing rules, enforced in decide_action and unit-tested:
- The operating day is tomorrow on ERCOT's clock (Central Time), regardless
  of where or when the job runs. UTC alone would flip the date around
  midnight and silently target the wrong day.
- If a commit file for that day already exists, do nothing (idempotent).
- If the operating day has already begun in Central Time, SKIP forever.
  Committing mid-day would mean scheduling hours whose prices are known.
- If the job starts more than commit_late_window_hours after its scheduled
  run time, SKIP even if technically before midnight. A near-midnight commit
  is within the letter of the rules but a late-window skip keeps the record
  clean and the guard simple.
"""

import json
import logging
import sys
from pathlib import Path

import pandas as pd

from src.optimize import solve_schedule
from src.prices import get_dam_prices, load_config

logger = logging.getLogger("commit_job")

REPO_ROOT = Path(__file__).resolve().parent.parent
COMMITS_DIR = REPO_ROOT / "data" / "commits"

ACTION_COMMIT = "COMMIT"
ACTION_ALREADY_COMMITTED = "ALREADY_COMMITTED"
ACTION_SKIP_DAY_STARTED = "SKIP_DAY_STARTED"
ACTION_SKIP_TOO_LATE = "SKIP_TOO_LATE"


def operating_date_for(now_utc, tz):
    """Tomorrow on the ERCOT (Central Time) calendar."""
    return (now_utc.tz_convert(tz) + pd.Timedelta(days=1)).date()


def decide_action(now_utc, operating_date, commit_exists, config):
    """Pure decision logic so the timing rules are unit-testable."""
    tz = config["market"]["timezone"]
    jobs = config["jobs"]
    if commit_exists:
        return ACTION_ALREADY_COMMITTED

    day_start_ct = pd.Timestamp(operating_date).tz_localize(tz)
    if now_utc >= day_start_ct:
        return ACTION_SKIP_DAY_STARTED

    # scheduled run is the day before the operating day, at commit_run_utc
    run_date = pd.Timestamp(operating_date) - pd.Timedelta(days=1)
    hh, mm = map(int, jobs["commit_run_utc"].split(":"))
    scheduled = run_date.tz_localize("UTC") + pd.Timedelta(hours=hh, minutes=mm)
    if now_utc > scheduled + pd.Timedelta(hours=jobs["commit_late_window_hours"]):
        return ACTION_SKIP_TOO_LATE

    return ACTION_COMMIT


def build_commit_record(operating_date, dam, schedule, config, generated_at_utc):
    """Everything needed to audit the day later: prices used, the schedule,
    solver metadata, and the asset config it was solved under."""
    return {
        "operating_date": str(operating_date),
        "generated_at_utc": generated_at_utc.isoformat(),
        "hub": config["market"]["hub"],
        "schedule_source": schedule["source"],
        "solver_status": schedule["solver_status"],
        "expected_dam_pnl_usd": round(schedule["pnl_usd"], 2),
        "asset": config["asset"],
        "hours": [
            {
                "interval_start_utc": dam.interval_start_utc.iloc[t].isoformat(),
                "dam_price_usd_per_mwh": float(dam.spp_usd_per_mwh.iloc[t]),
                "charge_mw": round(float(schedule["charge_mw"][t]), 3),
                "discharge_mw": round(float(schedule["discharge_mw"][t]), 3),
                "soc_end_mwh": round(float(schedule["soc_mwh"][t]), 3),
            }
            for t in range(len(dam))
        ],
    }


def run(now_utc=None, config=None, commits_dir=COMMITS_DIR):
    cfg = config or load_config()
    now_utc = now_utc or pd.Timestamp.now(tz="UTC")
    op_date = operating_date_for(now_utc, cfg["market"]["timezone"])
    commit_path = Path(commits_dir) / f"{op_date}.json"

    action = decide_action(now_utc, op_date, commit_path.exists(), cfg)
    if action != ACTION_COMMIT:
        logger.warning("%s operating_date=%s now_utc=%s", action, op_date, now_utc.isoformat())
        return action, None

    dam = get_dam_prices(str(op_date), cfg)  # raises after retries; never fabricates
    schedule = solve_schedule(dam.spp_usd_per_mwh.values, cfg)
    record = build_commit_record(op_date, dam, schedule, cfg, now_utc)

    commit_path.parent.mkdir(parents=True, exist_ok=True)
    with open(commit_path, "w") as f:
        json.dump(record, f, indent=2, sort_keys=True)
    logger.warning(
        "COMMITTED operating_date=%s source=%s expected_dam_pnl=%.2f file=%s",
        op_date, schedule["source"], schedule["pnl_usd"], commit_path.name,
    )
    return action, commit_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    try:
        action, path = run()
    except Exception:
        logger.exception("FAILED commit job")
        sys.exit(1)
    # skips exit 0: they are correct behavior, not failures
    sys.exit(0)
