"""Optimal daily battery schedule against a known hourly price curve.

How the LP encodes the physics:
- charge[t] and discharge[t] are MW held constant for hour t, so they double
  as MWh for that hour. Power limits cap them at the inverter rating.
- State of charge (SOC) is the battery's fuel gauge in MWh. Each hour it
  rises by eta * charge[t] and falls by discharge[t]. Bounding SOC in
  [0, energy_mwh] says the battery can be neither below empty nor above full,
  and it forbids discharging energy that was never stored.
- Efficiency sits on the charge leg: buying 1 MWh from the grid stores only
  eta MWh (heat, inverter, and pump losses). This is why a 90 percent RTE
  battery needs the sell price to exceed the buy price by at least 1/0.9,
  about 11 percent, before a cycle earns anything.
- SOC starts and ends the day at zero, so every day stands alone and no value
  hides in a carried-over charge.
- Total discharge is capped at max_cycles * energy_mwh (one full cycle by
  default), a simple stand-in for cell wear.

The objective maximizes market revenue: each hour earns price * discharge and
pays price * charge, minus an optional throughput cost per discharged MWh
(zero in v1).

The LP is solved by HiGHS through cvxpy. If the solver fails, a sort-based
heuristic produces a feasible (not necessarily optimal) schedule, and the
schedule records which path produced it.
"""

import logging

import numpy as np

logger = logging.getLogger(__name__)


class InfeasibleScheduleError(Exception):
    """Raised when neither the LP nor the heuristic produced a valid schedule."""


def solve_lp(prices, asset, solver_name="HIGHS"):
    """Solve the daily schedule LP. prices is a 1-D array, one price per hour
    of the operating day (24 normally, 23/25 on DST days)."""
    import cvxpy as cp

    prices = np.asarray(prices, dtype=float)
    n = len(prices)
    p_max = asset["power_mw"]
    e_max = asset["energy_mwh"]
    eta = asset["round_trip_efficiency"]
    throughput_cap = asset["max_cycles_per_day"] * e_max
    throughput_cost = asset.get("throughput_cost_usd_per_mwh", 0)

    charge = cp.Variable(n, nonneg=True)
    discharge = cp.Variable(n, nonneg=True)
    # soc[t] is the state of charge at the END of hour t; soc[-1] is the start
    soc = cp.cumsum(eta * charge - discharge)

    # SOC path: start + cumulative flows stays within [0, e_max], ends at soc_end
    soc_path = asset["soc_start_mwh"] + soc
    constraints = [
        charge <= p_max,
        discharge <= p_max,
        soc_path >= 0,
        soc_path <= e_max,
        soc_path[n - 1] == asset["soc_end_mwh"],
        cp.sum(discharge) <= throughput_cap,
    ]

    revenue = prices @ (discharge - charge) - throughput_cost * cp.sum(discharge)
    problem = cp.Problem(cp.Maximize(revenue), constraints)
    problem.solve(solver=solver_name)

    if problem.status not in ("optimal", "optimal_inaccurate"):
        raise InfeasibleScheduleError(f"LP status: {problem.status}")

    c = np.clip(charge.value, 0, p_max)
    d = np.clip(discharge.value, 0, p_max)
    # snap solver noise (1e-9 MW artifacts) to zero
    c[c < 1e-6] = 0.0
    d[d < 1e-6] = 0.0
    return {
        "charge_mw": c,
        "discharge_mw": d,
        "soc_mwh": asset["soc_start_mwh"] + np.cumsum(eta * c - d),
        "pnl_usd": float(prices @ (d - c) - throughput_cost * d.sum()),
        "source": "lp",
        "solver_status": problem.status,
    }


def solve_heuristic(prices, asset):
    """Sort-based fallback: pair cheap charge hours with pricy later discharge
    hours, greedily, most profitable pair first. Feasible by construction
    (every allocation is checked against a full SOC simulation) but not
    guaranteed optimal. Used only when the LP solver fails."""
    prices = np.asarray(prices, dtype=float)
    n = len(prices)
    p_max = asset["power_mw"]
    e_max = asset["energy_mwh"]
    eta = asset["round_trip_efficiency"]
    throughput_cost = asset.get("throughput_cost_usd_per_mwh", 0)
    throughput_left = asset["max_cycles_per_day"] * e_max

    charge = np.zeros(n)
    discharge = np.zeros(n)

    def soc_ok(c, d):
        soc = asset["soc_start_mwh"] + np.cumsum(eta * c - d)
        return soc.min() >= -1e-9 and soc.max() <= e_max + 1e-9 and abs(soc[-1] - asset["soc_end_mwh"]) < 1e-9

    # profit per bought MWh of moving energy from hour i to hour j > i
    pairs = [
        (eta * (prices[j] - throughput_cost) - prices[i], i, j)
        for i in range(n)
        for j in range(i + 1, n)
        if eta * (prices[j] - throughput_cost) - prices[i] > 1e-9
    ]
    pairs.sort(reverse=True)

    for _, i, j in pairs:
        buy_room = p_max - charge[i]
        sell_room = (p_max - discharge[j]) / eta
        cap_room = throughput_left / eta
        qty = min(buy_room, sell_room, cap_room)  # MWh bought at hour i
        if qty <= 1e-9:
            continue
        # shrink qty until the SOC path stays within the battery
        while qty > 1e-9:
            c_try, d_try = charge.copy(), discharge.copy()
            c_try[i] += qty
            d_try[j] += qty * eta
            if soc_ok(c_try, d_try):
                charge, discharge = c_try, d_try
                throughput_left -= qty * eta
                break
            qty /= 2

    return {
        "charge_mw": charge,
        "discharge_mw": discharge,
        "soc_mwh": asset["soc_start_mwh"] + np.cumsum(eta * charge - discharge),
        "pnl_usd": float(prices @ (discharge - charge) - throughput_cost * discharge.sum()),
        "source": "heuristic",
        "solver_status": "heuristic",
    }


def solve_schedule(prices, config):
    """Public entrypoint: LP first, heuristic fallback, always logs which path ran."""
    asset = config["asset"]
    try:
        result = solve_lp(prices, asset, config["solver"]["name"])
        logger.info("schedule solved by LP, pnl=%.2f", result["pnl_usd"])
        return result
    except Exception as e:
        logger.warning("LP failed (%s), falling back to heuristic", e)
        result = solve_heuristic(prices, asset)
        logger.info("schedule solved by heuristic, pnl=%.2f", result["pnl_usd"])
        return result
