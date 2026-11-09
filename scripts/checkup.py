#!/usr/bin/env python3
"""One command, plain English: is this thing still working, and is it safe?

Written to be run by someone who has not looked at this project in three months
and does not want to remember which of six log files to grep.  It answers, in
order, the only questions that matter after a long unattended run:

    1. Is real money reachable?            (the safety gate)
    2. Is the tracker alive?               (heartbeat)
    3. Can it still SEE the market?        (the silent failure mode)
    4. Is it doing anything?               (activity)
    5. Is it running out of room?          (disk / ledger / logs)

⚠ WHY 3 EXISTS, because it is the one nobody thinks to check.  In 2026-08 this
system ran for WEEKS with a perfect heartbeat while the engine could not read a
single order book -- Kalshi had renamed a field and the parser silently returned
nothing.  Both strategies logged zero signals, which was written up as a finding
("the market is always fully priced") when the truth was that we were blind.
A green heartbeat is not a working system, and this script refuses to say
"healthy" on the strength of one.

Read-only: it opens the ledger read-only, never writes, and never trades.

    .venv/bin/python scripts/checkup.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from weather_arb.config import Settings
from weather_arb import timeutils
from weather_arb.watchdog import latest_heartbeat_utc, probe_order_book, recent_activity

OK, WARN, BAD, INFO = "  OK  ", " WARN ", " BAD  ", " ..   "


def _line(status: str, label: str, detail: str = "") -> str:
    return f"[{status}] {label}" + (f"\n            {detail}" if detail else "")


def _mb(path: str) -> float:
    try:
        return os.path.getsize(path) / 1e6
    except OSError:
        return 0.0


def safety(settings: Settings) -> tuple[str, list[str]]:
    """Can this place a real order?

    ⚠ THIS FUNCTION MUST FAIL SAFE, and the first draft did not.  It computed
    "live" as ``credentials AND not dry_run``, so a box configured with
    ``dry_run=False`` and ``allow_live_model_edge=True`` reported a confident
    "PAPER MODE — no real money is reachable" purely because no API key happened
    to be installed.  That is the same mistake this project has already been
    burned by twice: **deriving safety from a circumstance rather than from the
    gate.**  Add one credential file and that box trades Strategy 2 at a measured
    -$3.26/trade, with the checkup still saying it is safe.

    So the verdict keys off `dry_run` and `allow_live_model_edge` -- the actual
    locks, enforced in `RiskManager.size_order` -- and the absence of credentials
    is reported as a mitigating circumstance, never as the reason it is safe.
    """
    creds = settings.kalshi.has_credentials
    cred_text = ("no Kalshi credentials installed" if not creds
                 else "credentials ARE installed")
    detail = (f"dry_run = {settings.dry_run} · "
              f"allow_live_model_edge = {settings.allow_live_model_edge} · {cred_text}")

    if settings.dry_run:
        return _line(OK, "PAPER MODE — orders are journaled, never sent", detail), []

    # dry_run is OFF. From here the only thing between this and real orders is
    # the per-strategy gate, and possibly a missing key that could appear later.
    todo = []
    if settings.allow_live_model_edge:
        status, headline = BAD, "LIVE — Strategy 2 IS CLEARED TO TRADE"
        todo.append(
            "Strategy 2 is cleared for live trading and is measured at "
            "-$3.26/trade over 21 days. Unless this is deliberate, set "
            "WA_ALLOW_LIVE_MODEL_EDGE=false in .env and restart the tracker.")
    else:
        status, headline = WARN, "LIVE — lock-in only (Strategy 2 blocked by the gate)"
    if not creds:
        todo.append(
            "dry_run is OFF. No credentials are installed today, so nothing can "
            "transmit yet — but that is an accident, not a safeguard. Installing "
            "an API key would make this box trade immediately.")
    return _line(status, headline, detail), todo


def heartbeat(settings: Settings) -> tuple[str, list[str]]:
    hb = latest_heartbeat_utc(settings.tracker_log_path)
    if hb is None:
        return _line(BAD, "No heartbeat found — is the tracker running?"), [
            "launchctl list | grep weatherarb   # then: launchctl kickstart -k "
            "gui/$(id -u)/com.weatherarb.tracker"]
    age = (timeutils.utc_now() - hb).total_seconds() / 60.0
    if age > settings.heartbeat_stale_minutes:
        return _line(BAD, f"Tracker silent for {age:.0f} min"), [
            "The tracker is down, asleep or crashed. Restart it with: "
            "launchctl kickstart -k gui/$(id -u)/com.weatherarb.tracker"]
    return _line(OK, f"Tracker alive (heartbeat {age:.0f} min ago)"), []


def eyesight(settings: Settings) -> tuple[str, list[str]]:
    """THE important one. See the module docstring."""
    try:
        ok, detail = asyncio.run(probe_order_book(settings))
    except Exception as exc:                                   # noqa: BLE001
        return _line(WARN, "Order-book probe could not run", str(exc)), []
    if ok is True:
        return _line(OK, "Order book READABLE — the engine can see the market", detail), []
    if ok is None:
        return _line(INFO, "Order book readability: could not tell", detail), []
    return _line(BAD, "ORDER BOOK UNREADABLE — the engine is BLIND", detail), [
        "This is the 2026-08 failure mode repeating: Kalshi likely renamed a "
        "field again. Every signal logged since it broke is void. Fix "
        "_orderbook_from_kalshi in weather_arb/market_client.py, then treat data "
        "back to the break as unusable."]


def activity(settings: Settings) -> tuple[str, list[str]]:
    ok, detail = recent_activity(settings.tracker_log_path,
                                 settings.watchdog_activity_minutes)
    if ok is True:
        return _line(OK, "Doing real work", detail), []
    if ok is None:
        return _line(INFO, "Activity: could not tell", detail), []
    return _line(BAD, "Alive but producing nothing", detail), [
        "The supervisor is looping but no evaluation is completing. Check "
        "logs/tracker.out.log for repeated errors."]


def capacity(settings: Settings) -> tuple[str, list[str]]:
    db = settings.db_path
    free_gb = shutil.disk_usage(os.path.dirname(os.path.abspath(db)) or ".").free / 1e9
    db_mb = _mb(db)
    log_dir = os.path.dirname(os.path.abspath(settings.tracker_log_path))
    log_mb = sum(_mb(os.path.join(log_dir, f)) for f in os.listdir(log_dir)
                 if f.endswith(".log")) if os.path.isdir(log_dir) else 0.0
    detail = f"ledger {db_mb:,.0f} MB · logs {log_mb:,.0f} MB · {free_gb:,.0f} GB free"
    if free_gb < 5:
        return _line(BAD, "Running out of disk", detail), [
            "Under 5 GB free. The ledger grows ~30 MB/day."]
    if free_gb < 15:
        return _line(WARN, "Disk getting tight", detail), []
    return _line(OK, "Room to run", detail), []


def ledger(settings: Settings) -> tuple[str, list[str]]:
    """What the run has actually accumulated. Read-only connection."""
    try:
        conn = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return _line(WARN, "Could not open the ledger read-only", str(exc)), []
    try:
        def count(sql: str) -> str:
            try:
                return f"{conn.execute(sql).fetchone()[0]:,}"
            except sqlite3.Error:
                return "n/a"
        detail = (f"forecast snapshots {count('select count(*) from model_prediction_history')}"
                  f" · settled markets {count('select count(*) from market_results')}"
                  f" · journaled orders {count('select count(*) from open_limit_orders')}"
                  f" · confirmed fills {count('select count(*) from trades')}")
        return _line(INFO, "Data accumulated", detail), []
    finally:
        conn.close()


def main() -> int:
    settings = Settings.from_env()
    print("\n  weather-arb checkup — " + timeutils.utc_now().strftime("%Y-%m-%d %H:%M UTC"))
    print("  " + "-" * 68)
    todo: list[str] = []
    worst = 0
    for fn in (safety, heartbeat, eyesight, activity, capacity, ledger):
        text, actions = fn(settings)
        print("  " + text)
        todo.extend(actions)
        if f"[{BAD}]" in text:
            worst = max(worst, 2)
        elif f"[{WARN}]" in text:
            worst = max(worst, 1)
    print("  " + "-" * 68)
    if todo:
        print("\n  WHAT TO DO:")
        for item in todo:
            print(f"    • {item}")
        print()
    else:
        print("\n  Nothing needs your attention.\n")
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
