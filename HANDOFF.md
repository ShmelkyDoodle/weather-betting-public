# weather-arb — handoff brief

Self-contained context for an LLM picking this up cold. `CLAUDE.md` (965 lines) is the
living reference with every detail; this is the strategic summary and the things most
likely to be got wrong. **Where the two disagree, the code wins — verify before acting.**

---

---

## ⚠⚠ CORRECTIONS 2026-08-26 — four claims below are FALSE. Read this first.

An independent audit (`AUDIT-2026-08-26.md`) re-derived the load-bearing numbers
in this file. Four did not survive. Three of them were filed under "SOLID —
mechanically verified". Everything downstream of them needs re-reading.

**1. "Strategy 1 safety: 421 detections, 0 wrong calls" — FALSE.**
`backtest.run_backtest` computes `matched_result` **only for detections that
clear the 98¢ ceiling** (`backtest.py:245-268`); the 420 priced-out ones are
counted and discarded ungraded. The line printed is the *tradeable* count, so the
real output was `1 / 0`. Reproducible grading of every detection now lives in
`scripts/replay_lockins.py`.

**2. `deterministic_lock_in` was UNSOUND — fixed 2026-08-26.** It compared a
float METAR reading against the raw integer strike, so `84.02 > 84` called the
`{83,84}` bucket a certain NO — but a true max in `[84.0, 84.5)` rounds to a CLI
of 84 and settles **YES**. Measured over real settlements:

      basis                     rule         detections  wrong   accuracy
      station_logs running max  legacy           564       18     96.81%
      station_logs running max  margin 0.5       527        2     99.62%
      daily_tmax (live input)   legacy           432       14     96.76%
      daily_tmax (live input)   margin 0.5       401        0    100.00%

The two survivors are the KDEN 2026-08-16 138.2 °F sensor spike, which the glitch
guard rejects in production. **The guard is load-bearing for safety, not just
accuracy.** New knob `WA_LOCKIN_MARGIN_F` (default 0.5 = exact certainty).
Reproduce: `.venv/bin/python scripts/replay_lockins.py`.

**3. "`R = CLI − METAR max` … never negative (n=245)" — FALSE.** R is definitively
negative on **16 of 245** station-days. The original figure came from rounding the
METAR max to an integer before differencing (which maps 84.02 → 84 and turns
R = −0.02 into 0). **So "R ≥ 0 is what makes Strategy 1 safe" is not a valid
proof.** Safety now comes from the margin in (2), which is derived from
`settlement_interval_f()` and asserted exhaustively in `tests/test_v34.py`.

**4. Every inference drawn from the ABSENCE of live signals is VOID.**
`_orderbook_from_kalshi` read `payload["orderbook"]`, but Kalshi now serves only
`orderbook_fp` (dollar-string prices, decimal sizes). **The live engine saw an
empty book for every market, on every call, for the whole paper run.** With no
ask, Strategy 1 always took the priced-out branch and Strategy 2 never found a
qualifying price — which is why both logged zero signals. That was read as
"lock-ins are always already at 100¢" and "the live ask is basically never inside
the fee-adjusted limit". **Neither is supported.** Fixed 2026-08-26
(`tests/test_v36.py`). Backtest numbers are unaffected — they read candlesticks.

**5. The 17.4¢ latency edge and the $1,183/100-contracts estimate are
UNREPRODUCIBLE.** The commits that recorded them (`36583d1`, `bd3348e`) changed
**only this file** — no script, no query, no cached data. Treat as a hypothesis,
not a prior. `weather_arb/experiment_report.py` now regenerates every statistic
it prints from persisted rows.

**FIRST LIVE RESULT (2026-08-26, n=10, one evening — NOT a conclusion).** With the
book readable, all 10 detections were rejected as **`no_quote`**: on a decided
market the `yes_dollars` book empties completely, so the sure side cannot be
bought at any price. Depth at signal median **0** against a control median of
**2,443** contracts. "Priced at 100¢" and "no bid at all" are indistinguishable in
candle data and economically opposite. Needs the full 21 days.

