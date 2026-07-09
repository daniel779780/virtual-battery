"""Constraint tests for the LP and the heuristic fallback, on synthetic curves.

These tests are the proof that the optimizer respects the battery's physics.
Every constraint from the briefing gets a direct test.
"""

import numpy as np
import pytest

from src.optimize import solve_heuristic, solve_lp, solve_schedule

ASSET = {
    "power_mw": 100,
    "energy_mwh": 200,
    "round_trip_efficiency": 0.90,
    "soc_start_mwh": 0,
    "soc_end_mwh": 0,
    "max_cycles_per_day": 1,
    "throughput_cost_usd_per_mwh": 0,
}
CONFIG = {"asset": ASSET, "solver": {"name": "HIGHS"}}
TOL = 1e-6


def assert_feasible(res, n):
    c, d, soc = res["charge_mw"], res["discharge_mw"], res["soc_mwh"]
    assert len(c) == len(d) == len(soc) == n
    assert (c >= -TOL).all() and (c <= ASSET["power_mw"] + TOL).all()
    assert (d >= -TOL).all() and (d <= ASSET["power_mw"] + TOL).all()
    assert (soc >= -TOL).all() and (soc <= ASSET["energy_mwh"] + TOL).all()
    assert abs(soc[-1] - ASSET["soc_end_mwh"]) < 1e-4
    assert d.sum() <= ASSET["max_cycles_per_day"] * ASSET["energy_mwh"] + 1e-4


@pytest.mark.parametrize("solver", [solve_lp, solve_heuristic])
def test_flat_prices_yield_zero_trading(solver):
    prices = np.full(24, 30.0)
    res = solver(prices, ASSET) if solver is solve_heuristic else solver(prices, ASSET)
    assert_feasible(res, 24)
    # with no spread, every cycle loses the efficiency toll, so do nothing
    assert res["pnl_usd"] == pytest.approx(0, abs=1e-4)
    assert res["discharge_mw"].sum() == pytest.approx(0, abs=1e-4)


def test_single_spike_charges_before_and_discharges_into_it():
    prices = np.full(24, 20.0)
    prices[18] = 1000.0
    res = solve_lp(prices, ASSET)
    assert_feasible(res, 24)
    # discharge the full 100 MW into the spike hour and nowhere else
    assert res["discharge_mw"][18] == pytest.approx(100, abs=1e-3)
    assert res["discharge_mw"].sum() == pytest.approx(100, abs=1e-3)
    # to deliver 100 MWh at 90 percent efficiency, buy 100/0.9 = 111.1 MWh, all before the spike
    assert res["charge_mw"].sum() == pytest.approx(100 / 0.9, abs=1e-3)
    assert res["charge_mw"][19:].sum() == pytest.approx(0, abs=1e-4)
    expected_pnl = 1000 * 100 - 20 * (100 / 0.9)
    assert res["pnl_usd"] == pytest.approx(expected_pnl, rel=1e-6)


def test_two_spike_hours_use_the_full_cycle_and_no_more():
    prices = np.full(24, 20.0)
    prices[18] = prices[19] = 500.0
    res = solve_lp(prices, ASSET)
    assert_feasible(res, 24)
    # 2-hour battery, one cycle: exactly 200 MWh discharged across the two spike hours
    assert res["discharge_mw"].sum() == pytest.approx(200, abs=1e-3)
    assert res["discharge_mw"][18] == pytest.approx(100, abs=1e-3)
    assert res["discharge_mw"][19] == pytest.approx(100, abs=1e-3)


def test_spread_below_efficiency_threshold_is_left_alone():
    # buy at 100, sell at 105: 0.9 * 105 = 94.5 < 100, a losing trade
    prices = np.full(24, 100.0)
    prices[20] = 105.0
    res = solve_lp(prices, ASSET)
    assert res["pnl_usd"] == pytest.approx(0, abs=1e-4)


def test_spread_above_efficiency_threshold_is_traded():
    # buy at 100, sell at 120: 0.9 * 120 = 108 > 100, profitable
    prices = np.full(24, 100.0)
    prices[20] = 120.0
    res = solve_lp(prices, ASSET)
    assert res["pnl_usd"] > 0
    assert res["discharge_mw"][20] == pytest.approx(100, abs=1e-3)


def test_discharge_never_precedes_stored_energy():
    # spike in hour 0: nothing is stored yet, so the battery must sit out
    prices = np.full(24, 20.0)
    prices[0] = 1000.0
    res = solve_lp(prices, ASSET)
    assert res["discharge_mw"][0] == pytest.approx(0, abs=1e-4)


@pytest.mark.parametrize("seed", range(5))
def test_constraints_hold_on_random_curves(seed):
    rng = np.random.default_rng(seed)
    prices = rng.uniform(5, 150, size=24)
    lp = solve_lp(prices, ASSET)
    h = solve_heuristic(prices, ASSET)
    assert_feasible(lp, 24)
    assert_feasible(h, 24)
    # the heuristic is feasible, so the LP optimum must be at least as good
    assert lp["pnl_usd"] >= h["pnl_usd"] - 1e-6
    assert lp["pnl_usd"] >= 0


@pytest.mark.parametrize("n", [23, 25])
def test_dst_day_lengths_are_handled(n):
    rng = np.random.default_rng(0)
    prices = rng.uniform(5, 150, size=n)
    res = solve_lp(prices, ASSET)
    assert_feasible(res, n)


def test_solve_schedule_reports_lp_source():
    prices = np.full(24, 30.0)
    res = solve_schedule(prices, CONFIG)
    assert res["source"] == "lp"


def test_solve_schedule_falls_back_to_heuristic():
    prices = np.full(24, 30.0)
    prices[18] = 90.0
    bad_config = {"asset": ASSET, "solver": {"name": "NO_SUCH_SOLVER"}}
    res = solve_schedule(prices, bad_config)
    assert res["source"] == "heuristic"
    assert res["pnl_usd"] > 0
    assert_feasible(res, 24)


def test_throughput_cost_discourages_marginal_cycles():
    # spread of 20 on a 100 charge price clears the efficiency bar barely;
    # a big throughput cost should kill the trade
    prices = np.full(24, 100.0)
    prices[20] = 120.0
    costly = dict(ASSET, throughput_cost_usd_per_mwh=50)
    res = solve_lp(prices, costly)
    assert res["discharge_mw"].sum() == pytest.approx(0, abs=1e-4)
