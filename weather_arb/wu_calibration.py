"""Recalibrate the forecast models to the Weather-Underground settlement basis.

.. warning::

   **LABELS INVERTED 2026-08-21.** Since 2026-08-14 Kalshi settles "according to
   The Weather Company", and TWC's published daily max was verified to equal the
   hourly-METAR max on 147/147 station-days. So in the output below the **``wu``
   column is now the Kalshi-relevant one** and the ``nws`` column (1-min/CLI) is
   the basis nobody settles on. The per-station "WU offset" is likewise no longer
   something to subtract before pricing a Kalshi bucket -- it is the CLI's excess
   over settlement (+0.80 F on average).

   One useful corollary: the tracker grades forecasts against ``daily_tmax``, i.e.
   the hourly-METAR max, so its grading basis was already the correct one by
   accident. Trading/backtest grading uses Kalshi's own settled results and is
   unaffected either way.

Original framing (now historical):

The tracker grades every model's daily-high forecast against the **NWS max** (what
Kalshi settles on). Polymarket / ForecastEx settle on the **WU hourly-METAR max**
-- lower by the intra-hour-spike gap, and at a *warmer* station for NYC/CHI
(LaGuardia/O'Hare). So a model that is well-calibrated for Kalshi is *biased* for
the WU venues, and its Brier leaderboard there is different.

This report re-grades each model's stored forecasts against **both** truths and
reports, per model, the bias/MAE under each -- plus the per-station **offset** you
would subtract from a high forecast before pricing a WU-settled bucket. It reads
``daily_forecasts`` + ``daily_tmax`` from the ledger and fetches the WU max from
the IEM archive. Read-only: no trading, no schema change.

    python -m weather_arb.wu_calibration [--days N] [--json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp

from .config import Settings
from .divergence import DivergenceClient, PM_NOT_LISTED, PM_STATION, CITY_ICAO

# Kalshi ICAO -> the station Polymarket actually settles that city on.
_ICAO_TO_CITY = {icao: city for city, icao in CITY_ICAO.items()}


def wu_station_for(icao: str) -> str:
    """Kalshi's settlement ICAO -> Polymarket's, or ``""`` if it lists no market.

    Reads :data:`divergence.PM_STATION` so there is one registry, not two.
    Corrected 2026-09-08: Denver maps to **KBKF** (Buckley SFB), not KDEN, and
    Philadelphia maps to nothing at all.  An unknown ICAO passes through, since
    the caller may be asking about a station outside the tracked seven.
    """
    city = _ICAO_TO_CITY.get(icao)
    if city is None:
        return icao
    if city in PM_NOT_LISTED:
        return ""
    return PM_STATION.get(city, icao)


@dataclass(frozen=True, slots=True)
class ForecastRow:
    icao: str
    model: str
    date: str
    forecast_f: float


def load_rows(db_path: str, before_date: str) -> list[ForecastRow]:
    """Settled (climate_date < before_date) model forecasts from the ledger.

    The NWS and WU truths are NOT taken from ``daily_tmax`` -- that is the
    tracker's own METAR max, which is incomplete during any downtime and would
    conflate a data gap with a settlement-basis difference. Both truths are
    recomputed from the complete IEM archive (see :func:`analyze`)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            """
            SELECT icao, model, climate_date AS date, forecast_high_f AS forecast_f
            FROM daily_forecasts WHERE climate_date < ?
            """,
            (before_date,),
        )
        return [ForecastRow(r["icao"], r["model"], r["date"], float(r["forecast_f"]))
                for r in cur.fetchall()]
    finally:
        conn.close()


def error_stats(errors: list[float]) -> dict:
    """Bias (mean signed error), MAE, RMSE for a list of forecast errors."""
    n = len(errors)
    if not n:
        return {"n": 0, "bias": None, "mae": None, "rmse": None}
    bias = sum(errors) / n
    mae = sum(abs(e) for e in errors) / n
    rmse = (sum(e * e for e in errors) / n) ** 0.5
    return {"n": n, "bias": bias, "mae": mae, "rmse": rmse}