Run `.venv/bin/python -m weather_arb.experiment_report --days 1 --gate0`.

---

## 1. What this is

An async Python system that trades **Kalshi daily-high-temperature markets** for 7 US
cities (NY, CHI, MIA, AUS, DEN, LAX, PHIL). Running unattended in **dry-run (paper) mode**
on the owner's Mac mini since 2026-07-22. No Kalshi API keys → `dry_run` auto-on → orders
are journaled, never sent.

Two strategies:
- **Strategy 1 — lock-in (deterministic).** Once the observed daily max crosses a bucket
  boundary the outcome is certain → buy the sure side up to 98¢. Near-riskless.
- **Strategy 2 — model edge.** Forecast P(bucket) vs market price; trade when the edge
  clears ~15% after fees.

- Repo: `github.com/ShmelkyDoodle/weather-betting` (private). Python 3.14 in `./.venv`.
- Deps: `aiohttp`, `aiosqlite`, `cryptography`. No pandas/numpy/scipy — **pure stdlib** for
  all analysis. Keep it that way.
- 32 offline test files in `tests/`. Run: `PYTHONPATH=. .venv/bin/python tests/test_v31.py`.
  All pass as of 2026-08-25.

---

## 1b. How much to trust each number in here

Not all of this is equally solid. Triage before acting:

**SOLID — mechanically verified, reproducible, locked by tests.** Treat as ground truth.
- Settlement basis = NWS CLI (245/245). Reproduce: `python -m weather_arb.settlement_basis`.
- Bucket geometry: integers, strict tails, 420/420 tiling. Locked in `tests/test_v27/v28`.
- The grid fix: event probabilities went 0.70 -> exactly 1.0, confirmed on live rows.
- The backfill: 994,819 rows, self-validating (max reproduction error 9.57e-13).
- The live gate truth table (`tests/test_v31`), and the L2 sweep math (`tests/test_v32`).
- Strategy 1 safety: 421 detections, 0 wrong calls.

**REASONABLE — measured out-of-sample, but a single window. Re-run before leaning on it.**
- Strategy 2 at −$3.26/trade; time-decay threshold harmful; persistence mildly positive.
- Inversion at −$0.27/trade — and note it is explicitly *unstable* (TRAIN +1.03, TEST −1.52).
- The ~48-minute 1-minute-vs-METAR lead (664 crossings, consistent across 4 stations).

**⚠ SUSPECT — do NOT let this drive a funding decision without redoing it.**
- **The $1,183 per 100 contracts latency-edge estimate (§3).** It is built on *top-of-book
  asks*, which the live engine would never have paid — it sizes with
  `buyable_*_size(ceiling)`, a depth-aware sweep. Known optimistic by an unknown amount.
  The L2 logging added 2026-08-25 exists specifically to replace this number with real VWAPs.
- The liquidity claim behind it (median lifetime volume 15,387) — the field names are right
  *now*, but it came out of an analysis that produced two wrong answers first.

**Author's note on reliability.** The ad-hoc analysis in this project has produced confident
wrong answers more than once — a broken shell filter that reported a missing heartbeat, a
misread of `model_predictions.created_at_utc` (it is upserted, so it never moves), an
out-of-range candle query returning zero rows, and Kalshi's `volume_fp` field read as
`volume`. Each was caught, but two were reported as conclusions before being caught. The
committed code is well covered by tests; **the exploratory numbers deserve independent
re-derivation.** When a result looks good, assume the measurement is broken until proven
otherwise — that heuristic has paid off every time it was applied here.

---

## 2. THE BOTTOM LINE (read this before proposing any work)

**Neither strategy makes money, and the reason is structural, not a bug.**

| finding | number |
|---|---|
| Strategy 2, corrected probabilities | **−$3.26/trade** |
| Strategy 2 *inverted* (fade the signal) | **−$0.27/trade** (and unstable across train/test) |
| Strategy 1, corrected backtest | **1 tradeable opportunity in 30 days, +$3.73** |
| Strategy 1 safety | **421 detections, 0 wrong calls** |
| Oracle (perfect forecast, same rule + fees) | **+$38.21/trade** |

