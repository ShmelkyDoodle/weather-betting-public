"""Retrospective backtest of Strategy 1 (deterministic lock-in).

For each *settled* Kalshi weather market over a past window, this:

1. reconstructs the intraday running max from the IEM ASOS **archive** for the
   settlement station's Local-Standard-Time climate day;
2. finds the moment that running max made the contract a certain YES (reached a
   tail-high floor) or certain NO (climbed past a bucket's cap);
3. reads the Kalshi **candlestick** price at that moment to see what you could
   have bought the winning side for; and
4. settles at 100¢, netting estimated fees → the realized lock-in edge.

Run:  ``python -m weather_arb.backtest --days 7``

Honest caveats: candlestick OHLC approximates fills (no book depth), and only the
deterministic Strategy 1 is covered — a Strategy 2 backtest needs archived
forecasts. Because official highs are >= our hourly-reconstructed max, the
deterministic calls are settlement-safe (never a false YES/NO).
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp

from typing import Callable, NamedTuple

from .arb_engine import (edge_limit_prices, get_dynamic_edge_threshold,
                         passive_buy_price, passive_sell_price)
from .calibration import CalibrationMap, fit_calibration
from .config import Settings, StationConfig
from .db import Database
from .fees import fee_cents, fee_fraction_per_contract
from .market_discovery import _parse_event_date
from .models import LOCKIN_MARGIN_F, Venue, WeatherMarket
from . import timeutils

_KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
_IEM_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
_KALSHI_CANDLES = _KALSHI + "/series/{series}/markets/{ticker}/candlesticks"


async def _kalshi_get(
    session: aiohttp.ClientSession, url: str, params: dict[str, object]
) -> dict[str, object]:
    """GET Kalshi JSON with backoff on 429/5xx (public endpoints, no auth)."""
    for attempt in range(6):
        async with session.get(url, params=params) as resp:
            if resp.status == 429 or resp.status >= 500:
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else min(8.0, 0.5 * (2 ** attempt))
                await asyncio.sleep(delay)
                continue
            if resp.status >= 400:
                return {}
            data = await resp.json(content_type=None)
            return data if isinstance(data, dict) else {}
    return {}


async def _settled_markets(
    session: aiohttp.ClientSession, series: str, min_close_ts: int
) -> list[dict[str, object]]:
    """Recent settled markets for a series (filtered by close time, paginated)."""
    out: list[dict[str, object]] = []
    cursor: Optional[str] = None
    for _ in range(10):
        params: dict[str, object] = {
            "series_ticker": series, "status": "settled",
            "limit": 200, "min_close_ts": min_close_ts,
        }
        if cursor:
            params["cursor"] = cursor
        data = await _kalshi_get(session, f"{_KALSHI}/markets", params)
        batch = data.get("markets", [])
        out.extend(batch)
        cursor = data.get("cursor") or None
        if not cursor or not batch:
            break
    return out
_CEILING = 98  # never assume a fill above this (matches live lock-in)


# IEM ASOS archive needs the state network; specifying it makes bulk queries reliable.
_IEM_NETWORKS = {
    "KMDW": "IL_ASOS", "KORD": "IL_ASOS", "KNYC": "NY_ASOS", "KMIA": "FL_ASOS",
    "KAUS": "TX_ASOS", "KDEN": "CO_ASOS", "KLAX": "CA_ASOS", "KPHL": "PA_ASOS",
    "KBOS": "MA_ASOS",
}


async def _iem_temps_all(
    session: aiohttp.ClientSession, icao: str, day_from: str, day_to: str
) -> list[tuple[datetime, float]]:
    """All (UTC time, °F) observations for a station over a date range (one request).

    Fetching the whole window at once — rather than per day — keeps us well under
    IEM's bulk-request throttling. Retries once on an empty body (throttled).
    """
    iem_id = icao[1:] if icao.startswith("K") else icao
    y1, m1, d1 = (int(x) for x in day_from.split("-"))
    y2, m2, d2 = (int(x) for x in day_to.split("-"))
    params: dict[str, object] = {
        "station": iem_id, "data": "tmpf", "tz": "Etc/UTC", "format": "onlycomma",
        "latlon": "no", "missing": "empty",
        "year1": y1, "month1": m1, "day1": d1, "year2": y2, "month2": m2, "day2": d2,
    }
    network = _IEM_NETWORKS.get(icao)
    if network:
        params["network"] = network
    for attempt in range(2):
        async with session.get(_IEM_URL, params=params) as resp:
            text = await resp.text()
        out: list[tuple[datetime, float]] = []
        for row in text.splitlines()[1:]:
            parts = row.split(",")
            if len(parts) < 3 or not parts[2].strip():
                continue
            try:
                ts = datetime.strptime(parts[1].strip(), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                out.append((ts, float(parts[2])))
            except ValueError:
                continue
        if out:
            out.sort()
            return out
        await asyncio.sleep(2.5)  # empty body -> likely throttled; back off once
    return []


def _temps_in_day(
    all_temps: list[tuple[datetime, float]], climate_date: str, offset: int
) -> list[tuple[datetime, float]]:
    start, end = timeutils.climate_day_bounds_utc(climate_date, offset)
    return [(ts, t) for (ts, t) in all_temps if start <= ts < end]


def _crossing(
    temps: list[tuple[datetime, float]], floor: Optional[float], cap: Optional[float],
    strike_type: str = "", margin_f: float = LOCKIN_MARGIN_F,
) -> tuple[Optional[datetime], Optional[str], float]:
    """First instant + side the running max makes settlement certain; plus that max.

    Delegates to :meth:`WeatherMarket.deterministic_lock_in` -- the exact rule the
    live engine fires on -- so this can never drift from production.

    ⚠ It used to re-derive the test inline as ``running >= floor`` for a tail-high
    bucket, ignoring ``strike_type``. Kalshi's tails are STRICT: a ``greater``
    bucket with floor F settles YES only at ``T >= F+1``. That bug invented 4 of
    the 8 "opportunities" in a 30-day run -- e.g. ``KXHIGHMIA-26JUL26-T94``
    (floor 94), where the CLI settled exactly 94 so the market resolved NO, but the
    backtest called it a certain YES at 4c. The live engine was always correct.

    ``margin_f`` is forwarded so the backtest fires on the SAME threshold the live
    engine does. Detection counts recorded before 2026-08-26 were taken at an
    effective margin of 0.0 and are not comparable.
    """
    market = WeatherMarket(
        ticker="", venue=Venue.KALSHI, icao="",
        floor_strike_f=floor, cap_strike_f=cap, strike_type=strike_type,
    )
    running = -1e9
    for ts, temp in temps:
        running = max(running, temp)
        det = market.deterministic_lock_in(running, margin_f=margin_f)
        if det is not None:
            side, _action = det
            return ts, side.value, running
    return None, None, (running if running > -1e9 else float("nan"))


def _dollars(value: object) -> Optional[float]:
    try:
        return float(value) if value is not None else None  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


async def _entry_cents(
    session: aiohttp.ClientSession, series: str, ticker: str,
    crossing: datetime, side: str,
) -> Optional[int]:
    cross_ts = int(crossing.timestamp())
    params = {"start_ts": cross_ts - 3600, "end_ts": cross_ts + 6 * 3600, "period_interval": 60}
    url = _KALSHI_CANDLES.format(series=series, ticker=ticker)
    data = await _kalshi_get(session, url, params)
    for c in data.get("candlesticks", []):
        if int(c.get("end_period_ts", 0)) < cross_ts:
            continue
        if side == "yes":
            ask = _dollars((c.get("yes_ask") or {}).get("close_dollars"))
            if ask is not None and ask > 0:
                return round(ask * 100)
        else:
            yes_bid = _dollars((c.get("yes_bid") or {}).get("close_dollars"))
            if yes_bid is not None:
                no_ask = round((1.0 - yes_bid) * 100)
                if no_ask > 0:
                    return no_ask
    return None


async def run_backtest(settings: Settings, *, days: int, size: int) -> dict[str, object]:
    session = aiohttp.ClientSession(headers={"User-Agent": settings.http_user_agent})
    today = timeutils.utc_now().date()
    cutoff = today.isoformat()
    day_from = (today - timedelta(days=days + 1)).isoformat()
    min_close_ts = int(timeutils.utc_now().timestamp()) - (days + 2) * 86400
    rows: list[dict[str, object]] = []
    detected = {"tradeable": 0, "priced_out": 0, "no_quote": 0}
    priced_out_rows: list[dict[str, object]] = []
    iem_cache: dict[str, list[tuple[datetime, float]]] = {}
    try:
        for series, icao in settings.weather_series:
            station = settings.station_by_icao(icao)
            if station is None:
                continue
            markets = await _settled_markets(session, series, min_close_ts)
            if icao not in iem_cache:
                iem_cache[icao] = await _iem_temps_all(session, icao, day_from, cutoff)
                await asyncio.sleep(0.8)  # be gentle with the IEM archive
            all_temps = iem_cache[icao]
            for m in markets:
                ticker = m.get("ticker")
                event_date = _parse_event_date(str(ticker or ""))
                if not ticker or not event_date or event_date >= cutoff:
                    continue
                if (_date_age_days(event_date, cutoff)) > days:
                    continue
                floor = _num(m.get("floor_strike"))
                cap = _num(m.get("cap_strike"))
                result = str(m.get("result", "")).lower()
                if result not in ("yes", "no") or (floor is None and cap is None):
                    continue
                temps = _temps_in_day(all_temps, event_date, station.standard_utc_offset_hours)
                if len(temps) < 5:
                    continue
                crossing, side, _ = _crossing(
                    temps, floor, cap, str(m.get("strike_type", "")),
                    margin_f=settings.risk.lockin_margin_f)
                if crossing is None or side is None:
                    continue
                entry = await _entry_cents(session, series, ticker, crossing, side)
                # Separate "never became certain" from "became certain too late to
                # be worth trading". The second is the real business question: by
                # the time the observation PROVES the outcome, has the market
                # already priced it? _CEILING is the live engine's own refusal
                # point (risk.lock_in_price_ceiling_cents).
                if entry is None:
                    detected["no_quote"] += 1
                    continue
                if entry >= _CEILING:
                    detected["priced_out"] += 1
                    priced_out_rows.append({"ticker": ticker, "date": event_date,
                                            "side": side, "entry_cents": entry})
                    continue
                detected["tradeable"] += 1
                # Settle against the OFFICIAL result, not the deterministic
                # assumption. This used to be `gross = (100 - entry) * size`
                # unconditionally, so a wrong call was still booked as a full win
                # and `matched_result` only ever fed the mismatch counter.
                won = (side == result)
                gross = (100 - entry) * size if won else -entry * size
                fee = fee_cents(entry, size)
                rows.append({
                    "ticker": ticker, "date": event_date, "side": side,
                    "entry_cents": entry, "matched_result": won,
                    "gross_usd": gross / 100.0, "fee_usd": fee / 100.0,
                    "net_usd": (gross - fee) / 100.0,
                })
    finally:
        await session.close()
    summary = _summarize(rows, size)
    summary["detected"] = detected
    summary["priced_out_rows"] = priced_out_rows
    return summary


def _summarize(rows: list[dict[str, object]], size: int) -> dict[str, object]:
    n = len(rows)
    net = sum(float(r["net_usd"]) for r in rows)
    gross = sum(float(r["gross_usd"]) for r in rows)
    fees = sum(float(r["fee_usd"]) for r in rows)
    entries = [int(r["entry_cents"]) for r in rows]
    mism = sum(1 for r in rows if not r["matched_result"])
    won_usd = sum(float(r["net_usd"]) for r in rows if r["matched_result"])
    lost_usd = sum(float(r["net_usd"]) for r in rows if not r["matched_result"])
    return {
        "opportunities": n, "size": size,
        "avg_entry_cents": (sum(entries) / n) if n else None,
        "avg_edge_per_contract_cents": (sum(100 - e for e in entries) / n) if n else None,
        "gross_usd": gross, "fees_usd": fees, "net_usd": net,
        "settlement_mismatches": mism, "rows": rows,
        "wins": n - mism, "won_usd": won_usd, "lost_usd": lost_usd,
        "net_per_opportunity": (net / n) if n else 0.0,
    }


def _num(v: object) -> Optional[float]:
    try:
        return float(v) if v is not None else None  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _date_age_days(date_str: str, today_str: str) -> int:
    d = datetime.strptime(date_str, "%Y-%m-%d")
    t = datetime.strptime(today_str, "%Y-%m-%d")
    return (t - d).days


def _render(s: dict[str, object]) -> str:
    n = int(s["opportunities"])
    lines = ["=" * 64, "  Strategy 1 (lock-in) backtest — deterministic edge", "=" * 64]
    lines.append(f"  lock-in opportunities found : {n}   (size assumed {s['size']} contracts)")
    if n:
        lines.append(f"  avg entry price             : {float(s['avg_entry_cents']):.1f}¢")
        lines.append(f"  avg edge per contract       : {float(s['avg_edge_per_contract_cents']):.1f}¢ "
                     f"(you buy this far below 100¢)")
        lines.append(f"  gross / fees / NET          : ${float(s['gross_usd']):,.2f} / "
                     f"${float(s['fees_usd']):,.2f} / ${float(s['net_usd']):,.2f}")
        lines.append(f"  net per opportunity         : ${float(s['net_per_opportunity']):,.2f}")
        lines.append(f"  correct / wrong calls       : {s['wins']} / {s['settlement_mismatches']}"
                     f"   (won ${float(s['won_usd']):,.2f}, lost ${float(s['lost_usd']):,.2f})")
        if s["settlement_mismatches"]:
            lines.append("  ⚠ a WRONG call is a REAL LOSS — settlement is supposed to be certain "
                         "here, so any nonzero count needs explaining before going live.")
    det = s.get("detected") or {}
    if det:
        total = det.get("tradeable", 0) + det.get("priced_out", 0) + det.get("no_quote", 0)
        lines.append("-" * 64)
        lines.append(f"  lock-ins DETECTED (certainty proved) : {total}")
        lines.append(f"    tradeable below the {_CEILING}c ceiling   : {det.get('tradeable', 0)}")
        lines.append(f"    already priced >= {_CEILING}c (no edge)  : {det.get('priced_out', 0)}"
                     f"   <-- market beat us to it")
        lines.append(f"    no quote at the crossing           : {det.get('no_quote', 0)}")
    if n:
        lines.append("-" * 64)
        for r in sorted(s["rows"], key=lambda x: str(x["date"]))[:20]:
            flag = "" if r["matched_result"] else "  <-- WRONG CALL (real loss)"
            lines.append(f"    {r['date']}  {r['ticker']:<26} buy {str(r['side']).upper():<3} "
                         f"@ {int(r['entry_cents'])}¢  net ${float(r['net_usd']):.2f}{flag}")
    else:
        lines.append("  (no deterministic lock-in opportunities in the window — try more --days)")
    lines.append("=" * 64)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Strategy 2 (model edge) backtest -- needs archived forecasts + settled results
# --------------------------------------------------------------------------- #
def _load_snapshots(db_path: str, cutoff: str,
                    grid_fixed: bool = False) -> dict[tuple[str, str], list[dict]]:
    """``(ticker, model) -> time-ordered [{created_at, prob, date}]`` for past days.

    Prefers the ``model_prediction_history`` lead-time series so the backtest can
    enter at the first snapshot an edge appears; for any (ticker, model) with no
    history rows (e.g. data logged before that table existed) it falls back to the
    single latest view in ``model_predictions``. Official YES/NO is pulled from
    Kalshi later (not the sparse local ``market_results`` table).

    With ``grid_fixed`` the history series reads ``prob_grid_fixed`` -- the same
    rows repriced on the corrected bucket grid by
    ``scripts/backfill_grid_probs.py``. Every stored ``prob`` predates the
    2026-08-23 grid fix, so this is the only way to backtest the fix without
    waiting for fresh data. Rows the backfill could not validate are NULL and are
    skipped, and the ``model_predictions`` fallback is dropped (it was never
    backfilled), so the two modes cover slightly different rows -- compare the
    printed trade counts before reading anything into a P&L difference."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    snaps: dict[tuple[str, str], list[dict]] = {}
    prob_col = "prob_grid_fixed" if grid_fixed else "prob"
    try:
        try:
            hist = conn.execute(
                f"""SELECT ticker, model, {prob_col} AS prob, climate_date AS date,
                          created_at_utc AS created_at
                   FROM model_prediction_history
                   WHERE climate_date < ? AND {prob_col} IS NOT NULL
                   ORDER BY created_at_utc ASC""",
                (cutoff,),
            ).fetchall()
        except sqlite3.OperationalError:
            hist = []          # table (or the backfilled column) not present
        for r in hist:
            snaps.setdefault((r["ticker"], r["model"]), []).append(dict(r))
        if not grid_fixed:
            for r in conn.execute(
                """SELECT ticker, model, prob, climate_date AS date, created_at_utc AS created_at
                   FROM model_predictions WHERE climate_date < ?""",
                (cutoff,),
            ):
                key = (r["ticker"], r["model"])
                if key not in snaps:                   # no lead-time series -> last view
                    snaps[key] = [dict(r)]
    finally:
        conn.close()
    return snaps


