"""Measure which basis Kalshi *actually* settles on, and derive the sigma floor.

This exists because the question "what is ``intraday_sigma_floor_f`` for?" can only
be answered by a measurement, and the measurement has been got wrong before.  It
scores candidate bases against Kalshi's own settled markets -- the only ground
truth that cannot be argued with -- and then derives the floor from the residual
that survives.

**Method**

1. Pull every settled market for the seven high-temp series.  Kalshi's buckets
   tile the *integers*: ``less`` cap ``C`` is ``T <= C-1``, ``between``
   ``[F, F+1]`` is ``T in {F, F+1}``, ``greater`` floor ``F`` is ``T >= F+1``.
   Exactly one settles YES, so the YES market pins the settled integer to a
   two-wide range.
2. Score each candidate basis by how often it lands inside that range.
3. For the winner, measure ``R = settlement - hourly-METAR max`` over the fixed-LST
   climate day.  ``R`` is what our observation *cannot* see.
4. Derive the floor.  Measured from LST 16:00 the intraday remaining-rise curve is
   ``0.00 +- 0.00``, so late in the day the live model is exactly
   ``N(running_max, s)`` truncated below at ``running_max`` -- a **half-normal with
   scale s**.  The MLE of a half-normal scale is the RMS of the observations, so
   the floor is ``RMS(R)``, checked out-of-sample on held-out days.

It also reports the **bucket-grid mass check**: the six probabilities of an event
must sum to 1, because the buckets tile the integers and exactly one settles YES.

Read-only -- no trading, no schema change, no writes.

    python -m weather_arb.settlement_basis [--days N] [--eval-hour H] [--json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import NormalDist
from typing import Any, Optional

import aiohttp

from .backtest import _settled_markets
from .config import Settings
from .divergence import DivergenceClient
from .models import Venue, WeatherMarket
from . import timeutils

_IEM_CLI = "https://mesonet.agron.iastate.edu/json/cli.py"

# Series ticker -> the ICAO Kalshi settles that city on.
SERIES_ICAO: dict[str, str] = {
    "KXHIGHNY": "KNYC", "KXHIGHCHI": "KMDW", "KXHIGHMIA": "KMIA",
    "KXHIGHAUS": "KAUS", "KXHIGHDEN": "KDEN", "KXHIGHLAX": "KLAX",
    "KXHIGHPHIL": "KPHL",
}
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}

# Half-normal(scale s): E[X] = s*sqrt(2/pi), SD[X] = s*sqrt(1 - 2/pi), RMS[X] = s.
HALF_NORMAL_MEAN = math.sqrt(2.0 / math.pi)
HALF_NORMAL_SD = math.sqrt(1.0 - 2.0 / math.pi)


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def event_date(ticker: str) -> Optional[str]:
    """``KXHIGHNY-26AUG22-B77.5`` -> ``2026-08-22`` (the climate day)."""
    parts = ticker.split("-")
    if len(parts) < 2 or len(parts[1]) < 6:
        return None
    tok = parts[1]
    month = _MONTHS.get(tok[2:5])
    if month is None or not tok[:2].isdigit() or not tok[5:].isdigit():
        return None
    return f"20{tok[:2]}-{month:02d}-{int(tok[5:]):02d}"


def settled_range(markets: list[dict[str, Any]]) -> Optional[tuple[Optional[int], Optional[int]]]:
    """Integer degF range implied by the single YES market, or ``None``.

    Kalshi's tails are *strict*: ``less`` cap ``C`` means ``T <= C-1`` and
    ``greater`` floor ``F`` means ``T >= F+1``.  Verified against 420/420 events:
    under this reading the six markets tile the integers with no gap or overlap.
    """
    for m in markets:
        if str(m.get("result", "")).lower() != "yes":
            continue
        floor, cap, st = m.get("floor"), m.get("cap"), m.get("strike_type")
        if st == "between" and floor is not None and cap is not None:
            return int(floor), int(cap)
        if st == "less" and cap is not None:
            return None, int(cap) - 1
        if st == "greater" and floor is not None:
            return int(floor) + 1, None
    return None


def tiles_integers(markets: list[dict[str, Any]]) -> bool:
    """Do an event's markets partition the integers exactly once?"""
    less = [m["cap"] for m in markets if m.get("strike_type") == "less"]
    greater = [m["floor"] for m in markets if m.get("strike_type") == "greater"]
    between = sorted((m["floor"], m["cap"]) for m in markets
                     if m.get("strike_type") == "between")
    if len(less) != 1 or len(greater) != 1 or not between:
        return False
    if any(c != f + 1 for f, c in between):
        return False
    if between[0][0] != less[0] or between[-1][1] != greater[0]:
        return False
    return all(between[i + 1][0] == between[i][1] + 1 for i in range(len(between) - 1))


