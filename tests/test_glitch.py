"""Focused test of METAR sensor-glitch protection + running Tmax."""
import asyncio, os, sys, tempfile
from datetime import timedelta
import os as _osp; sys.path.insert(0, _osp.path.dirname(_osp.path.dirname(_osp.path.abspath(__file__))))

from weather_arb.config import Settings
from weather_arb.db import Database
from weather_arb.metar_ingest import MetarPoller
from weather_arb.models import MetarObservation
from weather_arb import timeutils

failures = []
def check(name, cond):
    print(("PASS" if cond else "FAIL"), "-", name)
    if not cond: failures.append(name)

def C(f):  # F -> C for observation construction
    return (f - 32.0) * 5.0 / 9.0

async def main():
    settings = Settings()  # KORD/KMDW/KNYC/KBOS defaults; spike_threshold=5F, window=300s
    highs = []
    async def on_high(icao, cdate, tmax):
        highs.append((icao, tmax))

    with tempfile.TemporaryDirectory() as d:
        async with Database(os.path.join(d, "g.sqlite3")) as db:
            poller = MetarPoller(session=None, db=db, settings=settings, on_new_high=on_high)
            base = timeutils.parse_isoformat_utc("2025-07-21T18:00:00+00:00")  # noon CST-ish
            cdate = timeutils.climate_date_str(base, -6)

            async def feed(icao, temp_f, dew_f, minute):
                obs = MetarObservation(icao, base + timedelta(minutes=minute), C(temp_f), C(dew_f), "raw")
                await poller._process(obs)

            # 1) Baseline reading 80F establishes the day's max.
            await feed("KORD", 80.0, 60.0, 0)
            check("baseline sets tmax=80", await db.get_tmax("KORD", cdate) == 80.0)

            # 2) Ordinary hourly warm-up (+3F in an hour) accepted immediately.
            #    NOTE: the guard is now a RATE, so the interval matters as much as
            #    the step. +3F over an hour is 3F/h -- routine. The same +3F five
            #    minutes apart would be 36F/h and is held for confirmation.
            await feed("KORD", 83.0, 60.0, 60)
            check("incremental accepted -> 83", await db.get_tmax("KORD", cdate) == 83.0)

            # 3) Big spike +10F in 5 minutes held pending (no corroboration).
            await feed("KORD", 93.0, 61.0, 65)
            check("spike held pending -> still 83", await db.get_tmax("KORD", cdate) == 83.0)

            # 4) Confirmation within 5 min at ~93F validates the high.
            await feed("KORD", 92.5, 61.0, 68)
            t = await db.get_tmax("KORD", cdate)
            check("spike confirmed -> ~93", t is not None and t >= 92.5)

            # 5) Dewpoint far above temp is rejected (sensor fault), tmax unchanged.
            t_before = await db.get_tmax("KORD", cdate)
            await feed("KORD", 99.0, 130.0, 75)
            check("dewpoint>temp rejected", await db.get_tmax("KORD", cdate) == t_before)

            # 6) Neighbor corroboration: KMDW (~25km from KORD) spikes but KORD is
            #    already hot, so KMDW's big first-of-day jump is corroborated.
            await feed("KMDW", 78.0, 60.0, 60)    # KMDW baseline
            await feed("KMDW", 79.0, 60.0, 65)    # incremental
            # KORD last temp ~92.5 (recent); KMDW jumps to 92 (+13 from 79) -> corroborated
            await feed("KMDW", 92.0, 61.0, 68)
            check("neighbor-corroborated spike accepted", await db.get_tmax("KMDW", cdate) == 92.0)

            # Rejected reading is journaled with accepted=0 for audit.
            async with db._c.execute(
                "SELECT COUNT(*) AS n FROM station_logs WHERE icao='KORD' AND accepted=0"
            ) as cur:
                row = await cur.fetchone()
            check("rejected reading journaled accepted=0", row["n"] >= 1)

    check("callbacks fired for new highs", len(highs) >= 3)
    print("\n" + ("ALL PASS" if not failures else f"FAILURES: {failures}"))
    sys.exit(1 if failures else 0)

asyncio.run(main())