class Candle(NamedTuple):
    """One candlestick period, carrying both the *quote* at close and the period's
    *extremes* -- the latter are what a resting-order fill simulation needs.

    ``yes_ask``/``no_ask`` are the taker prices (``no_ask = 100 - yes_bid``) and are
    the only fields the taker backtest reads. ``ask_low``/``bid_high`` are how far
    the quote travelled, ``trade_low``/``trade_high`` the extremes of *executed*
    trades, and ``volume`` the contracts that actually changed hands. A resting bid
    is filled by a trade, not by a quote, so the honest fill tests use the trade
    extremes; the quote extremes only bound the optimistic case.
    """

    ts: int
    yes_ask: Optional[int]
    no_ask: Optional[int]
    yes_bid: Optional[int] = None
    ask_low: Optional[int] = None
    bid_high: Optional[int] = None
    trade_low: Optional[int] = None
    trade_high: Optional[int] = None
    volume: float = 0.0


def _cents(node: object, key: str) -> Optional[int]:
    """A ``{key}_dollars`` field of a candle sub-object as whole cents, or ``None``."""
    if not isinstance(node, dict):
        return None
    d = _dollars(node.get(key))
    return round(d * 100) if d is not None else None


async def _candle_series(session: aiohttp.ClientSession, series: str, ticker: str,
                         start_ts: int, end_ts: int, *, period_minutes: int = 1,
                         ) -> list[Candle]:
    """Fetch a ticker's candle series over the (padded) ``[start_ts, end_ts]`` window
    once, sorted by time. Bounded to the market's own life so we stay well under
    Kalshi's candlestick period cap.

    ``period_minutes`` is Kalshi's ``period_interval``, which is in **minutes** --
    the 60 this used to pass was hourly, not the 1-minute series the docstring
    claimed. Minute bars matter for the passive simulation: predictions land every
    30 minutes, so hour bars cannot say whether a resting order filled before or
    after it was repriced.
    """
    start = min(start_ts, end_ts) - 3600
    end = max(start_ts, end_ts) + 6 * 3600
    span_cap = 5 * 86400 if period_minutes >= 60 else 3 * 86400
    if end - start > span_cap:               # safety clamp under the period cap
        start = end - span_cap
    params = {"start_ts": start, "end_ts": end, "period_interval": period_minutes}
    data = await _kalshi_get(session, _KALSHI_CANDLES.format(series=series, ticker=ticker), params)
    out: list[Candle] = []
    for c in data.get("candlesticks", []):
        ask, bid, price = c.get("yes_ask"), c.get("yes_bid"), c.get("price")
        yes_ask = _cents(ask, "close_dollars")
        yes_bid = _cents(bid, "close_dollars")
        no_ask = (100 - yes_bid) if yes_bid is not None else None
        out.append(Candle(
            ts=int(c.get("end_period_ts", 0)),
            yes_ask=yes_ask if (yes_ask and yes_ask > 0) else None,
            no_ask=no_ask if (no_ask and no_ask > 0) else None,
            yes_bid=yes_bid,
            ask_low=_cents(ask, "low_dollars"),
            bid_high=_cents(bid, "high_dollars"),
            trade_low=_cents(price, "low_dollars"),
            trade_high=_cents(price, "high_dollars"),
            volume=_dollars(c.get("volume_fp")) or 0.0,
        ))
    out.sort(key=lambda c: c.ts)
    return out


def _candle_index_at(candles: list[Candle], ts: int) -> Optional[int]:
    """Index of the first candle at/after ``ts``, the last one if ``ts`` is beyond
    the series, or ``None`` for an empty series."""
    for i, c in enumerate(candles):
        if c.ts >= ts:
            return i
    return len(candles) - 1 if candles else None


def _price_at(candles: list[Candle], ts: int) -> tuple[Optional[int], Optional[int]]:
    """(yes_ask, no_ask) from the first candle at/after ``ts`` (the fillable price a
    prediction made at ``ts`` would face), or the last candle if ``ts`` is beyond."""
    i = _candle_index_at(candles, ts)
    return (candles[i].yes_ask, candles[i].no_ask) if i is not None else (None, None)


