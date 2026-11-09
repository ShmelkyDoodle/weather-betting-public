#!/usr/bin/env python3
"""Which Kalshi CATEGORIES have a favorite-longshot bias big enough to beat the fee?

THE HYPOTHESIS.  Prediction markets systematically overprice longshots and
underprice favorites -- people buy lottery tickets.  This repo already found a
real +0.6c/contract version of it, but measured at an average entry of **71c**,
where Kalshi's fee is ~1.5c/contract.  It lost by construction.

Kalshi's fee is `coefficient * contracts * P * (1-P)`, which is QUADRATIC and
collapses at the extremes:

      50c -> 1.75c      90c -> 0.63c      95c -> 0.33c      98c -> 0.14c

So the bias was measured in the one price region where the fee guarantees it
cannot be harvested.  This asks whether it survives where the fee is 5x smaller.

THE REASON THIS RUNS EXCHANGE-WIDE was that weather looked like the wrong place:
120 days / 464 events gave 96-99c at +0.68c/contract, CI [-0.04, +1.39] -- not
established -- while the longshot side was reliably negative.  Nobody is
emotionally attached to Tuesday's high in Austin, and a favorite-longshot bias
is a *sentiment* phenomenon, so it should be biggest where the crowd cares:
teams, elections, celebrities.  `--universe all` tests that.

⚠ IT CAME OUT BACKWARDS.  Weather is the ONLY place the favorite side survives,
and the reason has nothing to do with sentiment -- see the RESULT below.

======================================================================
PRE-REGISTRATION -- fixed before the first exchange-wide run, 2026-09-10.
======================================================================
Hundreds of series x 10 price buckets is thousands of cells, and the best of
thousands of noisy cells always looks excellent.  So the hypothesis is declared
directionally and up front, and only these cells are TESTED:

  H1 (primary)   pooled mean net edge in the FAVORITE band (ask 96-99c) > 0
  H2 (primary)   pooled mean net edge in the LONGSHOT band (ask  1-5c)  < 0

  Secondary family: the SAME two bands within each Kalshi `category`.  Holm
  step-down across that entire family (2 x n_categories), reported as adjusted
  p-values.  Nothing else is a test.

Everything else printed -- the ten-bucket curve, the per-horizon tables, the
chronological split -- is DESCRIPTIVE.  It is there to show shape and to expose
noise, and no cell in it may be promoted to a finding after the fact.  A real
favorite-longshot bias is two-sided (H1 and H2 both hold); one side alone is
noise or a bug.

  STABLE, as pre-declared, means all three of:
    (a) Holm-adjusted p < 0.05 on the pooled band,
    (b) both chronological halves have the same sign,
    (c) >= 30 independent event clusters.
  AMENDED after the first run (trap 6 below) with a fourth gate that can only
  ever make a claim weaker, never stronger:
    (d) >= 5 event clusters in which something actually LOST, so the band can
        bound its own downside.
  `verdict()` applies all four mechanically and names the first one a band
  fails, so the reader does not have to assemble it out of four tables.

======================================================================
THE SIX THINGS THAT WOULD FAKE A RESULT
======================================================================
1. **Conditioning on a price the market REACHED.**  Do NOT sample "markets that
   ever traded at 96c."  Every market that eventually settles YES necessarily
   passes through 96c on its way to 100c, while only *some* eventual-NO markets
   get there.  That conditions on the outcome and reports a wildly inflated
   settle rate.  Fix: sample at a **fixed horizon before close**, chosen in
   advance and independent of the outcome, then bucket by whatever price is
   found there.  That is an honest calibration curve.

2. **Look-ahead.**  `_candle_index_at` returns the first candle at/AFTER a
   timestamp -- up to a full bar in the future.  Same class of bug as the
   `period_interval` mistake that cost ~$0.81/trade.  `price_as_of` takes the
   last candle at or BEFORE the horizon and refuses a stale one.

3. **Pseudo-replication.**  A single market contributes up to 8 observations
   (4 horizons x 2 sides); markets inside one event are correlated because
   exactly one of them settles YES.  Treating them as independent understates
   the standard error badly -- see `cluster_stats`, where it flipped this
   study's conclusion once already.  The cluster unit is the market's OWN
   `event_ticker` from Kalshi, never a guess parsed out of the ticker string.

4. **The fee coefficient is NOT always 0.07.**  Kalshi publishes a per-series
   `fee_multiplier`, and it is not cosmetic: 19 MLB derivative series charge
   HALF fee and 14 series charge NONE.  Assuming 0.07 there would understate
   the net edge by up to a full cent -- in the harvestable direction, on some of
   the most-traded series on the exchange.  Every observation carries its own
   series coefficient (`0.07 * fee_multiplier`).  `fee_type` variants
   (`quadratic_with_maker_fees`) add a fee to MAKERS; we cross the spread, so
   the taker leg stays quadratic.  Recorded and reported either way.

5. **The ask is not a fill, and depth is unmeasured.**  Everything here reads
   top-of-book ask from a candle.  This repo has already measured what that
   hides: on a market whose outcome is decided, the sure side's book EMPTIES --
   depth median 0 against a control median of 2,443 contracts -- so "priced at
   99c" and "no bid at all" are indistinguishable in candle data and
   economically opposite.  That failure mode lives exactly in the 96-99c bucket
   this study is about.  **Every positive number below is an UPPER BOUND.**
   Partial guard: each observation records the traded volume of the bar it was
   sampled from, the zero-volume share is printed per bucket, and the primary
   test is re-run on bars that actually traded.

6. **A band with no observed losses cannot bound its own downside.**  ADDED
   AFTER THE FIRST EXCHANGE-WIDE RUN, and declared as an amendment rather than
   quietly folded in: this check can only ever make a claim WEAKER, never
   stronger, so it is not cell-shopping.  It exists because the first run
   produced a spectacular false positive that every other guard here waved
   through.  Entertainment's 96-99c band scored +1.56c, 95% CI [+1.32, +1.80],
   Holm-adjusted p = 0.0000 -- and had **zero losing events in 36**.  At a 98c
   ask the payoff is +2c or -98c, so with no loss in the sample the measured
   variance is the variance of the wins alone and the interval is meaningless.
   One additional losing event turns that +1.56c into -1.14c.  Holm does not
   catch this, because the problem is not the number of comparisons; it is that
   the statistic itself is degenerate.  Every band therefore reports its losing
   event count, a one-more-loss sensitivity, and a Wilson interval on the event
   win rate mapped through `net(p) = 100p - ask - fee`, which stays honest at
   zero failures.  Under `MIN_LOSSES` the t-interval is printed but disowned.

SELECTION (declared in advance, outcome-independent).  Exchange-wide, `/series`
lists ~14,000 series and one day alone settles ~150,000 markets -- 99.9% of them
auto-generated `KXMVECROSSCATEGORY` combos with no trading.  Enumerating
everything is neither possible at ~3 req/s nor meaningful.  So: within each
Kalshi category take the top `--series-per-category` series by TOTAL SETTLED
VOLUME in the window (volume does not depend on which way a market settled),
requiring at least `--min-markets` settled markets; within each series take up
to `--markets-per-series` markets spaced evenly by close time so both halves of
the chronological split are covered.  Ranking by volume deliberately selects the
most efficient, most-arbitraged markets -- the conservative direction, and the
only ones where a discovered edge could actually be traded.

======================================================================
RESULT, 2026-09-10.  EXCHANGE-WIDE: 117 series, 12 categories, 3,999 markets,
2,058 event clusters, 20,662 observations, closes spanning 2026-07-02..2026-09-09.
H1 IS DEAD EVERYWHERE EXCEPT WEATHER.  H2 SURVIVES AND IS NOT TRADEABLE.

⚠ THAT IS ~69 DAYS, NOT THE 120 REQUESTED, AND `--days` CANNOT FIX IT.  Kalshi's
`status=settled` listing only retains about ten weeks: enumerating 2,781 series
with `min_close_ts` set to 2026-05-10 returned 513,243 markets whose EARLIEST
close is 2026-06-30, with a cliff right there (42 markets on 06-30, 4,636 on
07-04).  Asking for a longer window silently gets you the same ~70 days.  So
every "is it stable" claim here is a split of one ten-week sample -- it can
catch a within-window regime change and cannot say anything about last winter.

**PIPELINE CHECK FIRST.**  Re-running the 7-series weather study through the
rewritten code (event_ticker clustering, per-series fees, the `bar_as_of`
refactor) gives **+0.64c, 463 events, CI [-0.07, +1.36]** against the previous
**+0.68c, 464 events, [-0.04, +1.39]**.  Reproduced to 0.04c, so the numbers
below are the data talking and not the rewrite.

**⚠⚠ AND THAT WEATHER RUN CLEARS ALL FOUR GATES -- the only H1 result on the
exchange that does.**  463 clusters, 13 of them saw a loss, Holm-adjusted
p = 0.039, both chronological halves positive (+0.22 then +1.05).  It is
**MARGINAL and should not be traded on**: the two-sided CI straddles zero, and
the Wilson interval on the win rate -- the instrument this file argues is the
honest one at these prices -- is [-0.81, +1.33] and includes zero too.  The
declared gate is the t-based p, so `verdict()` calls it a finding and flags the
disagreement rather than quietly moving the goalposts to kill it.  Read it as
"the one place worth another ten weeks of data", not as an edge.

**⚠ THE REASON IS EXECUTION COST, NOT SENTIMENT, AND IT INVERTS THE PREMISE.**
Weather's 96-99c spread is median 1c / p90 2c / mean 1.3c.  Exchange-wide the
same band is median 1c / p90 **10c** / mean 6.7c.  Weather is not special
because nobody is emotional about Austin -- it is special because its favorite
band is TIGHT.  The sentiment hypothesis picked the wrong variable: what
separates a harvestable bias from an unharvestable one here is the spread of the
market it lives in, and the emotive categories (Entertainment, Politics, Sports)
are not the tight ones.  Weather's own spread split makes the same point from
the inside -- its <=1c bucket is **+0.94c [+0.39, +1.50]**, its only bucket that
excludes zero.

**Across the other 11 categories, no category passes H1.**  Pooled, the 96-99c favorite band nets
**-2.67c/contract [-3.95, -1.39]** -- significantly NEGATIVE, the opposite of
the hypothesis.  The three categories with a positive point estimate
(Entertainment +1.56, Mentions +1.00, Crypto +1.24) all fail on the same gate:
they have seen almost no losses, so they cannot bound their own downside.  Note
the exchange-wide "Climate and Weather" category is NOT the 7-series study --
it is 12 series including KXHIGHTATL/DAL/HOU/PHX capped at 40 markets each, and
at that thinner sample (196 events, 4 losses) it reads -0.10c and fails.

**H2 passes pooled (-0.91c) and in 7 of 12 categories** -- Climate and Weather,
Economics, Elections, Entertainment, Financials, Mentions, and (at p=0.08)
Science and Technology.  Longshots really are overpriced, everywhere, exactly as
the weather-only study found.  But this repo has already measured what happens
when you try to harvest it: fading a longshot means BUYING THE OTHER SIDE at its
own ask, and the +0.6c gross bias is smaller than the spread you cross to get
it.  A confirmed bias that is only accessible through the expensive side of the
book is a description of the market, not a trade.

**THE PREMISE OF THIS WHOLE SCRIPT WAS WRONG, AND THAT IS THE REAL FINDING.**
It was built on "the fee is quadratic, so it collapses at 98c and the bias
becomes harvestable there."  The fee does collapse -- 0.14c/contract at 98c.
But the SPREAD in that same 96-99c band is **median 1c, p75 3c, p90 10c**
(mean 6.7c -- quote the median, the mean is the tail).  Even 1c is ~7x the 0.14c
fee, and every price bucket is net-negative because buying at the ask on both
sides of one market loses the spread plus both fees whatever the market believes
(event-weighted, so each row reconciles as NET = won - paid - fee):

      bucket    fee   spread      NET
       1-5     0.12      1.4    -0.91
      36-50    1.65      6.2   -10.05
      81-90    0.84      9.4    -7.64
      96-99    0.14      6.1    -2.67

⚠ AND THE SPREAD IS NOT MERELY IN THE WAY -- REMOVING IT DOES NOT REVEAL AN
EDGE.  Cutting the favorite band by the spread of the market each sample came
from gives a clean monotone gradient, and a tightest bucket that is still not
positive:

      spread <= 1c   520 ev   -0.70c  [-1.93, +0.53]
      spread 2-4c    304 ev   -1.68c  [-3.71, +0.35]
      spread >= 5c   207 ev   -6.22c  [-9.54, -2.91]

So the wide markets explain how MUCH is lost; they do not conceal a profit in
the tight ones.  Where execution is cheapest the favorite band is indistinguishable
from zero and pointed the wrong way.

So the honest statement is NOT "the spread is the binding constraint instead of
the fee."  Tightness turns out to be NECESSARY BUT NOT SUFFICIENT, and the two
spread splits say so together:

      <= 1c spread, exchange-wide   520 ev   -0.70c  [-1.93, +0.53]
      <= 1c spread, weather only    445 ev   +0.94c  [+0.39, +1.50]

Same execution cost, opposite results.  Cheap execution is what LETS a bias show
up; it does not manufacture one.  Across the exchange, at 96-99c, there is
nothing on the favorite side to harvest at either cost level.  In weather there
may be something, and it is visible only because the spread there is small
enough not to bury it.  The original premise -- that
the quadratic fee was the thing standing in the way, so the answer lay wherever
the fee vanishes -- was wrong about what the obstacle was.  There was no
obstacle; there was no edge.

Where a next attempt could go, stated as a hypothesis and not a result: the only
lever left is not paying the ask at all.  This whole study measures taking, and
the repo has already measured resting orders once (`backtest --strategy 2
--passive`, verdict: NO, because the taker rule was already patient).  A
favorite-longshot version of that question is different enough to be worth
asking, but it needs depth data this script cannot see.

⚠ WHAT "CATEGORY" MEANS HERE, because it is easy to over-read.  The selection
takes the 12 highest-volume series per category, so "Sports" is the twelve most
traded Sports series out of the 3,626 Kalshi lists -- KXMLBGAME, KXNCAAFGAME,
KXUFCFIGHT, the World Cup ones, ATP/PGA -- not Sports as a whole.  Every
category verdict is about its LIQUID CORE.  That is the right place to look
(a bias you cannot trade is not interesting) but it is not a statement about the
long tail, where spreads are wider still and the answer can only be worse.
Note too that "Climate and Weather" here is 12 series including KXHIGHTATL/DAL/
HOU/PHX, so it is NOT the same sample as the 7-series weather study.

Two supporting numbers, both pointing the same way:
* **43.9% of all samples, and 46.8% inside the 96-99c band, come from a bar in
  which nothing traded at all.**  Restricting to bars that actually traded moves
  the favorite band from -2.67c to -0.73c: most of the apparent loss lives in
  quotes nobody was standing behind.  That cuts both ways -- it also means the
  ask being read there is not a price anyone could have filled.
* Per-series fee coefficients mattered but not decisively in this sample: of the
  117 selected series, KXMLBGAME charges HALF fee and 16 carry maker-fee
  variants.  Assuming 0.07 would have mispriced MLB by ~0.9c/contract at 50c.

    PYTHONPATH=. .venv/bin/python scripts/favorite_bias.py --days 90
    PYTHONPATH=. .venv/bin/python scripts/favorite_bias.py --universe all --days 90
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable, NamedTuple, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp

from weather_arb.backtest import _KALSHI, _candle_series, _kalshi_get, _settled_markets
from weather_arb.fees import fee_cents
from weather_arb.settlement_basis import SERIES_ICAO

_UA = "weather-arb-research (read-only calibration study)"
HORIZONS_H = (24, 12, 6, 3)
# Fixed, declared before the first run. Edges are the interesting cells; the
# middle is included so the whole curve is visible rather than two cherry-picked ends.
BUCKETS = ((1, 5), (6, 10), (11, 20), (21, 35), (36, 50),
           (51, 65), (66, 80), (81, 90), (91, 95), (96, 99))
# The only two cells that are TESTED (see the pre-registration in the docstring).
FAVORITE_BAND = (96, 99)
LONGSHOT_BAND = (1, 5)
MIN_EVENTS = 30          # below this a band is reported but not tested
MIN_LOSSES = 5           # below this a band cannot bound its own downside (see band_test)
BASE_FEE_COEFFICIENT = 0.07


class SeriesMeta(NamedTuple):
    """What `/series` says about one series -- crucially including its own fee.

    ``coeff`` is ``0.07 * fee_multiplier``. Kalshi charges some series half fee
    and some none at all, so a single hard-coded 0.07 would make the net-edge
    column fiction on exactly the heavily-traded series (MLB derivatives) where
    the answer matters most.
    """

    ticker: str
    category: str
    title: str
    coeff: float
    fee_type: str
    frequency: str


async def series_catalog(session: aiohttp.ClientSession) -> dict[str, SeriesMeta]:
    """Every series Kalshi publishes, keyed by ticker (one unpaginated call)."""
    data = await _kalshi_get(session, f"{_KALSHI}/series", {})
    out: dict[str, SeriesMeta] = {}
    for s in data.get("series", []):
        if not isinstance(s, dict):
            continue
        ticker = str(s.get("ticker") or "")
        if not ticker:
            continue
        try:
            mult = float(s.get("fee_multiplier", 1) or 0.0)
        except (TypeError, ValueError):
            mult = 1.0
        out[ticker] = SeriesMeta(
            ticker=ticker,
            category=str(s.get("category") or "Uncategorised"),
            title=str(s.get("title") or ticker),
            coeff=BASE_FEE_COEFFICIENT * mult,
            fee_type=str(s.get("fee_type") or "quadratic"),
            frequency=str(s.get("frequency") or ""),
        )
    return out


def _ts(raw: object) -> Optional[int]:
    if not raw:
        return None
    try:
        return int(datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _fp(raw: object) -> float:
    """A Kalshi ``*_fp`` fixed-point string as a float.

    ⚠ The fields are ``volume_fp``/``open_interest_fp``. Reading plain
    ``volume``/``open_interest`` returns None everywhere and reads as "this
    market never traded" -- a mistake that nearly killed a real finding here once.
    """
    try:
        return float(str(raw))
    except (TypeError, ValueError):
        return 0.0


class MarketRow(NamedTuple):
    """One settled market, as enumeration found it (no prices yet).

    ``open_ts`` is recorded but not used by the analysis: it is in the on-disk
    enumeration cache so a later question about market lifetime (which horizons
    a series can even reach -- an hourly crypto market cannot reach 3h) can be
    answered without re-enumerating at ~3 req/s.
    """

    ticker: str
    series: str
    event: str
    result: str
    close_ts: int
    open_ts: Optional[int]
    volume: float


def _market_row(raw: dict[str, Any]) -> Optional[MarketRow]:
    result = raw.get("result")
    ticker = str(raw.get("ticker") or "")
    close_ts = _ts(raw.get("close_time"))
    if result not in ("yes", "no") or not ticker or close_ts is None:
        return None
    series = str(raw.get("series_ticker") or "") or ticker.split("-")[0]
    return MarketRow(
        ticker=ticker,
        series=series,
        # ⚠ Kalshi's own grouping. `event_of()` guesses it from the ticker and is
        # only right for the weather shape; see its docstring.
        event=str(raw.get("event_ticker") or "") or event_of(ticker),
        result=str(result),
        close_ts=close_ts,
        open_ts=_ts(raw.get("open_time")),
        volume=_fp(raw.get("volume_fp")),
    )


async def _gather_limited(coros: Iterable[Any], concurrency: int) -> list[Any]:
    """Run coroutines with a bounded fan-out.

    Kalshi's public API rate-limits hard: at concurrency 32 a probe got 69 of 80
    requests 429'd, and raising the fan-out from 8 to 24 did not move throughput
    off ~3 req/s at all. `_kalshi_get` backs off, so the only thing a wide
    fan-out buys is a longer backoff queue.
    """
    sem = asyncio.Semaphore(concurrency)

    async def run(c):
        async with sem:
            return await c

    return await asyncio.gather(*[run(c) for c in coros])


async def enumerate_series(
    session: aiohttp.ClientSession, series: list[str], min_close_ts: int,
    concurrency: int, progress: bool = True,
) -> dict[str, list[MarketRow]]:
    """Settled markets per series over the window. This is the slow phase."""
    out: dict[str, list[MarketRow]] = {}
    done = 0
    total = len(series)

    async def one(tk: str) -> tuple[str, list[MarketRow]]:
        nonlocal done
        raws = await _settled_markets(session, tk, min_close_ts)
        rows = [r for r in (_market_row(m) for m in raws) if r is not None]
        done += 1
        if progress and done % 250 == 0:
            print(f"    ...enumerated {done}/{total} series", file=sys.stderr, flush=True)
        return tk, rows

    for tk, rows in await _gather_limited((one(t) for t in series), concurrency):
        if rows:
            out[tk] = rows
    return out


def select_markets(
    found: dict[str, list[MarketRow]], catalog: dict[str, SeriesMeta],
    series_per_category: int, min_markets: int, markets_per_series: int,
) -> tuple[list[MarketRow], dict[str, int]]:
    """Apply the pre-registered selection rule. Nothing here looks at `result`.

    Top series per category by TOTAL SETTLED VOLUME, then markets spaced evenly
    by close time so a series contributes to both halves of the chronological
    split rather than clumping into one regime.
    """
    by_cat: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for tk, rows in found.items():
        if len(rows) < min_markets:
            continue
        meta = catalog.get(tk)
        if meta is None:
            continue
        by_cat[meta.category].append((sum(r.volume for r in rows), tk))

    picked: list[MarketRow] = []
    per_cat: dict[str, int] = {}
    for cat, entries in by_cat.items():
        entries.sort(reverse=True)
        chosen = [tk for _, tk in entries[:series_per_category]]
        per_cat[cat] = len(chosen)
        for tk in chosen:
            rows = sorted(found[tk], key=lambda r: r.close_ts)
            if len(rows) > markets_per_series:
                step = len(rows) / markets_per_series
                rows = [rows[min(len(rows) - 1, int(i * step))]
                        for i in range(markets_per_series)]
            picked.extend(rows)
    return picked, per_cat


def bar_as_of(candles, ts: int, tol_s: int):
    """The last candle at or BEFORE ``ts``, or ``None``.

    Strictly backward-looking: a candle after ``ts`` would be information the
    decision could not have had.  ``None`` if the newest prior candle is staler
    than ``tol_s``, so a long trading gap reads as unknown rather than as a live
    quote -- and if ``ts`` predates the market's first bar (a horizon longer than
    the market's whole life, routine for the hourly crypto series) nothing is
    returned at all rather than the opening print.
    """
    best = None
    for c in candles:
        if c.ts <= ts:
            best = c
        else:
            break
    if best is None or ts - best.ts > tol_s:
        return None
    return best


def price_as_of(candles, ts: int, tol_s: int) -> tuple[Optional[int], Optional[int]]:
    """(yes_ask, no_ask) from the last candle at or before ``ts``."""
    bar = bar_as_of(candles, ts, tol_s)
    return (bar.yes_ask, bar.no_ask) if bar is not None else (None, None)


async def fetch_prices(
    session: aiohttp.ClientSession, markets: list[MarketRow],
    catalog: dict[str, SeriesMeta], concurrency: int, progress: bool = True,
) -> list[dict[str, Any]]:
    """Candle-sample every selected market at each pre-registered horizon."""
    obs: list[dict[str, Any]] = []
    done = 0
    total = len(markets)

    async def one(m: MarketRow) -> list[dict[str, Any]]:
        nonlocal done
        lo = m.close_ts - max(HORIZONS_H) * 3600 - 7200
        cs = await _candle_series(session, m.series, m.ticker, lo, m.close_ts,
                                  period_minutes=60)
        done += 1
        if progress and done % 500 == 0:
            print(f"    ...priced {done}/{total} markets", file=sys.stderr, flush=True)
        if not cs:
            return []
        meta = catalog.get(m.series)
        coeff = meta.coeff if meta else BASE_FEE_COEFFICIENT
        rows = []
        for h in HORIZONS_H:
            ts = m.close_ts - h * 3600
            bar = bar_as_of(cs, ts, tol_s=3 * 3600)
            if bar is None:
                continue
            for side, ask in (("yes", bar.yes_ask), ("no", bar.no_ask)):
                if ask is None or not (1 <= ask <= 99):
                    continue
                rows.append({
                    "ticker": m.ticker, "series": m.series,
                    "category": meta.category if meta else "Uncategorised",
                    "event": m.event, "horizon_h": h, "side": side,
                    "ask": int(ask), "won": (m.result == side),
                    "close_ts": m.close_ts, "coeff": coeff,
                    "fee_type": meta.fee_type if meta else "quadratic",
                    "bar_volume": float(bar.volume or 0.0),
                })
        return rows

    for rows in await _gather_limited((one(m) for m in markets), concurrency):
        obs.extend(rows)
    return obs


async def collect(
    days: int, universe: str, concurrency: int, series_per_category: int,
    min_markets: int, markets_per_series: int, cache: Optional[str] = None,
) -> list[dict[str, Any]]:
    timeout = aiohttp.ClientTimeout(total=300)
    min_close = int(datetime.now(timezone.utc).timestamp()) - (days + 3) * 86400
    conn = aiohttp.TCPConnector(limit=max(concurrency, 4))
    async with aiohttp.ClientSession(timeout=timeout, connector=conn,
                                     headers={"User-Agent": _UA}) as s:
        catalog = await series_catalog(s)
        if universe == "weather":
            wanted = [t for t in SERIES_ICAO if t in catalog] or list(SERIES_ICAO)
            for t in wanted:                       # keep the weather path self-contained
                catalog.setdefault(t, SeriesMeta(t, "Climate and Weather", t,
                                                 BASE_FEE_COEFFICIENT, "quadratic", "daily"))
        else:
            wanted = sorted(catalog)
        print(f"  universe={universe}: {len(wanted)} series, {days}d window",
              file=sys.stderr, flush=True)

        found = await _load_or_enumerate(s, wanted, min_close, concurrency, cache)
        if universe == "weather":
            markets = [r for rows in found.values() for r in rows]
            per_cat = {"Climate and Weather": len(found)}
        else:
            markets, per_cat = select_markets(found, catalog, series_per_category,
                                              min_markets, markets_per_series)
        print(f"  enumerated {sum(len(v) for v in found.values())} settled markets in "
              f"{len(found)} series; selected {len(markets)} markets across "
              f"{len(per_cat)} categories", file=sys.stderr, flush=True)
        return await fetch_prices(s, markets, catalog, concurrency)


async def _load_or_enumerate(session, wanted, min_close, concurrency, cache):
    """Enumeration is the slow phase (~3 req/s x 14k series); cache it on disk."""
    if cache and os.path.exists(cache):
        with open(cache, encoding="utf-8") as fh:
            raw = json.load(fh)
        if raw.get("min_close") == min_close and raw.get("n_series") == len(wanted):
            print(f"  reusing enumeration cache {cache}", file=sys.stderr, flush=True)
            return {k: [MarketRow(*r) for r in v] for k, v in raw["found"].items()}
        print(f"  cache {cache} is for a different window -- re-enumerating",
              file=sys.stderr, flush=True)
    found = await enumerate_series(session, wanted, min_close, concurrency)
    if cache:
        with open(cache, "w", encoding="utf-8") as fh:
            json.dump({"min_close": min_close, "n_series": len(wanted),
                       "found": {k: [list(r) for r in v] for k, v in found.items()}}, fh)
    return found


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta (Lentz's method)."""
    tiny = 1e-30
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, 200):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        c = 1.0 + aa / c
        if abs(d) < tiny:
            d = tiny
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        c = 1.0 + aa / c
        if abs(d) < tiny:
            d = tiny
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        de = d * c
        h *= de
        if abs(de - 1.0) < 3e-12:
            break
    return h


