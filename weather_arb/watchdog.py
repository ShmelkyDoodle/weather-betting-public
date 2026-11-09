"""External watchdog: is the tracker alive and are the mappings still valid?

Run on a schedule (the launchd job runs it every 30 min). Because it runs in a
*separate* process, it can catch the case the tracker itself can't report -- the
tracker being down, asleep, or crashed -- by checking how long since its last
heartbeat. It also re-verifies the settlement mappings.

⚠ A FRESH HEARTBEAT IS NOT A HEALTHY SYSTEM. The 2026-08 orderbook outage ran for
weeks with a perfect heartbeat while the engine could not read a single order
book (see `probe_order_book`). So two liveness checks sit alongside the
heartbeat: an ACTIVE probe that reads a real book through the production parser,
and a PASSIVE check that the tracker has actually evaluated anything recently.
On any problem it fires an alert (ntfy + alerts.log).

    python -m weather_arb.watchdog

Exit code is non-zero when something needs attention.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from typing import Optional

from .alerts import AlertManager
from .config import Settings
from .market_client import KalshiClient
from . import timeutils
from .verify_mappings import verify_all


def _tail_text(path: str, nbytes: int = 65536) -> str:
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - nbytes))
        return handle.read().decode("utf-8", "ignore")


# The heartbeat scan grows from this until it finds one, or gives up at the cap.
_HEARTBEAT_SCAN_START = 65536              # 64 KB
_HEARTBEAT_SCAN_MAX = 64 * 1024 * 1024     # 64 MB


def latest_heartbeat_utc(log_path: str) -> Optional[datetime]:
    """Timestamp of the most recent ``heartbeat`` line in the tracker log.

    ⚠⚠ THIS CRIED WOLF FOR A WHOLE DAY ON 2026-10-23, and the shape of the
    mistake is worth keeping.  It used to read a FIXED 64 KB tail.  That is
    plenty at the tracker's normal ~500 KB/day, where 64 KB spans hours -- but
    during a network outage the tracker logs a fetch failure per retry, and that
    day it produced **75 MB in 24 hours**, about 150x normal, at ~1,954 bytes a
    line.  64 KB then covered roughly **34 lines, or about one minute**, so the
    10-minute heartbeat fell outside the window and this returned ``None``.

    The watchdog duly alerted "no heartbeat found -- is the tracker running?"
    **28 times while the tracker was perfectly healthy** (144 heartbeats emitted
    that day, dead on schedule; the largest heartbeat gap in the whole
    three-month run is 20 minutes).  The cost was not just noise: it buried the
    REAL problem -- the network was down -- under a confident wrong diagnosis,
    and it did so at exactly the moment someone would be reading the alerts.

    So the scan now GROWS until it finds a heartbeat.  A quiet log is answered
    from the first 64 KB; a log mid-storm keeps doubling (~1-2 MB is enough even
    at the 2026-10-23 rate) and stops at `_HEARTBEAT_SCAN_MAX`.  Reading a few MB
    on a 30-minute job is free; a false "your system is down" is not.

    ``None`` now means the cap was reached, or the whole file was read, with no
    heartbeat in it -- which is genuinely worth an alert.
    """
    if not os.path.exists(log_path):
        return None
    try:
        size = os.path.getsize(log_path)
    except OSError:
        return None

    nbytes = _HEARTBEAT_SCAN_START
    while True:
        latest: Optional[str] = None
        for line in _tail_text(log_path, nbytes).splitlines():
            if '"message":"heartbeat"' not in line:
                continue
            try:
                latest = json.loads(line).get("timestamp")
            except json.JSONDecodeError:
                continue
        if latest:
            try:
                return timeutils.parse_isoformat_utc(str(latest))
            except ValueError:
                return None
        # Nothing yet. Stop once we have read the whole file or hit the cap.
        if nbytes >= size or nbytes >= _HEARTBEAT_SCAN_MAX:
            return None
        nbytes *= 2


# How many live books must come back parseable-but-empty before we call it blind.
# One empty book is ordinary (a decided market, or nobody quoting); all of them
# is the signature of a field rename.
_BLIND_SAMPLE = 4


def recent_activity(log_path: str, within_minutes: float = 180.0) -> tuple[Optional[bool], str]:
    """Has the tracker DONE anything lately, not merely said it is alive?

    The heartbeat is emitted by the supervisor and keeps ticking even when every
    evaluation underneath it is failing or finding nothing -- which is exactly
    the state the 2026-08 outage produced. This looks for evidence of real work
    in the recent log: a market-edge evaluation, a lock-in detection, or a
    validated observation.

    ``None`` (not an alert) when the tail we can afford to read does not reach
    back far enough to judge. Being unable to see is not the same as being broken,
    and only one of those is worth waking someone up for.
    """
    if not os.path.exists(log_path):
        return None, "no tracker log"
    cutoff = timeutils.utc_now().timestamp() - within_minutes * 60.0
    # Any of these proves a full evaluation cycle actually ran and produced a
    # decision, rather than the supervisor merely looping.
    markers = ("model-edge signal", "lock-in detected", "validated new daily max",
               "market discovery refreshed")
    seen: dict[str, int] = {}
    oldest: Optional[float] = None
    for line in _tail_text(log_path, nbytes=2_000_000).splitlines():
        try:
            rec = json.loads(line)
            ts = timeutils.parse_isoformat_utc(str(rec.get("timestamp"))).timestamp()
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
        if oldest is None:
            oldest = ts
        if ts < cutoff:
            continue
        msg = str(rec.get("message", ""))
        if msg in markers:
            seen[msg] = seen.get(msg, 0) + 1
    if seen:
        summary = ", ".join(f"{k} x{v}" for k, v in sorted(seen.items()))
        return True, summary
    if oldest is not None and oldest > cutoff:
        return None, "log tail does not reach back far enough to judge"
    return False, (
        f"no evaluation activity in {within_minutes:.0f} min — the tracker is "
        "running but has produced no signal, detection or observation. That is "
        "how the 2026-08 blind-engine outage looked from the outside."
    )


async def probe_order_book(settings: Settings) -> tuple[Optional[bool], str]:
    """Can the engine still SEE the market? Returns (ok, detail).

    ⚠⚠ THIS IS THE CHECK THAT WOULD HAVE CAUGHT THE 2026-08 OUTAGE ON DAY ONE.
    The heartbeat check proves the tracker is *alive*; it cannot prove the tracker
    can still *read*.  When Kalshi moved the book from ``orderbook`` to
    ``orderbook_fp``, `_orderbook_from_kalshi` returned an empty book on every
    call, for every market, for the whole paper run -- and the heartbeat stayed
    perfectly fresh throughout.  Both strategies logged zero signals, which was
    written up as "the market is always fully priced" when the truth was that we
    could not read the market at all.  Weeks of data and two conclusions had to be
    retracted.  The same migration bit the candles: ``volume_fp`` read as
    ``volume`` showed zero volume everywhere.

    It calls `client.get_orderbook()` -- **the method the engine itself calls,
    through the same `_orderbook_from_kalshi`**.  A probe that re-implemented the
    parsing would be worthless: the bug WAS the parsing, and a fresh
    implementation would have been written against the new field names, passed,
    and certified a blind engine as healthy.

    ``None`` means "could not tell" (network down, no open markets) and is
    deliberately NOT an alert -- a dead network is the heartbeat check's job, and
    a watchdog that cries wolf is one you learn to ignore.
    """
    series = [s for s, _ in settings.weather_series][:3]
    if not series:
        return None, "no weather series configured"

    client = KalshiClient(settings)
    checked = 0
    try:
        for series_ticker in series:
            try:
                markets = await client.get_markets(
                    series_ticker=series_ticker, status="open", max_pages=1
                )
            except Exception:                      # noqa: BLE001 - transient, not an alert
                continue
            for raw in markets:
                ticker = str(raw.get("ticker") or "")
                if not ticker:
                    continue
                try:
                    book = await client.get_orderbook(ticker)
                except Exception:                  # noqa: BLE001
                    continue
                checked += 1
                levels = len(book.yes_bids) + len(book.no_bids)
                if levels:
                    ask = book.best_yes_ask
                    return True, (f"{ticker}: {levels} levels, best yes ask "
                                  f"{ask.price_cents if ask else 'n/a'}c "
                                  f"({checked} book(s) sampled)")
                if checked >= _BLIND_SAMPLE:
                    return False, (
                        f"{checked} live order books all parsed EMPTY — the engine is "
                        "blind. Did Kalshi rename the orderbook fields again? See "
                        "_orderbook_from_kalshi; last sampled " + ticker
                    )
    finally:
        await client.close()

    if checked:
        return None, f"only {checked} book(s) readable, all empty — inconclusive"
    return None, "no open market could be fetched (network?)"


async def run_watchdog(settings: Settings) -> tuple[bool, list[str]]:
    """Return (healthy, problems)."""
    problems: list[str] = []

    hb = latest_heartbeat_utc(settings.tracker_log_path)
    if hb is None:
        problems.append(
            f"no heartbeat anywhere in {settings.tracker_log_path} (scanned up to "
            f"{_HEARTBEAT_SCAN_MAX // (1024 * 1024)} MB) — is the tracker running?"
        )
    else:
        age_min = (timeutils.utc_now() - hb).total_seconds() / 60.0
        if age_min > settings.heartbeat_stale_minutes:
            problems.append(
                f"tracker last heartbeat {age_min:.0f} min ago "
                f"(> {settings.heartbeat_stale_minutes:.0f}) — down, asleep, or crashed?"
            )

    try:
        results, ok = await verify_all(settings)
        if not ok:
            flagged = [f"{r.series}({r.status})" for r in results if not r.is_confirmed_ok]
            problems.append("settlement mapping needs review: " + ", ".join(flagged))
    except Exception as exc:  # noqa: BLE001 - report as a problem, don't crash
        problems.append(f"mapping verification failed: {exc}")

    # Can the engine still SEE? A fresh heartbeat does not answer this.
    try:
        ok, detail = await probe_order_book(settings)
        if ok is False:
            problems.append("ORDER BOOK UNREADABLE: " + detail)
    except Exception as exc:  # noqa: BLE001 - a broken probe must not mute the rest
        problems.append(f"order-book probe failed: {exc}")

    # Has it DONE anything? (Backstop for "alive and idle-but-broken".)
    try:
        ok, detail = recent_activity(
            settings.tracker_log_path, settings.watchdog_activity_minutes
        )
        if ok is False:
            problems.append("NO RECENT ACTIVITY: " + detail)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"activity check failed: {exc}")

    return (not problems), problems


def main(argv: Optional[list[str]] = None) -> int:
    settings = Settings.from_env()
    healthy, problems = asyncio.run(run_watchdog(settings))
    alerts = AlertManager(settings)
    if healthy:
        alerts.log_ok("watchdog: tracker alive, mappings confirmed")
        print("watchdog: all healthy")
        return 0
    body = "\n".join(f"- {p}" for p in problems)
    alerts.send("⚠️ weather-arb needs attention", body, priority="high")
    print("watchdog ALERT:\n" + body)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
