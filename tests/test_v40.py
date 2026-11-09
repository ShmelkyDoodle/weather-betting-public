"""t1 depth probe: routine-vs-sub-hourly split, LST day window, T-group-only rule.

WHAT THIS LOCKS DOWN.  `scripts/t1_depth_probe.py` exists to sample the Kalshi
book at t1 -- when a bucket outcome becomes provable from 5-minute observations,
before the hourly METAR tells the market.  Three choices decide whether a sample
is really a t1 sample, and each has a plausible-looking wrong version:

1. **Which rows are "hourly".**  The probe's `hourly_would_know` field is the
   whole measurement: false means the market cannot yet know.  Get the routine
   filter wrong and every sample is mislabelled.  A routine METAR carries an
   `SLP` remark; the 5-minute MADIS reports are `AUTO` and carry `MADISHF`.

2. **Which day.**  Kalshi's climate day is fixed Local Standard Time, no DST.
   Using civil time would fold an evening reading into the wrong day.

3. **Which reading.**  T-group ONLY.  The whole-degree body group quantises to
   1.8 F and was measured (2026-09-08, 60 station-days) to prove the settled max
   on 0 of 60 days -- so a body-group fallback would manufacture certainty the
   real feed cannot support.  This is the difference between the live
   weather.gov feed (truncated, no RMK) and the IEM archive (full T-groups), and
   silently falling back is how a backtest would overstate what production sees.
"""
import os as _o, sys as _s
_ROOT = _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__)))
_s.path.insert(0, _ROOT); _s.path.insert(0, _o.path.join(_ROOT, "scripts"))

from datetime import datetime, timezone
from t1_depth_probe import (STATION, SUBHOURLY_OK, SUBHOURLY_QUANTISATION_F,
                            ROUTINE_QUANTISATION_F, is_routine,
                            observed_lower_bound_f, running_max)
from weather_arb.polymarket_basis import metar_temp_f

failures = []
def check(n, c):
    print(("PASS" if c else "FAIL"), "-", n)
    if not c: failures.append(n)

ROUTINE = ("KPHL 032054Z 28009KT 10SM FEW050 SCT190 32/19 A2980 RMK AO2 SLP089 "
           "T03220189 10328 20250 58012")
SUBHOURLY = ("KPHL 031940Z AUTO 24009KT 10SM SCT040 SCT090 31/23 A2979 RMK "
             "T03100230 MADISHF")
LIVE_TRUNCATED = "METAR KPHL 081825Z AUTO 31007KT 10SM FEW060 29/11 A3022"


def test_routine_vs_subhourly():
    check("routine METAR detected", is_routine(ROUTINE))
    check("sub-hourly MADIS not routine", not is_routine(SUBHOURLY))
    check("live truncated AUTO not routine", not is_routine(LIVE_TRUNCATED))
    # A SPECI with an SLP remark would be unusual, but AUTO before RMK still rules
    # it out -- the predicate must not be satisfied by SLP alone.
    check("AUTO before RMK is never routine",
          not is_routine("KPHL 031940Z AUTO 24009KT 10SM 31/23 A2979 RMK SLP089 T03100230"))


def test_tgroup_only_no_body_fallback():
    """The probe drops rows without a T-group. Verify the two decode differently."""
    v_sub, src_sub = metar_temp_f(SUBHOURLY)
    v_live, src_live = metar_temp_f(LIVE_TRUNCATED)
    check("archive sub-hourly yields a T-group", src_sub == "t_group")
    check("T-group is tenths-precision (31.0C = 87.8F)", abs(v_sub - 87.8) < 0.01)
    check("live truncated row has NO T-group", src_live != "t_group")
    # 29C -> 84.2F exactly; the true value could be anywhere in [28.5, 29.5)C,
    # i.e. +-0.9F. That 1.8F band is why the live feed cannot prove a crossing.
    check("live row falls back to whole degC", abs(v_live - 84.2) < 0.01)


def _obs(hhmm, val, routine):
    h, m = hhmm
    return (datetime(2026, 9, 8, h, m, tzinfo=timezone.utc), val, routine)


