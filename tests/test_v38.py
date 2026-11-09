"""#A Polymarket settlement basis: the decode, the row filter, the day window (offline).

WHAT THIS LOCKS DOWN. `weather_arb.polymarket_basis` scores candidate bases
against Polymarket's own resolved markets. Every candidate is "max of whole-degF
readings over a day", so the answer is decided by three choices that each have a
plausible-looking wrong version:

1. **Which reading.** The WRH Temp column is `Math.round(air_temp_F)` where
   air_temp comes from the METAR `RMK Txxxxxxxx` group (tenths of a degree C),
   NOT the whole-degree body group. Python's `round` is banker's rounding and
   would disagree with `Math.round` at exact halves -- and exact halves are
   reachable: 22.5 C is exactly 72.5 F, 27.5 C is exactly 81.5 F.

2. **Which rows.** "Show Hourly Data" is not a top-of-hour filter. Per
   `weather.gov/source/wrh/timeseries/obs.js` it keeps routine METARs (an
   `SLPnnn` remark) plus SPECIs, and drops the sub-hourly MADIS rows. The two
   feeds spell those differently -- Synoptic prefixes them `"METAR "`, IEM
   appends `MADISHF` -- so a predicate that handles only one dialect silently
   admits half of them. Including them is not cosmetic: on KLGA 2026-09-03 the
   official max is 87 (the bucket that paid) and the all-rows max is 88.

3. **Which day.** Kalshi's climate day is fixed Local Standard Time, no DST.
   Polymarket's WRH page requests `obtimezone=local`, so its day is the DST-aware
   civil day. During DST these differ by an hour, and a late-evening reading can
   land in different days on the two venues.

Nothing here touches the network, the ledger, or the running experiment.
"""
import os as _osp, sys
from datetime import datetime, timezone
_osp.path and sys.path.insert(0, _osp.path.dirname(_osp.path.dirname(_osp.path.abspath(__file__))))

from weather_arb.polymarket_basis import (
    AXIS, CANDIDATES, DayCase, Obs, PolyEvent, bucket_index, buckets_tile_integers,
    classify_source, day_max, divergence, in_bucket, is_hourly_row, is_routine,
    metar_body_temp_f, metar_temp_f, parse_bucket_label, parse_target, round_half_up,
    score_candidates, slug_for, standard_offset_hours, wilson_interval, wrh_headers,
)


_SAME = object()


def ob(ts, temp, official=True, routine=True, body=_SAME, source="t_group"):
    """An observation; ``body`` defaults to the same reading as the T-group.

    A distinct sentinel, not ``None``: ``body=None`` has to mean "this METAR
    carried no body group", which is a case the candidate scoring must handle.
    """
    return Obs(ts_utc=ts, temp_f=temp, body_f=temp if body is _SAME else body,
               official=official, routine=routine, source=source)

failures = []
def check(n, c):
    print(("PASS" if c else "FAIL"), "-", n)
    if not c: failures.append(n)


