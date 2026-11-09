"""CLI-vs-observed divergence: rounding, 2F bins, LST day max, classify, summarize.

Also locks the cross-venue station registry corrected 2026-09-08: Kalshi and
Polymarket share a station only at MIA/AUS/LAX, Polymarket settles Denver at
KBKF (Buckley SFB) rather than KDEN, and it lists no Philadelphia market at all.
"""
import asyncio, sys
from datetime import datetime, timezone
import os as _osp; sys.path.insert(0, _osp.path.dirname(_osp.path.dirname(_osp.path.abspath(__file__))))
from weather_arb.divergence import (
    round_f, bin_2f, day_max_f, classify, summarize, _parse_iem, alert_events,
    SAME_STATION, PM_STATION, PM_NOT_LISTED,
)

failures = []
def check(n, c):
    print(("PASS" if c else "FAIL"), "-", n)
    if not c: failures.append(n)


def _ts(h, m=0):
    return datetime(2026, 7, 20, h, m, tzinfo=timezone.utc)


def test_round_and_bins():
    check("round 81.5 -> 82 (half up)", round_f(81.5) == 82)
    check("round 81.4 -> 81", round_f(81.4) == 81)
    check("bin(81) = (80,81)", bin_2f(81) == (80, 81))
    check("bin(82) = (82,83)", bin_2f(82) == (82, 83))


def test_day_max_lst_window():
    # KDEN offset -7 (MST). Climate day 2026-07-20 = UTC 07:00 that day .. 07:00 next.
    series = [
        (_ts(6, 30), 99.0),   # 06:30Z = 2026-07-19 23:30 MST -> PREVIOUS climate day
        (_ts(21, 0), 101.0),  # in-day peak
        (_ts(23, 0), 100.0),
    ]
    m = day_max_f(series, "2026-07-20", -7)
    check("LST window excludes prior-day 06:30Z", m == 101.0)


def test_classify_diverges_and_bin():
    off = -7
    # 1-min peaks 102 at :37 (between METARs); METARs top out at 101.
    one_min = [(_ts(21, 37), 102.0), (_ts(21, 0), 101.0)]
    metar = [(_ts(20, 53), 100.0), (_ts(21, 53), 101.0)]
    d = classify("DEN", "KDEN", "2026-07-20", one_min, metar, off)
    check("nws=102 wu=101", d.nws_max_f == 102 and d.wu_max_f == 101)
    check("delta=+1 diverges", d.delta_f == 1 and d.diverges)
    check("crosses 2F bin (102 vs 101)", d.crosses_2f_bin)
    check("1-min ok (covered peak)", d.one_min_ok)


def test_gappy_one_min_never_negative():
    off = -8
    # 1-min archive gapped through the afternoon: its max (73) is below METAR (81).
    one_min = [(_ts(9, 0), 73.0)]
    metar = [(_ts(22, 0), 81.0)]
    d = classify("LAX", "KLAX", "2026-07-20", one_min, metar, off)
    check("nws floored to WU (81), never negative", d.nws_max_f == 81 and d.delta_f == 0)
    check("does not diverge on gappy data", not d.diverges)
    check("flagged as 1-min not ok", d.one_min_ok is False)


def test_summarize_counts():
    off = -6
    # All obs land in the 2026-07-20 LST day (12:00Z -> 06:00 local at offset -6).
    rows = [
        classify("MIA", "KMIA", "2026-07-20", [(_ts(20), 92.0)], [(_ts(20), 90.0)], off),  # +2 diverge, cross
        classify("MIA", "KMIA", "2026-07-20", [(_ts(19), 90.0)], [(_ts(19), 90.0)], off),  # 0, no diverge
        classify("MIA", "KMIA", "2026-07-20", [(_ts(12), 70.0)], [(_ts(18), 88.0)], off),  # gappy floor
    ]
    s = summarize(rows)
    check("3 days graded", s.days == 3)
    check("1 diverge day", s.n_diverge == 1)
    check("1 cross-bin day", s.n_cross_bin == 1)
    check("1 gappy day flagged", s.n_unreliable == 1)
    check("max delta = 2", s.max_delta == 2)
    check("mean delta on diverge = 2.0", s.mean_delta_on_diverge == 2.0)


def test_parse_iem_defensive():
    text = ("station,valid,tmpf\n"
            "MIA,2026-07-20 18:00,90.5\n"
            "MIA,2026-07-20 19:00,M\n"        # missing -> skipped
            "MIA,2026-07-20 20:00,\n"          # empty -> skipped
            "garbage line\n")
    rows = _parse_iem(text, ts_col=1, val_col=2)
    check("parses one valid row, skips M/empty/garbage", len(rows) == 1 and rows[0][1] == 90.5)