def t_sf(t: float, df: float) -> float:
    """P(T > t) for Student's t.

    A normal approximation would do for the pooled test (hundreds of clusters)
    but NOT for the per-category family, where a thin category can have 30-40
    event clusters and the normal tail is optimistic exactly where a false
    positive would be most tempting.  stdlib only, so the regularised incomplete
    beta is spelled out above.
    """
    if df <= 0:
        return 0.5
    x = df / (df + t * t)
    a, b = df / 2.0, 0.5
    if x <= 0.0:
        ib = 0.0
    elif x >= 1.0:
        ib = 1.0
    else:
        lbeta = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                 + a * math.log(x) + b * math.log(1.0 - x))
        front = math.exp(lbeta)
        ib = front * _betacf(a, b, x) / a if x < (a + 1.0) / (a + b + 2.0) \
            else 1.0 - math.exp(lbeta) * _betacf(b, a, 1.0 - x) / b
    half = min(1.0, max(0.0, ib)) / 2.0
    return half if t > 0 else 1.0 - half


def holm(pvals: dict[str, float]) -> dict[str, float]:
    """Holm step-down adjustment over a declared family.

    Hundreds of series x ten buckets is thousands of cells and the best of them
    is always impressive.  Only the pre-registered family goes through here; the
    descriptive tables are never adjusted because they are never tested.
    """
    items = sorted(pvals.items(), key=lambda kv: kv[1])
    m = len(items)
    out: dict[str, float] = {}
    running = 0.0
    for i, (key, p) in enumerate(items):
        running = max(running, min(1.0, (m - i) * p))
        out[key] = running
    return out