async def _truths(settings: Settings, by_icao_dates: dict[str, set[str]]
                  ) -> dict[tuple[str, str], tuple[Optional[int], Optional[int]]]:
    """``(icao, date) -> (nws_max_f, wu_max_f)`` recomputed from the IEM archive
    via the divergence engine (nws = 1-min/METAR CLI proxy at the Kalshi station;
    wu = hourly METAR at Polymarket's station)."""
    truths: dict[tuple[str, str], tuple[Optional[int], Optional[int]]] = {}
    async with aiohttp.ClientSession(headers={"User-Agent": settings.http_user_agent}) as session:
        client = DivergenceClient(session, settings)
        for icao, dates in by_icao_dates.items():
            station = settings.station_by_icao(icao)
            if station is None or not dates:
                continue
            city = _ICAO_TO_CITY.get(icao, icao)
            lo = min(datetime.fromisoformat(d).date() for d in dates)
            hi = max(datetime.fromisoformat(d).date() for d in dates)
            n_days = (hi - lo).days + 1
            for div in await client.analyze(city, icao, station.standard_utc_offset_hours, lo, n_days):
                truths[(icao, div.date)] = (div.nws_max_f, div.pm_max_f)
    return truths


async def analyze(settings: Settings, rows: list[ForecastRow]) -> dict:
    """Grade each stored forecast against the NWS and WU truths; aggregate."""
    by_icao_dates: dict[str, set[str]] = {}
    for r in rows:
        by_icao_dates.setdefault(r.icao, set()).add(r.date)
    truths = await _truths(settings, by_icao_dates)

    per_model_nws: dict[str, list[float]] = {}
    per_model_wu: dict[str, list[float]] = {}
    per_station_gap: dict[str, list[float]] = {}
    for r in rows:
        nws, wu = truths.get((r.icao, r.date), (None, None))
        if nws is not None:
            per_model_nws.setdefault(r.model, []).append(r.forecast_f - nws)
        if wu is not None:
            per_model_wu.setdefault(r.model, []).append(r.forecast_f - wu)
        if nws is not None and wu is not None:
            per_station_gap.setdefault(r.icao, []).append(nws - wu)

    models = sorted(set(per_model_nws) | set(per_model_wu))
    return {
        "models": {m: {"nws": error_stats(per_model_nws.get(m, [])),
                       "wu": error_stats(per_model_wu.get(m, []))} for m in models},
        "station_offset": {icao: (sum(g) / len(g)) for icao, g in per_station_gap.items() if g},
    }


def _signed(x: Optional[float]) -> str:
    return f"{x:+.1f}" if x is not None else "-"


def _plain(x: Optional[float]) -> str:
    return f"{x:.1f}" if x is not None else "-"


def _render(result: dict, days: int) -> str:
    out = ["=" * 78,
           f"  FORECAST RECALIBRATION: NWS (Kalshi) vs WU (Polymarket/ForecastEx)   {days}d",
           "=" * 78, "",
           f"  {'model':<12}{'n':>4}{'bias_NWS':>10}{'bias_WU':>10}{'MAE_NWS':>9}{'MAE_WU':>9}",
           "  " + "-" * 70]
    if not result["models"]:
        out.append("  (no settled forecasts in the ledger yet -- let the tracker run longer)")
    for model, s in result["models"].items():
        nws, wu = s["nws"], s["wu"]
        out.append(f"  {model:<12}{nws['n']:>4}{_signed(nws['bias']):>10}{_signed(wu['bias']):>10}"
                   f"{_plain(nws['mae']):>9}{_plain(wu['mae']):>9}")
    out.append("")
    out.append("  bias = mean(forecast - truth); positive = forecast runs hot vs that basis.")
    out.append("")
    out.append("  PER-STATION WU OFFSET (subtract from a high forecast before pricing a WU bucket):")
    if result["station_offset"]:
        for icao, off in sorted(result["station_offset"].items()):
            wu_icao = wu_station_for(icao)
            tag = ("  (no Polymarket market for this city)" if not wu_icao else
                   f"  (Kalshi {icao} -> Polymarket {wu_icao})" if wu_icao != icao else "")
            arrow = "WU settles LOWER" if off > 0 else "WU settles HIGHER" if off < 0 else "same"
            out.append(f"    {icao}: mean(NWS - WU) = {off:+.1f}F   [{arrow}]{tag}")
    else:
        out.append("    (none yet)")
    out.append("=" * 78)
    return "\n".join(out)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Recalibrate forecast models to the WU settlement basis (read-only)."
    )
    parser.add_argument("--days", type=int, default=30, help="look back N days (default 30)")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    today = datetime.now(timezone.utc).date()
    before = today.isoformat()
    cutoff = (today - timedelta(days=max(1, args.days))).isoformat()
    rows = [r for r in load_rows(settings.db_path, before) if r.date >= cutoff]
    result = asyncio.run(analyze(settings, rows))

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(_render(result, args.days))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
