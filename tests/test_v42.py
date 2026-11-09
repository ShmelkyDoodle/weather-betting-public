"""Watchdog: a fresh heartbeat is not a healthy system (offline).

WHAT THIS LOCKS DOWN.  The watchdog used to check exactly one thing -- how long
since the tracker's last heartbeat.  That catches "the process died".  It cannot
catch "the process is alive and recording garbage", and that is not hypothetical:

  **The 2026-08 outage ran for WEEKS with a perfect heartbeat.**  Kalshi moved the
  order book from `orderbook` to `orderbook_fp`; `_orderbook_from_kalshi` read
  the old key, got `{}`, and returned an empty book on every call for every
  market.  Both strategies logged zero signals.  That was written up as "the
  market is always fully priced" when the truth was that the engine could not see
  the market at all.  Weeks of data and two conclusions had to be retracted.

So two checks now sit beside the heartbeat, and these tests pin the properties
that make them worth having:

1. The order-book probe must go through **the production parser**.  A probe that
   re-implemented parsing would have been written against the new field names,
   passed, and certified a blind engine as healthy.  `test_probe_catches_the_
   regression_it_exists_for` feeds the OLD payload shape through the REAL
   `_orderbook_from_kalshi` and asserts the probe reports blindness.
2. Neither check may cry wolf.  A watchdog you learn to ignore is worse than
   none, so "cannot tell" (network down, log too short, one empty market) must
   return None, never a false alarm.
"""
import asyncio
import dataclasses
import json
import os as _o, sys as _s, tempfile
_ROOT = _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__)))
_s.path.insert(0, _ROOT); _s.path.insert(0, _o.path.join(_ROOT, "scripts"))

import checkup

from weather_arb import watchdog
from weather_arb.config import Settings
from weather_arb.market_client import _orderbook_from_kalshi
from weather_arb import timeutils

failures = []
def check(n, c):
    print(("PASS" if c else "FAIL"), "-", n)
    if not c: failures.append(n)


# --- a fake client whose ONLY job is to hand payloads to the real parser ------
class _FakeClient:
    """Serves raw Kalshi payloads; parsing is done by production code."""

    def __init__(self, payloads, n_markets=6, only_first_series=False):
        self._payloads = payloads          # list of raw orderbook payload dicts
        self._n = n_markets
        self._only_first = only_first_series
        self._series_seen = []
        self.books_served = 0
        self.closed = False

    async def get_markets(self, *, series_ticker=None, status=None, max_pages=1, **kw):
        # The probe sweeps several series; a sparse exchange serves markets for
        # only some of them, which is how it can run out of books to sample.
        if series_ticker not in self._series_seen:
            self._series_seen.append(series_ticker)
        if self._only_first and self._series_seen.index(series_ticker) > 0:
            return []
        return [{"ticker": f"{series_ticker}-26SEP12-T{80 + i}"} for i in range(self._n)]

    async def get_orderbook(self, ticker, depth=32):
        payload = self._payloads[min(self.books_served, len(self._payloads) - 1)]
        self.books_served += 1
        return _orderbook_from_kalshi(ticker, payload)      # <- THE REAL PARSER

    async def close(self):
        self.closed = True


def _settings():
    return Settings.from_env()


def _run_probe(payloads, n_markets=6, only_first_series=False):
    made = {}
    def factory(settings):
        made["client"] = _FakeClient(payloads, n_markets, only_first_series)
        return made["client"]
    original = watchdog.KalshiClient
    watchdog.KalshiClient = factory
    try:
        return asyncio.run(watchdog.probe_order_book(_settings())), made.get("client")
    finally:
        watchdog.KalshiClient = original


# Kalshi's CURRENT encoding, and the one it served before the migration.
NEW_STYLE = {"orderbook_fp": {"yes_dollars": [["0.4000", "947.00"]],
                             "no_dollars": [["0.5500", "120.00"]]}}
OLD_STYLE = {"orderbook": {"yes": [[40, 947]], "no": [[55, 120]]}}
EMPTY = {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}
RENAMED = {"orderbook_v3": {"yes_units": [["0.40", "947"]]}}   # a FUTURE rename


def test_probe_reads_a_healthy_book():
    (ok, detail), client = _run_probe([NEW_STYLE])
    check("a readable book reports healthy", ok is True)
    check("it names the levels it saw", "levels" in detail)
    check("it stops at the first good book (cheap)", client.books_served == 1)
    check("the client is always closed", client.closed is True)


def test_the_old_encoding_still_reads():
    """Both keys are accepted, so a rollback in either direction is a non-event."""
    (ok, _), _ = _run_probe([OLD_STYLE])
    check("the pre-migration payload still parses healthy", ok is True)