def event_of(ticker: str) -> str:
    """``KXHIGHMIA-26SEP08-B90.5`` -> ``KXHIGHMIA-26SEP08`` (the city-day event).

    ⚠ FALLBACK ONLY.  This assumes the weather ticker shape SERIES-DATE-STRIKE
    and is simply wrong elsewhere: ``KXMLBGAME-26SEP092210CINLAD-LAD`` happens to
    work, but plenty of series carry a different number of segments, and getting
    the event wrong silently breaks the clustering that decides this whole study.
    Every observation therefore carries Kalshi's OWN ``event_ticker``; this runs
    only when the API omitted one.
    """
    return "-".join(ticker.split("-")[:2])


def net_edge(o: dict[str, Any]) -> float:
    """Cents kept per contract: payout minus what you paid minus the fee.

    The fee uses the OBSERVATION'S OWN series coefficient, not a global 0.07.
    """
    coeff = float(o.get("coeff", BASE_FEE_COEFFICIENT))
    return (100.0 if o["won"] else 0.0) - o["ask"] - fee_cents(o["ask"], 100, coeff) / 100.0


def _event_key(o: dict[str, Any]) -> str:
    return str(o.get("event") or event_of(o["ticker"]))


def cluster_stats(xs: list[dict[str, Any]]) -> tuple[int, float, float, float]:
    """(events, mean net edge, CI low, CI high), clustered by event.

    ⚠⚠ CLUSTERING IS NOT COSMETIC -- IT FLIPPED THE CONCLUSION OF THIS STUDY.
    A single market contributes up to 8 observations here (4 horizons x 2 sides)
    and they are strongly correlated; markets *within* an event are correlated
    too, because exactly one of the buckets settles YES.  Treating them as
    independent understates the standard error badly.

    Measured on the 96-99c bucket, 120 days of weather markets:

        naive, all 1,381 observations independent : +0.78c  [+0.23, +1.32]  "significant"
        clustered by event (464 events)           : +0.68c  [-0.04, +1.39]  NOT significant

    Same data, opposite answer.  The clustered figure is the honest one.
    """
    by: dict[str, list[float]] = defaultdict(list)
    for o in xs:
        by[_event_key(o)].append(net_edge(o))
    per = [statistics.mean(v) for v in by.values()]
    n = len(per)
    if n < 2:
        return n, (per[0] if per else 0.0), float("-inf"), float("inf")
    m = statistics.mean(per)
    se = statistics.stdev(per) / math.sqrt(n)
    return n, m, m - 1.96 * se, m + 1.96 * se


