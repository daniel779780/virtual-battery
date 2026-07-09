"""Settlement math, pending-day logic, and ledger immutability tests."""

import csv
import json

import numpy as np
import pandas as pd
import pytest

from src.settle_job import (
    LEDGER_COLUMNS,
    append_rows,
    pending_days,
    settle_day,
    skip_row,
)

CONFIG = {
    "market": {"timezone": "US/Central", "hub": "HB_NORTH"},
    "jobs": {"ledger_start_date": "2026-07-10"},
    "asset": {
        "power_mw": 100,
        "energy_mwh": 200,
        "round_trip_efficiency": 0.90,
        "soc_start_mwh": 0,
        "soc_end_mwh": 0,
        "max_cycles_per_day": 1,
        "throughput_cost_usd_per_mwh": 0,
    },
    "solver": {"name": "HIGHS"},
}


def make_record(dam_prices, charge, discharge):
    starts = pd.date_range("2026-07-10 05:00", periods=len(dam_prices), freq="1h", tz="UTC")
    return {
        "operating_date": "2026-07-10",
        "generated_at_utc": "2026-07-09T21:05:00+00:00",
        "schedule_source": "lp",
        "asset": CONFIG["asset"],
        "hours": [
            {
                "interval_start_utc": s.isoformat(),
                "dam_price_usd_per_mwh": float(p),
                "charge_mw": float(c),
                "discharge_mw": float(d),
            }
            for s, p, c, d in zip(starts, dam_prices, charge, discharge)
        ],
    }


def make_rt_hourly(prices):
    starts = pd.date_range("2026-07-10 05:00", periods=len(prices), freq="1h", tz="UTC")
    return pd.DataFrame({"interval_start_utc": starts, "spp_usd_per_mwh": prices})


def test_settlement_math_recomputable_by_hand():
    # buy 100 MWh at $20 in hour 1, sell 90 MWh at $50 in hour 3
    dam = [30.0, 20.0, 30.0, 50.0] + [30.0] * 20
    charge = [0.0, 100.0] + [0.0] * 22
    discharge = [0.0, 0.0, 0.0, 90.0] + [0.0] * 20
    rt = [30.0, 25.0, 30.0, 40.0] + [30.0] * 20

    row = settle_day(make_record(dam, charge, discharge), make_rt_hourly(rt), CONFIG)
    # DAM P&L: 90 * 50 - 100 * 20 = 2500
    assert row["dam_pnl_usd"] == pytest.approx(2500.0)
    # RT diagnostic: 90 * 40 - 100 * 25 = 1100
    assert row["rt_settled_pnl_usd"] == pytest.approx(1100.0)
    assert row["pf_rt_optimum_usd"] > 0
    assert row["capture_rate"] == pytest.approx(2500.0 / row["pf_rt_optimum_usd"], abs=1e-3)
    assert row["pnl_per_mw_day"] == pytest.approx(25.0)
    assert row["top_bottom_spread_usd"] == pytest.approx(30.0)
    assert row["cycles_used"] == pytest.approx(90 / 200)
    assert row["status"] == "SETTLED"


def test_flat_rt_prices_leave_capture_rate_undefined():
    dam = [20.0] * 24
    row = settle_day(
        make_record(dam, [0.0] * 24, [0.0] * 24),
        make_rt_hourly([30.0] * 24),
        CONFIG,
    )
    assert row["capture_rate"] == ""
    assert "undefined" in row["notes"]


def test_misaligned_hours_refuse_to_settle():
    from src.prices import PriceDataError

    record = make_record([20.0] * 24, [0.0] * 24, [0.0] * 24)
    with pytest.raises(PriceDataError):
        settle_day(record, make_rt_hourly([30.0] * 23), CONFIG)


def test_pending_days_walks_from_start_to_yesterday(tmp_path):
    now = pd.Timestamp("2026-07-13 09:00", tz="UTC")  # July 13 04:00 CT
    days = pending_days(now, CONFIG, tmp_path, tmp_path / "ledger.csv")
    assert [str(d) for d in days] == ["2026-07-10", "2026-07-11", "2026-07-12"]


def test_pending_days_empty_before_launch(tmp_path):
    now = pd.Timestamp("2026-07-10 09:00", tz="UTC")
    assert pending_days(now, CONFIG, tmp_path, tmp_path / "ledger.csv") == []


def test_pending_days_excludes_settled(tmp_path):
    ledger = tmp_path / "ledger.csv"
    append_rows(ledger, [skip_row("2026-07-10", "test")])
    now = pd.Timestamp("2026-07-12 09:00", tz="UTC")
    days = pending_days(now, CONFIG, tmp_path, ledger)
    assert [str(d) for d in days] == ["2026-07-11"]


def test_ledger_append_never_rewrites_existing_bytes(tmp_path):
    ledger = tmp_path / "ledger.csv"
    append_rows(ledger, [skip_row("2026-07-10", "first")])
    before = ledger.read_bytes()

    append_rows(ledger, [skip_row("2026-07-11", "second")])
    after = ledger.read_bytes()
    assert after.startswith(before), "existing ledger bytes were modified"

    with open(ledger) as f:
        rows = list(csv.DictReader(f))
    assert [r["operating_date"] for r in rows] == ["2026-07-10", "2026-07-11"]
    assert list(rows[0].keys()) == LEDGER_COLUMNS


def test_rerun_is_idempotent(tmp_path):
    ledger = tmp_path / "ledger.csv"
    append_rows(ledger, [skip_row("2026-07-10", "only day")])
    now = pd.Timestamp("2026-07-11 09:00", tz="UTC")
    assert pending_days(now, CONFIG, tmp_path, ledger) == []
    append_rows(ledger, [])  # settling nothing changes nothing
    with open(ledger) as f:
        assert len(list(csv.DictReader(f))) == 1