def test_probe_catches_the_regression_it_exists_for():
    """⚠ THE WHOLE POINT. A future field rename must surface as an ALERT.

    `RENAMED` is what the next migration looks like from here: a payload the
    production parser does not recognise, which yields an empty book rather than
    an exception. That is precisely how the 2026-08 outage presented -- no crash,
    no traceback, just a market we could not see.
    """
    (ok, detail), client = _run_probe([RENAMED])
    check("an unrecognised payload shape is reported BLIND", ok is False)
    check("the alert names the culprit function",
          "_orderbook_from_kalshi" in detail)
    check("it sampled several books before alarming", client.books_served >= 4)
    # And confirm the premise: this really does parse to nothing, not raise.
    book = _orderbook_from_kalshi("T", RENAMED)
    check("the renamed payload really is silently empty, not an error",
          len(book.yes_bids) + len(book.no_bids) == 0)


def test_one_empty_market_is_not_an_alert():
    """A decided market, or one nobody is quoting, is ordinary. Only ALL of them
    being empty is the signature of a rename."""
    (ok, _), _ = _run_probe([EMPTY, EMPTY, NEW_STYLE])
    check("an empty book followed by a good one is healthy", ok is True)
    # A sparse exchange: fewer live books in total than the blind threshold.
    # Two empty books is not evidence of anything, so it must not alarm.
    (ok2, detail2), client2 = _run_probe([EMPTY], n_markets=2, only_first_series=True)
    check("too few samples to judge returns None, not False", ok2 is None)
    check("and says so", "inconclusive" in detail2)
    check("it really did run out of books rather than short-circuiting",
          client2.books_served == 2)


def test_probe_never_cries_wolf_when_the_network_is_down():
    class _Dead:
        async def get_markets(self, **kw): raise OSError("network down")
        async def get_orderbook(self, *a, **kw): raise OSError("network down")
        async def close(self): pass
    original = watchdog.KalshiClient
    watchdog.KalshiClient = lambda s: _Dead()
    try:
        ok, detail = asyncio.run(watchdog.probe_order_book(_settings()))
    finally:
        watchdog.KalshiClient = original
    check("a dead network is 'cannot tell', not 'broken'", ok is None)
    check("the heartbeat check owns that case", "network" in detail)


# --- passive activity check ---------------------------------------------------
def _log(lines):
    fh = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False)
    for rec in lines:
        fh.write(json.dumps(rec) + "\n")
    fh.close()
    return fh.name


def _rec(message, minutes_ago):
    ts = timeutils.utc_now().timestamp() - minutes_ago * 60
    from datetime import datetime, timezone
    return {"timestamp": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
            "level": "INFO", "message": message, "context": {}}


def test_activity_check_separates_working_from_merely_breathing():
    busy = _log([_rec("heartbeat", 100), _rec("model-edge signal", 5)])
    ok, detail = watchdog.recent_activity(busy, 180)
    check("evidence of real work reads healthy", ok is True)
    check("it says what it saw", "model-edge signal" in detail)

    # Heartbeats only: the exact shape of the 2026-08 outage.
    breathing = _log([_rec("heartbeat", m) for m in (200, 120, 60, 5)])
    ok2, detail2 = watchdog.recent_activity(breathing, 180)
    check("heartbeats alone are NOT healthy", ok2 is False)
    check("the alert explains the failure mode", "blind-engine" in detail2)
    _o.unlink(busy); _o.unlink(breathing)


def test_activity_check_admits_when_it_cannot_judge():
    """A log that does not reach back far enough is unknown, not broken."""
    short = _log([_rec("heartbeat", 2)])
    ok, detail = watchdog.recent_activity(short, 180)
    check("a too-short log returns None", ok is None)
    check("and says why", "reach back" in detail)
    ok2, _ = watchdog.recent_activity("/nonexistent/path.log", 180)
    check("a missing log returns None", ok2 is None)
    _o.unlink(short)


def test_the_checkup_verdict_fails_safe():
    """⚠ THE FIRST DRAFT OF `checkup.safety` GOT THIS WRONG, in the exact way this
    project has been burned by twice: it derived safety from a CIRCUMSTANCE.

    It computed live as `credentials AND not dry_run`, so a box with
    `dry_run=False` and `allow_live_model_edge=True` printed a confident
    "PAPER MODE -- no real money is reachable" purely because no API key happened
    to be installed. Drop a credential file on that box and it trades Strategy 2
    at -$3.26/trade, with the checkup still calling it safe. The verdict must key
    off the GATES, which is what `RiskManager.size_order` actually enforces.
    """
    base = Settings.from_env()
    def verdict(**kw):
        return checkup.safety(dataclasses.replace(base, **kw))

    text, todo = verdict(dry_run=True, allow_live_model_edge=True)
    check("dry_run on is paper mode regardless of the gate", "PAPER MODE" in text)
    check("and needs no action", todo == [])

    # The case the first draft mis-reported.
    text, todo = verdict(dry_run=False, allow_live_model_edge=True)
    check("dry_run OFF is never reported as paper mode", "PAPER MODE" not in text)
    check("an open Strategy-2 gate is the worst status", checkup.BAD in text)
    check("it names the -$3.26/trade cost", any("3.26" in t for t in todo))
    check("a missing credential is a circumstance, not a safeguard",
          any("not a safeguard" in t for t in todo))

    text, todo = verdict(dry_run=False, allow_live_model_edge=False)
    check("dry_run off with the gate shut is a warning, not an all-clear",
          checkup.WARN in text and "PAPER MODE" not in text)