def test_running_max_respects_lst_day_and_channel():
    # KPHL is LST -5. 2026-09-09T04:30Z = 2026-09-08 23:30 LST -> still the 8th.
    #                 2026-09-09T05:30Z = 2026-09-09 00:30 LST -> the 9th.
    obs = [
        _obs((14, 0), 80.0, True),      # routine, in day
        _obs((17, 25), 90.0, False),    # sub-hourly peak, in day
        _obs((17, 54), 88.0, True),     # routine, in day, lower than sub-hourly
    ]
    late = (datetime(2026, 9, 9, 5, 30, tzinfo=timezone.utc), 99.0, False)
    # running_max returns the PROVABLE LOWER BOUND, not the raw reading: the
    # sub-hourly peak of 90.0 only proves 89.1, the routine 88.0 proves 87.95.
    both, at_b = running_max(obs + [late], "2026-09-08", -5, routine_only=False)
    check("next LST day excluded from the max",
          abs(both - (90.0 - SUBHOURLY_QUANTISATION_F)) < 1e-9)
    check("max timestamp is the sub-hourly peak", at_b == obs[1][0])
    only_rt, at_r = running_max(obs + [late], "2026-09-08", -5, routine_only=True)
    check("routine-only max ignores the sub-hourly peak",
          abs(only_rt - (88.0 - ROUTINE_QUANTISATION_F)) < 1e-9)
    check("routine-only timestamp is the routine row", at_r == obs[2][0])
    # This gap IS the edge: 90 vs 88 means sub-hourly proves a threshold the
    # hourly channel does not yet -- exactly what hourly_would_know detects.
    check("sub-hourly max exceeds routine max here", both > only_rt)
    empty, at_e = running_max([], "2026-09-08", -5, routine_only=False)
    check("no observations -> None, not 0.0", empty is None and at_e is None)


def test_station_table_matches_measured_coverage():
    check("all 7 Kalshi stations present", len(STATION) == 7)
    check("KDEN has NO sub-hourly feed (measured 0 rows / 2 days)",
          "KDEN" not in SUBHOURLY_OK)
    check("KNYC excluded too (~12 rows/day, not 5-minute)", "KNYC" not in SUBHOURLY_OK)
    check("the five usable stations", SUBHOURLY_OK == {"KMDW", "KMIA", "KAUS", "KLAX", "KPHL"})
    # LST offsets are fixed, no DST -- matching Kalshi's settlement window.
    check("LST offsets are the fixed standard ones",
          [STATION[k][2] for k in ("KNYC", "KMDW", "KDEN", "KLAX")] == [-5, -6, -7, -8])


def test_subhourly_tgroup_is_whole_degc_not_tenths():
    """THE trap. The 5-minute MADIS T-group LOOKS like tenths and is not.

    Measured over 17,007 reports at 4 stations: 100.0% of sub-hourly T-groups end
    in .0, against 8-12% for routine METARs (which is chance). The sensor reports
    whole degrees Celsius and pads a zero. Treating it as exact invented certainty
    that was not there -- 4 of 18 sub-hourly-only lock-ins were WRONG against
    official settlement (22%) before this was applied, versus 0 of 111 that the
    routine METAR also proved. After the fix: 0 wrong in 539.
    """
    check("sub-hourly band is +-0.5degC = 0.9F", abs(SUBHOURLY_QUANTISATION_F - 0.9) < 1e-9)
    check("routine band is genuine tenths", ROUTINE_QUANTISATION_F <= 0.05)
    # KPHL 2026-08-30: sub-hourly said T0310 (31.0C = 87.8F); the CLI high was 87,
    # so the [86,87] bucket settled YES. Raw 87.8 >= 87.5 would call it certain NO.
    raw = 87.8
    check("raw sub-hourly reading would clear 87.5", raw > 87.5)
    lb = observed_lower_bound_f(raw, routine=False)
    check("its provable lower bound does NOT", lb <= 87.5)
    check("lower bound is 86.9F", abs(lb - 86.9) < 1e-9)
    # The routine METAR that hour read 30.6C = 87.08F -- genuinely tenths, and it
    # correctly never proved certainty either.
    check("routine 87.08F also does not clear 87.5",
          observed_lower_bound_f(87.08, routine=True) <= 87.5)
    # A full degree C higher DOES prove it: 32C = 89.6F, lower bound 88.7F.
    check("32C does prove it", observed_lower_bound_f(89.6, routine=False) > 87.5)


def test_lower_bound_is_never_optimistic():
    for v in (60.0, 75.5, 87.8, 100.4):
        for routine in (True, False):
            check(f"lower bound <= reading ({v}, routine={routine})",
                  observed_lower_bound_f(v, routine) <= v)
    check("sub-hourly bound is strictly more conservative than routine",
          observed_lower_bound_f(80.0, False) < observed_lower_bound_f(80.0, True))


def main():
    test_routine_vs_subhourly()
    test_tgroup_only_no_body_fallback()
    test_running_max_respects_lst_day_and_channel()
    test_station_table_matches_measured_coverage()
    test_subhourly_tgroup_is_whole_degc_not_tenths()
    test_lower_bound_is_never_optimistic()
    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