def first_qualifying_edge(
    snapshots: list[dict], candles: list[tuple[int, Optional[int], Optional[int]]],
    threshold: float, coeff: float,
    calibration: Optional["CalibrationMap"] = None, min_price_cents: int = 0,
    threshold_fn: Optional[Callable[[int], float]] = None,
    persist_seconds: float = 0.0,
) -> Optional[tuple[str, int, str, float]]:
    """Walk ``snapshots`` oldest-first; return ``(side, entry_cents, created_at,
    side_prob)`` at the first snapshot whose edge clears the fee-adjusted threshold
    at that time's market price, else ``None``. Models the realistic 'enter when
    the edge appears'. ``side_prob`` is the (possibly calibrated) probability of
    the side actually bought -- the calibration-fitting target for a later refit.

    ``threshold_fn`` (epoch seconds -> threshold) replaces the flat ``threshold``
    with the live time-decay rule; ``persist_seconds`` additionally requires the
    edge to have been present on the SAME side for that long before entering,
    mirroring ``ArbitrageEngine._edge_persisted``. Both default to today's
    behavior.

    ⚠ Persistence is only as fine-grained as the snapshot series, which is ~30 min
    (78.9% of gaps are exactly 30 min). Live the engine re-evaluates every 120 s
    and would reset the timer on a mid-window flicker this cannot see, so a
    backtested persistence gate is the OPTIMISTIC bound on the live one."""
    first_seen_ts: Optional[int] = None
    first_seen_side: Optional[str] = None
    for snap in snapshots:
        try:
            ts = int(timeutils.parse_isoformat_utc(str(snap["created_at"])).timestamp())
        except (ValueError, TypeError):
            continue
        yes_ask, no_ask = _price_at(candles, ts)
        thr = threshold_fn(ts) if threshold_fn is not None else threshold
        trade = _edge_trade(float(snap["prob"]), yes_ask, no_ask, thr, coeff,
                            calibration=calibration, min_price_cents=min_price_cents)
        if trade is None:                       # edge gone -> restart the clock
            first_seen_ts = first_seen_side = None
            continue
        side, entry, side_prob = trade
        if persist_seconds > 0.0:
            if first_seen_side != side:         # new side -> restart the clock
                first_seen_ts, first_seen_side = ts, side
                continue
            if ts - (first_seen_ts or ts) < persist_seconds:
                continue
        return side, entry, str(snap["created_at"]), side_prob
    return None


def _edge_trade(prob: float, yes_ask: Optional[int], no_ask: Optional[int],
                threshold: float, coeff: float,
                calibration: Optional["CalibrationMap"] = None, min_price_cents: int = 0,
                ) -> Optional[tuple[str, int, float]]:
    """Pick the fee-adjusted model-edge trade, mirroring the live Strategy-2 test.

    Buy YES when ``prob - yes_ask`` clears ``threshold + fee``; buy NO when
    ``(1-prob) - no_ask`` clears it. Returns ``(side, entry_cents, side_prob)`` or
    ``None``. ``side_prob`` is the (possibly calibrated) probability actually used
    for that side's edge check -- the trustworthy quantity for later calibration
    fitting, as opposed to the raw model ``prob``.

    With ``calibration`` given, the raw probability for each side is passed through
    :meth:`CalibrationMap.apply` *before* the edge check -- this is the diagnosed
    fix for the models' tail overconfidence (see module docstring in
    :mod:`calibration`): a claim the raw model rates 30% but that empirically wins
    ~2% of the time gets recalibrated down and no longer clears the threshold.
    ``min_price_cents`` additionally refuses to trade below a price floor regardless
    of edge, as cheap defense-in-depth against exactly that failure mode.
    """
    best: Optional[tuple[str, int, float]] = None
    best_edge = 0.0
    yes_prob = calibration.apply(prob) if calibration else prob
    no_prob = calibration.apply(1.0 - prob) if calibration else (1.0 - prob)
    if yes_ask is not None and yes_ask >= min_price_cents:
        edge = yes_prob - yes_ask / 100.0
        if edge >= threshold + fee_fraction_per_contract(yes_ask, coeff) and edge > best_edge:
            best, best_edge = ("yes", yes_ask, yes_prob), edge
    if no_ask is not None and no_ask >= min_price_cents:
        edge = no_prob - no_ask / 100.0
        if edge >= threshold + fee_fraction_per_contract(no_ask, coeff) and edge > best_edge:
            best, best_edge = ("no", no_ask, no_prob), edge
    return best


def _date_window(days: Optional[int], start_date: Optional[str],
                 end_date: Optional[str]) -> tuple[str, str]:
    """Resolve a ``[start_date, end_date)`` window from either an explicit range
    or a trailing ``days`` count ending today."""
    end = end_date or timeutils.utc_now().date().isoformat()
    if start_date:
        return start_date, end
    if days is None:
        raise ValueError("run_model_backtest needs either days or start_date")
    start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=days)).date().isoformat()
    return start, end


def _snaps_in_window(all_snaps: dict[tuple[str, str], list[dict]],
                     start_date: str, end_date: str) -> dict[tuple[str, str], list[dict]]:
    return {
        k: v for k, v in all_snaps.items()
        if v and v[0].get("date") and start_date <= str(v[0]["date"]) < end_date
    }


async def _fetch_market_data(
    session: aiohttp.ClientSession, snaps: dict[tuple[str, str], list[dict]], days_span: int,
    *, period_minutes: int = 1,
) -> tuple[dict[str, str], dict[str, list]]:
    """Fetch, once, everything the simulation needs for ``snaps``' tickers:
    official YES/NO per ticker (from Kalshi settled markets, not the sparse local
    ``market_results`` table) and one bounded candle series per ticker."""
    tickers = sorted({t for (t, _m) in snaps})
    min_close_ts = int(timeutils.utc_now().timestamp()) - (days_span + 2) * 86400
    results: dict[str, str] = {}
    for series in sorted({t.split("-")[0] for t in tickers}):
        for m in await _settled_markets(session, series, min_close_ts):
            tk, res = m.get("ticker"), str(m.get("result", "")).lower()
            if tk and res in ("yes", "no"):
                results[str(tk)] = res

    ticker_span: dict[str, tuple[int, int]] = {}
    for (ticker, _model), snapshots in snaps.items():
        for s in snapshots:
            try:
                ts = int(timeutils.parse_isoformat_utc(str(s["created_at"])).timestamp())
            except (ValueError, TypeError):
                continue
            lo, hi = ticker_span.get(ticker, (ts, ts))
            ticker_span[ticker] = (min(lo, ts), max(hi, ts))

    candle_cache: dict[str, list] = {}
    for ticker, (lo, hi) in ticker_span.items():
        if ticker in results:
            candle_cache[ticker] = await _candle_series(session, ticker.split("-")[0], ticker,
                                                       lo, hi, period_minutes=period_minutes)
    return results, candle_cache


def _iso_ts(ts: int) -> str:
    """UTC ISO-8601 for a candle's epoch second."""
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _spread_at(candles: list[Candle], ts: int) -> Optional[int]:
    """``yes_ask - yes_bid`` in cents at/after ``ts``, or ``None`` when either side
    of the book is missing. A zero bid is *not* a 0c bid, it is an empty bid side,
    so it is excluded rather than counted as a huge spread."""
    i = _candle_index_at(candles, ts)
    if i is None:
        return None
    c = candles[i]
    if c.yes_ask is None or c.yes_bid is None or c.yes_bid < 1:
        return None
    return c.yes_ask - c.yes_bid


def _trade_row(ticker: str, date: object, side: str, entry_cents: int, entered_at: str,
               side_prob: float, result: str, size: int, *, spread_cents: Optional[int] = None,
               execution: str = "taker", filled_at: Optional[str] = None) -> dict:
    """One settled paper trade, net of the fee its entry price incurs."""
    won = (side == result)
    fee = fee_cents(entry_cents, size)
    payout = (100 if won else 0) * size
    return {
        "ticker": ticker, "date": date, "side": side, "entry_cents": entry_cents,
        "entered_at": entered_at, "prob": side_prob, "won": won,
        "net_usd": (payout - entry_cents * size - fee) / 100.0, "fee_usd": fee / 100.0,
        "spread_cents": spread_cents, "execution": execution, "filled_at": filled_at,
    }


def _simulate(
    snaps: dict[tuple[str, str], list[dict]], results: dict[str, str],
    candle_cache: dict[str, list], threshold: float, coeff: float, size: int, *,
    calibration: Optional[dict[str, CalibrationMap]] = None, min_price_cents: int = 0,
    threshold_fns: Optional[dict[str, Callable[[int], float]]] = None,
    persist_seconds: float = 0.0,
) -> tuple[dict[str, list[dict]], int]:
    """Pure simulation over already-fetched ``results``/``candle_cache`` (no network):
    for each (ticker, model), enter at the first qualifying edge and settle. Returns
    (per_model trade rows, markets evaluated). Reused for train/test/calibrated runs
    against the same fetched data, so honest A/B comparisons cost one fetch, not two.

    This is the **taker** baseline: every entry crosses the spread, paying the ask
    for YES or hitting the bid to buy NO. Each row carries the spread it crossed,
    which is the whole prize :func:`_simulate_passive` is playing for."""
    per_model: dict[str, list[dict]] = {}
    n_eval = 0
    for (ticker, model), snapshots in snaps.items():
        if ticker not in results or ticker not in candle_cache:
            continue
        n_eval += 1
        cal = calibration.get(model) if calibration else None
        candles = candle_cache[ticker]
        trade = first_qualifying_edge(
            snapshots, candles, threshold, coeff,
            calibration=cal, min_price_cents=min_price_cents,
            threshold_fn=(threshold_fns or {}).get(ticker),
            persist_seconds=persist_seconds)
        if trade is None:
            continue
        side, entry, entered_at, side_prob = trade
        spread = None
        try:
            spread = _spread_at(candles, int(timeutils.parse_isoformat_utc(entered_at).timestamp()))
        except (ValueError, TypeError):
            pass
        per_model.setdefault(model, []).append(
            _trade_row(ticker, snapshots[0]["date"], side, entry, entered_at, side_prob,
                       results[ticker], size, spread_cents=spread, execution="taker"))
    return per_model, n_eval


# --------------------------------------------------------------------------- #
# Passive (maker) placement -- does resting inside the spread beat crossing it?
# --------------------------------------------------------------------------- #
FILL_MODELS = ("touch", "trade", "through")


def _passive_fill_index(candles: list[Candle], lo: int, hi: int, side: str,
                        yes_price: int, fill_model: str, size: int) -> Optional[int]:
    """Index of the first candle in ``[lo, hi]`` at which a resting order at
    ``yes_price`` (YES terms) would have been filled, else ``None``.

    A resting order is not a trade. It fills only if the market comes to it, so the
    whole credibility of a passive backtest lives in this function -- assume fills
    and you manufacture a fake win. Three deliberately different standards of proof
    are offered so the answer is read as a bracket, not a point:

    ``touch``    quote-based: the best *offer* came down to our bid. A quote is not
                 an execution and a 1-lot quote is not 100 lots, so this is the
                 weakest evidence -- but note it is NOT simply the loosest of the
                 three. A print can happen at the bid without the offer ever
                 reaching us, so ``touch`` and ``trade`` overlap rather than nest,
                 and each catches fills the other misses.
    ``trade``    print-based: a trade actually printed at a price our order would
                 have taken. Someone did transact there and we were the
                 better-priced side of the book, so we would have been hit first.
                 The default.
    ``through``  print-based and strictly stronger: a trade printed strictly *past*
                 our price **and** the period turned over at least our own ``size``.
                 We fill even from the back of the queue, with the depth to fill the
                 whole order. A strict subset of ``trade``; the conservative bound.
    """
    for i in range(max(lo, 0), min(hi, len(candles) - 1) + 1):
        c = candles[i]
        if side == "yes":                       # resting BUY YES at yes_price
            if fill_model == "touch":
                if c.ask_low is not None and c.ask_low <= yes_price:
                    return i
            elif fill_model == "trade":
                if c.volume > 0 and c.trade_low is not None and c.trade_low <= yes_price:
                    return i
            elif c.volume >= size and c.trade_low is not None and c.trade_low < yes_price:
                return i
        else:                                   # resting SELL YES at yes_price (= buy NO)
            if fill_model == "touch":
                if c.bid_high is not None and c.bid_high >= yes_price:
                    return i
            elif fill_model == "trade":
                if c.volume > 0 and c.trade_high is not None and c.trade_high >= yes_price:
                    return i
            elif c.volume >= size and c.trade_high is not None and c.trade_high > yes_price:
                return i
    return None


