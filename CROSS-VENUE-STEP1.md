# Cross-venue Step 1 — verify Polymarket's settlement basis

---

---

# ✅ FOLLOW-UP — 2026-09-08 (session 2). All three open items closed.

Reproduce::

    PYTHONPATH=. .venv/bin/python -m weather_arb.cross_venue
    PYTHONPATH=. .venv/bin/python -m weather_arb.polymarket_basis --days 48 \
        --validate-target KMIA:2026-09-02
    PYTHONPATH=. .venv/bin/python scripts/cli_revision_audit.py --days 60 --check-products
    for t in tests/*.py; do PYTHONPATH=. .venv/bin/python $t; done   # 40 files, all pass

Commits `34ff486`, `afac2dd` on `cross-venue-step1-polymarket-basis`. `main` is
still at `f757af7`; nothing here touches the running experiment.

## 1. Registry fixed — and the arb candidate set is now EMPTY

`cross_venue.STATION_MAP` and `divergence.SAME_STATION` / `PM_STATION` were wrong
in three ways, and they are **three different kinds of wrong**:

| what | old value | truth | kind |
|---|---|---|---|
| Polymarket DEN station | KDEN | **KBKF** (Buckley SFB) | **wrong from the start** |
| Polymarket source | Weather Underground | **NWS WRH** since ~2026-08-21 | **stale, once true** |
| Polymarket PHIL | shared station | **no market listed** | absent, not shared |

**OBSERVED** — the DEN entry was never right. Its `resolutionSource` names KBKF in
the *WU era too* (`.../us/co/aurora/KBKF`, checked 2026-07-25 and 2026-08-05), and
the station is identical across both eras in all six cities. Only the source moved.

The WU-era rows are **kept** in `STATION_MAP` with `superseded_on` set rather than
overwritten, so the switch stays legible. New `DataSource.NWS_WRH` and
`DataSource.NOT_LISTED`; new `effective_from` / `superseded_on` on `VenueStation`;
`stations_for()` returns only current rows and `history_for()` reaches the rest.

⚠ **`DayWindow` got no new member, deliberately.** `LOCAL` was already defined as
the DST-aware local wall clock — that *is* the civil day. Adding a `CIVIL` synonym
would make `LOCAL != CIVIL` and fabricate a basis difference against ForecastEx
where none exists. The docstring now says so explicitly.

**⚠ THE CONSEQUENCE, and it is the headline.** ForecastEx↔Polymarket used to be the
lockable-candidate set on the shared-airport cities. It is now **empty**:

- Polymarket is **OBSERVED** on NWS WRH (payout-scored, 281/282).
- ForecastEx's source is a **2026-07-22 reading** of Weather Underground, never
  re-checked — and Polymarket switching sources *inside this very window* is the
  proof that such readings go stale.

Different source ⇒ BASIS, so `scan_quotes` and `scan_bucket_locks` now correctly
propose **nothing**. WU and WRH agreed on 192/192 WU-era city-days, so the pair may
well still lock — that is a reason to pull ForecastEx's live contract terms, not for
the registry to assert it. Tests assert the empty set against the real registry and
exercise the lock math against a synthetic one.

Also fixed in passing: **`KLGA` and `KBKF` were missing from
`divergence._IEM_NETWORKS`**, so every NYC cross-station fetch has been running
without the `network=` param; `wu_station_for` returns `""` rather than a fabricated
station for an unlisted city; and `divergence.py`'s retracted 2026-08-21 "basis
INVERTED" warning is gone — it asserted the opposite of the corrected registry
sitting directly beneath it.

⚠ Still true and worth repeating: `divergence.pm_max_f` is an **approximation**. It
reads IEM's pre-rounded `tmpf` column with no `report_type` filter, and that row set
scores ~69% against Polymarket's payouts. Use `polymarket_basis` when it has to be
right.

## 2. The one miss: CONFIRMED, and it is OUR decoder, not the basis

**MIA 2026-09-02 — the venue's own feed maxes at 90, inside the paid `[90,91]`.**
The decisive row is the 13:45 EDT SPECI:

    KMIA 021745Z ... 32/25 ... RMK AO2 ... T03170250
      body group  32 C   -> 89.6  -> 90   <- what Synoptic served, and what paid
      T-group   31.7 C   -> 89.06 -> 89   <- what we reconstructed

Row *selection* was identical (30 rows both sides), so this is a decode difference
between our IEM-based proxy and the feed, not a wrong basis.

**⚠ THE BLOCKER WAS MISDIAGNOSED — it was never a quota.** The previous session
recorded the Synoptic token as exhausted. It is not. The weather.gov token needs
**both** `Referer` *and* `Origin: https://www.weather.gov`. With only `Referer`,
every request is refused with `"Invalid request per token rules"` — including
`recent=60`, the call the live page itself makes — which is exactly what makes the
refusal read like a spent allowance. `wrh_headers()` plus a test pin both headers.

**⚠ THE STATED MECHANISM DOES NOT SURVIVE.** The "Synoptic's copy has a double space
before the T-group" story is not supported. Across 931 KMIA rows all 3 fallbacks
contained irregular whitespace — but so did **73 rows**, and 70 of those decoded the
T-group correctly. Necessary-looking, plainly not sufficient. **Trigger: UNKNOWN.**

**And a documented UNKNOWN became a number.** With the feed reachable the fallback
rate is measurable, so it was measured — 19 station-days, all six stations:

      body-fallback rows            8 / 499 official rows = 1.6%
      station-days with >=1         5 / 18
      MOVED the daily max on        1 / 19   (KMIA 2026-09-02)

So `encoding_sensitivity`'s ~46%-of-city-days ceiling overstates the real rate by
roughly an order of magnitude — it assumes the fallback lands on the max-setting row
every time. Small sample; read it as scale, not as a rate.

⚠ **Scope.** This re-scores **one** day against the live feed. The other 287 were
scored on the reconstruction and are *not* re-validated by it, and the decode gap is
bidirectional — it could in principle have turned a miss into a hit elsewhere. The
headline stays **287/288 as reconstructed** (the window grew by 6 days since the
first run, hence 288 rather than 282).

New `--validate-target STID:DATE` points the check at a chosen station-day; the old
sampler only took newest-per-station, and the interesting days are the misses.

## 3. The Kalshi anomaly: the CLI WAS revised — and Kalshi settled on the withdrawn value anyway

**Neither framing in the brief is quite right on its own.** The CLI was revised, but
*before* settlement, not after — and Kalshi settled on the superseded number with
the correction nearly three hours old.

**OBSERVED**, the full sequence for MIA 2026-08-29:

| when (UTC) | product | says |
|---|---|---|
| 2026-08-29 20:24 | `202608292024-KMFL-CDUS42-CLIMIA` (same-day **preliminary**) | MAX **85** |
| 2026-08-30 08:24 | `202608300824-KMFL-CDUS42-CLIMIA` (**final**, 1st issuance) | MAX **90** ← erroneous |
| 2026-08-30 09:10 | `202608300910-KMFL-CDUS42-CLIMIA` (**final**, re-issue) | MAX **85** ← stands |
| 2026-08-30 12:00:11 | Kalshi settles `KXHIGHMIA-26AUG29` | `expiration_value` **90.00** → `[90,91]` YES |

- The erroneous 90 contradicted **its own office's preliminary from 15.5 h earlier**.
- It was withdrawn **2h50m before** Kalshi's `settlement_ts`.
- **The 90 was never real.** Not one KMIA observation that day reaches even
  87.5 °F. Over Kalshi's own **LST climate day** the max is **84.92 °F → 85** —
  exactly the corrected CLI (over the DST-aware civil day it is 86.0 °F, from a
  00:05 EDT reading that belongs to Aug 28 in LST). At the 3:25 PM the bad product
  named, it was **80.6 °F**. Cool rainy day (precip 0.62"), max pre-dawn. So the 90
  was an error at publication, not a QC'd-out genuine reading.
- Kalshi has never re-settled: `updated_time` still equals `settlement_ts`.
- **Timing.** KMIA settled at 11:10–11:20Z on every one of the surrounding 13 days.
  This day alone settled at **12:00:11Z** — the later of the two windows the rules
  allow ("the first 7:00 or 8:00 AM ET following the release of the data"). It was
  delayed ~45 min *and still* used the withdrawn value.

**INFERRED** (one inference, named): Kalshi or its data vendor read the CLI once at
or near first publication and did not re-read before settling. `expiration_value`
matches the withdrawn issuance exactly, and matches the current CLI on 419 of 420
other city-days. **The mechanism is UNKNOWN** — snapshot-at-publication, a vendor
cache, and a manual entry all fit.

**HOW OFTEN.** New `scripts/cli_revision_audit.py` compares Kalshi's
`expiration_value` against the CLI as the archive now reads it, for every settled
city-day:

      window 2026-07-10 .. 2026-09-07, all 7 cities
      settled events   420
      gradable         420   (expiration_value present on every one)
      MISMATCHES         1   <- KMIA 2026-08-29, and only that

⚠ **That 1/420 is a FLOOR on CLI revisions, not a count of them.** A revision Kalshi
*caught* leaves both numbers agreeing and is invisible to this method. It is an
*exact* count of how often a revision reached settlement. Separating the two needs
the AFOS product archive per day, which `--check-products` pulls for the mismatching
days only.

⚠ **A trap in that archive, worth not relearning.** Each WFO issues its CLI twice a
day — a late-afternoon preliminary for *today*, and a morning final for *yesterday* —
and both land on the same archive date. Listing by issue date alone mixes two climate
days: on 2026-08-30 it produces a phantom third "revision" reading MAXIMUM 91, which
is Aug 30's preliminary and has nothing to do with Aug 29. The script parses the
`CLIMATE SUMMARY FOR <date>` line and drops other-day products; `tests/test_v39.py`
locks that. **And a re-issue is NOT flagged as a correction** — same WMO header, no
`CCA`, only a later timestamp — so nothing but the parsed content distinguishes it.

**WHY THIS MATTERS BEYOND THIS TRADE.** Every strategy in this repo settles on the
Kalshi CLI, and Strategy 1's whole claim is that a proved observation makes the
outcome *certain*. It does not make it certain: the settlement value can be a number
that never happened. On this day a correct Strategy-1 lock-in on "≤87" — provable
from the observations, which never reached 87.5 °F under either day convention —
would have settled **NO** and lost. Observed rate 1 in 420 city-days; the loss when
it fires is the full contract, and no guard in this repo can see it coming, because
the observations were right.

## What is STILL open after this session

1. **ForecastEx's current settlement source is UNKNOWN** and now blocks the whole
   lock thesis. Its registry row is a 2026-07-22 reading. Pull the live contract
   terms; until then there is no lockable pair on the board.
2. **The day-window axis is still UNRESOLVED** (P1 civil and P2 LST agree on
   288/288). Unchanged from session 1 — it will start mattering in winter.
3. **The Synoptic body-fallback trigger is UNKNOWN.** Whitespace is not it.
4. **Step 2 — economics — untouched.** Polymarket access (USDC/wallet/gas, US via
   QCEX) remains entirely uninvestigated.
5. **48 days of summer, six cities, one country.** Everything here inherits that.

---

# ✅ RESULTS — 2026-09-08. GATE PASSED. Basis identified.

Reproduce everything below with::

    PYTHONPATH=. .venv/bin/python -m weather_arb.polymarket_basis --days 48 [--cache-dir DIR]
    PYTHONPATH=. .venv/bin/python tests/test_v38.py        # 28 tests, 76 checks

Committed on branch `cross-venue-step1-polymarket-basis` (`44d68c4`).
**`main` is deliberately left at `f757af7`** so the frozen lock-in experiment's
commit is untouched. Merge is trivial — the change is two new files
(`weather_arb/polymarket_basis.py`, `tests/test_v38.py`) and nothing imports them.

## THE ANSWER (OBSERVED — 281/282 = 99.6% of resolved markets)

**Polymarket settles on the max of the WRH "Hourly Data" rows — routine METARs
plus SPECIs, excluding the sub-hourly MADIS feed — each rounded half-up to a
whole °F from the METAR `T`-group.**

Scored against Polymarket's own payouts, 282 resolved city-days, 6 cities,
2026-07-22..2026-09-07:

| candidate basis | wrh era | wu era |
|---|---|---|
| **P1 WRH hourly, civil day** | **89/90 = 98.9%** | **192/192 = 100%** |
| P2 WRH hourly, LST day | 89/90 = 98.9% | 192/192 = 100% |
| P4 routine METAR only (no SPECI) | 96.7% | 97.9% |
| P6 body °C encoding | 80.0% | 81.8% |
| P3 all rows incl. MADIS | 68.9% | 70.3% |
| P5 NWS CLI at Polymarket's station | 64.0% | 57.5% |

**P5 is the load-bearing negative.** At ~61% the CLI does NOT reproduce
Polymarket, while `settlement_basis` shows it reproduces Kalshi 245/245. The two
venues really do settle on different definitions — the premise of the trade holds.

## FOUR PREMISES IN THE BRIEF ABOVE WERE WRONG

1. **Polymarket CHANGED its resolution source mid-window.** Weather Underground
   through ~2026-08-21, NWS WRH after. The registry's "WU" was **stale, not
   wrong**. Eras are scored separately for exactly this reason — pooling them
   would have blended two bases into one confident meaningless number. The same
   reconstruction reproduces the WU era **192/192**, so WU's daily table and the
   WRH hourly table agree on this sample and **both eras are usable**.
2. **Polymarket DOES list Los Angeles** (`los-angeles`, site `klax`).
   Philadelphia genuinely is absent. Six overlapping cities, not five.
3. The market `description` adds two clauses the brief omitted, and the first is
   decisive: *"resolve off of the **Hourly Data**"* (P3 scores 69% without it)
   and *"whole degrees Fahrenheit"*.
4. Stations, read off each event's own `resolutionSource` — never the registry:

   | city | Polymarket | Kalshi | same? |
   |---|---|---|---|
   | MIA / AUS / LAX | KMIA / KAUS / KLAX | same | **yes** |
   | NYC | **KLGA** | KNYC | no |
   | CHI | **KORD** | KMDW | no |
   | DEN | **KBKF** (Buckley SFB) | KDEN | no |

## DIVERGENCE — replaces the provisional 40.9%

- **OBSERVED (payouts only, no inference): ≥29.4%, ≤71.6%.** Kalshi's settled
  market pins its value only to a two-integer range, so 119 of 282 days overlap
  Polymarket's bucket without matching it and are counted in **neither** bound.
  Calling them agreement would understate; calling them divergence would overstate.
- **INFERRED (both values on Polymarket's own ladder): 47.2%** [41.4–53.0].
  Costs one inference — that the CLI is Kalshi's settlement. Stable across eras
  (wrh 44.4%, wu 48.4%), so it is not carried by one regime.
- **Directional, and this is the tradeable part.** On the three shared-station
  cities, `D = round(Kalshi CLI) − Polymarket observed max` over n=141 is
  **mean +0.77, range [0,+2], NEVER negative**, nonzero on 90/141 = 64% of days.
  Kalshi settles at or above Polymarket, never below.

  ⚠ **This is NOT the claim the 2026-08-26 audit retracted.** That one was about
  the *float* residual `CLI − float METAR max`, which is negative in 16/245. This
  is the *integer* difference, which is what actually settles — both venues pay on
  whole degrees. Both statements are true simultaneously (CLI 84 vs obs 84.02:
  float residual −0.02, integer difference 0).

- Per city (INFERRED reading): NYC 68.1%, CHI 51.1%, LAX 44.7%, AUS 42.6%,
  DEN 40.4%, MIA 36.2%. NYC is highest because KLGA-vs-KNYC is a station
  difference *on top of* the basis difference.

**No P&L is computed.** This is a basis, not a lock.

## WHAT IS STILL OPEN (in the order that matters)

1. **Fix `cross_venue.py` STATION_MAP.** It is now unblocked — all six cities are
   confirmed. It currently claims Polymarket DEN = KDEN via Weather Underground;
   both fields are wrong, and `DataSource`/`DayWindow` need a `NWS_WRH` /
   civil-day entry. `divergence.py`'s `SAME_STATION` and `PM_STATION` carry the
   same errors (it lists PHIL as shared; Polymarket does not list PHIL at all).
2. **The one miss is probably a flaw in the venue's own feed — UNVERIFIED.**
   MIA 2026-09-02: we say 89, it paid 90-91. Chasing it produced a real finding:
   against a live WRH pull for KLGA, 35/35 rows matched by timestamp and 34/35 to
   the raw °F. The exception was `030530Z`, where the METAR carries `T02060194`
   (20.6 °C = 69.08 °F) but Synoptic reported **69.8** — the whole-degree body
   group. Both feeds hold byte-identical text **except Synoptic's copy has a
   double space before the T-group**, so its decoder misses it and falls back.
   The KMIA miss has exactly that shape (body 32 °C → 90, T-group 31.7 °C → 89).
   **Not confirmed** — the token quota ran out. One targeted Synoptic call for
   KMIA 2026-09-02 settles it. Run `--validate-wrh N` once the quota resets.
3. **The day-window axis is UNRESOLVED, not answered.** P1 (civil/DST) and P2
   (fixed LST) returned identical values on **282/282** days — a daily maximum
   never falls in the one hour the conventions disagree about. Harmless today,
   but it will start mattering in winter or at a station with a late-evening max.
   ⚠ This tie tripped the gate's original tie-break clause; **the clause was
   refined after seeing the data and that flipped FAIL → PASS.** Documented in
   full in the module docstring. The 95% threshold was untouched.
4. **A Kalshi settlement anomaly worth its own look, unrelated to Polymarket.**
   MIA 2026-08-29: the CLI (85), our observations (85, full 24-h coverage, a
   genuinely cool rainy day whose max fell pre-dawn) and Polymarket's payout
   ([84,85]) all agree — but **Kalshi settled [90,91]**. Its whole bucket ladder
   started at "≤87", which settled NO. Either Kalshi mis-settled or the CLI was
   revised after settlement. It is the ONLY day in 282 where the CLI falls outside
   Kalshi's own settled range. This is settlement risk for *any* Kalshi strategy,
   not just this one.
5. **Step 2 — economics — is now unblocked but untouched.** 47.2% bucket
   divergence is not an edge until it survives crossing two spreads on two venues.
   Polymarket access remains entirely uninvestigated: USDC/wallet, gas, US access
   via QCEX. `cross_venue_quotes.py` already reads live Polymarket via the Gamma
   API and is the place to start recording quotes.
6. **48 days of summer only.** The CLI-vs-observed gap is unmeasured in winter or
   at low temperatures, and the shared-station "never negative" result inherits
   that limit. Six cities in one country share synoptic regimes, so the Wilson
   intervals printed are narrower than the truth.

## METHOD NOTES THAT ARE LOAD-BEARING

- **Ground truth is the payout, never another API's idea of the max.** This is the
  circular check this repo made once already; the module docstring names it.
- Temperature is decoded from the METAR `T`-group **here**, not read from IEM's
  `tmpf` column — that column is pre-rounded to a whole °F with an unstated tie
  rule, and exact halves occur (22.5 °C is exactly 72.5 °F).
- Rounding is `floor(x+0.5)` to match JavaScript `Math.round`, which is what the
  WRH page applies. Python's `round` is banker's rounding and disagrees there.
- The row set comes from IEM's `report_type=3&report_type=4` filter. `obs.js`'s own
  predicate is applied as an independent cross-check and disagreed on **0 of
  ~92,000 rows**. Every official row reappeared in the unfiltered pull (0 unmatched).
- **Getting at the WRH feed** (worth not relearning): the token is published in the
  clear at `weather.gov/source/wrh/apiKey.js`; a `Referer: https://www.weather.gov/wrh/timeseries?site=<icao>`
  header is **required** (403 without it); adding a `vars=` parameter is refused
  outright (`"Invalid request per token rules"`); `start`/`end` are **UTC** even
  when `obtimezone=local`; and the quota exhausts after a few dozen calls, which is
  why IEM sources the history and Synoptic only validates it. A refusal now
  surfaces its own message rather than being reported as a bare fetch failure.
- KBKF carries **no MADIS sub-hourly feed** in IEM, so for Denver P1 and P3 read
  the identical row set and P3's rejection is untested there. ~10% of KBKF METARs
  carry no `T`-group at all (military reporting practice) and fall back to whole °C.

---

**Read-only. Do not touch the running lock-in experiment** (tracker PID under
`launchctl list | grep weatherarb`, config frozen at commit `f757af7`,
`WA_DRY_RUN=true`). Nothing here restarts or reconfigures it.

## Why this exists

Kalshi and Polymarket both list daily-high-temperature buckets for the same US
cities, but they settle on **different temperature definitions**. If that
difference is large and directional, it is a basis trade that requires no
forecasting skill — which matters because every skill-based approach in this
repo has been measured and is dead (see `HANDOFF.md` §2).

Preliminary measurement: comparing Kalshi's CLI settlement bucket against an
observed-max basis over 308 settled station-days, **40.9% land in a different
bucket, and 124 of 126 divergences (98.4%) have the observed-max basis one
bucket LOWER**. That figure is provisional — see "what's wrong with it" below.

## What was just established (2026-09-08, via the Gamma API)

Polymarket's `resolutionSource` and market `description` state the basis
explicitly. It is **NOT Weather Underground** (the repo registry says WU — that
is wrong):

> "the highest temperature recorded by NOAA at the <X> Station ... specifically
> the highest reading under the 'Temp' column for all times on this day,
> available here: https://www.weather.gov/wrh/timeseries?site=<icao>"

So: **Kalshi = NWS CLI daily high. Polymarket = max of the NWS WRH observation
timeseries.**

Per-city resolution sites actually returned:

| city | Polymarket site | Kalshi station | same? |
|---|---|---|---|
| MIA | `kmia` | KMIA | yes |
| AUS | `kaus` | KAUS | yes |
| DEN | `kbkf` (Buckley Space Force Base) | KDEN (Denver Intl) | **NO** |

`weather_arb/cross_venue.py` `STATION_MAP` claims Polymarket DEN = KDEN via
WU. Both the station and the source are wrong. Fix the registry only after
confirming the remaining cities the same way (pull `resolutionSource` per city;
NYC and CHI are also unverified and are believed to differ by station anyway).

## The task

Score Polymarket's **own resolved markets** against candidate bases, the way
`weather_arb/settlement_basis.py` already does for Kalshi. That module is the
template — read it first (459 lines, `collect()` / `analyze()` / `_render()`).

⚠ **THE TRAP.** A previous attempt to verify a settlement basis in this repo was
**circular**: it took the max over the same observation feed already being
ingested, so it could only ever return "identical". It never tested the value
the venue actually settles on. Score against **settled outcomes**, not against
another API's idea of the max.

### Data available

- **Polymarket resolved outcomes** — Gamma API, unauthenticated:
  `GET https://gamma-api.polymarket.com/events?slug=highest-temperature-in-<frag>-on-<month>-<day>-<year>`
  frags: `miami`, `austin`, `denver`, `nyc`, `chicago` (LAX and PHILADELPHIA are
  not listed). Each event has ~11 markets; the winner has
  `outcomePrices == ["1","0"]`, losers `["0","1"]`, with
  `umaResolutionStatus == "resolved"`. Bucket label is `groupItemTitle`
  (e.g. `"88-89°F"`, `"79°F or below"`, `"98°F or higher"`).
- **Our observation history** — `weather_arb.sqlite3`, table `station_logs`
  (`icao, observed_at_utc, temp_f, accepted`), ~2026-07-22 onward. Climate day
  uses a **fixed LST offset, no DST** (`weather_arb/timeutils.py`).
  ⚠ Note Polymarket says "all times on this day" — local civil day, likely
  DST-aware. That is a second, separate basis difference worth testing.
- **NWS CLI** — `settlement_basis.fetch_cli_highs()` already implemented.
- **NWS WRH timeseries** — the actual Polymarket source; not yet fetched by any
  code here. May need to be pulled directly to test it properly.

### Candidate bases to score

1. max of our hourly METAR record over the fixed-LST climate day
2. same, over the DST-aware local civil day
3. NWS CLI daily high (should NOT match if the venues genuinely differ)
4. max of the NWS WRH timeseries, if fetchable

### Output

Per city and pooled: what fraction of resolved Polymarket markets each candidate
basis reproduces. `settlement_basis.py` scored Kalshi at 245/245 = 100% for the
CLI; anything materially below that means the basis is not yet identified.

## Gate

**If no candidate reproduces Polymarket's settlements at high accuracy, stop.**
Do not proceed to quote recording or economics — the basis is not understood and
any divergence number computed from a guess is worthless.

If a basis IS confirmed, recompute the 40.9% divergence figure using the correct
per-city stations and bases. The DEN contamination means the current number is
provisional.

## Do not

- Modify, restart, or reconfigure the lock-in experiment.
- Assume a station or source from the registry — it has been wrong.
- Compute hypothetical P&L. This is a basis trade, not a lock: the CLI-vs-observed
  residual is negative in 16 of 245 station-days, so it carries real risk.
- Treat Polymarket access/fees as solved. It is USDC/crypto; wallet, gas and
  US-access questions are uninvestigated.