def _wilson(k: float, n: int, z: float = 1.959963985) -> tuple[float, float]:
    """Score interval for a rate. Unlike a normal/t interval it stays sane at k=n
    (zero observed failures), which is exactly the case that matters below."""
    if n == 0:
        return 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(max(0.0, p * (1 - p) / n + z * z / (4 * n * n)))
    return max(0.0, (c - m) / d), min(1.0, (c + m) / d)


def band_test(xs: list[dict[str, Any]], direction: int) -> dict[str, Any]:
    """One PRE-REGISTERED directional test on one price band, clustered by event.

    ``direction`` is +1 for "favorites should be profitable" and -1 for
    "longshots should lose"; the p-value is one-sided in that declared direction,
    which is legitimate only because the direction was fixed in advance.

    ⚠⚠ THE t-INTERVAL IS NOT TRUSTWORTHY AT THE EXTREMES, and the first
    exchange-wide run proved it.  Entertainment's 96-99c band came back
    +1.56c with a 95% CI of [+1.32, +1.80] and a Holm-adjusted p of 0.0000 --
    apparently the cleanest result on the exchange.  It had **zero losing events
    out of 36**.  The payoff here is +2c or -98c, so with no loss in the sample
    the observed variance is the variance of the WINS ONLY and the interval
    describes nothing.  ONE more losing event takes that +1.56c to -1.14c.

    So three fragility fields travel with every test, and they are the reason a
    band can be "significant" and still not a finding:

      ``losses``        event clusters in which at least one observation lost
      ``plus_one``      the mean recomputed with one extra loss at the band's mean ask
      ``wilson``        net-edge interval from a Wilson CI on the event win rate,
                        which does not collapse when no loss has been seen

    The Wilson bound is the honest one at the extremes: net(p) = 100p - ask - fee
    is linear in the win rate, so an interval on the rate maps straight onto it.
    """
    by: dict[str, list[float]] = defaultdict(list)
    won: dict[str, list[bool]] = defaultdict(list)
    for o in xs:
        by[_event_key(o)].append(net_edge(o))
        won[_event_key(o)].append(bool(o["won"]))
    per = [statistics.mean(v) for v in by.values()]
    n = len(per)
    res = {"n_obs": len(xs), "events": n, "mean": statistics.mean(per) if per else 0.0,
           "lo": float("-inf"), "hi": float("inf"), "p": 1.0, "testable": False,
           "losses": 0, "plus_one": 0.0, "wlo": float("-inf"), "whi": float("inf"),
           "fragile": True}
    if not per:
        return res
    # Events that actually SAW downside. Counting only total wipeouts would miss
    # a mixed event -- several near-certain NOs in one event where one of them
    # broke -- and downside observed is the whole point of this number.
    res["losses"] = sum(1 for v in won.values() if not all(v))
    ask = statistics.mean(o["ask"] for o in xs)
    fee = statistics.mean(fee_cents(o["ask"], 100, float(o.get("coeff", BASE_FEE_COEFFICIENT)))
                          for o in xs) / 100.0
    res["plus_one"] = statistics.mean(per + [-ask - fee])
    k = sum(statistics.mean([1.0 if b else 0.0 for b in v]) for v in won.values())
    plo, phi = _wilson(k, n)
    res["wlo"], res["whi"] = 100.0 * plo - ask - fee, 100.0 * phi - ask - fee
    # A band with almost no observed losses cannot bound its own downside.
    res["fragile"] = res["losses"] < MIN_LOSSES
    if n < 2:
        return res
    se = statistics.stdev(per) / math.sqrt(n)
    res["lo"], res["hi"] = res["mean"] - 1.96 * se, res["mean"] + 1.96 * se
    if se > 0:
        res["p"] = t_sf(direction * res["mean"] / se, n - 1)
    res["testable"] = n >= MIN_EVENTS
    return res