def _passive_quote(prob: float, threshold: float, coeff: float, yes_bid: Optional[int],
                   yes_ask: Optional[int], improve: int,
                   calibration: Optional[CalibrationMap] = None, min_price_cents: int = 0,
                   ) -> Optional[tuple[str, int, float]]:
    """The resting order the live engine would place: ``(side, yes_price, side_prob)``.

    Mirrors the passive branch of ``ArbitrageEngine._model_edge_order`` -- priced off
    the shared :func:`edge_limit_prices` rule, improving the near side by ``improve``
    ticks without crossing, refusing a price under the floor. ``yes_price`` is always
    in YES terms: a ``no`` order is a resting SELL of YES there, costing
    ``100 - yes_price`` per NO contract. Ties go to the larger model edge."""
    buy_limit, sell_limit, yes_prob, no_prob = edge_limit_prices(prob, threshold, coeff,
                                                                calibration)
    best: Optional[tuple[str, int, float]] = None
    best_edge = 0.0
    if buy_limit >= 1:
        p = passive_buy_price(buy_limit, yes_bid, yes_ask, improve)
        if p is not None and p >= min_price_cents and (yes_prob - p / 100.0) > best_edge:
            best, best_edge = ("yes", p, yes_prob), yes_prob - p / 100.0
    if sell_limit <= 99:
        p = passive_sell_price(sell_limit, yes_bid, yes_ask, improve)
        if p is not None and (100 - p) >= min_price_cents and (no_prob - (100 - p) / 100.0) > best_edge:
            best, best_edge = ("no", p, no_prob), no_prob - (100 - p) / 100.0
    return best


def _simulate_passive(
    snaps: dict[tuple[str, str], list[dict]], results: dict[str, str],
    candle_cache: dict[str, list], threshold: float, coeff: float, size: int, *,
    fill_model: str = "trade", improve: int = 1, cross: bool = True,
    calibration: Optional[dict[str, CalibrationMap]] = None, min_price_cents: int = 0,
) -> tuple[dict[str, list[dict]], int, dict[str, int]]:
    """Simulate Strategy 2 placing **resting** orders instead of crossing the spread.

    ``cross=True`` reproduces the live ``WA_ENABLE_PASSIVE_MODEL_EDGE`` flag exactly:
    the engine's passive branch is an ``elif``, so it still takes whatever is already
    inside its limit and only rests when it is not -- passive placement there *adds*
    trades the taker rule skipped, it does not make the existing ones cheaper.
    ``cross=False`` is the pure-maker alternative that flag does not implement: never
    cross, rest every edge, trade only what gets filled.

    Order lifecycle follows the live engine, including its quirks: a resting order is
    replaced only when a *different* price is computed, and an evaluation that
    produces no order at all leaves the previous one working (``_evaluate_model_edge``
    returns before reconciling). That last one is adverse to us -- it can fill on an
    edge that has since evaporated -- so simulating it is the conservative choice.

    Fills are only ever credited to candles strictly *after* the bar the order was
    priced off, so an order can never be filled by a print that preceded it.
    Returns (per_model rows, markets evaluated, fill statistics)."""
    per_model: dict[str, list[dict]] = {}
    n_eval = 0
    stats = {"rested": 0, "filled": 0, "taken": 0, "unfilled": 0}
    for (ticker, model), snapshots in snaps.items():
        if ticker not in results or ticker not in candle_cache:
            continue
        candles = candle_cache[ticker]
        if not candles:
            continue
        n_eval += 1
        cal = calibration.get(model) if calibration else None
        result, date = results[ticker], snapshots[0]["date"]
        row: Optional[dict] = None
        rest: Optional[tuple[str, int, float, str]] = None   # side, yes_price, prob, placed_at
        scan_from: Optional[int] = None
        ever_rested = False

        for snap in snapshots:
            try:
                ts = int(timeutils.parse_isoformat_utc(str(snap["created_at"])).timestamp())
            except (ValueError, TypeError):
                continue
            i = _candle_index_at(candles, ts)
            if i is None:
                continue
            # 1. Did the order resting since the previous decision fill before this one?
            if rest is not None and scan_from is not None:
                hit = _passive_fill_index(candles, scan_from, i - 1, rest[0], rest[1],
                                          fill_model, size)
                if hit is not None:
                    side, yes_price, prob_at_rest, placed_at = rest
                    cost = yes_price if side == "yes" else 100 - yes_price
                    row = _trade_row(ticker, date, side, cost, placed_at, prob_at_rest,
                                     result, size, execution="passive",
                                     filled_at=_iso_ts(candles[hit].ts))
                    break
            c = candles[i]
            prob = float(snap["prob"])
            # 2. The live engine never rests what it can already cross.
            if cross:
                trade = _edge_trade(prob, c.yes_ask, c.no_ask, threshold, coeff,
                                    calibration=cal, min_price_cents=min_price_cents)
                if trade is not None:
                    side, entry, side_prob = trade
                    row = _trade_row(ticker, date, side, entry, str(snap["created_at"]),
                                     side_prob, result, size,
                                     spread_cents=_spread_at(candles, ts), execution="taker")
                    stats["taken"] += 1
                    break
            # 3. Otherwise (re)price the resting order; an evaluation with no order
            #    leaves the previous one working, exactly as live does.
            quote = _passive_quote(prob, threshold, coeff, c.yes_bid, c.yes_ask, improve,
                                   calibration=cal, min_price_cents=min_price_cents)
            if quote is not None:
                side, yes_price, side_prob = quote
                rest = (side, yes_price, side_prob, str(snap["created_at"]))
                ever_rested = True
            if rest is not None:
                scan_from = i + 1

        if row is None and rest is not None and scan_from is not None:
            hit = _passive_fill_index(candles, scan_from, len(candles) - 1, rest[0], rest[1],
                                      fill_model, size)
            if hit is not None:
                side, yes_price, prob_at_rest, placed_at = rest
                cost = yes_price if side == "yes" else 100 - yes_price
                row = _trade_row(ticker, date, side, cost, placed_at, prob_at_rest, result,
                                 size, execution="passive",
                                 filled_at=_iso_ts(candles[hit].ts))

        if ever_rested:
            stats["rested"] += 1
            if row is not None and row["execution"] == "passive":
                stats["filled"] += 1
            elif row is None:
                stats["unfilled"] += 1
        if row is not None:
            per_model.setdefault(model, []).append(row)
    return per_model, n_eval, stats


def _exec_stats(rows: list[dict]) -> dict[str, float]:
    """Pooled execution stats for a set of trade rows."""
    n = len(rows)
    net = sum(float(r["net_usd"]) for r in rows)
    return {
        "trades": n,
        "net_usd": net,
        "per_trade": (net / n) if n else 0.0,
        "hit_rate": (sum(1 for r in rows if r["won"]) / n) if n else 0.0,
        "mean_entry_cents": (sum(int(r["entry_cents"]) for r in rows) / n) if n else 0.0,
        "fees_usd": sum(float(r["fee_usd"]) for r in rows),
    }


def _pool(per_model: dict[str, list[dict]]) -> list[dict]:
    return [r for rows in per_model.values() for r in rows]


SPREAD_BANDS = ((1, 10), (11, 25), (26, 50), (51, 99))


