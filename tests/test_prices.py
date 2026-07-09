"""Validation and timezone tests for the price module. No network needed."""

import pandas as pd
import pytest

from src.prices import PriceDataError, _expected_hours, _validate_and_convert, rt_hourly_average

TZ = "US/Central"


def test_normal_day_has_24_hours():
    assert _expected_hours("2026-07-07", TZ) == 24


def test_spring_forward_day_has_23_hours():
    # 2026-03-08: clocks jump 2am -> 3am Central, the day is one hour short
    assert _expected_hours("2026-03-08", TZ) == 23


def test_fall_back_day_has_25_hours():
    # 2026-11-01: clocks repeat 1am-2am Central, the day is one hour long
    assert _expected_hours("2026-11-01", TZ) == 25


def _fake_dam(n=24, hub="HB_NORTH", nan_at=None):
    start = pd.Timestamp("2026-07-07", tz=TZ)
    idx = [start + pd.Timedelta(hours=i) for i in range(n)]
    spp = [20.0 + i for i in range(n)]
    if nan_at is not None:
        spp[nan_at] = float("nan")
    return pd.DataFrame(
        {
            "Interval Start": idx,
            "Interval End": [t + pd.Timedelta(hours=1) for t in idx],
            "Location": hub,
            "Market": "DAY_AHEAD_HOURLY",
            "SPP": spp,
        }
    )


def test_validate_accepts_complete_day_and_converts_to_utc():
    out = _validate_and_convert(_fake_dam(), "2026-07-07", 24, "HB_NORTH", "DAM")
    assert len(out) == 24
    assert str(out["interval_start_utc"].dt.tz) == "UTC"
    # midnight Central on 2026-07-07 (CDT, UTC-5) is 05:00 UTC
    assert out["interval_start_utc"].iloc[0] == pd.Timestamp("2026-07-07 05:00:00", tz="UTC")


def test_validate_rejects_missing_hours():
    with pytest.raises(PriceDataError):
        _validate_and_convert(_fake_dam(n=23), "2026-07-07", 24, "HB_NORTH", "DAM")


def test_validate_rejects_nan_prices():
    with pytest.raises(PriceDataError):
        _validate_and_convert(_fake_dam(nan_at=5), "2026-07-07", 24, "HB_NORTH", "DAM")


def test_validate_rejects_wrong_hub():
    with pytest.raises(PriceDataError):
        _validate_and_convert(_fake_dam(hub="HB_WEST"), "2026-07-07", 24, "HB_NORTH", "DAM")


def test_rt_hourly_average_means_four_intervals():
    start = pd.Timestamp("2026-07-07 05:00:00", tz="UTC")
    idx = [start + pd.Timedelta(minutes=15 * i) for i in range(8)]  # two hours
    df = pd.DataFrame(
        {
            "interval_start_utc": idx,
            "spp_usd_per_mwh": [10.0, 20.0, 30.0, 40.0, 100.0, 100.0, 100.0, 100.0],
        }
    )
    hourly = rt_hourly_average(df)
    assert len(hourly) == 2
    assert hourly["spp_usd_per_mwh"].tolist() == [25.0, 100.0]
