"""WU recalibration: error stats + WU-station resolution (offline)."""
import asyncio, sys
import os as _osp; sys.path.insert(0, _osp.path.dirname(_osp.path.dirname(_osp.path.abspath(__file__))))
from weather_arb.wu_calibration import error_stats, wu_station_for

failures = []
def check(n, c):
    print(("PASS" if c else "FAIL"), "-", n)
    if not c: failures.append(n)


def test_error_stats():
    s = error_stats([1.0, 1.0, -2.0])
    check("n = 3", s["n"] == 3)
    check("bias = 0.0 (mean signed)", abs(s["bias"] - 0.0) < 1e-9)
    check("mae = 1.333", abs(s["mae"] - (4.0 / 3.0)) < 1e-9)
    check("rmse = sqrt(2)", abs(s["rmse"] - (2.0 ** 0.5)) < 1e-9)
    empty = error_stats([])
    check("empty -> n 0, None stats", empty["n"] == 0 and empty["bias"] is None)


def test_wu_station_for():
    check("NYC -> LaGuardia", wu_station_for("KNYC") == "KLGA")
    check("CHI -> O'Hare", wu_station_for("KMDW") == "KORD")
    # Corrected 2026-09-08: Denver is NOT a shared station.  Polymarket settles it
    # at Buckley SFB in both the WU and WRH eras; this used to assert KDEN.
    check("DEN -> Buckley SFB (was wrongly KDEN)", wu_station_for("KDEN") == "KBKF")
    # A genuinely shared station passes through unchanged.
    check("same-station city unchanged (MIA)", wu_station_for("KMIA") == "KMIA")
    check("same-station city unchanged (AUS)", wu_station_for("KAUS") == "KAUS")
    check("same-station city unchanged (LAX)", wu_station_for("KLAX") == "KLAX")
    # Philadelphia has no Polymarket market -> empty, not a fabricated station.
    check("PHIL -> no Polymarket station", wu_station_for("KPHL") == "")
    check("unknown icao unchanged", wu_station_for("KZZZ") == "KZZZ")


async def main():
    test_error_stats(); test_wu_station_for()
    print("\n" + ("ALL PASS" if not failures else f"FAILURES: {failures}"))
    sys.exit(1 if failures else 0)

asyncio.run(main())