def _spread_census(per_model: dict[str, list[dict]], size: int, improve: int) -> dict[str, object]:
    """What crossing the spread actually costs on the markets Strategy 2 enters.

    This bounds the prize before any passive code is worth writing: a resting order
    can capture at most the spread minus the tick it improves by, and only on the
    trades it actually gets filled on. Entries where one side of the book was empty
    are excluded rather than counted as an enormous spread.
    """
    rows = _pool(per_model)
    priced = [r for r in rows if r.get("spread_cents") is not None]

    def band_stats(rs: list[dict]) -> dict[str, float]:
        n = len(rs)
        if not n:
            return {"n": 0, "mean_spread": 0.0, "mean_entry": 0.0, "mean_fee_usd": 0.0,
                    "max_saving_usd": 0.0}
        spread = sum(int(r["spread_cents"]) for r in rs) / n
        return {
            "n": n,
            "mean_spread": spread,
            "mean_entry": sum(int(r["entry_cents"]) for r in rs) / n,
            "mean_fee_usd": sum(float(r["fee_usd"]) for r in rs) / n,
            "max_saving_usd": max(0.0, spread - improve) * size / 100.0,
        }

    spreads = sorted(int(r["spread_cents"]) for r in priced)
    return {
        "entries": len(rows),
        "priced": len(priced),
        "unpriced": len(rows) - len(priced),
        "median_spread": float(spreads[len(spreads) // 2]) if spreads else 0.0,
        "overall": band_stats(priced),
        "bands": [
            {"lo": lo, "hi": hi,
             **band_stats([r for r in priced if lo <= int(r["entry_cents"]) <= hi])}
            for lo, hi in SPREAD_BANDS
        ],
    }


def _matched_pairs(taker: dict[str, list[dict]], passive: dict[str, list[dict]]
                   ) -> dict[str, object]:
    """Apples-to-apples taker-vs-passive on the (ticker, model) pairs **both** traded.

    Passive trades less than taker does -- a resting order that never fills is not a
    trade. Comparing pooled totals therefore rewards passive for its non-fills, which
    on a net-negative strategy looks exactly like skill. Only the intersection is a
    real comparison; ``capture_rate`` is how much of the taker book passive actually
    got, and the unmatched buckets say what each side traded alone."""
    t = {(r["ticker"], m): r for m, rows in taker.items() for r in rows}
    p = {(r["ticker"], m): r for m, rows in passive.items() for r in rows}
    both = sorted(set(t) & set(p))
    same = [k for k in both if t[k]["side"] == p[k]["side"]]
    flip = [k for k in both if t[k]["side"] != p[k]["side"]]
    ts, ps = _exec_stats([t[k] for k in both]), _exec_stats([p[k] for k in both])
    t_same, p_same = _exec_stats([t[k] for k in same]), _exec_stats([p[k] for k in same])
    t_flip, p_flip = _exec_stats([t[k] for k in flip]), _exec_stats([p[k] for k in flip])
    return {
        "n": len(both),
        "same_side": len(same),
        "taker": ts,
        "passive": ps,
        "delta_per_trade": ps["per_trade"] - ts["per_trade"],
        "same_side_n": len(same),
        "same_side_taker": t_same,
        "same_side_passive": p_same,
        "same_side_delta_per_trade": p_same["per_trade"] - t_same["per_trade"],
        "same_side_entry_delta": p_same["mean_entry_cents"] - t_same["mean_entry_cents"],
        "flip_n": len(flip),
        "flip_taker": t_flip,
        "flip_passive": p_flip,
        "flip_delta_per_trade": p_flip["per_trade"] - t_flip["per_trade"],
        "capture_rate": (len(both) / len(t)) if t else 0.0,
        "taker_only": _exec_stats([t[k] for k in sorted(set(t) - set(p))]),
        "passive_only": _exec_stats([p[k] for k in sorted(set(p) - set(t))]),
    }


DEFAULT_IMPROVE_GRID = (0, 1, 2, 3)


async def run_passive_backtest(
    settings: Settings, *, days: int, size: int, train_frac: float = 0.6,
    improve: int = 1, min_price_cents: int = 0,
    fill_models: tuple[str, ...] = FILL_MODELS,
    improve_grid: tuple[int, ...] = DEFAULT_IMPROVE_GRID,
    grid_fixed: bool = False,
) -> dict[str, object]:
    """Taker vs passive (resting) placement, on the chronological split the other
    honest tests use.

    The question is whether Strategy 2 should stop crossing the spread. Answering
    it needs three things this returns together: the **prize** (what the spread
    actually costs on the markets we enter -- an upper bound before any code is
    worth writing), the **execution effect** (on the same market, same side, same
    settlement -- the only apples-to-apples comparison), and the **selection
    effect** (what each execution traded alone, where a passive non-fill on a
    losing strategy masquerades as skill).

    Nothing is fitted, so the train/test split plays the role the ``--sweep``
    fixed-threshold panel does: a result present in BOTH windows is structural, one
    present only in TEST is the favorable-regime trap this repo has already fallen
    into once. ``improve_grid`` sweeps the one tunable knob
    (``WA_PASSIVE_IMPROVE_TICKS``) so the verdict is a surface, not a point.

    Two passive variants, because they are different propositions: ``add`` is what
    ``WA_ENABLE_PASSIVE_MODEL_EDGE`` does today (its passive branch is an ``elif``,
    so it still crosses whatever is inside its limit and only rests what it could
    not take -- it *adds* trades rather than making existing ones cheaper), and
    ``only`` is the pure-maker strategy of never crossing, which that flag does not
    implement.
    """
    end = timeutils.utc_now().date().isoformat()
    start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=days)).date().isoformat()
    split = (datetime.strptime(start, "%Y-%m-%d")
             + timedelta(days=round(days * train_frac))).date().isoformat()
    threshold = settings.risk.model_edge_threshold
    coeff = settings.kalshi_fee_coefficient

    all_snaps = _load_snapshots(settings.db_path, end, grid_fixed=grid_fixed)
    full_snaps = _snaps_in_window(all_snaps, start, end)
    windows = {"train": _snaps_in_window(all_snaps, start, split),
               "test": _snaps_in_window(all_snaps, split, end)}
    grid = tuple(sorted({improve, *improve_grid}))   # always includes the primary

    session = aiohttp.ClientSession(headers={"User-Agent": settings.http_user_agent})
    try:
        results, candles = await _fetch_market_data(session, full_snaps, days)
    finally:
        await session.close()

    full_taker, n_eval = _simulate(full_snaps, results, candles, threshold, coeff, size,
                                   min_price_cents=min_price_cents)
    taker_per, taker = {}, {}
    for name, snaps in windows.items():
        per, _ = _simulate(snaps, results, candles, threshold, coeff, size,
                           min_price_cents=min_price_cents)
        taker_per[name] = per
        taker[name] = _exec_stats(_pool(per))

    # One simulation per (fill model, mode, improve, window) -- free, no network.
    sims: dict[tuple, tuple[dict, dict]] = {}
    for fm in fill_models:
        for mode, cross in (("add", True), ("only", False)):
            for imp in grid:
                for name, snaps in windows.items():
                    per, _n, fills = _simulate_passive(
                        snaps, results, candles, threshold, coeff, size, fill_model=fm,
                        improve=imp, cross=cross, min_price_cents=min_price_cents)
                    sims[(fm, mode, imp, name)] = (per, fills)

    pooled, execution, selection = [], [], []
    for fm in fill_models:
        for mode in ("add", "only"):
            entry: dict[str, object] = {"fill_model": fm, "mode": mode}
            for name in windows:
                per, fills = sims[(fm, mode, improve, name)]
                entry[name] = _exec_stats(_pool(per))
                entry[f"{name}_fills"] = fills
            pooled.append(entry)
            for imp in grid:
                row: dict[str, object] = {"fill_model": fm, "mode": mode, "improve": imp}
                for name in windows:
                    row[name] = _matched_pairs(taker_per[name], sims[(fm, mode, imp, name)][0])
                execution.append(row)
            sel: dict[str, object] = {"fill_model": fm, "mode": mode, "improve": improve}
            for name in windows:
                sel[name] = _matched_pairs(taker_per[name], sims[(fm, mode, improve, name)][0])
            selection.append(sel)

    return {
        "train_window": [start, split], "test_window": [split, end], "days": days,
        "size": size, "improve_ticks": improve, "improve_grid": list(grid),
        "threshold": threshold, "min_price_cents": min_price_cents,
        "predictions_scanned": n_eval,
        "spread": _spread_census(full_taker, size, improve),
        "taker": taker, "pooled": pooled, "execution": execution, "selection": selection,
    }


def _render_passive(s: dict[str, object]) -> str:
    tw, ew = s["train_window"], s["test_window"]      # type: ignore[index]
    sp = s["spread"]                                  # type: ignore[assignment]
    ov = sp["overall"]
    size, improve = s["size"], s["improve_ticks"]
    lines = ["=" * 88,
             "  Strategy 2 — taker vs passive (resting) placement, out-of-sample split",
             "=" * 88,
             f"  train {tw[0]} .. {tw[1]}    test {ew[0]} .. {ew[1]}    "
             f"size {size}, threshold {s['threshold']:.0%}",
             "",
             "  1. THE PRIZE — what crossing costs on the markets Strategy 2 actually enters"]
    if not ov["n"]:
        lines.append("     (no taker entries with a two-sided book in the window)")
    else:
        lines.append(f"     entries {sp['entries']}   two-sided book {sp['priced']}"
                     f"   (excluded, one side empty: {sp['unpriced']})")
        lines.append(f"     mean spread {ov['mean_spread']:.2f}c   median {sp['median_spread']:.0f}c"
                     f"   mean entry {ov['mean_entry']:.1f}c")
        lines.append(f"     mean fee ${ov['mean_fee_usd']:.2f}/trade   vs  max spread capture "
                     f"${ov['max_saving_usd']:.2f}/trade  (spread - {improve} tick, {size} contracts)")
        lines.append(f"     {'entry band':>12}{'n':>7}{'spread':>9}{'mean entry':>12}"
                     f"{'fee $':>9}{'max capture $':>15}")
        for b in sp["bands"]:
            if b["n"]:
                band = f"{b['lo']}-{b['hi']}c"
                lines.append(f"     {band:>12}{b['n']:>7}{b['mean_spread']:>9.2f}"
                             f"{b['mean_entry']:>12.1f}{b['mean_fee_usd']:>9.2f}"
                             f"{b['max_saving_usd']:>15.2f}")
        lines.append("     Max capture is a ceiling: it assumes every resting order fills.")

    lines.append("")
    lines.append(f"  2. POOLED P&L — all models, improve={improve}. Contaminated by trade count;")
    lines.append("     on a losing strategy, filling less lowers total loss regardless of skill.")
    lines.append(f"     {'execution':>18}{'TRAIN trades':>14}{'TRAIN/trade':>13}"
                 f"{'TEST trades':>13}{'TEST/trade':>12}")
    tk = s["taker"]                                   # type: ignore[assignment]
    lines.append(f"     {'taker (live)':>18}{tk['train']['trades']:>14}"
                 f"{tk['train']['per_trade']:>13.2f}{tk['test']['trades']:>13}"
                 f"{tk['test']['per_trade']:>12.2f}")
    for r in s["pooled"]:                             # type: ignore[index]
        label = f"{r['fill_model']}/{r['mode']}"
        lines.append(f"     {label:>18}{r['train']['trades']:>14}{r['train']['per_trade']:>13.2f}"
                     f"{r['test']['trades']:>13}{r['test']['per_trade']:>12.2f}")

    lines.append("")
    lines.append("  3. EXECUTION EFFECT — the verdict. Matched (ticker, model) pairs where both")
    lines.append("     executions traded the SAME SIDE: same market, same settlement, so the only")
    lines.append("     difference is the price paid. Swept over the improve-ticks knob.")
    lines.append(f"     {'fill':>9}{'mode':>6}{'imp':>5}{'TRAIN n':>10}{'entry Δ':>10}"
                 f"{'Δ $/tr':>9}{'TEST n':>10}{'entry Δ':>10}{'Δ $/tr':>9}")
    for r in s["execution"]:                          # type: ignore[index]
        tr, te = r["train"], r["test"]
        lines.append(f"     {r['fill_model']:>9}{r['mode']:>6}{r['improve']:>5}"
                     f"{tr['same_side_n']:>10}{tr['same_side_entry_delta']:>+10.2f}"
                     f"{tr['same_side_delta_per_trade']:>+9.2f}"
                     f"{te['same_side_n']:>10}{te['same_side_entry_delta']:>+10.2f}"
                     f"{te['same_side_delta_per_trade']:>+9.2f}")
    lines.append("     'entry Δ' = cents passive paid MINUS cents taker paid (negative = passive")
    lines.append("     got in cheaper). Δ $/tr should be ≈ -entry Δ x size/100: same bet, only the")
    lines.append("     price moved. If it is not, the two runs are not comparing the same trade.")

    lines.append("")
    lines.append(f"  3b. WHAT PANEL 3 EXCLUDES — improve={improve}. Opposite-side matched pairs are")
    lines.append("      NOT execution: the two runs entered at different snapshots, so they are")
    lines.append("      acting on different forecasts. Trades made alone are selection, not skill.")
    lines.append(f"      {'fill/mode':>16}{'window':>8}{'flip n':>8}{'flip Δ$':>10}"
                 f"{'taker-only n':>14}{'their $/tr':>12}{'pasv-only n':>13}{'their $/tr':>12}")
    for r in s["selection"]:                          # type: ignore[index]
        for name in ("train", "test"):
            m = r[name]
            label = f"{r['fill_model']}/{r['mode']}"
            lines.append(f"      {label:>16}{name:>8}{m['flip_n']:>8}"
                         f"{m['flip_delta_per_trade']:>+10.2f}{m['taker_only']['trades']:>14}"
                         f"{m['taker_only']['per_trade']:>12.2f}{m['passive_only']['trades']:>13}"
                         f"{m['passive_only']['per_trade']:>12.2f}")

    lines.append("")
    lines.append(f"  4. FILL RATES — (ticker, model) pairs that ever rested an order, improve={improve}")
    lines.append(f"     {'fill/mode':>16}{'TRAIN rested':>14}{'TRAIN filled':>14}"
                 f"{'TEST rested':>13}{'TEST filled':>13}")
    for r in s["pooled"]:                             # type: ignore[index]
        label = f"{r['fill_model']}/{r['mode']}"
        trf, tef = r["train_fills"], r["test_fills"]
        tr_pct = f"{trf['filled']} ({trf['filled'] / trf['rested']:.0%})" if trf["rested"] else "0"
        te_pct = f"{tef['filled']} ({tef['filled'] / tef['rested']:.0%})" if tef["rested"] else "0"
        lines.append(f"     {label:>16}{trf['rested']:>14}{tr_pct:>14}"
                     f"{tef['rested']:>13}{te_pct:>13}")
    lines.append("=" * 88)
    return "\n".join(lines)


