"""Timing-rule tests for the commit job. These encode integrity rules 2 and 3."""

import json

import pandas as pd
import pytest

from src.commit_job import (
    ACTION_ALREADY_COMMITTED,
    ACTION_COMMIT,
    ACTION_SKIP_DAY_STARTED,
    ACTION_SKIP_TOO_LATE,
    build_commit_record,
    decide_action,
    operating_date_for,
)

CONFIG = {
    "market": {"timezone": "US/Central", "hub": "HB_NORTH"},
    "jobs": {"commit_run_utc": "21:00", "commit_late_window_hours": 5},
    "asset": {"power_mw": 100},
}


def ts(s):
    return pd.Timestamp(s, tz="UTC")


def test_operating_date_is_tomorrow_in_central_not_utc():
    # 02:00 UTC on July 9 is still 21:00 CT on July 8, so tomorrow is July 9
    assert str(operating_date_for(ts("2026-07-09 02:00"), "US/Central")) == "2026-07-09"
    # by 21:00 UTC on July 9 (16:00 CT), tomorrow is July 10
    assert str(operating_date_for(ts("2026-07-09 21:00"), "US/Central")) == "2026-07-10"


def test_on_time_run_commits():
    # scheduled run: 21:00 UTC July 9 for operating day July 10
    assert decide_action(ts("2026-07-09 21:05"), "2026-07-10", False, CONFIG) == ACTION_COMMIT


def test_existing_commit_file_short_circuits():
    assert decide_action(ts("2026-07-09 21:05"), "2026-07-10", True, CONFIG) == ACTION_ALREADY_COMMITTED


def test_late_start_within_window_commits():
    # 4 hours late is inside the 5-hour window
    assert decide_action(ts("2026-07-10 01:00"), "2026-07-10", False, CONFIG) == ACTION_COMMIT


def test_late_start_beyond_window_skips():
    # 02:01 UTC July 10 is 5h01m past the scheduled run, still 21:01 CT July 9,
    # so the operating day has not begun, but the window has closed
    assert decide_action(ts("2026-07-10 02:01"), "2026-07-10", False, CONFIG) == ACTION_SKIP_TOO_LATE


def test_run_after_operating_day_begins_skips():
    # 05:00 UTC July 10 is midnight CT July 10: the day has started
    assert decide_action(ts("2026-07-10 05:00"), "2026-07-10", False, CONFIG) == ACTION_SKIP_DAY_STARTED
    assert decide_action(ts("2026-07-10 12:00"), "2026-07-10", False, CONFIG) == ACTION_SKIP_DAY_STARTED


def test_day_started_outranks_late_window_message():
    # both guards fire this far in; the day-started one must win because it is
    # the integrity rule, not just the engineering rule
    assert decide_action(ts("2026-07-11 04:00"), "2026-07-10", False, CONFIG) == ACTION_SKIP_DAY_STARTED


def test_commit_record_is_json_serializable_and_complete():
    idx = pd.date_range("2026-07-10 05:00", periods=24, freq="1h", tz="UTC")
    dam = pd.DataFrame({"interval_start_utc": idx, "spp_usd_per_mwh": [30.0] * 24})
    schedule = {
        "charge_mw": [0.0] * 24,
        "discharge_mw": [0.0] * 24,
        "soc_mwh": [0.0] * 24,
        "pnl_usd": 0.0,
        "source": "lp",
        "solver_status": "optimal",
    }
    record = build_commit_record("2026-07-10", dam, schedule, CONFIG, ts("2026-07-09 21:05"))
    encoded = json.dumps(record, sort_keys=True)
    decoded = json.loads(encoded)
    assert decoded["operating_date"] == "2026-07-10"
    assert len(decoded["hours"]) == 24
    assert decoded["hours"][0]["dam_price_usd_per_mwh"] == 30.0