def test_the_checkup_exit_code_reflects_the_worst_finding():
    """It should be usable from cron: 0 fine, 1 look at it, 2 act now."""
    check("the three statuses are distinguishable",
          len({checkup.OK, checkup.WARN, checkup.BAD}) == 3)
    # _line must embed the status so main() can rank it.
    for status in (checkup.OK, checkup.WARN, checkup.BAD):
        check(f"_line embeds {status.strip()} for ranking",
              f"[{status}]" in checkup._line(status, "x"))


def _storm_log(n_error_lines=400, heartbeat_every=120, pad=1800):
    """A log shaped like 2026-10-23: a heartbeat buried under an error storm.

    ``pad`` mimics the real ~1,954-byte average line that day, so a fixed 64 KB
    tail spans only a few dozen lines -- about a minute of wall clock.
    """
    fh = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False)
    for i in range(n_error_lines):
        msg = "heartbeat" if i % heartbeat_every == 0 else "orderbook fetch failed"
        # ⚠ COMPACT separators: the tracker logs `"message":"heartbeat"` with no
        # space, and `latest_heartbeat_utc` substring-matches exactly that. A
        # fixture using json.dumps' default `": "` spacing is silently invisible
        # to it -- which is how this test first failed.
        fh.write(json.dumps({
            "timestamp": _rec(msg, 0)["timestamp"], "level": "ERROR",
            "message": msg, "context": {"detail": "x" * pad}},
            separators=(",", ":")) + "\n")
    fh.close()
    return fh.name


def test_an_error_storm_cannot_fake_a_dead_tracker():
    """⚠⚠ THIS CRIED WOLF FOR A DAY ON 2026-10-23 -- 28 alerts saying "is the
    tracker running?" while the tracker was emitting 144 heartbeats on schedule.

    `latest_heartbeat_utc` read a FIXED 64 KB tail. That is hours of a quiet log
    (~500 KB/day) but during a network outage the tracker logs a failure per
    retry: that day it produced 75 MB in 24 hours at ~1,954 bytes a line, so
    64 KB covered ~34 lines, about ONE MINUTE, and the 10-minute heartbeat fell
    outside it. The cost was not noise alone -- it buried the real problem (the
    network was down) under a confident wrong diagnosis.

    Verified against the real log: replaying the three times the watchdog
    actually alerted, the old rule found 0 heartbeats at each; the growing scan
    finds them 5-9 minutes old.
    """
    path = _storm_log()
    # The old behaviour, reproduced exactly: a fixed 64 KB window.
    in_64k = [l for l in watchdog._tail_text(path, 65536).splitlines()
              if '"message":"heartbeat"' in l]
    check("a fixed 64KB tail finds NO heartbeat in a storm", in_64k == [])
    check("but the growing scan finds one",
          watchdog.latest_heartbeat_utc(path) is not None)
    _o.unlink(path)


def test_the_scan_still_gives_up_when_there_is_truly_no_heartbeat():
    """Growing the window must not turn a real outage into a shrug."""
    fh = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False)
    for _ in range(50):
        fh.write(json.dumps(_rec("orderbook fetch failed", 1), separators=(",", ":")) + "\n")
    fh.close()
    check("no heartbeat anywhere still returns None",
          watchdog.latest_heartbeat_utc(fh.name) is None)
    check("a missing file still returns None",
          watchdog.latest_heartbeat_utc("/nonexistent/x.log") is None)
    _o.unlink(fh.name)


def test_the_scan_is_cheap_on_a_normal_log():
    """A quiet log must still be answered from the first small read -- the whole
    point is to pay the cost only when the log is pathological."""
    fh = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False)
    for i in range(200):
        fh.write(json.dumps(_rec("heartbeat" if i % 5 == 0 else "model-edge signal", 1),
                            separators=(",", ":")) + "\n")
    fh.close()
    size = _o.path.getsize(fh.name)
    check("the fixture is smaller than one scan step", size < watchdog._HEARTBEAT_SCAN_START)
    check("and a heartbeat is found", watchdog.latest_heartbeat_utc(fh.name) is not None)
    check("the scan is bounded", watchdog._HEARTBEAT_SCAN_MAX >= 16 * 1024 * 1024)
    _o.unlink(fh.name)


def main():
    test_probe_reads_a_healthy_book()
    test_the_old_encoding_still_reads()
    test_probe_catches_the_regression_it_exists_for()
    test_one_empty_market_is_not_an_alert()
    test_probe_never_cries_wolf_when_the_network_is_down()
    test_activity_check_separates_working_from_merely_breathing()
    test_activity_check_admits_when_it_cannot_judge()
    test_the_checkup_verdict_fails_safe()
    test_the_checkup_exit_code_reflects_the_worst_finding()
    test_an_error_storm_cannot_fake_a_dead_tracker()
    test_the_scan_still_gives_up_when_there_is_truly_no_heartbeat()
    test_the_scan_is_cheap_on_a_normal_log()
    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