async def run_model_backtest(
    settings: Settings, *, days: Optional[int] = None, size: int, persist: bool = False,
    start_date: Optional[str] = None, end_date: Optional[str] = None,
    calibration: Optional[dict[str, CalibrationMap]] = None, min_price_cents: int = 0,
    grid_fixed: bool = False,
) -> dict[str, object]:
    """Simulate Strategy-2 (model edge) over settled markets, per forecast model.

    Enters each (ticker, model) at the first archived snapshot whose edge clears
    the fee-adjusted threshold at that time's market price (full lead time), holds
    to settlement, and nets fees. One candle series is fetched per ticker. With
    ``persist`` the per-model P&L is cached to ``model_pnl`` for the auto_pnl
    selector. ``calibration`` and ``min_price_cents`` apply the fixes diagnosed in
    :mod:`calibration` -- see ``--calibrate`` for an honest, out-of-sample test of
    whether they actually help (fitting and evaluating on the same trades would
    just curve-fit noise).
    """
    start, end = _date_window(days, start_date, end_date)
    threshold = settings.risk.model_edge_threshold
    coeff = settings.kalshi_fee_coefficient
    all_snaps = _load_snapshots(settings.db_path, end, grid_fixed=grid_fixed)
    snaps = _snaps_in_window(all_snaps, start, end)
    days_span = (datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")).days

    session = aiohttp.ClientSession(headers={"User-Agent": settings.http_user_agent})
    try:
        results, candle_cache = await _fetch_market_data(session, snaps, days_span)
        per_model, n_eval = _simulate(snaps, results, candle_cache, threshold, coeff, size,
                                      calibration=calibration, min_price_cents=min_price_cents)
    finally:
        await session.close()
    summary = _summarize_s2(per_model, size, n_eval)
    if persist and summary.get("models"):
        db = Database(settings.db_path)
        await db.connect()
        try:
            await db.record_model_pnl(summary["models"])  # type: ignore[arg-type]
        finally:
            await db.close()
    return summary


def _calibration_pairs(rows: list[dict]) -> list[tuple[float, float]]:
    """(side_prob, outcome) pairs from trade rows -- the calibration-fitting target:
    probability of the side actually bought, vs whether it actually won."""
    return [(float(r["prob"]), 1.0 if r["won"] else 0.0) for r in rows]


async def run_calibration_backtest(
    settings: Settings, *, days: int, size: int, train_frac: float = 0.6,
    min_bin_samples: int = 15, min_price_cents: int = 0, persist: bool = False,
    grid_fixed: bool = False,
) -> dict[str, object]:
    """Honest, out-of-sample test of probability recalibration + a price floor.

    Chronological split (never fit and evaluate on the same trades, which would
    just curve-fit noise): fits a per-model :class:`CalibrationMap` from the TRAIN
    window's uncalibrated trades, then runs the held-out TEST window twice --
    uncalibrated (baseline) and with that calibration + ``min_price_cents``
    applied -- so the reported improvement is genuinely out-of-sample. One market
    data fetch covers the whole window; simulation (free, no network) runs
    multiple times against it. With ``persist``, ALSO fits a final calibration
    on the *full* window (more data for deployment) and caches it to
    ``model_calibration`` for live use once the held-out result justifies it.
    """
    end = timeutils.utc_now().date().isoformat()
    start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=days)).date().isoformat()
    split = (datetime.strptime(start, "%Y-%m-%d")
             + timedelta(days=round(days * train_frac))).date().isoformat()

    threshold = settings.risk.model_edge_threshold
    coeff = settings.kalshi_fee_coefficient
    all_snaps = _load_snapshots(settings.db_path, end, grid_fixed=grid_fixed)
    full_snaps = _snaps_in_window(all_snaps, start, end)
    train_snaps = _snaps_in_window(all_snaps, start, split)
    test_snaps = _snaps_in_window(all_snaps, split, end)

    session = aiohttp.ClientSession(headers={"User-Agent": settings.http_user_agent})
    try:
        results, candle_cache = await _fetch_market_data(session, full_snaps, days)

        train_models, _ = _simulate(train_snaps, results, candle_cache, threshold, coeff, size)
        calibration: dict[str, CalibrationMap] = {}
        for model, rows in train_models.items():
            cm = fit_calibration(_calibration_pairs(rows), min_bin_samples=min_bin_samples)
            if cm is not None:
                calibration[model] = cm

        baseline_models, n_eval = _simulate(test_snaps, results, candle_cache, threshold, coeff, size)
        fixed_models, _ = _simulate(test_snaps, results, candle_cache, threshold, coeff, size,
                                    calibration=calibration, min_price_cents=min_price_cents)
    finally:
        await session.close()

    if persist and calibration:
        full_models, _ = _simulate(full_snaps, results, candle_cache, threshold, coeff, size)
        full_calibration = {
            model: cm for model, rows in full_models.items()
            if (cm := fit_calibration(_calibration_pairs(rows), min_bin_samples=min_bin_samples))
            is not None
        }
        db = Database(settings.db_path)
        await db.connect()
        try:
            await db.record_model_calibration(full_calibration)
        finally:
            await db.close()

    return {
        "train_window": [start, split], "test_window": [split, end],
        "train_days": round(days * train_frac), "test_days": days - round(days * train_frac),
        "calibrated_models": sorted(calibration),
        "min_price_cents": min_price_cents,
        "baseline": _summarize_s2(baseline_models, size, n_eval),
        "fixed": _summarize_s2(fixed_models, size, n_eval),
    }


DEFAULT_THRESHOLD_GRID = (0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50)
DEFAULT_FLOOR_GRID = (0, 10, 20, 30)


def _config_stats(rows: list[dict]) -> dict[str, float]:
    n = len(rows)
    net = sum(float(r["net_usd"]) for r in rows)
    return {"trades": n, "net_usd": net, "per_trade": (net / n) if n else 0.0,
            "hit_rate": (sum(1 for r in rows if r["won"]) / n) if n else 0.0}


async def run_threshold_sweep(
    settings: Settings, *, days: int, size: int, train_frac: float = 0.6,
    min_train_trades: int = 20, thresholds: tuple[float, ...] = DEFAULT_THRESHOLD_GRID,
    floors: tuple[int, ...] = DEFAULT_FLOOR_GRID,
    grid_fixed: bool = False,
) -> dict[str, object]:
    """Honest, out-of-sample sweep of the model-edge threshold x price floor.

    The live default (15% threshold, no floor) was measured to over-select cheap
    longshots. Grid-searching a replacement on the same trades it is scored on
    would just curve-fit noise -- with a grid this size (8 x 4 per model) that is
    a near-certainty -- so this uses the same chronological split
    :func:`run_calibration_backtest` does: **pick** the best (threshold, floor) per
    model on the TRAIN window, then **score** only that choice on the held-out
    TEST window, against the live default on the same held-out trades.

    Selection maximizes total net on train subject to ``min_train_trades``, so a
    config cannot win by trading almost never -- the failure mode the price floor
    already exhibits (it lifts hit rate while making per-trade P&L worse).

    Read ``test`` rows, never ``train``: the train column is the inflated number
    that survived selection, and the gap between them is the overfit.
    """
    end = timeutils.utc_now().date().isoformat()
    start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=days)).date().isoformat()
    split = (datetime.strptime(start, "%Y-%m-%d")
             + timedelta(days=round(days * train_frac))).date().isoformat()
    coeff = settings.kalshi_fee_coefficient
    live_threshold = settings.risk.model_edge_threshold

    all_snaps = _load_snapshots(settings.db_path, end, grid_fixed=grid_fixed)
    full_snaps = _snaps_in_window(all_snaps, start, end)
    train_snaps = _snaps_in_window(all_snaps, start, split)
    test_snaps = _snaps_in_window(all_snaps, split, end)

    session = aiohttp.ClientSession(headers={"User-Agent": settings.http_user_agent})
    try:
        results, candles = await _fetch_market_data(session, full_snaps, days)
    finally:
        await session.close()

    # One simulation per grid cell per window (free -- no network).
    def grid(snaps):
        out: dict[tuple[float, int], dict[str, list[dict]]] = {}
        for thr in thresholds:
            for floor in floors:
                per, _ = _simulate(snaps, results, candles, thr, coeff, size,
                                   min_price_cents=floor)
                out[(thr, floor)] = per
        return out

    train_grid, test_grid = grid(train_snaps), grid(test_snaps)
    models = sorted({m for cell in train_grid.values() for m in cell})

    rows: list[dict[str, object]] = []
    for model in models:
        best_cell, best_net = None, None
        for cell, per in train_grid.items():
            stats = _config_stats(per.get(model, []))
            if stats["trades"] < min_train_trades:
                continue
            if best_net is None or stats["net_usd"] > best_net:
                best_cell, best_net = cell, stats["net_usd"]
        if best_cell is None:
            continue
        thr, floor = best_cell
        baseline_test = _config_stats(test_grid.get((live_threshold, 0), {}).get(model, []))
        chosen_test = _config_stats(test_grid[best_cell].get(model, []))
        rows.append({
            "model": model, "threshold": thr, "floor_cents": floor,
            "train": _config_stats(train_grid[best_cell].get(model, [])),
            "test": chosen_test, "test_baseline": baseline_test,
            "test_delta_usd": chosen_test["net_usd"] - baseline_test["net_usd"],
        })
    rows.sort(key=lambda r: -r["test"]["net_usd"])   # type: ignore[index]

    # Robustness check with NOTHING fitted: aggregate every model at each fixed
    # threshold, in both windows. Per-model selection can be fooled by a favorable
    # test regime; a gradient that holds in BOTH windows with zero parameters
    # chosen cannot be. This, not the picked-config table, is the real evidence.
    fixed: list[dict[str, object]] = []
    for thr in thresholds:
        def agg(g):
            rs = [r for per in [g[(thr, 0)]] for rows_ in per.values() for r in rows_]
            return _config_stats(rs)
        fixed.append({"threshold": thr, "train": agg(train_grid), "test": agg(test_grid)})

    return {
        "fixed_threshold": fixed,
        "train_window": [start, split], "test_window": [split, end],
        "size": size, "min_train_trades": min_train_trades,
        "live_threshold": live_threshold,
        "grid": {"thresholds": list(thresholds), "floors": list(floors)},
        "rows": rows,
    }


DEFAULT_DYN_MAX_GRID = (0.15, 0.20, 0.25, 0.30, 0.35)
DEFAULT_DYN_MIN_GRID = (0.02, 0.04, 0.06, 0.08, 0.10)
DEFAULT_PERSIST_GRID = (0.0, 900.0, 1800.0)


def _station_offsets(settings: Settings) -> dict[str, int]:
    """Series-ticker prefix -> the station's fixed LST offset (cities span -5..-8)."""
    out: dict[str, int] = {}
    for series, icao in settings.weather_series:
        station = settings.station_by_icao(icao)
        if station is not None:
            out[series] = station.standard_utc_offset_hours
    return out


