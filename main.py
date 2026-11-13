#!/usr/bin/env python3
"""Polymarket "Next CEO of NVIDIA?" forecaster (make.fun bounty).

Approach: empirical h-step change quantiles (historical simulation), per market.

For each market, the forecast for minute at+h is built from that market's own
price history: the empirical distribution of h-minute price changes.  The band
therefore widens the way the market itself has widened -- quiet markets get a
narrow band, jumpy markets a wide one, jumps included -- instead of one fixed
Gaussian spread for every market.  Quantiles are computed at a small set of
horizons and interpolated in log(h), which keeps the run far inside the
8-second cap.  The median stays at the last price plus the empirical median
change (about zero: a prediction-market price is a martingale, so the current
price is the best point forecast).  A small Gaussian spread floor keeps the
band from collapsing to zero on a market that has never moved, so one jump
does not cost everything.  Pure stdlib, no network, no randomness: two runs
write identical bytes.
"""

import json
import math
import os
import sys

INPUT_DIR = os.environ.get("MAKEFUN_INPUT_DIR", "/input")
OUTPUT_DIR = os.environ.get("MAKEFUN_OUTPUT_DIR", "/output")

QUANTILES = [0.05, 0.25, 0.5, 0.75, 0.95]
# Standard normal quantiles, only used for the spread floor / fallback.
Z = [-1.644854, -0.674490, 0.0, 0.674490, 1.644854]
# Horizons at which empirical change quantiles are computed directly.
HORIZONS = [1, 2, 4, 8, 16, 32, 64, 128, 240]
# Minimum samples for trusting an empirical horizon; below this, Gaussian fallback.
MIN_SAMPLES = 50
# Spread floor: half-width per sqrt-minute for the outer quantiles, so a market
# with no observed moves still carries a small band (one jump is possible).
SIGMA_FLOOR = 0.0005
# Tuning knobs (set from practice scores; the grader runs with these values).
SPREAD_SHRINK = 0.6
HISTORY_MINUTES = 720


def price6(x):
    """Clamp to [0, 1] and round to 6 decimals (0, never -0)."""
    return round(min(1.0, max(0.0, x)) * 1e6) / 1e6 + 0.0


def quantile_sorted(xs, tau):
    """Linear-interpolation quantile of an already sorted list."""
    n = len(xs)
    if n == 0:
        return 0.0
    if n == 1:
        return xs[0]
    pos = tau * (n - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return xs[lo] * (1.0 - frac) + xs[hi] * frac


def sigma_1min(prices):
    """Robust per-minute spread: 1.4826 * median absolute 1-min change."""
    diffs = [abs(prices[i + 1] - prices[i]) for i in range(len(prices) - 1)]
    if not diffs:
        return SIGMA_FLOOR
    diffs.sort()
    mad = quantile_sorted(diffs, 0.5)
    return max(mad * 1.4826, SIGMA_FLOOR)


def horizon_quantiles(prices, horizons):
    """For each horizon, the five empirical quantiles of that horizon's changes.

    Returns {h: [q05, q25, q50, q75, q95]} using only horizons with enough
    samples; horizons without enough samples are omitted (caller falls back).
    """
    n = len(prices)
    out = {}
    for h in horizons:
        m = n - h
        if m < MIN_SAMPLES:
            continue
        changes = sorted(prices[i + h] - prices[i] for i in range(m))
        out[h] = [quantile_sorted(changes, tau) * SPREAD_SHRINK for tau in QUANTILES]
    return out


def interp_quantile(h, hq, sigma):
    """Quantiles for horizon h, interpolated in log(h) between computed horizons.

    Falls back to Gaussian z*sigma*sqrt(h) when no empirical horizon is near.
    Returns the five change-quantiles (offsets from the last price).
    """
    hs = sorted(hq)
    if not hs:
        return [z * sigma * math.sqrt(h) for z in Z]
    if h <= hs[0]:
        base = hq[hs[0]]
        # Scale the nearest empirical horizon down by sqrt(h / h0).
        s = math.sqrt(h / hs[0])
        return [v * s for v in base]
    if h >= hs[-1]:
        base = hq[hs[-1]]
        s = math.sqrt(h / hs[-1])
        return [v * s for v in base]
    # Bracket h between two computed horizons, interpolate in log(h).
    for a, b in zip(hs, hs[1:]):
        if a <= h <= b:
            wa = math.log(b / h) / math.log(b / a)
            wb = 1.0 - wa
            qa, qb = hq[a], hq[b]
            return [qa[j] * wa + qb[j] * wb for j in range(5)]
    return [z * sigma * math.sqrt(h) for z in Z]


def forecast_market(prices, horizon):
    """240 rows x 5 quantiles for one market."""
    # Only the last HISTORY_MINUTES feed the distribution, so it tracks the
    # current volatility regime; the last price is unchanged (it is the tail).
    prices = prices[-HISTORY_MINUTES:]
    last = prices[-1]
    sigma = sigma_1min(prices)
    hq = horizon_quantiles(prices, HORIZONS)
    rows = []
    for h in range(1, horizon + 1):
        dq = interp_quantile(h, hq, sigma)
        # Re-center: the median forecast is the last price.  A prediction-market
        # price is a martingale, so the empirical median change is past drift /
        # sampling noise, not signal -- keeping it pulled the median off the
        # last price and cost more than the spread shape ever saved.
        med = dq[2]
        dq = [d - med for d in dq]
        # Spread floor: never narrower than the Gaussian floor around the median.
        row = []
        for j in range(5):
            v = last + dq[j]
            floor = last + Z[j] * SIGMA_FLOOR * math.sqrt(h)
            v = max(v, floor) if j >= 2 else min(v, floor)
            row.append(price6(v))
        # Enforce non-decreasing (clamping can only break it at the edges).
        for j in range(1, 5):
            if row[j] < row[j - 1]:
                row[j] = row[j - 1]
        rows.append(row)
    return rows


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    inputs = sorted(f for f in os.listdir(INPUT_DIR) if f.endswith(".in"))
    for fname in inputs:
        case_id = fname[: -len(".in")]
        with open(os.path.join(INPUT_DIR, fname), "r", encoding="utf-8") as f:
            case = json.load(f)
        horizon = case["horizon_minutes"]
        forecasts = []
        for m in case["markets"]:
            prices = m["prices"]
            if not prices:
                raise ValueError("market %s has no prices" % m["id"])
            forecasts.append({"id": m["id"], "q": forecast_market(prices, horizon)})
        out = {"forecasts": forecasts}
        with open(os.path.join(OUTPUT_DIR, case_id + ".out"), "w", encoding="utf-8") as f:
            f.write(json.dumps(out) + "\n")


if __name__ == "__main__":
    main()