def test_alert_events_state_changes():
    off = -7

    def mk(one_min, metar):
        return classify("DEN", "KDEN", "2026-07-20",
                        [(_ts(21), one_min)], [(_ts(21), metar)], off)

    no_div = mk(100.0, 100.0)      # delta 0
    diverge = mk(102.0, 101.0)     # +1, crosses bin (102 vs 101)
    # First sighting of a divergence alerts (appeared + bin flip).
    ev = alert_events(None, diverge)
    check("first divergence alerts", any("appeared" in e for e in ev))
    check("bin flip alerted", any("bin flip" in e for e in ev))
    # Persisting divergence (same state) does not re-alert -> no spam.
    check("persistent divergence is silent", alert_events(diverge, diverge) == [])
    # Non-diverging cycle is silent.
    check("no divergence -> no alert", alert_events(no_div, no_div) == [])
    # Divergence newly appearing after a quiet cycle alerts.
    check("new divergence after quiet alerts", alert_events(no_div, diverge) != [])


def test_pm_station_cross_venue():
    off = -5  # KNYC (EST)
    # Kalshi @ Central Park: 1-min 88, METAR 87. Polymarket @ LaGuardia: METAR 90 (warmer).
    one_min = [(_ts(20), 88.0)]
    cp_metar = [(_ts(20, 53), 87.0)]
    lga_metar = [(_ts(20, 53), 90.0), (_ts(21, 53), 89.0)]
    d = classify("NYC", "KNYC", "2026-07-20", one_min, cp_metar, off,
                 pm_icao="KLGA", pm_metar=lga_metar)
    check("nws (Central Park CLI) = 88", d.nws_max_f == 88)
    check("wu (Central Park METAR) = 87", d.wu_max_f == 87)
    check("pm (LaGuardia METAR) = 90 @KLGA", d.pm_max_f == 90 and d.pm_icao == "KLGA")
    check("cross_station True", d.cross_station)
    check("same-station delta +1 (Kalshi higher vs its own METAR)", d.delta_f == 1 and d.diverges)
    check("cross-venue pm_delta -2 (Polymarket's warmer station settles higher)",
          d.pm_delta_f == -2 and not d.pm_diverges)
    # A genuinely same-station city (MIA/AUS/LAX): pm defaults to the Kalshi
    # station, pm == wu, not cross-station.
    d2 = classify("MIA", "KMIA", "2026-07-20", one_min, cp_metar, -5)
    check("same-station (MIA): pm defaults to wu, not cross-station",
          d2.pm_max_f == d2.wu_max_f and not d2.cross_station and d2.same_station)
    # Denver is NOT same-station: Polymarket settles at Buckley SFB (KBKF), a
    # separate airfield.  It was listed as shared until 2026-09-08 and was never so.
    check("DEN is not in SAME_STATION", "DEN" not in SAME_STATION)
    check("PM_STATION maps DEN to KBKF", PM_STATION.get("DEN") == "KBKF")
    bkf_metar = [(_ts(20, 53), 93.0)]
    d3 = classify("DEN", "KDEN", "2026-07-20", one_min, cp_metar, -7,
                  pm_icao="KBKF", pm_metar=bkf_metar)
    check("DEN cross-station against KBKF", d3.cross_station and d3.pm_icao == "KBKF")
    check("DEN pm max reads Buckley, not Denver Intl", d3.pm_max_f == 93 and d3.wu_max_f == 87)
    check("DEN not flagged same_station", not d3.same_station)
    # Philadelphia has no Polymarket market at all.  It must report None, NOT fall
    # back to Kalshi's own station -- that would fabricate a 0F cross-venue basis
    # for a market nobody can trade.
    check("PHIL is in PM_NOT_LISTED", "PHIL" in PM_NOT_LISTED)
    d4 = classify("PHIL", "KPHL", "2026-07-20", one_min, cp_metar, -5)
    check("PHIL pm_listed is False", not d4.pm_listed)
    check("PHIL pm_max_f is None (no fabricated basis)", d4.pm_max_f is None)
    check("PHIL pm_icao is blank", d4.pm_icao == "")
    check("PHIL pm_delta_f is None", d4.pm_delta_f is None and not d4.pm_diverges)
    check("PHIL Kalshi-side measurement still works", d4.wu_max_f == 87 and d4.nws_max_f == 88)


async def main():
    test_round_and_bins(); test_day_max_lst_window(); test_classify_diverges_and_bin()
    test_gappy_one_min_never_negative(); test_summarize_counts(); test_parse_iem_defensive()
    test_alert_events_state_changes(); test_pm_station_cross_venue()
    print("\n" + ("ALL PASS" if not failures else f"FAILURES: {failures}"))
    sys.exit(1 if failures else 0)

asyncio.run(main())