def _threshold_fns(
    snaps: dict[tuple[str, str], list[dict]], offsets: dict[str, int],
    *, max_threshold: float, min_threshold: float, start_hour: float, end_hour: float,
) -> dict[str, Callable[[int], float]]:
    """Per-ticker ``epoch -> threshold``, using that ticker's own LST offset.

    Built per ticker rather than globally because the seven cities span UTC-5..-8:
    a single clock would apply LA's morning threshold to New York's afternoon.
    Calls the **live** rule (:func:`arb_engine.get_dynamic_edge_threshold`).
    """
    fns: dict[str, Callable[[int], float]] = {}
    for ticker, _model in snaps:
        if ticker in fns:
            continue
        offset = offsets.get(ticker.split("-")[0])
        if offset is None:
            continue

        def fn(ts: int, _off: int = offset) -> float:
            return get_dynamic_edge_threshold(
                timeutils.local_hour_lst(timeutils.from_epoch_seconds(ts), _off),
                max_threshold=max_threshold, min_threshold=min_threshold,
                start_hour=start_hour, end_hour=end_hour)

        fns[ticker] = fn
    return fns


async def run_dynamic_sweep(
    settings: Settings, *, days: int, size: int, train_frac: float = 0.6,
    min_train_trades: int = 20,
    max_grid: tuple[float, ...] = DEFAULT_DYN_MAX_GRID,
    min_grid: tuple[float, ...] = DEFAULT_DYN_MIN_GRID,
    persist_grid: tuple[float, ...] = DEFAULT_PERSIST_GRID,
    grid_fixed: bool = False,
) -> dict[str, object]:
    """Out-of-sample sweep of the time-decay threshold (backlog 0b).

    Same discipline as :func:`run_threshold_sweep`: **pick** the best
    (max_threshold, min_threshold) per model on the TRAIN window, **score** only
    that pick on the held-out TEST window against the live flat threshold on the
    same held-out trades, and then a pooled panel at each FIXED config in both
    windows with nothing fitted. **Read the pooled panel**, not the per-model
    picked-config table -- with a 5x5 grid per model the latter is mostly
    selection noise (the 2026-08-21 sweep learned this the hard way).

    Persistence is swept separately (``persist_grid``) at the live flat threshold,
    because it is an independent question and confounding the two would make
    neither readable.

    ⚠ Two caveats that limit what this can prove:
    * Snapshots are ~30 min apart, so a persistence gate is coarse here and is the
      OPTIMISTIC bound on the live 120 s-loop version (see
      :func:`first_qualifying_edge`).
    * Every stored ``prob`` predates the 2026-08-23 grid fix. Run with
      ``--grid-fixed`` for numbers that reflect the corrected geometry; without it
      the thresholds are being tuned against probabilities that summed to 0.70.
    """
    end = timeutils.utc_now().date().isoformat()
    start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=days)).date().isoformat()
    split = (datetime.strptime(start, "%Y-%m-%d")
             + timedelta(days=round(days * train_frac))).date().isoformat()
    coeff = settings.kalshi_fee_coefficient
    live_threshold = settings.risk.model_edge_threshold
    risk = settings.risk
    offsets = _station_offsets(settings)

    all_snaps = _load_snapshots(settings.db_path, end, grid_fixed=grid_fixed)
    full_snaps = _snaps_in_window(all_snaps, start, end)
    train_snaps = _snaps_in_window(all_snaps, start, split)
    test_snaps = _snaps_in_window(all_snaps, split, end)

    session = aiohttp.ClientSession(headers={"User-Agent": settings.http_user_agent})
    try:
        results, candles = await _fetch_market_data(session, full_snaps, days)
    finally:
        await session.close()

    def grid(snaps):
        out: dict[tuple[float, float], dict[str, list[dict]]] = {}
        for hi in max_grid:
            for lo in min_grid:
                if lo > hi:                       # inverted schedule is meaningless
                    continue
                fns = _threshold_fns(
                    snaps, offsets, max_threshold=hi, min_threshold=lo,
                    start_hour=risk.dynamic_edge_start_hour,
                    end_hour=risk.dynamic_edge_end_hour)
                per, _ = _simulate(snaps, results, candles, live_threshold, coeff, size,
                                   threshold_fns=fns)
                out[(hi, lo)] = per
        return out

    train_grid, test_grid = grid(train_snaps), grid(test_snaps)
    base_train, _ = _simulate(train_snaps, results, candles, live_threshold, coeff, size)
    base_test, _ = _simulate(test_snaps, results, candles, live_threshold, coeff, size)
    models = sorted({m for cell in train_grid.values() for m in cell})

    rows: list[dict[str, object]] = []
    for model in models:
        best_cell, best_net = None, None
        for cell, per in train_grid.items():
            st = _config_stats(per.get(model, []))
            if st["trades"] < min_train_trades:
                continue
            if best_net is None or st["net_usd"] > best_net:
                best_cell, best_net = cell, st["net_usd"]
        if best_cell is None:
            continue
        chosen = _config_stats(test_grid[best_cell].get(model, []))
        baseline = _config_stats(base_test.get(model, []))
        rows.append({
            "model": model, "max_threshold": best_cell[0], "min_threshold": best_cell[1],
            "train": _config_stats(train_grid[best_cell].get(model, [])),
            "test": chosen, "test_baseline": baseline,
            "test_delta_usd": chosen["net_usd"] - baseline["net_usd"],
        })
    rows.sort(key=lambda r: -r["test"]["net_usd"])          # type: ignore[index]

    # Pooled, NOTHING fitted: every model aggregated at each fixed config.
    def pooled(g, cell):
        allrows = [r for per in [g[cell]] for rs in per.values() for r in rs]
        return _config_stats(allrows)

    fixed = [{
        "max_threshold": hi, "min_threshold": lo,
        "train": pooled(train_grid, (hi, lo)), "test": pooled(test_grid, (hi, lo)),
    } for (hi, lo) in sorted(train_grid)]

    base_pool = {
        "train": _config_stats([r for rs in base_train.values() for r in rs]),
        "test": _config_stats([r for rs in base_test.values() for r in rs]),
    }

    # Persistence, swept alone at the live flat threshold.
    persist: list[dict[str, object]] = []
    for secs in persist_grid:
        ptr, _ = _simulate(train_snaps, results, candles, live_threshold, coeff, size,
                           persist_seconds=secs)
        pte, _ = _simulate(test_snaps, results, candles, live_threshold, coeff, size,
                           persist_seconds=secs)
        persist.append({
            "persist_seconds": secs,
            "train": _config_stats([r for rs in ptr.values() for r in rs]),
            "test": _config_stats([r for rs in pte.values() for r in rs]),
        })

    return {
        "mode": "dynamic-sweep", "days": days, "size": size,
        "start": start, "split": split, "end": end,
        "grid_fixed": grid_fixed, "live_threshold": live_threshold,
        "start_hour": risk.dynamic_edge_start_hour,
        "end_hour": risk.dynamic_edge_end_hour,
        "rows": rows, "fixed": fixed, "baseline": base_pool, "persistence": persist,
    }


def _render_dynamic_sweep(s: dict[str, object]) -> str:
    out = ["=" * 78,
           f"  TIME-DECAY THRESHOLD SWEEP (0b) -- {s['days']}d, size {s['size']}, "
           f"{'GRID-FIXED probs' if s['grid_fixed'] else 'stored (pre-fix) probs'}",
           f"  train {s['start']}..{s['split']}   test {s['split']}..{s['end']}   "
           f"decay {s['start_hour']:.0f}h->{s['end_hour']:.0f}h LST", "=" * 78]
    b = s["baseline"]                                        # type: ignore[index]
    out.append(f"  live flat threshold {s['live_threshold']:.2f}, pooled: "
               f"TRAIN {b['train']['per_trade']:+.2f}/tr ({b['train']['trades']} tr)  "
               f"TEST {b['test']['per_trade']:+.2f}/tr ({b['test']['trades']} tr)")

    out += ["", "  POOLED at each FIXED (max,min) -- nothing fitted. READ THIS PANEL:",
            f"    {'max':>5s} {'min':>5s} | {'TRAIN $/tr':>11s} {'tr':>6s} | "
            f"{'TEST $/tr':>10s} {'tr':>6s}"]
    for f in s["fixed"]:                                     # type: ignore[union-attr]
        out.append(f"    {f['max_threshold']:5.2f} {f['min_threshold']:5.2f} | "
                   f"{f['train']['per_trade']:11.2f} {f['train']['trades']:6d} | "
                   f"{f['test']['per_trade']:10.2f} {f['test']['trades']:6d}")

    out += ["", "  PERSISTENCE alone, at the live flat threshold (pooled, nothing fitted):",
            f"    {'persist_s':>9s} | {'TRAIN $/tr':>11s} {'tr':>6s} | "
            f"{'TEST $/tr':>10s} {'tr':>6s}"]
    for pr in s["persistence"]:                              # type: ignore[union-attr]
        out.append(f"    {pr['persist_seconds']:9.0f} | "
                   f"{pr['train']['per_trade']:11.2f} {pr['train']['trades']:6d} | "
                   f"{pr['test']['per_trade']:10.2f} {pr['test']['trades']:6d}")

    out += ["", "  Per-model picked config (SELECTION NOISE -- do not read as a result):",
            f"    {'model':14s} {'max':>5s} {'min':>5s} {'TEST $/tr':>10s} "
            f"{'base $/tr':>10s} {'delta$':>9s}"]
    for r in s["rows"]:                                      # type: ignore[union-attr]
        out.append(f"    {r['model']:14s} {r['max_threshold']:5.2f} "
                   f"{r['min_threshold']:5.2f} {r['test']['per_trade']:10.2f} "
                   f"{r['test_baseline']['per_trade']:10.2f} {r['test_delta_usd']:9.0f}")
    out += ["", "  Snapshots are ~30 min apart, so the persistence rows are the OPTIMISTIC",
            "  bound on the live 120s-loop gate. Nothing here should flip a flag on its own.",
            "=" * 78]
    return "\n".join(out)


def _render_sweep(s: dict[str, object]) -> str:
    tw, ew = s["train_window"], s["test_window"]     # type: ignore[index]
    lines = ["=" * 84,
             "  Strategy 2 — threshold x price-floor sweep (out-of-sample)",
             "=" * 84,
             f"  train (pick here) {tw[0]} .. {tw[1]}      "
             f"test (score here) {ew[0]} .. {ew[1]}",
             f"  grid {len(s['grid']['thresholds'])} thresholds x "      # type: ignore[index]
             f"{len(s['grid']['floors'])} floors, "                      # type: ignore[index]
             f"min {s['min_train_trades']} train trades, size {s['size']}"]
    rows = s["rows"]                                  # type: ignore[assignment]
    if not rows:
        lines.append("  (no model had enough train trades — let the tracker run longer)")
        lines.append("=" * 84)
        return "\n".join(lines)
    lines.append("")
    lines.append(f"  {'model':<13}{'picked':>12}{'TRAIN net':>11}"
                 f"{'TEST net':>10}{'TEST/trade':>12}{'vs live':>10}{'trades':>8}")
    lines.append("  " + "-" * 78)
    for r in rows:
        pick = f"{r['threshold']:.0%}/{r['floor_cents']}c"
        lines.append(
            f"  {r['model']:<13}{pick:>12}{r['train']['net_usd']:>11.2f}"
            f"{r['test']['net_usd']:>10.2f}{r['test']['per_trade']:>12.2f}"
            f"{r['test_delta_usd']:>+10.2f}{r['test']['trades']:>8}")
    lines.append("  " + "-" * 78)
    held = [r for r in rows if r["test_delta_usd"] > 0]
    lines.append(f"  {len(held)}/{len(rows)} models improved on the HELD-OUT window "
                 f"vs the live {s['live_threshold']:.0%}/0c default.")
    lines.append("  TRAIN net is inflated — it is the max of the grid. TEST net is the honest")
    lines.append("  number; a large TRAIN-TEST gap is the overfit, not a result.")
    lines.append("")
    lines.append("  Robustness — ALL models pooled at a FIXED threshold, nothing fitted:")
    lines.append(f"    {'thresh':>8}{'TRAIN net':>12}{'TRAIN/trade':>13}"
                 f"{'TEST net':>11}{'TEST/trade':>12}")
    for f in s["fixed_threshold"]:                    # type: ignore[index]
        lines.append(f"    {f['threshold']:>7.0%}{f['train']['net_usd']:>12.2f}"
                     f"{f['train']['per_trade']:>13.2f}{f['test']['net_usd']:>11.2f}"
                     f"{f['test']['per_trade']:>12.2f}")
    lines.append("    A gradient that holds in BOTH columns is structural, not a lucky window.")
    lines.append("=" * 84)
    return "\n".join(lines)