def _in(o: dict[str, Any], band: tuple[int, int]) -> bool:
    return band[0] <= o["ask"] <= band[1]


def spreads(obs: list[dict[str, Any]]) -> dict[tuple[str, int], int]:
    """(ticker, horizon) -> quoted spread, from the two sides of the SAME bar.

    ``no_ask = 100 - yes_bid``, so ``yes_ask + no_ask - 100`` is exactly
    ``yes_ask - yes_bid``.  This is why every bucket below comes back negative:
    buying at the ask on both sides of one market is a guaranteed loss of the
    spread plus both fees, whatever the market believes.  The spread column is
    the yardstick the net column has to be read against -- a bucket losing less
    than half the spread is being held up by a real mispricing, one losing more
    is not.  It is NOT an alternative edge estimate: nobody fills you at the mid.
    """
    pair: dict[tuple[str, int], dict[str, int]] = defaultdict(dict)
    for o in obs:
        pair[(o["ticker"], o["horizon_h"])][o["side"]] = o["ask"]
    return {k: v["yes"] + v["no"] - 100
            for k, v in pair.items() if "yes" in v and "no" in v}


def _event_means(xs: list[dict[str, Any]], value) -> list[float]:
    """Per-event means of ``value``, so every column is weighted the way the test is."""
    by: dict[str, list[float]] = defaultdict(list)
    for o in xs:
        v = value(o)
        if v is not None:
            by[_event_key(o)].append(float(v))
    return [statistics.mean(v) for v in by.values() if v]