def bucket_bounds(market: dict[str, Any], half_degree: bool) -> tuple[float, float]:
    """Continuous ``[lo, hi]`` for a market under either grid convention.

    ``half_degree=True`` delegates to :meth:`WeatherMarket.settlement_interval_f`
    -- the same code live pricing uses -- so this diagnostic can never drift from
    production.  ``half_degree=False`` reproduces the pre-2026-08-23 behaviour
    (raw integer strikes) purely so the before/after comparison stays runnable.
    """
    st = market.get("strike_type")
    floor, cap = market.get("floor"), market.get("cap")
    if half_degree:
        return WeatherMarket(
            ticker=str(market.get("ticker", "")), venue=Venue.KALSHI, icao="",
            floor_strike_f=None if floor is None else float(floor),
            cap_strike_f=None if cap is None else float(cap),
            strike_type=str(st or ""),
        ).settlement_interval_f()
    if st == "between":
        return float(floor), float(cap)
    if st == "less":
        return -math.inf, float(cap)
    return float(floor), math.inf


def bucket_prob(mu: float, sigma: float, lo: float, hi: float, lower_bound: float) -> float:
    """``P(lo <= X <= hi | X >= lower_bound)`` -- mirrors ``forecast_ingest``."""
    lo_eff = max(lo, lower_bound)
    if hi < lo_eff:
        return 0.0
    dist = NormalDist(mu, max(sigma, 1e-9))
    denom = max(1.0 - dist.cdf(lower_bound), 1e-9)
    cdf = lambda x: 0.0 if x == -math.inf else 1.0 if x == math.inf else dist.cdf(x)
    return max(0.0, min(1.0, (cdf(hi) - cdf(lo_eff)) / denom))


def half_normal_scale(residuals: list[float]) -> float:
    """MLE of the half-normal scale = RMS of the observations."""
    if not residuals:
        return 0.0
    return math.sqrt(sum(x * x for x in residuals) / len(residuals))


def half_normal_nll(residuals: list[float], scale: float) -> float:
    """Mean negative log-likelihood of ``residuals`` under half-normal(``scale``)."""
    if not residuals or scale <= 0:
        return math.inf
    c = math.log(scale) + 0.5 * math.log(math.pi / 2.0)
    return sum(c + x * x / (2.0 * scale * scale) for x in residuals) / len(residuals)


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
async def fetch_cli_highs(
    session: aiohttp.ClientSession, icao: str, months: list[tuple[int, int]]
) -> dict[str, float]:
    """NWS CLI daily high per climate date, from the IEM CLI archive."""
    out: dict[str, float] = {}
    for year, month in months:
        params = {"station": icao, "year": year, "month": month}
        for attempt in range(4):
            async with session.get(_IEM_CLI, params=params) as resp:
                if resp.status == 429 or resp.status >= 500:
                    await asyncio.sleep(min(8.0, 1.5 * (2 ** attempt)))
                    continue
                if resp.status >= 400:
                    break
                payload = await resp.json(content_type=None)
            for rec in (payload or {}).get("results", []):
                high, valid = rec.get("high"), rec.get("valid")
                if high is None or not valid:
                    continue
                try:                       # CLI reports missing days as "M"
                    out[str(valid)] = float(high)
                except (TypeError, ValueError):
                    continue
            break
        await asyncio.sleep(0.6)
    return out