def _summarize_s2(per_model: dict[str, list[dict]], size: int, n_preds: int,
                  min_trades: int = 20) -> dict[str, object]:
    """Per-model P&L summary, ranked by net.

    ``best_model`` ignores models with fewer than ``min_trades`` trades. Models do
    NOT all cover the same population: an ``_id`` variant only exists once the
    climate day is under way (no observation -> no intraday estimate), so it has no
    next-day snapshots and naturally accumulates far fewer trades than its base
    model. Ranking those head-to-head on net P&L crowns whichever model happened to
    trade least, so the headline needs a sample floor -- mirroring the
    ``min_trades`` guard :meth:`Database.best_model_by_pnl` already applies before
    ``auto_pnl`` will trade a model.
    """
    models: dict[str, dict] = {}
    for model, rows in per_model.items():
        n = len(rows)
        wins = sum(1 for r in rows if r["won"])
        models[model] = {
            "trades": n, "wins": wins,
            "hit_rate": (wins / n) if n else None,
            "net_usd": sum(float(r["net_usd"]) for r in rows),
            "fees_usd": sum(float(r["fee_usd"]) for r in rows),
            "rows": rows,
        }
    ranked = sorted(models.items(), key=lambda kv: -kv[1]["net_usd"])
    eligible = [m for m, v in ranked if v["trades"] >= min_trades]
    return {"size": size, "predictions_scanned": n_preds, "models": dict(ranked),
            "best_model": eligible[0] if eligible else None,
            "best_model_min_trades": min_trades}


def _render_s2(s: dict[str, object]) -> str:
    lines = ["=" * 68, "  Strategy 2 (model edge) backtest — forecast vs market price", "=" * 68]
    lines.append(f"  predictions scanned : {s['predictions_scanned']}   "
                 f"(size assumed {s['size']} contracts)")
    models = s["models"]  # type: ignore[assignment]
    if not models:
        lines.append("  (no settled model predictions in the window — let the tracker run longer)")
        lines.append("=" * 68)
        return "\n".join(lines)
    lines.append(f"  {'model':<12}{'trades':>7}{'hit%':>7}{'net $':>11}{'fees $':>10}")
    lines.append("  " + "-" * 62)
    floor = int(s.get("best_model_min_trades", 20))  # type: ignore[arg-type]
    thin = False
    for model, m in models.items():  # already ranked by net
        hit = f"{100 * m['hit_rate']:.0f}%" if m["hit_rate"] is not None else "-"
        mark = " *" if m["trades"] < floor else ""
        thin = thin or bool(mark)
        lines.append(f"  {model:<12}{m['trades']:>7}{hit:>7}{m['net_usd']:>11.2f}"
                     f"{m['fees_usd']:>10.2f}{mark}")
    lines.append("  " + "-" * 62)
    best = s["best_model"]
    lines.append(f"  best model by net P&L: {best}" if best else
                 f"  best model by net P&L: (none with >= {floor} trades yet)")
    if thin:
        lines.append(f"  * under {floor} trades — too thin to rank. Models do not cover the same")
        lines.append("    population: `_id` variants only exist once the climate day is under way,")
        lines.append("    so they have no next-day entries and trade far less than their base model.")
    lines.append("  Enters at the first archived snapshot an edge appears (model_prediction_history);")
    lines.append("  markets logged before that table existed fall back to the last stored view (edges")
    lines.append("  near close, conservative). Net of fees; paper only. --persist caches for auto_pnl.")
    lines.append("=" * 68)
    return "\n".join(lines)


def _render_calibration(r: dict[str, object]) -> str:
    lines = ["=" * 78, "  CALIBRATION FIX: honest out-of-sample before/after", "=" * 78]
    tw, te = r["train_window"], r["test_window"]  # type: ignore[assignment]
    lines.append(f"  train: {tw[0]}..{tw[1]} ({r['train_days']}d, fits calibration)  "
                 f"|  test: {te[0]}..{te[1]} ({r['test_days']}d, held out)")
    lines.append(f"  calibrated models: {', '.join(r['calibrated_models']) or '(none -- too little train data)'}"
                 f"   min_price_cents={r['min_price_cents']}")
    base, fixed = r["baseline"]["models"], r["fixed"]["models"]  # type: ignore[index]
    lines.append("")
    lines.append(f"  {'model':<12}{'base trades':>12}{'base net$':>11}{'fixed trades':>13}{'fixed net$':>12}{'delta$':>10}")
    lines.append("  " + "-" * 74)
    models = sorted(set(base) | set(fixed), key=lambda m: -(fixed.get(m, {}).get("net_usd", 0)))
    tot_base = tot_fixed = 0.0
    for m in models:
        b, f = base.get(m, {}), fixed.get(m, {})
        bn, fn = b.get("net_usd", 0.0), f.get("net_usd", 0.0)
        tot_base += bn; tot_fixed += fn
        lines.append(f"  {m:<12}{b.get('trades', 0):>12}{bn:>11.2f}{f.get('trades', 0):>13}"
                     f"{fn:>12.2f}{fn-bn:>10.2f}")
    lines.append("  " + "-" * 74)
    lines.append(f"  {'TOTAL':<12}{'':<12}{tot_base:>11.2f}{'':<13}{tot_fixed:>12.2f}{tot_fixed-tot_base:>10.2f}")
    lines.append("")
    lines.append("  'base' = today's threshold-only rule; 'fixed' = calibrated probability + price")
    lines.append("  floor. Both run on the SAME held-out test trades never used to fit calibration --")
    lines.append("  a real out-of-sample read, not curve-fit noise. --persist also caches a full-window")
    lines.append("  fit to model_calibration for live use once this comparison looks trustworthy.")
    lines.append("=" * 78)
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Backtest Strategy 1 (lock-in) or 2 (model edge).")
    parser.add_argument("--strategy", type=int, choices=(1, 2), default=1,
                        help="1 = deterministic lock-in (default); 2 = model edge")
    parser.add_argument("--days", type=int, default=7, help="lookback window in days")
    parser.add_argument("--size", type=int, default=100, help="assumed contracts per trade")
    parser.add_argument("--sweep-dynamic", action="store_true",
                        help="out-of-sample sweep of the 0b time-decay threshold "
                             "(max 0.15-0.35 x min 0.02-0.10) plus a persistence panel")
    parser.add_argument("--grid-fixed", action="store_true",
                        help="read prob_grid_fixed (repriced on the corrected bucket grid "
                             "by scripts/backfill_grid_probs.py) instead of the stored prob")
    parser.add_argument("--persist", action="store_true",
                        help="strategy 2: cache per-model P&L to model_pnl (for auto_pnl selection); "
                            "with --calibrate, also caches a full-window calibration fit")
    parser.add_argument("--min-price", type=int, default=0,
                        help="strategy 2: refuse entries priced below this many cents "
                            "(defense-in-depth against overconfident longshot edges)")
    parser.add_argument("--calibrate", action="store_true",
                        help="strategy 2: fit isotonic probability recalibration on the first "
                            "part of --days and report an honest before/after on the held-out rest")
    parser.add_argument("--calib-train-frac", type=float, default=0.6,
                        help="--calibrate/--sweep: fraction of --days used to pick on (default 0.6)")
    parser.add_argument("--sweep", action="store_true",
                        help="strategy 2: out-of-sample sweep of the model-edge threshold x "
                            "price floor — picks the best config per model on the first part of "
                            "--days, scores only that pick on the held-out rest")
    parser.add_argument("--min-train-trades", type=int, default=20,
                        help="--sweep: a config needs this many train trades to be pickable, "
                            "so it cannot win by trading almost never (default 20)")
    parser.add_argument("--passive", action="store_true",
                        help="strategy 2: measure the spread Strategy 2 crosses, then compare "
                             "taker vs resting (maker) placement under three fill models, on the "
                             "same train/test split --calibrate and --sweep use")
    parser.add_argument("--passive-improve", type=int, default=1,
                        help="--passive: ticks to improve the near side by when resting "
                             "(mirrors WA_PASSIVE_IMPROVE_TICKS, default 1)")
    parser.add_argument("--fill-model", choices=FILL_MODELS, action="append",
                        help="--passive: restrict to one fill model (repeatable); "
                             "default reports all three as a bracket")
    parser.add_argument("--improve-grid", type=int, action="append",
                        help="--passive: improve-tick values to sweep in the execution panel "
                             "(repeatable; default 0 1 2 3)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    if args.strategy == 2 and args.passive:
        summary = asyncio.run(run_passive_backtest(
            settings, days=args.days, size=args.size, train_frac=args.calib_train_frac,
            improve=args.passive_improve, min_price_cents=args.min_price,
            fill_models=tuple(args.fill_model or FILL_MODELS),
            improve_grid=tuple(args.improve_grid or DEFAULT_IMPROVE_GRID),
            grid_fixed=args.grid_fixed))
        render = _render_passive
    elif args.strategy == 2 and args.sweep_dynamic:
        summary = asyncio.run(run_dynamic_sweep(
            settings, days=args.days, size=args.size, train_frac=args.calib_train_frac,
            min_train_trades=args.min_train_trades, grid_fixed=args.grid_fixed))
        render = _render_dynamic_sweep
    elif args.strategy == 2 and args.sweep:
        summary = asyncio.run(run_threshold_sweep(
            settings, days=args.days, size=args.size, train_frac=args.calib_train_frac,
            min_train_trades=args.min_train_trades, grid_fixed=args.grid_fixed))
        render = _render_sweep
    elif args.strategy == 2 and args.calibrate:
        summary = asyncio.run(run_calibration_backtest(
            settings, days=args.days, size=args.size, train_frac=args.calib_train_frac,
            min_price_cents=args.min_price, persist=args.persist,
            grid_fixed=args.grid_fixed))
        render = _render_calibration
    elif args.strategy == 2:
        summary = asyncio.run(run_model_backtest(
            settings, days=args.days, size=args.size, persist=args.persist,
            min_price_cents=args.min_price, grid_fixed=args.grid_fixed))
        render = _render_s2
    else:
        summary = asyncio.run(run_backtest(settings, days=args.days, size=args.size))
        render = _render
    if args.json:
        import json
        print(json.dumps(summary, default=str, indent=2))
    else:
        print(render(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