def table(obs: list[dict[str, Any]], label: str) -> str:
    """The descriptive curve.

    ⚠ EVERY column is EVENT-WEIGHTED, not observation-weighted, so the row
    reconciles: NET == won - paid - fee to rounding.  Mixing the two is a trap --
    an event contributing 56 observations and an event contributing 2 are one
    cluster each in the test, so an observation-weighted `won%` next to an
    event-weighted NET can look like arithmetic nonsense (Sports' 96-99c row read
    98.2% won at 97.9c paid but -3.65c net, because the losing events were small).
    """
    sp = spreads(obs)
    out = [f"  {label}   (n={len(obs)} observations, event-weighted)",
           f"    {'price':<9}{'events':>8}{'paid':>7}{'won':>7}{'fee':>6}{'spread':>8}"
           f"{'NET c/ct':>10}{'  clustered 95% CI':>21}   {'no-trade':>9}"]
    for lo, hi in BUCKETS:
        xs = [o for o in obs if lo <= o["ask"] <= hi]
        if len(xs) < 25:
            continue
        paid = statistics.mean(_event_means(xs, lambda o: o["ask"]))
        won = 100 * statistics.mean(_event_means(xs, lambda o: 1.0 if o["won"] else 0.0))
        fee = statistics.mean(_event_means(
            xs, lambda o: fee_cents(o["ask"], 100,
                                    float(o.get("coeff", BASE_FEE_COEFFICIENT))) / 100.0))
        quoted = _event_means(xs, lambda o: sp.get((o["ticker"], o["horizon_h"])))
        spread = statistics.mean(quoted) if quoted else float("nan")
        # Share of samples taken from a bar in which NOTHING traded -- the
        # quoted-but-empty-book case that makes a 96-99c ask look like free money.
        quiet = 100 * statistics.mean(_event_means(
            xs, lambda o: 0.0 if o.get("bar_volume") else 1.0))
        ev_n, m, clo, chi = cluster_stats(xs)
        verdict_ = "POSITIVE" if clo > 0 else ("NEGATIVE" if chi < 0 else "~ 0")
        out.append(f"    {lo:>3}-{hi:<5}{ev_n:>8}{paid:>7.1f}{won:>6.1f}%{fee:>6.2f}"
                   f"{spread:>8.1f}{m:>+10.2f}   [{clo:>+6.2f},{chi:>+6.2f}] {verdict_:<8}"
                   f"{quiet:>8.0f}%")
    return "\n".join(out)


def _fmt_test(name: str, r: dict[str, Any], adj: Optional[float]) -> str:
    """Two lines: the pre-registered test, then whether it can bound its downside."""
    head = (f"    {name:<26}{r['events']:>7} ev  {r['mean']:>+7.2f}c"
            f"   [{r['lo']:>+6.2f},{r['hi']:>+6.2f}]")
    if not r["testable"]:
        head += f"      --  (< {MIN_EVENTS} clusters)"
    else:
        p = adj if adj is not None else r["p"]
        head += f"  p={p:<7.4f}" + ("**" if p < 0.05 else "  ")
        if p < 0.05 and r["fragile"]:
            head += "  ⚠ FRAGILE, NOT A FINDING"
    tail = (f"      {r['losses']:>3} event(s) saw a loss  one more -> {r['plus_one']:>+7.2f}c"
            f"   win-rate CI net [{r['wlo']:>+7.2f},{r['whi']:>+7.2f}]")
    if r["fragile"]:
        tail += f"   <- under {MIN_LOSSES} losses: t-CI above is meaningless"
    return head + "\n" + tail


def preregistered(obs: list[dict[str, Any]]) -> str:
    """The only cells that are TESTED. Everything else printed is descriptive."""
    out = ["  " + "=" * 88,
           "  PRE-REGISTERED TESTS -- declared before the run, one-sided in the declared",
           "  direction. Holm-adjusted within each family. Nothing else below is a test.",
           "  " + "=" * 88, "",
           "  PRIMARY (family of 2, pooled over every category)"]
    fav = band_test([o for o in obs if _in(o, FAVORITE_BAND)], +1)
    lng = band_test([o for o in obs if _in(o, LONGSHOT_BAND)], -1)
    prim = holm({"fav": fav["p"], "lng": lng["p"]})
    out.append(_fmt_test(f"H1 favorites {FAVORITE_BAND[0]}-{FAVORITE_BAND[1]}c > 0",
                         fav, prim["fav"]))
    out.append(_fmt_test(f"H2 longshots {LONGSHOT_BAND[0]}-{LONGSHOT_BAND[1]}c < 0",
                         lng, prim["lng"]))
    out.append("")
    out.append("  A real favorite-longshot bias needs BOTH. One side alone is noise or a bug.")

    cats = sorted({o["category"] for o in obs})
    if len(cats) > 1:
        out += ["", f"  SECONDARY (family of {2 * len(cats)} = 2 bands x {len(cats)} categories,"
                    "  Holm-adjusted across all of them)"]
        raw: dict[str, float] = {}
        res: dict[str, dict[str, Any]] = {}
        for c in cats:
            sub = [o for o in obs if o["category"] == c]
            for tag, band, d in (("fav", FAVORITE_BAND, +1), ("lng", LONGSHOT_BAND, -1)):
                key = f"{c}|{tag}"
                res[key] = band_test([o for o in sub if _in(o, band)], d)
                raw[key] = res[key]["p"]
        adj = holm(raw)
        for c in cats:
            n_ev = len({_event_key(o) for o in obs if o["category"] == c})
            n_se = len({o["series"] for o in obs if o["category"] == c})
            out.append(f"    {c}  ({n_se} series, {n_ev} events)")
            for tag, lbl in (("fav", "favorites 96-99c > 0"), ("lng", "longshots 1-5c < 0")):
                out.append("  " + _fmt_test(lbl, res[f"{c}|{tag}"], adj[f"{c}|{tag}"]))
    return "\n".join(out)