def utc(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


# Real METARs pulled from the IEM archive for KLGA 2026-09-03.
ROUTINE = "KLGA 030051Z 09007KT 10SM BKN014 OVC031 21/18 A3009 RMK AO2 SLP189 T02060183 $"
SPECI   = "KLGA 030005Z 09007KT 10SM BKN013 OVC035 21/18 A3008 RMK AO2 T02060183 $"
MADIS   = "KLGA 030000Z AUTO 07007KT 10SM BKN013 OVC037 21/18 A3008 RMK T02100180 MADISHF"
# The same MADIS observation as Synoptic serves it -- note the "METAR " prefix,
# which is exactly why obs.js's startsWith(SITE) test rejects it.
MADIS_SYNOPTIC = "METAR KLGA 030000Z AUTO 07007KT 10SM BKN013 OVC037 21/18 A3008"


# --------------------------------------------------------------------------- #
# 1. Rounding: half-up, like JavaScript, not banker's
# --------------------------------------------------------------------------- #
def test_rounding_is_half_up_not_bankers():
    # 22.5 C == 72.5 F and 27.5 C == 81.5 F exactly; Math.round gives 73 and 82.
    check("22.5C -> 73F (banker's would say 72)", round_half_up(22.5 * 9 / 5 + 32) == 73)
    check("27.5C -> 82F (banker's would say 82)", round_half_up(27.5 * 9 / 5 + 32) == 82)
    check("32.5C -> 91F (banker's would say 90)", round_half_up(32.5 * 9 / 5 + 32) == 91)
    check("round-half-up disagrees with round() somewhere",
          any(round_half_up(x + 0.5) != round(x + 0.5) for x in range(60, 110)))
    check("negative half rounds toward +inf like Math.round", round_half_up(-2.5) == -2)
    check("ordinary values unchanged", (round_half_up(69.08), round_half_up(69.98)) == (69, 70))


def test_rounding_then_max_equals_max_then_rounding():
    # Rounding is monotone, so it does not matter whether the venue rounds each
    # reading or only the daily max. This is why IEM's pre-rounded tmpf would be
    # safe for the max -- but NOT for the tie rule, which is why we decode the
    # T-group ourselves instead.
    vals = [72.4, 72.5, 71.9, 68.0]
    check("max(round(x)) == round(max(x))",
          max(round_half_up(v) for v in vals) == round_half_up(max(vals)))


# --------------------------------------------------------------------------- #
# 2. Temperature decode: T-group beats the body group
# --------------------------------------------------------------------------- #
def test_t_group_is_preferred_over_body():
    # Body says 21/18 -> 21 C -> 69.8 F. The T-group says 20.6 C -> 69.08 F.
    # Synoptic reports 69.08 for this observation, so the T-group is the truth.
    val, src = metar_temp_f(ROUTINE)
    check("T-group used", src == "t_group")
    check("T-group value is 20.6C = 69.08F", abs(val - 69.08) < 1e-9)
    check("body group would have given a different whole degree",
          round_half_up(val) != round_half_up(21 * 9 / 5 + 32))


def test_negative_t_group():
    m = "KDEN 031253Z 18004KT 10SM CLR M03/M08 A3012 RMK AO2 SLP123 T10331083"
    val, src = metar_temp_f(m)
    check("negative T-group decodes", src == "t_group" and abs(val - (-3.3 * 9 / 5 + 32)) < 1e-9)


def test_body_fallback_and_missing():
    body_only = "KDEN 031253Z 18004KT 10SM CLR M03/M08 A3012"
    val, src = metar_temp_f(body_only)
    check("body fallback decodes negatives", src == "body" and abs(val - 26.6) < 1e-9)
    val2, src2 = metar_temp_f("KLGA 031253Z AUTO 18004KT 10SM CLR A3012")
    check("no temperature at all -> missing", val2 is None and src2 == "missing")
    # A remark that merely contains a T-like token must not be mistaken for one.
    check("body group is taken from before RMK",
          metar_temp_f("KLGA 031253Z 18004KT 10SM CLR 21/18 A3012 RMK AO2 SLP123")[1] == "body")


# --------------------------------------------------------------------------- #
# 3. Row filter: routine + SPECI in, MADIS out, in BOTH feed dialects
# --------------------------------------------------------------------------- #
def test_hourly_row_filter():
    check("routine METAR (has SLP) is hourly", is_hourly_row(ROUTINE, "KLGA"))
    check("SPECI (no SLP, starts with site) is hourly", is_hourly_row(SPECI, "KLGA"))
    check("MADIS row, IEM dialect, is NOT hourly", not is_hourly_row(MADIS, "KLGA"))
    check("MADIS row, Synoptic dialect, is NOT hourly",
          not is_hourly_row(MADIS_SYNOPTIC, "KLGA"))
    check("routine detection needs the SLP remark",
          is_routine(ROUTINE) and not is_routine(SPECI) and not is_routine(MADIS))


def test_including_madis_changes_the_answer():
    # The KLGA 2026-09-03 shape: official max 87 (the bucket that paid) vs 88
    # once the sub-hourly rows are admitted. If this ever stops differing, the
    # "Hourly Data" clause has stopped mattering and P1 vs P3 is no longer a test.
    obs = [
        ob(utc(2026, 9, 3, 18, 51), 87.08),
        ob(utc(2026, 9, 3, 19, 5), 87.80, official=False, routine=False),
    ]
    off, _ = day_max(obs, lambda o: o.official, "2026-09-03", "America/New_York", -5, True)
    allr, _ = day_max(obs, lambda o: True, "2026-09-03", "America/New_York", -5, True)
    check("official-only max is 87", off == 87)
    check("all-rows max is 88", allr == 88)


# --------------------------------------------------------------------------- #
# 4. Day window: civil (DST) vs fixed LST
# --------------------------------------------------------------------------- #
def test_civil_and_lst_days_differ_under_dst():
    # 2026-09-04 00:30 UTC is 2026-09-03 20:30 EDT (civil) but 19:30 EST (LST),
    # so both windows contain it. 2026-09-04 04:30 UTC is 00:30 EDT on the 4th
    # (civil, next day) yet 23:30 EST on the 3rd (LST, same day). That hour is
    # the whole basis-vs-window question.
    late = ob(utc(2026, 9, 4, 4, 30), 95.0)
    day = ob(utc(2026, 9, 3, 18, 51), 80.0)
    obs = [day, late]
    civil, n_civil = day_max(obs, lambda o: o.official, "2026-09-03", "America/New_York", -5, True)
    lst, n_lst = day_max(obs, lambda o: o.official, "2026-09-03", "America/New_York", -5, False)
    check("civil day excludes the post-midnight-EDT reading", civil == 80 and n_civil == 1)
    check("LST day still includes it", lst == 95 and n_lst == 2)


def test_standard_offset_ignores_dst():
    check("New York standard offset is -5 even though the window is in DST",
          standard_offset_hours("America/New_York", 2026) == -5)
    check("Denver -7", standard_offset_hours("America/Denver", 2026) == -7)
    check("Los Angeles -8", standard_offset_hours("America/Los_Angeles", 2026) == -8)


def test_empty_day_returns_none_not_zero():
    obs = [ob(utc(2026, 9, 3, 18), 80.0)]
    val, n = day_max(obs, lambda o: o.official, "2026-09-01", "America/New_York", -5, True)
    check("a day with no observations is None, not 0", val is None and n == 0)


# --------------------------------------------------------------------------- #
# 5. Bucket labels
# --------------------------------------------------------------------------- #
LADDER = ["75°F or below", "76-77°F", "78-79°F", "80-81°F", "82-83°F", "84-85°F",
          "86-87°F", "88-89°F", "90-91°F", "92-93°F", "94°F or higher"]


def test_bucket_labels():
    check("interior", parse_bucket_label("88-89°F") == (88, 89))
    check("low tail", parse_bucket_label("79°F or below") == (None, 79))
    check("high tail", parse_bucket_label("98°F or higher") == (98, None))
    check("unparsed label is rejected, not guessed",
          parse_bucket_label("Something else entirely") is None)


def test_ladder_tiles_the_integers():
    b = [parse_bucket_label(x) for x in LADDER]
    check("the real 11-bucket ladder tiles", buckets_tile_integers(b))
    gap = [x for x in b if x != (82, 83)]
    check("a gap is rejected", not buckets_tile_integers(gap))
    overlap = b + [(83, 84)]
    check("an overlap is rejected", not buckets_tile_integers(overlap))
    check("a missing tail is rejected",
          not buckets_tile_integers([x for x in b if x != (None, 75)]))


def test_in_bucket_open_tails():
    check("below tail", in_bucket(70, None, 75) and not in_bucket(76, None, 75))
    check("above tail", in_bucket(99, 94, None) and not in_bucket(93, 94, None))
    check("None value never counts as a hit", not in_bucket(None, 80, 81))


# --------------------------------------------------------------------------- #
# 6. Source classification -- the two eras
# --------------------------------------------------------------------------- #
def test_classify_source():
    check("WRH era + station",
          classify_source("https://www.weather.gov/wrh/timeseries?site=kbkf") == ("wrh", "KBKF"))
    check("WU era + station",
          classify_source("https://www.wunderground.com/history/daily/us/co/aurora/KBKF")
          == ("wu", "KBKF"))
    check("unknown source is flagged, not defaulted",
          classify_source("https://example.com/whatever") == ("other", None))
    check("empty source", classify_source("") == ("other", None))


def test_slug_shape():
    from datetime import date
    check("slug matches the live Gamma slug",
          slug_for("los-angeles", date(2026, 9, 3))
          == "highest-temperature-in-los-angeles-on-september-3-2026")


# --------------------------------------------------------------------------- #
# 7. Scoring and divergence bookkeeping
# --------------------------------------------------------------------------- #
def mkcase(city, date_s, win, cand, k_lo=None, k_hi=None, cli=None, era="wrh",
           stn="KLGA", buckets=None):
    ev = PolyEvent(city=city, event_date=date_s, slug="s", source_url="u", era=era,
                   station=stn, win_lo=win[0], win_hi=win[1],
                   buckets=buckets or (win,), tiling_ok=True)
    c = DayCase(city=city, date=date_s, event=ev, station=stn)
    for name in list(CANDIDATES) + ["P5 NWS CLI at Polymarket station"]:
        c.candidates[name] = None
    c.candidates["P1 WRH hourly, civil day"] = cand
    c.kalshi_lo, c.kalshi_hi, c.kalshi_cli = k_lo, k_hi, cli
    return c


def test_scoring_counts_missing_separately():
    cases = [mkcase("MIA", "2026-09-01", (88, 89), 88),
             mkcase("MIA", "2026-09-02", (88, 89), 90),
             mkcase("MIA", "2026-09-03", (88, 89), None)]
    s = score_candidates(cases)["P1 WRH hourly, civil day"]
    check("a day we could not evaluate is 'missing', never a miss",
          s["n"] == 2 and s["hits"] == 1 and s["missing"] == 1)
    check("rate is over the usable days", abs(s["rate"] - 0.5) < 1e-9)


def test_divergence_three_way_classification():
    cases = [
        # Kalshi settled {90,91}; Polymarket paid 88-89 -> disjoint, real divergence.
        mkcase("MIA", "2026-09-01", (88, 89), 88, k_lo=90, k_hi=91, cli=90.0),
        # Kalshi {88,89} inside Polymarket 88-89 -> consistent.
        mkcase("MIA", "2026-09-02", (88, 89), 89, k_lo=88, k_hi=89, cli=88.0),
        # Kalshi {89,90} straddles the boundary -> undetermined, NOT agreement.
        mkcase("MIA", "2026-09-03", (88, 89), 89, k_lo=89, k_hi=90, cli=89.0),
    ]
    d = divergence(cases, "P1 WRH hourly, civil day")
    b = d["bucket_lower_bound"]
    check("one disjoint day counted as divergent", b["divergent"] == 1)
    check("the straddling day is undetermined, not consistent", b["undetermined"] == 1)
    check("the lower bound uses all three days as the denominator", b["n"] == 3)
    check("per-city breakdown adds up",
          d["per_city"]["MIA"] == {"divergent": 1, "consistent": 1, "undetermined": 1})


def test_divergence_delta_direction():
    cases = [mkcase("MIA", "2026-09-01", (88, 89), 88, k_lo=90, k_hi=91, cli=90.0)]
    d = divergence(cases, "P1 WRH hourly, civil day")
    s = d["cli_delta_all"]
    check("D = Kalshi CLI - Polymarket observed, so Kalshi higher is positive",
          s["n"] == 1 and s["mean"] == 2 and s["kalshi_higher"] == 1)


def test_divergence_skips_days_without_a_kalshi_settlement():
    cases = [mkcase("MIA", "2026-09-01", (88, 89), 88)]
    d = divergence(cases, "P1 WRH hourly, civil day")
    check("no Kalshi settlement -> excluded from the denominator, and counted",
          d["bucket_lower_bound"]["n"] == 0 and d["per_city"]["MIA"]["no_kalshi"] == 1)


def test_wilson_interval_sanity():
    lo, hi = wilson_interval(50, 100)
    check("interval brackets the point estimate", lo < 0.5 < hi)
    lo2, hi2 = wilson_interval(50, 1000)
    check("more data narrows it", (hi2 - lo2) < (hi - lo))
    check("n=0 does not divide by zero", wilson_interval(0, 0) == (0.0, 0.0))
    lo3, hi3 = wilson_interval(10, 10)
    check("a perfect score still has an interval below 1", lo3 < 1.0 and hi3 <= 1.0)




# --------------------------------------------------------------------------- #
# 8. The body-vs-T-group encoding axis, and the bucket ladder lookup
# --------------------------------------------------------------------------- #
# KMIA 2026-09-02 15:53Z -- the one day the winning candidate missed. The body
# group and the T-group land in DIFFERENT whole degrees here, and Polymarket paid
# the bucket the body group implies. That made "the venue reads the body group" a
# real hypothesis; scoring it on all 282 city-days rejected it (80% vs 98.9%), so
# the miss stays unexplained rather than being explained by a rule fitted to it.
KMIA_MISS = ("KMIA 021553Z 00000KT 10SM SCT028CB BKN055 BKN300 32/24 A3000 "
             "RMK AO2 SLP158 CB NE AND SE-S MOV NE CB DSNT NW T03170239")


def test_body_and_t_group_can_straddle_a_whole_degree():
    t_val, src = metar_temp_f(KMIA_MISS)
    b_val = metar_body_temp_f(KMIA_MISS)
    check("T-group is used when present", src == "t_group")
    check("T-group 31.7C -> 89", round_half_up(t_val) == 89)
    check("body 32C -> 90", round_half_up(b_val) == 90)
    check("the two encodings disagree by a whole degree here",
          round_half_up(t_val) != round_half_up(b_val))


# KLGA 2026-09-03 05:30Z. Byte-identical in both feeds EXCEPT that Synoptic's
# copy has a double space before the T-group -- and Synoptic reported 69.8 (the
# 21 C body group) where the T-group says 20.6 C = 69.08 F. Our decoder must not
# inherit that fragility: whitespace before the group is not information.
DOUBLE_SPACE = ("KLGA 030530Z 00000KT 10SM -TSRA SCT008 BKN024CB OVC060 21/19 A3002 "
                "RMK AO2 TSB30 PRESFR OCNL LTGIC OHD-W TS OHD-W MOV SE P0003  T02060194")


def test_t_group_survives_the_whitespace_that_trips_the_source():
    val, src = metar_temp_f(DOUBLE_SPACE)
    check("double space before the T-group still parses", src == "t_group")
    check("and yields 20.6C = 69.08F -> 69", round_half_up(val) == 69)
    check("the source's fallback would have said 70 (21C body)",
          round_half_up(metar_body_temp_f(DOUBLE_SPACE)) == 70)


def test_body_decoder_handles_negatives_and_absence():
    check("negative body group",
          abs(metar_body_temp_f("KDEN 031253Z 18004KT 10SM CLR M03/M08 A3012") - 26.6) < 1e-9)
    check("no body group -> None",
          metar_body_temp_f("KLGA 031253Z AUTO 18004KT 10SM CLR A3012") is None)


def test_candidate_axes_are_declared():
    check("every candidate declares which axis it varies",
          all(name in AXIS for name in CANDIDATES))
    check("the day-window variant is the only one tagged day_window",
          [k for k, v in AXIS.items() if v == "day_window"] == ["P2 WRH hourly, LST day"])


def test_missing_reading_does_not_silently_become_a_miss():
    # A row present in the window but with no usable temperature must not be
    # read as a low reading; the day is scored on what is left, or is None.
    obs = [ob(utc(2026, 9, 3, 18), 80.0, body=None)]
    val, n = day_max(obs, lambda o: o.official, "2026-09-03", "America/New_York", -5,
                     True, lambda o: o.body_f)
    check("rows are counted even when the reading is missing", n == 1)
    check("but a missing reading yields None, not 0", val is None)


def test_bucket_index_finds_the_containing_bucket():
    b = tuple(parse_bucket_label(x) for x in LADDER)
    check("interior value", bucket_index(88, b) == b.index((88, 89)))
    check("low tail", bucket_index(60, b) == 0)
    check("high tail", bucket_index(120, b) == len(b) - 1)
    check("None value -> None", bucket_index(None, b) is None)
    check("a tiling ladder always finds a bucket",
          all(bucket_index(v, b) is not None for v in range(50, 130)))


def test_grid_divergence_has_no_undetermined_category():
    # Kalshi CLI 90 and Polymarket 88 land in different buckets of Polymarket's
    # OWN ladder, so this reading is decided on every day -- unlike the
    # payout-only bound, which leaves straddling days undetermined.
    ev_buckets = tuple(parse_bucket_label(x) for x in LADDER)
    c = mkcase("MIA", "2026-09-01", (88, 89), 88, k_lo=89, k_hi=90, cli=90.0,
               buckets=ev_buckets)
    d = divergence([c], "P1 WRH hourly, civil day")
    g = d["bucket_on_poly_grid"]
    check("the straddling day is undetermined under the payout bound",
          d["bucket_lower_bound"]["undetermined"] == 1)
    check("but decided under the grid reading", g["n"] == 1 and g["divergent"] == 1)


def test_grid_divergence_flags_cli_inconsistent_with_kalshi():
    ev_buckets = tuple(parse_bucket_label(x) for x in LADDER)
    c = mkcase("MIA", "2026-09-01", (88, 89), 88, k_lo=80, k_hi=81, cli=90.0,
               buckets=ev_buckets)
    d = divergence([c], "P1 WRH hourly, civil day")
    cons = d["bucket_on_poly_grid"]["cli_vs_kalshi_consistency"]
    check("a CLI outside Kalshi's own settled range is counted, not ignored",
          cons["checked"] == 1 and cons["cli_outside_kalshi_range"] == 1)


def test_wrh_headers_carry_both_referer_and_origin():
    """The Synoptic token needs BOTH headers, and the failure mode lies about why.

    With only a Referer, every request -- including the recent=60 call the live
    page itself makes -- returns "Invalid request per token rules", which reads
    like an exhausted quota and was recorded as one on 2026-09-08. It is not.
    Dropping either header here would silently reintroduce that misdiagnosis.
    """
    h = wrh_headers("KMIA")
    check("Referer names the site", h.get("Referer", "").endswith("?site=kmia"))
    check("Origin is weather.gov", h.get("Origin") == "https://www.weather.gov")
    check("station is lowercased in the Referer", "KMIA" not in h["Referer"])
    check("both headers present, not one", set(h) == {"Referer", "Origin"})


def test_parse_target_round_trip_and_rejections():
    """--validate-target must fail loudly on a malformed spec, not silently skip.

    A typo that parsed to something plausible would send the check at the wrong
    station-day and report agreement about a day nobody asked about.
    """
    check("parses STID:DATE", parse_target("KMIA:2026-09-02") == ("KMIA", "2026-09-02"))
    check("uppercases the station", parse_target("kmia:2026-09-02")[0] == "KMIA")
    check("tolerates surrounding space", parse_target(" kmia : 2026-09-02 ")
          == ("KMIA", "2026-09-02"))
    for bad in ("KMIA", "KMIA:", ":2026-09-02", "KMIA:2026-13-02", "KMIA:09-02-2026", ""):
        try:
            parse_target(bad)
            check(f"rejects {bad!r}", False)
        except ValueError:
            check(f"rejects {bad!r}", True)


def test_body_fallback_is_what_moved_the_one_miss():
    """MIA 2026-09-02, the single miss in 288 city-days, decoded both ways.

    The venue's feed served the whole-degree BODY group on the 13:45 SPECI, which
    rounds to 90 and pays; the tenths-precision T-group rounds to 89 and does not.
    This is the exact row, verbatim from the live Synoptic pull. It pins the two
    decodes apart so a future 'simplification' of metar_temp_f cannot collapse them.
    """
    m = ("KMIA 021745Z 06006KT 10SM TS SCT030CB BKN046 BKN300 32/25 A2997 RMK AO2 "
         "RAB1655E04 TSE42B45 OCNL LTGICCG NE AND SW TS NE AND  SW MOV N P0000 T03170250")
    tg, src = metar_temp_f(m)
    body = metar_body_temp_f(m)
    check("T-group decodes 31.7C = 89.06F", tg is not None and abs(tg - 89.06) < 0.01)
    check("T-group is reported as the source", src == "t_group")
    check("body decodes 32C = 89.6F", body is not None and abs(body - 89.6) < 0.01)
    check("they round to DIFFERENT whole degrees", round_half_up(tg) == 89
          and round_half_up(body) == 90)
    # The paid bucket was 90-91: the body decode pays, the T-group decode does not.
    check("body decode lands in the paid bucket", in_bucket(round_half_up(body), 90, 91))
    check("T-group decode does not", not in_bucket(round_half_up(tg), 90, 91))


def test_double_space_is_not_the_fallback_trigger():
    """The 'double space before the T-group' explanation is UNKNOWN, not established.

    Measured on the live feed: all 3 KMIA fallbacks contained irregular whitespace,
    but so did 73 of 931 rows and 70 of those decoded the T-group fine. Our own
    decoder is unaffected either way -- these are all rows the feed got wrong and
    we got right, which is the direction that matters for the reconstruction.
    """
    cases = [
        "KMIA 021745Z 32/25 RMK AO2 TS NE AND  SW MOV N P0000 T03170250",   # dbl space, far from group
        "KMIA 031840Z 29/24 RMK AO2 TS OHD MOV N P0005  T02890239",         # dbl space before group
        "KMIA 010400Z 26/24 RMK AO2  FRQ LTGICCG OHD T02610244  RVRNO",     # dbl space both sides
    ]
    for m in cases:
        tg, src = metar_temp_f(m)
        check(f"T-group still decoded despite irregular whitespace ({m[-12:]})",
              tg is not None and src == "t_group")


def main():
    test_rounding_is_half_up_not_bankers()
    test_rounding_then_max_equals_max_then_rounding()
    test_t_group_is_preferred_over_body()
    test_negative_t_group()
    test_body_fallback_and_missing()
    test_hourly_row_filter()
    test_including_madis_changes_the_answer()
    test_civil_and_lst_days_differ_under_dst()
    test_standard_offset_ignores_dst()
    test_empty_day_returns_none_not_zero()
    test_bucket_labels()
    test_ladder_tiles_the_integers()
    test_in_bucket_open_tails()
    test_classify_source()
    test_slug_shape()
    test_scoring_counts_missing_separately()
    test_divergence_three_way_classification()
    test_divergence_delta_direction()
    test_divergence_skips_days_without_a_kalshi_settlement()
    test_wilson_interval_sanity()
    test_body_and_t_group_can_straddle_a_whole_degree()
    test_t_group_survives_the_whitespace_that_trips_the_source()
    test_body_decoder_handles_negatives_and_absence()
    test_candidate_axes_are_declared()
    test_missing_reading_does_not_silently_become_a_miss()
    test_bucket_index_finds_the_containing_bucket()
    test_grid_divergence_has_no_undetermined_category()
    test_grid_divergence_flags_cli_inconsistent_with_kalshi()
    test_wrh_headers_carry_both_referer_and_origin()
    test_parse_target_round_trip_and_rejections()
    test_body_fallback_is_what_moved_the_one_miss()
    test_double_space_is_not_the_fallback_trigger()
    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