The oracle number is the key diagnostic: **the plumbing converts skill fine.** The gap is
forecast skill, and there is none available — the system prices *public* NWS/NBM/MOS data
into a market that is pricing *the same public data*. No informational advantage exists by
construction. That single fact explains why Strategy 2 loses, why fading it also loses, and
why model choice is the smallest lever measured ($3.42/trade across 15 model variants).

**Do not propose better forecasting models.** It has been measured as the smallest lever,
and frontier AI weather models (GraphCast/GenCast/Aurora/AIFS) are gridded ~0.25° products
while this needs station-level daily max at a specific ASOS site — exactly what NBM/MOS
already downscale to.

---

## 3. The one real edge, and why it is not reachable

**The market reprices exactly when the hourly METAR publishes — but the outcome is decided
~48 minutes earlier.**

Traced `KXHIGHPHIL-26AUG03-T87`: 21¢ two hours before the crossing, 55¢ one hour before,
**99/100¢ six minutes after the METAR published**. The market is not ahead of us; it reads
the same feed at the same time.

The 1-minute ASOS record proves a bucket crossing a **median 48 minutes** before the hourly
METAR does (664 crossings, 4 stations × 12 days; 82% have ≥20 min, 59% ≥40 min). Replicated
on price: 247 lock-in windows, 7 stations, 20 days, **0 wrong calls**.

- Median ask at t1 is **100¢** — 56% of windows have no edge.
- But **68 of 247 (28%)** are discounted below 98¢, mean **17.4¢** gross ≈ **$1,183 per 100
  contracts over 20 days**.
- Those discounts are in the **most liquid** markets (median lifetime volume 15,387, peak OI
  ~11,000) — not an illiquidity artifact.

**⚠ BLOCKER: there is no real-time sub-hourly feed.** IEM's 1-minute archive is **~2 days
stale** (same-day and previous-day requests return *zero* observations; measured 2026-08-25).
`onemin_ingest` polls exactly that endpoint, so **`WA_ENABLE_1MIN_LOCKIN` is structurally
incapable of ever helping** — it is not "unmeasured", it is unusable. Replace the source or
delete the feature.

**⚠ UNRESOLVED: depth.** The ask at t1 is *top-of-book close* for that minute. High lifetime
volume does not prove 100 contracts were available at 14¢ at that instant, and the
candlestick endpoint carries no depth data. **Resolve this before spending money on a feed.**

**⚠ 2026-08-26: the instrument meant to resolve it was broken, and is now partly fixed.** The
`lock-in L2 depth` probe shipped in `a446dd2` sat *below* the price-ceiling gate, so with 420
of 421 lock-ins already at 100¢ it would have accrued ~1 sample/month; it was also never
loaded, because the tracker had not been restarted since the commit. Both fixed: the probe is
above the gate, a `lock-in detected` line now fires before any gating (first live
corroboration of the 421-detections claim), and the tracker was restarted 2026-08-26T03:28Z.
Verified firing.
**But this still only samples t2.** The 17.4¢ discount lives at t1, ~48 min earlier, and is
structurally unobservable without a real-time sub-hourly feed. **The $1,183 figure remains
unvalidated and item 1 below remains open** — do not read "depth is now being logged" as
"depth is resolved".

**So an edge here is a data-acquisition problem, not a modelling problem.** Unverified
candidates for a low-latency ASOS feed: NOAA MADIS, Synoptic Data / MesoWest.

---

## 4. Verified ground truth (do not re-litigate; re-measure if you doubt it)

**Kalshi settles on the NWS CLI daily high.** 245/245 settled events, 100% at every city.
The hourly-METAR max reproduces it only 62% of the time. Run
`python -m weather_arb.settlement_basis` to reproduce.

