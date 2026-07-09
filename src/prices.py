"""ERCOT settlement point price pulls via gridstatus, with retries and validation.

Market background:
ERCOT runs two markets that produce two different prices for the same hour.
The day-ahead market (DAM) clears once per day around 13:30 Central and sets
one price per hour for tomorrow. The real-time market settles what actually
happens, in 15-minute intervals, and can swing far above or below DAM when
weather or outages surprise the grid. This project commits a schedule against
DAM prices before the day starts; the gap between DAM and real-time is where
all the uncertainty lives.

A settlement point hub (like HB_NORTH) is a published average across many
individual grid nodes in a region. Hubs are the standard trading benchmark
because single-node prices carry local congestion noise.

Integrity rules enforced here: never fabricate or interpolate a price. If a
pull fails after retries or returns an incomplete day, raise; callers log a
SKIP. All returned timestamps are UTC.
"""

import logging
import time
from pathlib import Path

import pandas as pd
import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


def load_config(path=CONFIG_PATH):
    with open(path) as f:
        return yaml.safe_load(f)


class PriceDataError(Exception):
    """Raised when price data is missing or fails validation. Never worked around."""


def _expected_hours(operating_date, tz):
    """Hours in the operating day in local time: 24 normally, 23 on the
    spring-forward day, 25 on the fall-back day. DST is the top source of
    silent bugs in power data; counting hours explicitly catches it."""
    # Localize both calendar midnights, then measure absolute time between them.
    # (Adding Timedelta(days=1) to an aware timestamp adds 24 absolute hours and
    # would hide DST days; the naive-then-localize order is deliberate.)
    d = pd.Timestamp(operating_date)
    start = d.tz_localize(tz)
    end = (d + pd.Timedelta(days=1)).tz_localize(tz)
    return int((end - start) / pd.Timedelta(hours=1))


def _pull_with_retries(pull, retries, backoff_seconds, what):
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            return pull()
        except Exception as e:  # network, ERCOT MIS hiccups, parse failures
            last_err = e
            logger.warning("%s pull attempt %d/%d failed: %s", what, attempt, retries, e)
            if attempt < retries:
                time.sleep(backoff_seconds * attempt)
    raise PriceDataError(f"{what} pull failed after {retries} attempts: {last_err}")


def _validate_and_convert(df, operating_date, expected_rows, hub, what):
    if df is None or len(df) == 0:
        raise PriceDataError(f"{what}: empty result for {operating_date}")
    if set(df["Location"].unique()) != {hub}:
        raise PriceDataError(f"{what}: expected only {hub}, got {df['Location'].unique()}")
    if len(df) != expected_rows:
        raise PriceDataError(f"{what}: expected {expected_rows} rows for {operating_date}, got {len(df)}")
    if df["SPP"].isna().any():
        raise PriceDataError(f"{what}: NaN prices for {operating_date}")
    out = df[["Interval Start", "Interval End", "Location", "Market", "SPP"]].copy()
    out.columns = ["interval_start_utc", "interval_end_utc", "location", "market", "spp_usd_per_mwh"]
    out["interval_start_utc"] = out["interval_start_utc"].dt.tz_convert("UTC")
    out["interval_end_utc"] = out["interval_end_utc"].dt.tz_convert("UTC")
    return out.sort_values("interval_start_utc").reset_index(drop=True)


def get_dam_prices(operating_date, config=None):
    """24 hourly DAM prices (23/25 on DST days) for the configured hub, UTC timestamps."""
    cfg = config or load_config()
    m = cfg["market"]
    import gridstatus  # imported here so tests of validation logic need no network stack

    iso = gridstatus.Ercot()
    df = _pull_with_retries(
        lambda: iso.get_spp(
            date=operating_date,
            market=m["dam_market"],
            locations=[m["hub"]],
            location_type=m["location_type"],
        ),
        cfg["jobs"]["retries"],
        cfg["jobs"]["retry_backoff_seconds"],
        f"DAM {operating_date}",
    )
    hours = _expected_hours(operating_date, m["timezone"])
    return _validate_and_convert(df, operating_date, hours, m["hub"], "DAM")


def get_rt_prices(operating_date, config=None):
    """15-minute real-time prices (96 normally, 92/100 on DST days), UTC timestamps."""
    cfg = config or load_config()
    m = cfg["market"]
    import gridstatus

    iso = gridstatus.Ercot()
    df = _pull_with_retries(
        lambda: iso.get_spp(
            date=operating_date,
            market=m["rt_market"],
            locations=[m["hub"]],
            location_type=m["location_type"],
        ),
        cfg["jobs"]["retries"],
        cfg["jobs"]["retry_backoff_seconds"],
        f"RT {operating_date}",
    )
    intervals = _expected_hours(operating_date, m["timezone"]) * 4
    return _validate_and_convert(df, operating_date, intervals, m["hub"], "RT")


def rt_hourly_average(rt_df):
    """Average the four 15-minute prices in each hour.

    Used for the perfect-foresight benchmark so it is directly comparable to
    the hourly DAM schedule. A battery holding constant output for an hour
    earns the average of that hour's four interval prices, so this loses no
    money accuracy for hourly-block dispatch.
    """
    hourly = (
        rt_df.set_index("interval_start_utc")["spp_usd_per_mwh"]
        .resample("1h")
        .mean()
        .reset_index()
    )
    if hourly["spp_usd_per_mwh"].isna().any():
        raise PriceDataError("RT hourly aggregation produced NaN, refusing to fill")
    return hourly


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(description="Print a sample day of DAM and RT prices")
    parser.add_argument("--date", required=True, help="operating date, YYYY-MM-DD")
    args = parser.parse_args()

    dam = get_dam_prices(args.date)
    rt = get_rt_prices(args.date)
    rt_h = rt_hourly_average(rt)
    print(f"\nDAM hourly prices for {args.date} ({len(dam)} rows):")
    print(dam.to_string(index=False))
    print(f"\nRT 15-min prices: {len(rt)} rows, "
          f"min {rt.spp_usd_per_mwh.min():.2f}, max {rt.spp_usd_per_mwh.max():.2f}")
    print(f"\nRT hourly averages ({len(rt_h)} rows):")
    print(rt_h.to_string(index=False))
    spread = dam.spp_usd_per_mwh.max() - dam.spp_usd_per_mwh.min()
    print(f"\nDAM top-bottom spread: ${spread:.2f}/MWh")