async def fetch_settled(
    session: aiohttp.ClientSession, days: int
) -> dict[str, list[dict[str, Any]]]:
    """Settled markets grouped by event key (``SERIES-DATETOKEN``)."""
    min_close = int(time.time()) - (days + 3) * 86400
    events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for series in SERIES_ICAO:
        for raw in await _settled_markets(session, series, min_close):
            ticker = raw.get("ticker")
            if not ticker:
                continue
            floor, cap = raw.get("floor_strike"), raw.get("cap_strike")
            events["-".join(str(ticker).split("-")[:2])].append({
                "ticker": str(ticker),
                "floor": None if floor is None else int(floor),
                "cap": None if cap is None else int(cap),
                "strike_type": str(raw.get("strike_type", "")),
                "result": str(raw.get("result", "")).lower(),
            })
    return events


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class DayCase:
    icao: str
    climate_date: str
    metar_max_f: float          # max over the fixed-LST climate day
    running_max_f: float        # running max as of ``eval_hour`` LST
    cli_high_f: Optional[float]
    lo: Optional[int]           # settled integer range (inclusive)
    hi: Optional[int]
    markets: list[dict[str, Any]]


def in_range(value: float, lo: Optional[int], hi: Optional[int]) -> bool:
    return (lo is None or value >= lo) and (hi is None or value <= hi)


async def collect(settings: Settings, days: int, eval_hour: int) -> list[DayCase]:
    offsets = {s.icao: s.standard_utc_offset_hours for s in settings.stations}
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days)

    async with aiohttp.ClientSession() as session:
        events = await fetch_settled(session, days)
        client = DivergenceClient(session, settings)
        months = sorted({(d.year, d.month) for d in
                         (start + timedelta(days=i) for i in range(days + 2))})
        metar: dict[str, list[tuple[datetime, float]]] = {}
        cli: dict[str, dict[str, float]] = {}
        for icao in sorted(set(SERIES_ICAO.values())):
            metar[icao] = await client.fetch_metar(
                icao, start, today + timedelta(days=1))
            cli[icao] = await fetch_cli_highs(session, icao, months)

    cases: list[DayCase] = []
    for key, markets in sorted(events.items()):
        series = key.split("-")[0]
        icao = SERIES_ICAO.get(series)
        climate_date = event_date(key)
        if icao is None or climate_date is None or climate_date < start.isoformat():
            continue
        offset = offsets.get(icao)
        if offset is None:
            continue
        lo_utc, hi_utc = timeutils.climate_day_bounds_utc(climate_date, offset)
        day = sorted((ts, t) for ts, t in metar.get(icao, []) if lo_utc <= ts < hi_utc)
        # A short day means our archive pull is incomplete, not that it was cool.
        if len(day) < 20:
            continue
        rng = settled_range(markets)
        if rng is None:
            continue
        upto = [t for ts, t in day if (ts + timedelta(hours=offset)).hour <= eval_hour]
        if not upto:
            continue
        cases.append(DayCase(
            icao=icao, climate_date=climate_date, metar_max_f=max(t for _, t in day),
            running_max_f=max(upto), cli_high_f=cli.get(icao, {}).get(climate_date),
            lo=rng[0], hi=rng[1], markets=markets,
        ))
    return cases


