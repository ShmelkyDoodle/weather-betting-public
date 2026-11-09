"""CLI revision audit: the summary-date filter and the month span (offline).

WHAT THIS LOCKS DOWN. `scripts/cli_revision_audit.py` establishes that Kalshi
settled KXHIGHMIA-26AUG29 on a CLI value that had been withdrawn 2h50m earlier.
That conclusion rests on being able to say which issuances describe which day,
and there is a specific way to get it wrong.

Each WFO issues its CLI TWICE per calendar day:

  * a late-afternoon PRELIMINARY for *today* so far ("VALID TODAY AS OF 0400 PM"),
  * a morning FINAL for *yesterday*.

Both land on the same date in the AFOS archive. Listing by issue date alone
therefore mixes two different climate days -- and on 2026-08-30 that produced a
phantom third "revision" reading MAXIMUM 91, which is Aug 30's preliminary, not a
correction to Aug 29 at all. Reporting it as one would have turned a clean
two-issuance revision into an incoherent three-way disagreement.

A re-issue is NOT flagged as a correction -- same WMO header, no CCA, only a
later timestamp -- so nothing but the parsed content distinguishes it.
"""
import os as _o, sys as _s
_s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
_s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))), "scripts"))

from datetime import date
from cli_revision_audit import months_spanned, parse_cli_text

failures = []
def check(n, c):
    print(("PASS" if c else "FAIL"), "-", n)
    if not c: failures.append(n)


# Verbatim from the IEM AFOS archive, trimmed to the lines that matter.
FINAL_FIRST = """CDUS42 KMFL 300824
CLIMIA

CLIMATE REPORT
NATIONAL WEATHER SERVICE MIAMI,FL
424 AM EDT SUN AUG 30 2026

...THE MIAMI CLIMATE SUMMARY FOR AUGUST 29 2026...

TEMPERATURE (F)
 YESTERDAY
  MAXIMUM         90   3:25 PM  95    1952  90      0       93
  MINIMUM         76   8:12 AM  69    1912  77     -1       77
"""

FINAL_REISSUE = """CDUS42 KMFL 300910
CLIMIA

CLIMATE REPORT
NATIONAL WEATHER SERVICE MIAMI,FL
510 AM EDT SUN AUG 30 2026

...THE MIAMI CLIMATE SUMMARY FOR AUGUST 29 2026...

TEMPERATURE (F)
 YESTERDAY
  MAXIMUM         85   5:11 AM  95    1952  90     -5       93
  MINIMUM         76   8:12 AM  69    1912  77     -1       77
"""

SAME_DAY_PRELIM = """CDUS42 KMFL 302025
CLIMIA

...THE MIAMI CLIMATE SUMMARY FOR AUGUST 30 2026...

VALID TODAY AS OF 0400 PM LOCAL TIME.

 TODAY
  MAXIMUM         91   1:49 PM  95    1951  90      1       93
"""


def test_the_two_final_issuances_disagree_by_five_degrees():
    a, b = parse_cli_text(FINAL_FIRST), parse_cli_text(FINAL_REISSUE)
    check("first final summarises Aug 29", a["summary_date"] == "2026-08-29")
    check("re-issue summarises Aug 29 too", b["summary_date"] == "2026-08-29")
    check("neither is preliminary", not a["preliminary"] and not b["preliminary"])
    check("first says 90", a["maximum_f"] == 90)
    check("re-issue says 85", b["maximum_f"] == 85)
    # Kalshi's expiration_value for KXHIGHMIA-26AUG29 was 90.00 -- the withdrawn one.
    check("they disagree, which is what makes this a revision",
          a["maximum_f"] != b["maximum_f"])


def test_same_day_preliminary_is_not_a_revision():
    """The trap: it is CLIMIA, issued 2026-08-30, and it is NOT about Aug 29."""
    p = parse_cli_text(SAME_DAY_PRELIM)
    check("preliminary summarises Aug 30, not Aug 29", p["summary_date"] == "2026-08-30")
    check("preliminary is flagged preliminary", p["preliminary"] is True)
    check("its MAXIMUM parses (91) but belongs to another day", p["maximum_f"] == 91)
    # Filtering on summary_date is what keeps it out of Aug 29's issuance list.
    kept = [t for t in (FINAL_FIRST, FINAL_REISSUE, SAME_DAY_PRELIM)
            if parse_cli_text(t)["summary_date"] == "2026-08-29"]
    check("only the two Aug-29 finals survive the filter", len(kept) == 2)


def test_parse_is_defensive_about_junk():
    empty = parse_cli_text("")
    check("empty text -> no date, no max", empty["summary_date"] is None
          and empty["maximum_f"] is None and empty["preliminary"] is False)
    no_max = parse_cli_text("...THE MIAMI CLIMATE SUMMARY FOR AUGUST 29 2026...\n"
                            "  MAXIMUM         MM   MM\n")
    check("a missing 'MM' maximum is None, not 0", no_max["maximum_f"] is None)
    check("but the date still parses", no_max["summary_date"] == "2026-08-29")
    bad_month = parse_cli_text("...THE X CLIMATE SUMMARY FOR SMARCH 29 2026...")
    check("an unparseable month yields None, not a crash",
          bad_month["summary_date"] is None)
    neg = parse_cli_text("...THE X CLIMATE SUMMARY FOR JANUARY 5 2026...\n"
                         "  MAXIMUM        -12   3:25 AM\n")
    check("negative maxima parse (this runs in winter too)", neg["maximum_f"] == -12)


def test_months_spanned_covers_the_window():
    check("single month", months_spanned(date(2026, 8, 3), date(2026, 8, 29))
          == [(2026, 8)])
    check("two months", months_spanned(date(2026, 7, 30), date(2026, 8, 2))
          == [(2026, 7), (2026, 8)])
    check("year boundary", months_spanned(date(2025, 12, 30), date(2026, 1, 2))
          == [(2025, 12), (2026, 1)])
    check("a 60-day window never returns fewer than 2 months",
          len(months_spanned(date(2026, 7, 10), date(2026, 9, 7))) == 3)


def main():
    test_the_two_final_issuances_disagree_by_five_degrees()
    test_same_day_preliminary_is_not_a_revision()
    test_parse_is_defensive_about_junk()
    test_months_spanned_covers_the_window()
    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