def stability(obs: list[dict[str, Any]]) -> str:
    """The pre-declared stability check: same sign in both chronological halves."""
    out = ["  STABILITY -- nothing is fitted, so a real effect should hold in both halves.",
           "  A sign flip is a regime, not an edge.", ""]
    if not obs:
        return "\n".join(out)
    mid = sorted(o["close_ts"] for o in obs)[len(obs) // 2]
    out.append(f"    {'category':<26}{'band':<12}{'earlier':>10}{'later':>10}   "
               f"{'halves':<12}{'vs hypothesis'}")
    for cat in sorted({o["category"] for o in obs}) + ["ALL POOLED"]:
        sub = obs if cat == "ALL POOLED" else [o for o in obs if o["category"] == cat]
        for tag, band, want in (("favorites", FAVORITE_BAND, +1),
                                ("longshots", LONGSHOT_BAND, -1)):
            early = [o for o in sub if o["close_ts"] <= mid and _in(o, band)]
            late = [o for o in sub if o["close_ts"] > mid and _in(o, band)]
            if len(early) < 25 or len(late) < 25:
                continue
            _, me, _, _ = cluster_stats(early)
            _, ml, _, _ = cluster_stats(late)
            # Two DIFFERENT questions, which it is easy to conflate: do the
            # halves agree with each other (stability), and do they point the way
            # the hypothesis predicted (direction)? A band can be rock-steady and
            # steadily refute H1.
            agree = "FLIPS SIGN" if (me > 0) != (ml > 0) else (
                "both +" if me > 0 else "both -")
            matches = (me > 0 and ml > 0) if want > 0 else (me < 0 and ml < 0)
            out.append(f"    {cat:<26}{tag:<12}{me:>+10.2f}{ml:>+10.2f}   "
                       f"{agree:<12}{'as predicted' if matches else 'against H'}")
    return "\n".join(out)


def coverage(obs: list[dict[str, Any]]) -> str:
    """What was actually measured -- and the two things that limit what it means."""
    out = ["  COVERAGE", ""]
    n_ev = len({_event_key(o) for o in obs})
    by_series: dict[str, set[str]] = defaultdict(set)
    for o in obs:
        by_series[o["series"]].add(_event_key(o))
    top = sorted(by_series.items(), key=lambda kv: -len(kv[1]))[:6]
    span = (min(o["close_ts"] for o in obs), max(o["close_ts"] for o in obs))
    out.append(f"    {len(obs)} observations, {len({o['ticker'] for o in obs})} markets, "
               f"{n_ev} event clusters, {len(by_series)} series, "
               f"{len({o['category'] for o in obs})} categories")
    out.append("    close times span "
               f"{datetime.fromtimestamp(span[0], timezone.utc):%Y-%m-%d} .. "
               f"{datetime.fromtimestamp(span[1], timezone.utc):%Y-%m-%d}"
               "   (⚠ two separate limits: Kalshi's settled listing retains only"
               " ~70 days whatever --days says, and `_settled_markets` stops at 10"
               " pages = 2,000 markets, which bites 47 high-frequency series)")
    out.append("    biggest contributors by event share (the pooled estimand is their mix):")
    for s, evs in top:
        out.append(f"      {s:<28}{len(evs):>7} events{100*len(evs)/max(1,n_ev):>7.1f}%")

    out.append("")
    out.append("    horizon coverage -- a market shorter-lived than a horizon contributes")
    out.append("    nothing there, so short series drop out of the long horizons silently:")
    for h in HORIZONS_H:
        xs = [o for o in obs if o["horizon_h"] == h]
        out.append(f"      {h:>3}h before close{len(xs):>9} obs  "
                   f"{len({o['ticker'] for o in xs}):>6} markets")

    out.append("")
    out.append("    FEE COEFFICIENT ACTUALLY USED (per series, from Kalshi's fee_multiplier --")
    out.append("    assuming 0.07 everywhere would make the net column fiction on these):")
    seen: dict[tuple[float, str], set[str]] = defaultdict(set)
    for o in obs:
        seen[(round(float(o.get("coeff", BASE_FEE_COEFFICIENT)), 4),
              str(o.get("fee_type", "quadratic")))].add(o["series"])
    for (coeff, ftype), sers in sorted(seen.items()):
        note = "  <- HALF FEE" if 0 < coeff < BASE_FEE_COEFFICIENT else (
            "  <- NO FEE" if coeff == 0 else "")
        out.append(f"      coeff {coeff:<8}{ftype:<32}{len(sers):>4} series{note}")
        if coeff != BASE_FEE_COEFFICIENT or ftype != "quadratic":
            out.append(f"        {', '.join(sorted(sers)[:8])}")
    return "\n".join(out)


def depth_caveat(obs: list[dict[str, Any]]) -> str:
    """Re-run the primary tests on bars that actually traded.

    The ask is not a fill.  On a market whose outcome is already decided the sure
    side's book empties (measured here before: depth median 0 vs a control median
    of 2,443), so a 96-99c quote can be a phantom.  Restricting to bars with
    non-zero traded volume does not prove depth -- it only removes the samples
    where we can PROVE nothing traded.  The headline stays an upper bound.
    """
    traded = [o for o in obs if o.get("bar_volume")]
    fav_all = [o for o in obs if _in(o, FAVORITE_BAND)]
    quiet_all = 100.0 * (len(obs) - len(traded)) / max(1, len(obs))
    quiet_fav = 100.0 * sum(1 for o in fav_all if not o.get("bar_volume")) / max(1, len(fav_all))
    out = ["  DEPTH SENSITIVITY -- the ask is not a fill.", "",
           f"    bars with zero traded volume: {quiet_all:.1f}% of all samples, "
           f"{quiet_fav:.1f}% inside the {FAVORITE_BAND[0]}-{FAVORITE_BAND[1]}c band", ""]
    for label, xs in (("all samples", obs), ("bars that traded", traded)):
        fav = band_test([o for o in xs if _in(o, FAVORITE_BAND)], +1)
        lng = band_test([o for o in xs if _in(o, LONGSHOT_BAND)], -1)
        out.append(f"    {label:<20} favorites {fav['mean']:>+7.2f}c "
                   f"[{fav['lo']:>+6.2f},{fav['hi']:>+6.2f}] ({fav['events']} ev)   "
                   f"longshots {lng['mean']:>+7.2f}c "
                   f"[{lng['lo']:>+6.2f},{lng['hi']:>+6.2f}] ({lng['events']} ev)")
    out.append("")
    out.append("    Even a surviving positive number is an UPPER BOUND: it is top-of-book,")
    out.append("    one contract deep at best, before any market impact.")
    return "\n".join(out)


def spread_split(obs: list[dict[str, Any]]) -> str:
    """The favorite band cut by the SPREAD of the market each sample came from.

    DESCRIPTIVE, and added after the first run -- but it is a cut on a
    transaction COST known before the outcome, not a hunt through outcome cells,
    and it makes the conclusion weaker rather than stronger.  It is here because
    the mean spread is a badly skewed statistic (median 1c, p90 10c) and quoting
    the mean alone overstates the story.

    The point: if the spread were the only thing standing between this idea and
    a profit, the tightest markets would show the edge.  They do not.
    """
    sp = spreads(obs)
    fav = [o for o in obs if _in(o, FAVORITE_BAND) and (o["ticker"], o["horizon_h"]) in sp]
    out = ["  SPREAD SPLIT (descriptive) -- is the spread the only thing in the way?", ""]
    if len(fav) < 100:
        return "\n".join(out + ["    too few paired samples to split"])
    vals = sorted(sp[(o["ticker"], o["horizon_h"])] for o in fav)
    q = lambda f: vals[min(len(vals) - 1, int(len(vals) * f))]
    out.append(f"    spread in the {FAVORITE_BAND[0]}-{FAVORITE_BAND[1]}c band (n={len(vals)}): "
               f"median {q(.5)}c, p75 {q(.75)}c, p90 {q(.9)}c, mean "
               f"{statistics.mean(vals):.1f}c -- the mean is the tail, not the typical market")
    out.append("")
    for lbl, pred in (("spread <= 1c", lambda x: x <= 1), ("spread 2-4c", lambda x: 2 <= x <= 4),
                      ("spread >= 5c", lambda x: x >= 5)):
        xs = [o for o in fav if pred(sp[(o["ticker"], o["horizon_h"])])]
        if len(xs) < 25:
            continue
        r = band_test(xs, +1)
        quiet = 100.0 * sum(1 for o in xs if not o.get("bar_volume")) / len(xs)
        out.append(f"    {lbl:<14}{r['events']:>5} ev  {r['mean']:>+7.2f}c  "
                   f"[{r['lo']:>+6.2f},{r['hi']:>+6.2f}]  {r['losses']:>3} saw a loss"
                   f"  no-trade {quiet:>3.0f}%")
    tight = [o for o in fav if sp[(o["ticker"], o["horizon_h"])] <= 1]
    out.append("")
    if len(tight) >= 25:
        r = band_test(tight, +1)
        if r["lo"] > 0:
            out.append("    The tightest bucket IS positive, so on this sample execution cost is")
            out.append("    what stands between the bias and a profit -- not the absence of a bias.")
            out.append("    That is a claim about a 1c-spread subsample, and it is still an upper")
            out.append("    bound: top-of-book, one contract deep, before impact.")
        else:
            out.append("    Monotone in the spread, as it must be -- but the tightest bucket, where")
            out.append("    the spread is ~1c against a 0.14c fee, is STILL not positive. Widening")
            out.append("    spreads explain how much is lost, not whether anything is there to win.")
    return "\n".join(out)


def _halves(obs: list[dict[str, Any]], band: tuple[int, int]):
    mid = sorted(o["close_ts"] for o in obs)[len(obs) // 2]
    e = [o for o in obs if o["close_ts"] <= mid and _in(o, band)]
    l = [o for o in obs if o["close_ts"] > mid and _in(o, band)]
    return e, l


def verdict(obs: list[dict[str, Any]]) -> str:
    """Apply the pre-declared STABLE definition mechanically, so the reader is not
    left to assemble it out of four tables and talk themselves into something.

    A band is a FINDING only if it clears every gate: enough clusters, enough
    OBSERVED LOSSES to bound its downside, a Holm-adjusted p under 0.05, and the
    same sign in both chronological halves.  Reported per band, with the first
    gate it fails.
    """
    out = ["  " + "=" * 88, "  VERDICT (all four pre-declared gates, first failure named)",
           "  " + "=" * 88]
    cats = sorted({o["category"] for o in obs})
    raw, res = {}, {}
    for c in cats:
        sub = [o for o in obs if o["category"] == c]
        for tag, band, d in (("fav", FAVORITE_BAND, +1), ("lng", LONGSHOT_BAND, -1)):
            res[f"{c}|{tag}"] = band_test([o for o in sub if _in(o, band)], d)
            raw[f"{c}|{tag}"] = res[f"{c}|{tag}"]["p"]
    adj = holm(raw) if raw else {}
    for tag, band, d, lbl in (("fav", FAVORITE_BAND, +1, "H1 favorites 96-99c > 0"),
                              ("lng", LONGSHOT_BAND, -1, "H2 longshots 1-5c < 0")):
        pooled = band_test([o for o in obs if _in(o, band)], d)
        rows = [("POOLED (primary)", pooled, pooled["p"])]
        rows += [(c, res[f"{c}|{tag}"], adj[f"{c}|{tag}"]) for c in cats]
        out.append(f"\n  {lbl}")
        for name, r, p in rows:
            e, l = _halves(obs, band) if name.startswith("POOLED") else _halves(
                [o for o in obs if o["category"] == name], band)
            if not r["testable"]:
                why = (f"only {r['events']} event cluster"
                       f"{'' if r['events'] == 1 else 's'} (need {MIN_EVENTS})")
            elif r["losses"] < MIN_LOSSES:
                plural = "" if r["losses"] == 1 else "s"
                why = (f"only {r['losses']} event{plural} saw a loss "
                       f"(need {MIN_LOSSES}) -- cannot bound its downside")
            elif p >= 0.05:
                why = f"Holm-adjusted p={p:.4f}"
            elif len(e) < 25 or len(l) < 25:
                why = "too thin to split chronologically"
            else:
                me, ml = cluster_stats(e)[1], cluster_stats(l)[1]
                ok = (me > 0 and ml > 0) if d > 0 else (me < 0 and ml < 0)
                why = "" if ok else f"halves disagree ({me:+.2f} then {ml:+.2f})"
            # Gate (a) is the DECLARED t-based p, so it stays the gate. But the
            # Wilson interval is the instrument this file argues is honest at the
            # extremes, and when the two disagree the reader has to know: it means
            # the result rests on which interval you believe, not on the data.
            straddles = r["wlo"] < 0 < r["whi"]
            note = ("   (⚠ marginal: the Wilson interval on the win rate still "
                    "includes zero)" if not why and straddles else "")
            out.append(f"    {name:<26}{r['mean']:>+8.2f}c   "
                       f"{'*** FINDING ***' if not why else 'no: ' + why}{note}")
    return "\n".join(out)


def analyze(obs: list[dict[str, Any]]) -> str:
    out = ["=" * 92,
           "  FAVORITE-LONGSHOT BIAS vs KALSHI'S QUADRATIC FEE -- EXCHANGE-WIDE",
           "  price sampled at a FIXED horizon before close (never a price the market reached)",
           "=" * 92, ""]
    if not obs:
        return "\n".join(out + ["  no observations"])
    out += [verdict(obs), "", coverage(obs), "", preregistered(obs), "",
            depth_caveat(obs), "", spread_split(obs), ""]
    out.append("  " + "-" * 88)
    out.append("  DESCRIPTIVE ONLY BELOW THIS LINE -- these cells were not tested and may not")
    out.append("  be promoted to findings. NET is cents kept per contract, clustered by event;")
    out.append("  'no-trade' is the share of samples whose bar had zero traded volume.")
    out.append("")
    out.append(table(obs, "ALL HORIZONS POOLED"))
    out.append("")
    for cat in sorted({o["category"] for o in obs}):
        out.append(table([o for o in obs if o["category"] == cat], f"category: {cat}"))
        out.append("")
    for h in HORIZONS_H:
        out.append(table([o for o in obs if o["horizon_h"] == h], f"{h}h before close"))
        out.append("")
    out.append("  " + "-" * 88)
    out.append(stability(obs))
    out.append("=" * 92)
    return "\n".join(out)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--universe", choices=("weather", "all"), default="weather",
                    help="'weather' reproduces the original 7-series study")
    ap.add_argument("--concurrency", type=int, default=6,
                    help="Kalshi rate-limits hard; >8 just lengthens the backoff queue")
    ap.add_argument("--series-per-category", type=int, default=12)
    ap.add_argument("--min-markets", type=int, default=20)
    ap.add_argument("--markets-per-series", type=int, default=24)
    ap.add_argument("--cache", default=None,
                    help="path to cache the slow enumeration phase")
    ap.add_argument("--from-json", default=None,
                    help="re-analyse a previous --json dump without refetching")
    ap.add_argument("--json", default=None)
    args = ap.parse_args(argv)
    if args.from_json:
        with open(args.from_json, encoding="utf-8") as fh:
            obs = json.load(fh)
    else:
        obs = asyncio.run(collect(
            max(7, args.days), args.universe, args.concurrency,
            args.series_per_category, args.min_markets, args.markets_per_series,
            args.cache))
    print(analyze(obs))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(obs, fh)
        print(f"\nwrote {args.json} ({len(obs)} observations)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