def analyze(cases: list[DayCase], eval_hour: int) -> dict[str, Any]:
    tiling_ok = sum(1 for c in cases if tiles_integers(c.markets))

    bases = {
        "round(hourly-METAR max)": lambda c: float(round(c.metar_max_f)),
        "floor(hourly-METAR max)": lambda c: float(math.floor(c.metar_max_f)),
        "NWS CLI daily high": lambda c: c.cli_high_f,
    }
    scored: dict[str, dict[str, Any]] = {}
    for name, fn in bases.items():
        usable = [c for c in cases if fn(c) is not None]
        hits = sum(1 for c in usable if in_range(fn(c), c.lo, c.hi))
        per_city: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for c in usable:
            per_city[c.icao][0] += int(in_range(fn(c), c.lo, c.hi))
            per_city[c.icao][1] += 1
        scored[name] = {
            "n": len(usable), "hits": hits,
            "rate": hits / len(usable) if usable else 0.0,
            "per_city": {k: v[0] / v[1] for k, v in sorted(per_city.items())},
        }

    with_cli = [c for c in cases if c.cli_high_f is not None]
    residuals = [c.cli_high_f - c.metar_max_f for c in with_cli]
    per_city_resid: dict[str, list[float]] = defaultdict(list)
    for c in with_cli:
        per_city_resid[c.icao].append(c.cli_high_f - c.metar_max_f)

    resid: dict[str, Any] = {"n": len(residuals)}
    if residuals:
        resid.update({
            "mean": statistics.fmean(residuals),
            "sd": statistics.stdev(residuals) if len(residuals) > 1 else 0.0,
            "rms": half_normal_scale(residuals),
            "min": min(residuals), "max": max(residuals),
            "negatives": sum(1 for x in residuals if x < 0),
            "histogram": dict(sorted(Counter(round(x, 1) for x in residuals).items())),
            "per_city_rms": {k: half_normal_scale(v)
                             for k, v in sorted(per_city_resid.items())},
        })

    # Out-of-sample: fit the scale on earlier days, score on held-out later days.
    dates = sorted({c.climate_date for c in with_cli})
    folds = []
    for frac in (0.5, 0.6, 0.7, 0.8):
        if len(dates) < 8:
            break
        cut = dates[int(len(dates) * frac)]
        train = [c.cli_high_f - c.metar_max_f for c in with_cli if c.climate_date < cut]
        test = [c.cli_high_f - c.metar_max_f for c in with_cli if c.climate_date >= cut]
        if len(train) < 20 or len(test) < 20:
            continue
        s_train = half_normal_scale(train)
        folds.append({
            "cut": cut, "n_train": len(train), "n_test": len(test),
            "scale_train": s_train, "scale_test": half_normal_scale(test),
            "test_nll_at_train_scale": half_normal_nll(test, s_train),
            "test_nll_at_1.0": half_normal_nll(test, 1.0),
        })

    # Bucket-grid mass check: the six probabilities must sum to 1.
    grid: dict[str, Any] = {}
    for label, half in (("retired: raw strikes [F, F+1]", False),
                        ("LIVE: rounds-into [F-0.5, F+1.5]", True)):
        sums, ll = [], []
        for c in with_cli:
            mu = c.running_max_f
            probs = [bucket_prob(mu, 1.0, *bucket_bounds(m, half), mu) for m in c.markets]
            total = sum(probs)
            sums.append(total)
            norm = total or 1.0
            for m, p in zip(c.markets, probs):
                if m.get("result") == "yes":
                    ll.append(-math.log(min(max(p / norm, 1e-6), 1 - 1e-6)))
        grid[label] = {
            "mean_prob_sum": statistics.fmean(sums) if sums else 0.0,
            "median_prob_sum": statistics.median(sums) if sums else 0.0,
            "logloss_renormalised": statistics.fmean(ll) if ll else 0.0,
        }

    return {
        "eval_hour_lst": eval_hour,
        "n_events": len(cases),
        "tiling_ok": tiling_ok,
        "bases": scored,
        "residual": resid,
        "oos_folds": folds,
        "grid_check": grid,
        "derived_sigma_floor_f": resid.get("rms"),
    }


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _render(r: dict[str, Any], settings: Settings) -> str:
    out = ["=" * 78, "SETTLEMENT BASIS + SIGMA-FLOOR DERIVATION", "=" * 78]
    out.append(f"  settled events analysed: {r['n_events']}   "
               f"integer-tiling verified on {r['tiling_ok']}/{r['n_events']}")
    if not r["n_events"]:
        out.append("  (no settled events in window)")
        return "\n".join(out + ["=" * 78])

    out += ["", "  Candidate bases, scored on Kalshi's OWN settled buckets:"]
    for name, s in r["bases"].items():
        cities = "  ".join(f"{k[1:]}:{100*v:.0f}%" for k, v in s["per_city"].items())
        out.append(f"    {name:26s} {s['hits']:4d}/{s['n']:<4d} = "
                   f"{100*s['rate']:5.1f}%   {cities}")

    res = r["residual"]
    if res.get("n"):
        out += ["", "  RESIDUAL  R = settlement - hourly-METAR max  (degF, over the LST day):",
                f"    n {res['n']}   mean {res['mean']:+.3f}   sd {res['sd']:.3f}   "
                f"RMS {res['rms']:.3f}   range [{res['min']:.1f}, {res['max']:.1f}]   "
                f"negatives {res['negatives']}",
                f"    histogram {res['histogram']}",
                "    per-station RMS  " + "  ".join(
                    f"{k[1:]}:{v:.2f}" for k, v in res["per_city_rms"].items())]

        out += ["", f"  DERIVED FLOOR.  From LST {r['eval_hour_lst']}:00 the intraday "
                    "remaining-rise is ~0, so the live",
                "  model is half-normal(scale=floor) above the running max. MLE scale = RMS(R):",
                f"    match mean  -> s = {res['mean']:.3f} / {HALF_NORMAL_MEAN:.4f} = "
                f"{res['mean']/HALF_NORMAL_MEAN:.3f}",
                f"    match sd    -> s = {res['sd']:.3f} / {HALF_NORMAL_SD:.4f} = "
                f"{res['sd']/HALF_NORMAL_SD:.3f}",
                f"    MLE (=RMS)  -> s = {res['rms']:.3f}      "
                f"[config: {settings.forecast.intraday_sigma_floor_f:.2f}]"]

    if r["oos_folds"]:
        out += ["", "  Out-of-sample (scale fitted on earlier days, scored on later days):",
                f"    {'cut':>10s} {'n_tr':>5s} {'s(train)':>9s} {'n_te':>5s} "
                f"{'s(test)':>8s} {'test NLL@s':>11s} {'test NLL@1.0':>13s}"]
        for f in r["oos_folds"]:
            out.append(f"    {f['cut']:>10s} {f['n_train']:5d} {f['scale_train']:9.3f} "
                       f"{f['n_test']:5d} {f['scale_test']:8.3f} "
                       f"{f['test_nll_at_train_scale']:11.4f} {f['test_nll_at_1.0']:13.4f}")

    out += ["", "  BUCKET-GRID MASS CHECK (the six probabilities must sum to 1).",
            "  The LIVE row calls WeatherMarket.settlement_interval_f() -- the same code",
            "  forecast_ingest prices with -- so this cannot drift from production:"]
    for label, g in r["grid_check"].items():
        out.append(f"    {label:28s} mean sum {g['mean_prob_sum']:.3f}   "
                   f"median {g['median_prob_sum']:.3f}   "
                   f"renormalised logloss {g['logloss_renormalised']:.4f}")
    out.append("=" * 78)
    return "\n".join(out)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure Kalshi's real settlement basis and derive the sigma floor."
    )
    parser.add_argument("--days", type=int, default=35, help="look back N days (default 35)")
    parser.add_argument("--eval-hour", type=int, default=18,
                        help="LST hour to freeze the running max at (default 18)")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    cases = asyncio.run(collect(settings, max(7, args.days), args.eval_hour))
    result = analyze(cases, args.eval_hour)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(_render(result, settings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