**Bucket geometry** — buckets tile the **integers**, both tails **strict** (verified 420/420
events tile with no gap or overlap):
- `less`, cap `C` → `T <= C-1`
- `between`, floor `F`, cap `F+1` → `T ∈ {F, F+1}` (the `B101.5` in a ticker is a *midpoint
  label*; the API's `floor_strike`/`cap_strike` are the integers)
- `greater`, floor `F` → `T >= F+1`

**The settlement residual** `R = CLI − hourly-METAR max`: mean **+0.81 °F**, RMS **1.08**,
range [0, 3], **never negative** (n=245). `R >= 0` is what makes Strategy 1 safe — both
lock-in branches need `settlement >= observed`.

**Climate day uses a FIXED Local-Standard-Time offset (no DST).** Cities span UTC−5..−8.
Do not "fix" this to DST-aware local time.

---

## 5. Landmines (each of these cost real debugging time)

1. **A "verified" finding can be circular.** The 2026-08-21 session concluded "TWC == our
   hourly-METAR max, 147/147, 0.00 °F" — by taking the max of TWC's *hourly observations*
   feed, i.e. the same data we already ingest. It could only ever return "identical". It
   drove several downstream conclusions, all wrong. **Score against settlements Kalshi
   actually paid out, never against a third-party observations API.**

2. **Kalshi candles use `volume_fp` and `open_interest_fp`**, not `volume`/`open_interest`.
   Reading the wrong keys yields silent zeros and looks exactly like "this market never
   traded". This nearly buried the §3 finding.

3. **`period_interval` on the candles endpoint is in MINUTES, not seconds.** A previous
   session passed 60 believing it was seconds, read hourly bars, and every Strategy-2 P&L
   was ~$0.81/trade optimistic from look-ahead. Also: an out-of-range `start_ts`/`end_ts`
   silently returns zero candles.

4. **`trades` can NEVER populate in dry-run.** `place_limit_order`'s dry-run branch returns
   `filled_count=0`, and `_record_fill` (the only writer of `trades`) needs `> 0`. Query
   `open_limit_orders` instead — journaled before transmit, never deleted. Use
   `scripts/query_lockins.sql`.

5. **A lock-in order's `price_cents` is always 98** (the ceiling, a marketable limit), *not*
   the ask crossed. The real ask is in the ntfy payload and the `lock-in signal` log line.

6. **Fractional Kelly is MODEL_EDGE-only** (`risk_manager.py:186` gates on
   `strategy is Strategy.MODEL_EDGE` **and** `win_prob is not None`). Lock-in orders are
   `LOCK_IN` with no `model_prob`, so `WA_KELLY_FRACTION` is a **no-op for Strategy 1**.

7. **`model_predictions` is upserted**, so its `created_at_utc` never moves. It is not a
   "last written" signal. Use the append-only `model_prediction_history`.

8. **`slots=True` dataclass:** never read a field default via `ClassName.field` — use a
   default instance (`RiskConfig()`), or you get the slot descriptor.

9. **IEM ASOS throttles bulk requests** (fetch one date-range per station with `network=`);
   **Kalshi 429s under load** (use `backtest._kalshi_get`'s backoff).

10. **`KNYC:9:US` in TWC's location namespace silently aliases to KLGA.** Use the geocode
    endpoint and assert on `obs_id`.

---

## 6. Live system

Running via launchd (`~/Library/LaunchAgents/`): `com.weatherarb.tracker` (the engine),
`.verify`, `.weekly` (Mon 08:00 — backfills `prob_grid_fixed`, then regenerates
`report.html`), `.watchdog`, plus optional `.divergence` and `.logrotate`.

- Restarted **2026-08-24T00:05:22Z** onto the corrected bucket grid + kinematic METAR guard.
- Health: `tail logs/tracker.out.log` (heartbeat ~10 min, `markets_tracked` > 0);
  `grep MISMATCH logs/*.log` should be empty.
- Restart: `launchctl unload ~/Library/LaunchAgents/com.weatherarb.tracker.plist; sleep 5;
  launchctl load ~/Library/LaunchAgents/com.weatherarb.tracker.plist`
- `.env` exists (gitignored). ntfy topic `weatherarb-<redacted>`, **verified delivering to
  the owner's phone**. Diagnostic: `.venv/bin/python test_push.py`.
  ⚠ `AlertManager.send` swallows every exception — "no traceback" is not "it arrived".
  ⚠ ntfy's poll endpoint lags 2–3 s, so an immediate read-back false-negatives.
- ⚠ `.env` was copied from `.env.example`, which ships `WA_ARB_EVAL_SECONDS=20` against a
  code default of 120. It is **pinned to 120** — 20 would be 6× the Kalshi orderbook load.

**All feature flags default OFF and every one has been measured:**

| flag | verdict |
|---|---|
| `WA_ALLOW_LIVE_MODEL_EDGE` | the live gate — keeps Strategy 2 shadowed when `WA_DRY_RUN=false` |
| `WA_ENABLE_DYNAMIC_EDGE` | time-decay threshold — **measured harmful**, all 25 configs lost |
| `WA_ENABLE_EDGE_PERSISTENCE` | mildly positive, unproven (backtest is the optimistic bound) |
| `WA_ENABLE_PASSIVE_MODEL_EDGE` | **measured, costs money** |
| `WA_ENABLE_1MIN_LOCKIN` | **structurally unusable** — its source is 2 days stale |
| `WA_ENABLE_PROBABILITY_CALIBRATION` | reduces loss ~72%, still net-negative |

**Going live safely:** `WA_DRY_RUN` is global, but `WA_ALLOW_LIVE_MODEL_EDGE` (default false)
blocks model-edge orders in `RiskManager.size_order` once dry-run is off. Truth table in
`tests/test_v31.py`. Dry-run behaviour is byte-for-byte unchanged, so paper measurement of
Strategy 2 continues.

---

## 7. Commands

```
.venv/bin/python -m weather_arb.<cmd>
  report [--html]            P&L, hit rate, calibration, model leaderboard
  signals                    plain-English "what to bet & when" (places nothing)
  settlement_basis           which basis Kalshi settles on + the sigma-floor derivation
  backtest --strategy 1      lock-in backtest (detection / priced-out / tradeable breakdown)
  backtest --strategy 2 [--grid-fixed] [--sweep|--sweep-dynamic|--passive|--calibrate]
  divergence | intraday | wu_calibration | cross_venue | cross_venue_quotes | verify_mappings
```

`--grid-fixed` reads `prob_grid_fixed` — history repriced on the corrected bucket grid.
**Every stored `prob` predates the 2026-08-23 grid fix**, so without this flag a Strategy-2
backtest measures the old, broken pricing.

---

## 8. If you pick this up, do this

1. **Resolve depth** (§3) — STILL OPEN. Live t2 capture is now working (see §3), but t2 is
   after the repricing. Answering the *t1* depth question requires the same low-latency feed
   as item 2, so these two are not independent: **item 2 is a prerequisite for item 1**, not
   an alternative to it. What t2 capture can settle cheaply in the meantime: whether a
   discounted lock-in has any resting size at all, which bounds the optimism.
2. **Price out a low-latency ASOS feed** (MADIS, Synoptic). Without it, §3 is unreachable and
   the honest answer is that this system has no edge.
3. **Do not fund the account on Strategy 1's merits** — 421 detections, 420 already at
   exactly 100¢, ~$45/year at 100 contracts.
4. Re-run `backfill_grid_probs.py --corrected-since 2026-08-24T00:05:22+00:00` before any
   `--grid-fixed` backtest (the weekly job does this; the timestamp is a **fixed historical
   constant** — never change it to "now").

**Working method that has repeatedly paid off here:** measure against ground truth that
actually paid out; split train/test before believing any tuned number; and when a result
looks too good, assume the measurement is broken until proven otherwise. In this session
that discipline overturned five separate conclusions — including two of my own, twice in the
same analysis.
