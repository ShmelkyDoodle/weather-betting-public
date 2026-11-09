# CLAUDE.md — weather-arb (read this first)

Async Kalshi weather-derivative arbitrage system. This file is the handoff/onboarding
summary; **deep detail is in [README.md](README.md)**. If anything here conflicts with the
code, the code wins — verify before acting.

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

**6. ⚠⚠ SAFETY-RELEVANT, ADDED 2026-09-10. "Strategy 2 has never produced an
order" is FALSE and was quoted in the go-live section as a mitigating factor.**
It is the downstream victim of correction 4: the engine saw an empty book for
every market, so of course it never fired. With the book readable since
2026-08-26 the running tracker shows **5,398 model-edge signals** and
**4,853 `open_limit_orders` rows, every one `model_edge`** (4,458 canceled,
395 resting right now). **Flipping `WA_DRY_RUN=false` today would immediately
place live Strategy-2 orders at a measured −$3.26/trade** if the gate were open.
The one thing standing in the way is `WA_ALLOW_LIVE_MODEL_EDGE` (default false).
Do not re-derive comfort from an order count — verify the gate.
Meanwhile Strategy 1 has **273 `lock-in detected`** events and **zero** orders,
which is the expected result, not a fault.

**7. ⚠ "Kalshi settles on the CLI, 245/245 = 100%" and "R is never negative" — BOTH
superseded 2026-10-30 by a 693-event re-audit (`AUDIT-2026-10-30.md`).** The basis claim
survives at **99.79%**, not 100%: the August window ended six days before its only
counterexample (MIA 2026-08-29, Kalshi settled a **withdrawn** CLI issuance). `R >= 0` is
**false** at scale (3/468 negative, min −3.00 °F) — the second time that particular prop has
been kicked out from under Strategy 1. The strategy's live record is intact (**0 wrong in 940**
graded detections) but it rests on the **glitch guard and the 0.5 °F margin**, not on a basis
property. **Riskless is the wrong word; "bounded below breakeven, and still only worth $3.64 in
six weeks" is the right one.** Reproduce:
`.venv/bin/python scripts/settlement_audit.py --days 100 --from-db`.

---

## Current status

**✅ SAFETY POSTURE VERIFIED 2026-09-10 — no real money is reachable. Three independent
locks, all confirmed on the running system:**

      WA_DRY_RUN=true                  in .env (explicit)
      WA_ALLOW_LIVE_MODEL_EDGE=false   in .env (explicit) -> Strategy 2 blocked even if dry_run is off
      Kalshi credentials               ABSENT -> dry_run would auto-engage anyway
      "LIVE TRADING ENABLED" warning   not present in tracker.out.log

Tracker up since **2026-09-02** (so the 2026-08-23 grid + METAR fixes are live), heartbeat
fresh, no `MISMATCH` in any log. **Re-check with the four lines above before ever touching
`WA_DRY_RUN`** — and read correction 6 at the top first: the old "Strategy 2 never fires
anyway" reassurance is dead.

- **Running UNATTENDED in DRY-RUN (paper) mode** on the user's Mac mini since 2026-07-22
  (clean ledger `weather_arb.sqlite3`). No Kalshi API keys → `dry_run` auto-on → orders are
  **journaled, never sent**. It is measuring, not trading real money.
- Repo: `github.com/ShmelkyDoodle/weather-betting` (private). Latest known commit `6b75f36`
  (run `git log --oneline -5` for current).
- **Python 3.14** in `./.venv`. Deps: `aiohttp`, `aiosqlite`, `cryptography` (`requirements.txt`).

## What it does
Two strategies, **limit-orders only**, 7 cities (NY, CHI, MIA, AUS, DEN, LAX, PHIL):
- **Strategy 1 — lock-in (deterministic):** once the day's observed high crosses a bucket
  boundary the outcome is certain → buy the sure side up to 98¢. Event-driven, near-riskless,
  *infrequent*.
- **Strategy 2 — model edge:** forecast P(bucket) vs. market price; trade when the edge clears
  ~15% after fees.
- **Forecasting:** 4 models logged as shadow predictions per market/day — `current` (NWS
  gridpoint), `nbm` + `gfs_mos` (station MOS via IEM), `ensemble`, plus `*_bc` bias-corrected
  and `*_id` intraday-sharpened variants (and `intraday` alone). The report ranks them by
  **Brier**; trading auto-selects the best once ≥20 settled samples exist
  (`WA_TRADING_MODEL=auto`), else falls back to `current`. `auto_pnl` instead selects
  the highest backtested-P&L model from the `model_pnl` cache (Brier ≠ profitability), then Brier.
  An `auto` pick only *trades* an `_id` model when `WA_ENABLE_INTRADAY_FORECAST` is on.

## Running services (launchd, in ~/Library/LaunchAgents/)
| Label | What | Output |
|---|---|---|
| `com.weatherarb.tracker` | the engine (`weather_arb.main`) | `logs/tracker.out.log` (JSON; heartbeat ~10 min) |
| `com.weatherarb.verify` | daily settlement-mapping check | `logs/verify.log` |
| `com.weatherarb.weekly` | Mon 08:00: **backfill `prob_grid_fixed`**, then dashboard regen | `report.html` |
| `com.weatherarb.watchdog` | every 30 min: heartbeat-stale + mapping + **order-book readable + activity** (2026-09-11) | `logs/alerts.log` (+ ntfy) |
| `com.weatherarb.divergence` *(optional)* | long-running CLI-vs-METAR divergence alerts — ⚠ its original framing was RIGHT — the 08-21 "inverted" note is retracted, see "SETTLEMENT BASIS" | `logs/divergence.log` (+ ntfy) |
| `com.weatherarb.logrotate` *(optional)* | daily 03:30: rotate oversized `logs/*.log` (copytruncate + gzip) | `logs/logrotate.log` |

**Health check, one command:** `.venv/bin/python scripts/checkup.py` — safety gate, heartbeat,
**can the engine still SEE the market**, is it doing anything, disk, and what the run has
accumulated. Plain English, read-only, exit 0/1/2. Use this after any long gap instead of
grepping logs.

⚠ **A FRESH HEARTBEAT IS NOT A HEALTHY SYSTEM.** The 2026-08 orderbook outage ran for weeks
with a perfect heartbeat while `_orderbook_from_kalshi` returned an empty book on every call
(see CORRECTION 4). Both strategies logged zero signals and that was written up as a finding.
The watchdog now also (a) fetches a live book **through the production parser** — a probe that
re-implemented parsing would have passed while production failed — and (b) checks the tracker
has produced a signal/detection/observation recently (`WA_WATCHDOG_ACTIVITY_MINUTES`, default
180). Locked by `tests/test_v42.py`, which replays the exact regression.

Older manual check: `tail logs/tracker.out.log` (is the heartbeat timestamp fresh, markets_tracked>0?);
`grep MISMATCH logs/*.log` (should be empty). Restart a job: `launchctl unload` then `load` its
plist (no DB reset needed — schema is `CREATE IF NOT EXISTS` + additive migration).

## Commands (from repo root; `.venv/bin/python -m weather_arb.<cmd>`)
- `report [--html] [--json]` — P&L, hit rate, calibration, model leaderboard (net of fees)
- `signals` — plain-English "what to bet & when" (nothing is placed)
- `backtest --strategy 2 --calibrate [--min-price N] [--persist] --days 21` — honest
  out-of-sample test of the calibration fix: fits per-model isotonic recalibration
  (`weather_arb.calibration`) on the earlier part of the window, reports base-vs-fixed
  P&L on the held-out rest. Diagnosed 2026-08-14: models are well-calibrated in
  aggregate but overconfident on cheap longshots the edge rule over-selects; fix gave
  ~72% out-of-sample loss reduction (still net-negative). `--persist` caches to
  `model_calibration`; live use is opt-in via `WA_ENABLE_PROBABILITY_CALIBRATION` +
  `WA_MODEL_EDGE_MIN_PRICE_CENTS` (both off by default). DO NOT go live yet.
- `backtest [--strategy 1|2] [--persist] --days 30` — Strategy-1 lock-in (IEM archive +
  candlesticks) or Strategy-2 model edge. Strategy 2 enters each (ticker,model) at the FIRST
  archived snapshot an edge appears (`model_prediction_history`; falls back to last view for
  pre-feature data), holds to settlement; official YES/NO from Kalshi settled markets (not the
  sparse local table). Per-model P&L/hit-rate net of fees — reveals forecast accuracy ≠ edge
  profitability. `--persist` caches P&L to `model_pnl` for `WA_TRADING_MODEL=auto_pnl`.
- `backtest --strategy 2 --sweep [--days N] [--calib-train-frac 0.6] [--min-train-trades 20]`
  — **out-of-sample** threshold x price-floor sweep (#4). Picks the best config per model on
  the TRAIN part of the window, scores ONLY that pick on the held-out TEST part, and pools
  all models at each FIXED threshold in both windows (nothing fitted) as the robustness
  check. Read the fixed panel, not the picked-config table.
- `backtest --strategy 2 --passive [--days N] [--passive-improve N] [--fill-model M]`
  — **taker vs resting (maker) placement** (#5). Three panels, in order: the PRIZE
  (measured spread on the markets Strategy 2 actually enters), the EXECUTION EFFECT
  (matched (ticker,model) pairs where both runs traded the SAME SIDE — same market,
  same settlement, so only the price differs), and the SELECTION EFFECT (what each
  run traded alone). Sweeps `improve` ticks in both train and test. **Read panel 3
  only**: pooled per-trade P&L is contaminated by trade count, and opposite-side
  matched pairs are different forecasts, not better execution. Verdict below.
- `settlement_basis [--days N] [--eval-hour H] [--json]` — **which basis Kalshi actually
  settles on**, scored against its own settled markets, plus the derivation of
  `intraday_sigma_floor_f` (half-normal MLE of the settlement residual, with a walk-forward
  out-of-sample check) and the bucket-grid mass check. Read-only. Run this before touching
  the floor or believing anything about the settlement basis.
  ⚠ **Its `--days` is capped by Kalshi, not by you** — the settled listing retains ~67 days
  (cliff measured at 2026-08-23 on 2026-10-29), so `--days 140` silently returns the same 67.
  To reach further back use `scripts/settlement_audit.py --from-db`, which adds the local
  `market_results` ledger (back to 2026-07-21).
- `scripts/settlement_audit.py [--days N] [--from-db] [--size N] [--cache F] [--from-cache F]`
  — **is Strategy 1's certainty proof actually certain?** (added 2026-10-30). Scores the CLI
  against Kalshi's `expiration_value`, lists **every** disagreeing event individually, splits
  the residual by DIRECTION (only a settlement *below* our observation can break a lock-in),
  then replays the live `deterministic_lock_in` over every settled market — on both the raw
  IEM archive and production's own `daily_tmax` — and grades on Kalshi's own result. Also
  prints the ledger's graded verdict per source and the breakeven wrong-rate. Read-only; the
  ledger is opened `mode=ro`. See `AUDIT-2026-10-30.md` for the findings.
- `backtest --strategy 2 --sweep-dynamic [--grid-fixed] [--days N]` — out-of-sample sweep of
  the 0b time-decay threshold (max 0.15-0.35 x min 0.02-0.10) plus a standalone persistence
  panel. Read the pooled fixed-config panel, not the per-model picked-config table. Verdict
  2026-08-23: decay is harmful, persistence mildly positive — see backlog 0b.
- `backtest --strategy 2 --grid-fixed` — same as above but reads `prob_grid_fixed`, the
  history repriced on the corrected bucket grid by `scripts/backfill_grid_probs.py`. Every
  stored `prob` predates the 2026-08-23 grid fix, so this is the only way to backtest the fix
  without waiting for fresh data. See "BUCKET GRID" for the result.
- `verify_mappings` — check series→station vs Kalshi live rules
- `cross_venue [--scope us|global] [--json]` — cross-venue basis monitor: maps how
  Kalshi / ForecastEx / Polymarket (+ Betfair/Smarkets under `global`) settle each
  city and flags which pairs are lockable-arb candidates vs basis-only. Reference
  only, never trades. Toggle scope via `WA_VENUE_SCOPE` (default `us`).
- `cross_venue_quotes [--date Y-M-D] [--city X] [--all] [--json]` — live read-only
  quote monitor: pulls per-bucket top-of-book and scans arb pairs for a lock
  (`yes_ask+no_ask<100¢` net fees). Polymarket via public Gamma API (no key);
  ForecastEx needs `IBKR_WEB_API_BASE` (no public feed) else Polymarket-only.
- `divergence [--date Y-M-D] [--city X] [--days N] [--watch] [--json]` — CLI-vs-METAR
  divergence. ✅ **Its ORIGINAL framing is correct** (see "SETTLEMENT BASIS"): `nws_max_f`
  (1-min/CLI) IS the Kalshi basis (245/245), `wu_max_f` (hourly METAR) is the
  Polymarket/ForecastEx basis. The 08-21 "INVERTED" warning is **retracted** — it rested on a
  circular measurement. So `delta_f` describes a real cross-venue settlement edge, and the
  MIA/AUS/DEN/LAX/PHIL edge is back on (shared station, different basis).
  ⚠ **The in-module docstring still carries the retracted 08-21 inversion and is wrong.**
  `--watch` =
  live detector: polls today's running divergence and pushes an ntfy alert (via
  AlertManager) when a divergence/bin-flip newly appears. IEM 1-min archive lags ~1h.
- `intraday [--city X] [--now] [--json]` — intraday daily-max model: learns the
  per-local-hour "remaining rise" (`daily_max - running_max_at_h`) climatology from
  the IEM archive; `--now` predicts today's max from the running max. Uncertainty
  collapses to ~0 after peak (mid-afternoon). **Now wired live** — see "Intraday
  sharpening" below.
- `wu_calibration [--days N] [--json]` — ✅ **its `nws` column is the Kalshi-relevant one**
  (the 08-21 swap is retracted; Kalshi settles on the CLI). ⚠ **The in-module docstring still
  carries the retracted inversion and is wrong.** Recalibrate models to the WU
  settlement basis: re-grades stored forecasts (bias/MAE) against NWS (Kalshi) AND
  WU (Polymarket/ForecastEx) truths, both from the IEM archive (NOT daily_tmax,
  which is incomplete during downtime), and prints the per-station WU offset.
- `watchdog` — manual health check
- `logrotate [--dir logs --max-mb 50 --keep 5]` — copytruncate+gzip rotation for
  oversized launchd logs (safe while services hold the files open)

## Intraday sharpening (`*_id`) — wired live 2026-08-16
`intraday.IntradayCurves` feeds the per-LST-hour remaining-rise curve into `ForecastClient`.
Each model gains a blended `<model>_id` variant (plus standalone `intraday`), logged as a
shadow prediction like `_bc`/`climo` so Brier and `backtest --strategy 2` grade it for free.
- **Blend** (`intraday.blend_normal`): mean is precision-weighted (forecast leads in the
  morning, the observation takes over after peak); sigma is the **sharper of the two, NOT**
  the Bayesian product rule — the two errors describe the same air mass, so the product rule
  would manufacture confidence. Don't "fix" this to `sqrt(1/(1/sa²+1/sb²))`.
- **Sigma floor** `WA_INTRADAY_SIGMA_FLOOR_F` (default 1.0 °F). ✅ **DERIVED 2026-08-23 — the
  original rationale was CORRECT and the value is right. Keep 1.0. Do NOT set it to 0.**
  It is a settlement-basis allowance for hourly-METAR-vs-CLI, and that gap is real: `R =
  settlement − hourly-METAR max` has mean **+0.81 °F**, RMS **1.08**, range [0,3], never
  negative (n=245; see "SETTLEMENT BASIS"). The 08-21 claim that the gap is 0.00 °F came from
  a circular measurement.
  **Why the floor is exactly the right shape for it:** measured from LST 16:00 the intraday
  remaining-rise curve is `0.00 ± 0.00` (n=224/hour), so late in the day the model is exactly
  `N(running_max, floor)` truncated below at `running_max` — a **half-normal with scale =
  floor**. The MLE of a half-normal scale is the RMS of the residual → **1.08**. Its implied
  mean shift is `0.798 × floor = +0.80 °F`, essentially the measured +0.81: **the truncation
  is already doing the recentring**, which is why adding an explicit bias term on top
  over-corrects (tested: worse out-of-sample in 3 of 4 folds).
  **Held at 1.0**, not 1.08: out-of-sample the two are indistinguishable (test NLL splits 2-2
  across four walk-forward folds), and moving a live constant on a tie would perturb the
  in-flight `_id` Brier experiment for nothing. Re-run `python -m weather_arb.settlement_basis`
  before changing it. Per-station RMS varies (DEN 1.52 vs MDW 0.81) but per-station fitting
  overfit badly out-of-sample at n=32/station — do not split it yet.
- **Non-blocking:** a curve costs ~12 windowed IEM fetches (~45s/station/day, measured).
  Lookups never wait — they return `None`, kick off a background build, and the engine
  forecasts unsharpened until it lands. Failures back off 30 min (no request storm).
  Cached per (icao, MM-DD). `signals` (one-shot CLI) uses `wait_for_build=True` instead.
- **Trading gate:** `WA_ENABLE_INTRADAY_FORECAST` (default off). Off = shadow only.
- Cost: 5 new shadow models (10 → 15), so `model_prediction_history` grows ~50% faster.

## ✅ SETTLEMENT BASIS — RE-MEASURED 2026-08-23, RE-AUDITED AT SCALE 2026-10-30
**Kalshi settles on the NWS CLI daily high.** Re-audited over the full settled record —
**693 events, 7 stations, 2026-07-22..2026-10-28** (`AUDIT-2026-10-30.md`):

| candidate basis | reproduces Kalshi's settled bucket |
|---|---|
| **NWS CLI daily high — exact value** | **467/468 = 99.79%** |
| **NWS CLI daily high — inside the paid bucket** | **692/693 = 99.86%** |
| round(hourly-METAR max) | 300/469 = 64.0% |

⚠ **IT IS NOT 100%, AND THE OLD WINDOW WAS LUCKY, NOT WRONG.** The 2026-08-23 run really did
score 245/245 — it **ended six days before the only counterexample**. The single disagreement
is **MIA 2026-08-29: settled 90 against a CLI of 85**, because the CLI was issued twice and
Kalshi settled the **withdrawn** issuance 2h50m after the correction was published
(`CROSS-VENUE-STEP1.md` §3). **No guard in this repo can see that coming** — our observations
were right. Rate 1/468 = 0.21%.

Reproduce with `python -m weather_arb.settlement_basis` (new, read-only). This is the
authoritative check: it scores candidates against settlements Kalshi actually paid out,
not against a third-party API.

**⚠ THE 2026-08-21 "TWC == our hourly-METAR max, 147/147, 0.00 °F" FINDING WAS CIRCULAR.**
It took the max over TWC's *hourly observations* feed — which is the same hourly METAR data
we already ingest — so it could only ever return "identical". It never tested the value
Kalshi settles on. Verified 2026-08-23 at KNYC: TWC's hourly-obs max (78, 76 on 08-21/08-22)
equals our METAR max exactly, while Kalshi settled [79,80] and [77,78] — matching the CLI
(79, 77). TWC's own `max_temp` field is a 24 h *trailing* max and matches neither. **The
+0.80 °F CLI-vs-METAR gap that session measured was real; it just attributed it to the wrong
side.** Do not redo this from an observations endpoint — score against settlements.

**BUCKET GEOMETRY (verified, 420/420 events tile with no gap or overlap):** buckets tile the
**integers**, and both tails are **strict**:
- `less`, cap `C`  → `T <= C-1`
- `between`, floor `F`, cap `F+1` → `T in {F, F+1}` (two integers; the `B101.5` in the ticker
  is a midpoint *label*, the API's `floor_strike`/`cap_strike` are the integers)
- `greater`, floor `F` → `T >= F+1`

Exactly one of the six settles YES. Floor parity varies by city-day (217 even / 203 odd).

**THE RESIDUAL (this is what the sigma floor is for):**
`R = settlement − hourly-METAR max` over the fixed-LST climate day, n=245:
mean **+0.81 °F**, sd 0.71, **RMS 1.08**, range **[0, 3]**, and ~~never negative~~
⚠ **"NEVER NEGATIVE" IS FALSE AT SCALE — re-measured 2026-10-30, n=468: mean +0.748,
RMS 1.019, range [−3.00, +5.00], and R < 0 on 3 events (0.64%).** The floor is unaffected
(RMS 1.019 vs 1.08 — still ~1.0), but **"R ≥ 0 is what makes Strategy 1 safe" is dead for
the second time.** See the Strategy-1 section below for what actually makes it safe.
Histogram `{0: 86, 1: 121, 2: 36, 3: 2}`. Per-station RMS: DEN 1.52 (outlier), MIA 1.04,
LAX 1.03, NYC 1.03, AUS 1.00, PHL 0.96, MDW 0.81.
Cause: the hourly METAR is an instantaneous reading; the CLI max comes from the continuous
record, so it catches peaks between observations. `R >= 0` always — this is a one-sided gap.

**Downstream consequences, re-derived (these REVERSE the 08-21 conclusions):**
- **`intraday_sigma_floor_f`'s original rationale was CORRECT, not void.** It really is a
  settlement-basis allowance for hourly-METAR-vs-CLI. Derived value **1.08** (see below).
- **`WA_ENABLE_1MIN_LOCKIN` is NOT "definitively harmful"** — that verdict rested on the
  inverted premise. It moves T_max *toward* the CLI basis, i.e. toward settlement. It is now
  directionally RIGHT. It is still **unmeasured as a net win** (its guards and ~1 h IEM lag
  are untested against settlements), so leave it off until measured — but the reason changed.
- **`divergence.py`'s ORIGINAL framing was right**; the 08-21 "INVERTED" correction was itself
  the error. `nws_max_f` (1-min/CLI) is the Kalshi basis; `wu_max_f` (hourly METAR) is what
  Polymarket/ForecastEx use. Its `delta_f` describes a real cross-venue settlement edge again,
  and the MIA/AUS/DEN/LAX/PHIL edge is **back on** (shared station, *different* basis).
  ⚠ Docstrings in `divergence.py` / `wu_calibration.py` still carry the 08-21 inversion —
  they are wrong and not yet corrected in-module.
- **`wu_calibration`: the `nws` column is the Kalshi-relevant one** (back to the original
  reading). Corollary that still holds: the tracker grades forecasts against `daily_tmax`
  (hourly METAR), so its grading basis is *not* the settlement basis — it grades ~0.81 °F low.
  Trading/backtest grading uses Kalshi's own settled results and is unaffected.
- **Strategy 1 lock-in is SAFE in the measured record — but `R >= 0` is NOT why.**
  ⚠ **RE-AUDITED 2026-10-30 (`AUDIT-2026-10-30.md`).** Both branches do need
  `settlement >= observed`, but `R >= 0` is **false** (3/468 negative, min −3.00 °F), so it
  guarantees nothing. The live record still holds — **0 wrong in 940 graded `metar`
  detections** — and what holds it is the **METAR glitch guard** plus the **0.5 °F margin**,
  both of which were *fully consumed* in at least one observed case:
  - Replaying the live rule on the **raw IEM archive**: 1,468 detections, **1 wrong**
    (KNYC 2026-08-27 `T80`, −$185.48). On production's own `daily_tmax`: **0 wrong in 1,331**.
    The whole difference is the guard, which rejected that day's 67.2 °F/hour sensor spike.
  - **The direction is what saves us, and it is only half a guarantee.** Every certainty
    branch proves `settlement >= boundary`, so an **upward** settlement error is harmless in
    all three branches and a **downward** one is fatal in all three (asserted in
    `tests/test_v43.py`). The one Kalshi-side error measured so far was upward (+5) — but
    n=1 establishes no direction, so do **not** bank on it.
  - Breakeven wrong-rate at 189 contracts / 98¢ is **1.862%** (win +$3.52, lose −$185.48).
    The live 0/940 bounds the rate at **<0.41%**, below breakeven. The *tradeable* sample,
    though, is still **n = 1**.
- **`bin_2f` in `divergence.py` is still on the wrong grid** — even-floor 2 °F bins, but real
  floors vary in parity. Approximate; use the listed tickers for real work.

**STILL UNKNOWN (not measured — do not assume):**
- Whether the CLI-vs-METAR gap holds in winter / at low temperatures — 33 days of summer only.
- TWC preliminary→final revision size. Now *moot for the floor* (the floor is derived from the
  CLI residual, which subsumes it), but still unknown.
- Whether Polymarket/ForecastEx track the hourly-METAR max exactly (assumed, never measured).

**Still valid from the 08-21 session:** the station identities (NYC→KNYC Central Park,
CHI→KMDW, MIA, AUS, DEN, LAX, PHL), the `verify_mappings.classify_rules` CLI-product-code
matcher, and `detect_authority()`/`EXPECTED_AUTHORITY` blocking a series that drifts.
⚠ **TRAP, still true:** `KNYC:9:US` in TWC's location namespace silently aliases to KLGA.
Use `/v1/geocode/40.7789/-73.9692/observations/historical.json` and assert on `obs_id`.

## ✅ BUCKET GRID — FIXED 2026-08-23 (was: misaligned by half a degree)
**`forecast_ingest` priced a 2-integer bucket as a 1 °F window, losing ~30% of the
probability mass.** Independent of the basis question and larger than anything the floor does.
- `_estimate` calls `_truncated_normal_between(mu, sigma, lo=floor_f, hi=cap_f)`. For a
  `between` bucket that is `[F, F+1]` — **1.0 °F wide**. But the bucket covers the *integers*
  `{F, F+1}`, which in continuous space is **`[F-0.5, F+1.5]`, 2.0 °F wide**. The tails are
  wrong the other way (too wide by 0.5).
- **Measured on stored predictions:** the six probabilities of an event sum to **0.70 on
  average** (median 0.72, p05 0.29) across 4,408 floor-bound `_id` snapshots. They must sum
  to 1 — the buckets tile the integers and exactly one settles YES.
- **Measured out-of-sample against real settlements** (245 events, 4 walk-forward folds, the
  late-day model replayed exactly): fixing the grid takes log-loss **2.46-2.55 → 0.53-0.56**
  and Brier **0.068-0.084 → 0.057-0.062**. Stable in every fold, both metrics. On the RAW
  (un-renormalised) probabilities the live edge rule actually sees, log-loss is 2.95-3.02.
**THE FIX (applied):** `WeatherMarket.settlement_interval_f()` in `models.py` returns the
continuous region that *rounds into* a bucket; `forecast_ingest._estimate` integrates over
that instead of the raw strikes. Geometry lives **next to `settles_yes`** and its branching
mirrors it, so the pricing grid and the settlement rule cannot drift — `tests/test_v28.py`
asserts they agree on every integer 60..110. `settlement_basis.bucket_bounds(half_degree=True)`
now *delegates* to the same method, so the diagnostic cannot drift from production either.
- Verified: 1,260 event x scenario combinations on real Kalshi market shapes sum to
  **1.000000**; `signals` runs clean over 84 live markets; all **29** test files pass.
- Rounding is half-up, so integer `k` owns `[k-0.5, k+0.5)`. Interior `[F, F+1]` →
  `[F-0.5, F+1.5]`; strict `less` cap `C` → `(-inf, C-0.5]`; strict `greater` floor `F` →
  `[F+0.5, inf)`. Non-strict `_or_equal` variants move the other way (back-compat only —
  live Kalshi markets are all strict).
- ✅ **RESTART DONE — verified 2026-09-10.** This needed a tracker restart to reach live
  pricing, as did the kinematic METAR guard (commit `6b75f36`, 2026-08-23). The tracker has
  been up since **2026-09-02**, i.e. after both, so **both fixes are live**. (Check it the
  same way: `ps -o lstart= -p $(launchctl list | awk '/weatherarb.tracker/{print $1}')`
  against the commit date.) Nothing is pending here any more.
- ⚠ **This RESETS the `_id` Brier experiment.** Accepted deliberately: the grid fix is a
  correctness fix with ~4.6x the effect, and the experiment was comparing models that were
  all mis-pricing every bucket. Restart the clock on it from the restart date.
- ✅ **BACKFILLED 2026-08-23 — the backtest can measure it now.** See below.

### Backfill: `scripts/backfill_grid_probs.py` + `backtest --grid-fixed`
Every stored `prob` predates the fix, so the backtest was blind to it. The script reprices
all history onto the corrected grid into a **new column `prob_grid_fixed`** (the original
`prob` is never touched — it is the only record of what the live system actually did, and the
pre-fix baseline the corrected numbers get measured against). `backtest --strategy 2
--grid-fixed` reads that column.
- **980,749 / 980,940 rows repriced (99.98%)**, 191 left NULL. Runtime ~10 s.
- **The truncation bound is not stored** — `_estimate` conditions on `observed_tmax_f` from
  `daily_tmax`, which is updated in place with no history. It is **inverted in closed form
  from the stored probability** (two candidate roots per market), scored against every market
  in the (event, model, minute) snapshot, which shares one bound. **Self-validating:** the
  chosen bound must reproduce the stored OLD prob — max error **9.57e-13**, median 0.
- **`blend` cannot be inverted** — it is `0.5*climo + 0.5*(ensemble if present else current)`
  in *probability* space (`forecast_ingest.py:291`), so its stored `forecast_high_f`/`sigma_f`
  are not the parameters of any normal. It is **recomposed** from the corrected components
  instead. Missing this first showed up as 99.1% failures on `blend` and 0% everywhere else;
  fixing it took validation from 90.2% → **99.99%** of snapshots.
- ⚠ **Do not expect the 6 corrected probs to sum to 1 on every snapshot** (75.8% do). The
  exceptions are real: the gridpoint forecast cache (900 s TTL) can **refresh mid-scan**, so
  the live system genuinely priced one event off two different forecasts. 93% of deviating
  snapshots contain a varying mu/sigma outright; the rest inherit it through `ensemble`
  (whose *sigma* moves when a component refreshes) and `blend`. Verified case:
  `KXHIGHPHIL-26AUG05 18:28:08`, five markets at mu=82.0 and `T82` at mu=84.0.
- ✅ **Wired into the weekly job 2026-08-24.** `weekly.sh` runs
  `backfill_grid_probs.py --corrected-since 2026-08-24T00:05:22+00:00 --no-backup` **before**
  the report, so `--grid-fixed` never silently drops the latest week. Non-fatal (the dashboard
  must still render if the backfill hiccups) and `--no-backup` (a weekly VACUUM INTO would
  leave a ~200 MB snapshot every week). ⚠ **`GRID_FIX_CUTOVER` is a fixed historical constant
  — never change it to `date -u`/"now"**: that would push correctly-priced post-restart rows
  through the inversion path, where they fail validation at best and fit noise at worst.
- ⚠ **After the tracker restart, re-run with `--corrected-since <restart time UTC>`.** New
  rows are then born correct (`prob` is already right) but their `prob_grid_fixed` is NULL,
  so `--grid-fixed` would silently skip every post-restart row. That flag copies `prob`
  across for them and keeps the inversion away from rows that were never priced on the old
  grid (inverting one would at best fail validation, at worst fit noise).
- Safety: takes a `VACUUM INTO` snapshot first (`*.sqlite3.pre-*`, gitignored), commits in
  20k-row chunks — **not** one transaction, because the live tracker runs with
  `busy_timeout=5000` and a single ~1M-row lock would fail its writes. Idempotent/resumable.
  Verified during the real run: tracker heartbeat stayed fresh, 0 lock errors.

**RESULT (21 days, same window both ways, size 100). 14/15 models improve, mean
+$1.00/trade; pooled −$4.34 → −$3.26/trade (7,123 → 8,123 trades):**

      model         stored $/tr  hit%   grid-fixed $/tr  hit%   delta
      current_id       -2.00     36%        -0.50        30%    +1.50
      ensemble_id      -2.11     35%        -0.87        31%    +1.24
      gfs_mos_id       -1.85     35%        -1.07        31%    +0.78
      nbm_id           -1.29     38%        -1.29        31%    +0.01
      intraday         -1.77     36%        -1.81        31%    -0.04
      current          -4.39     37%        -2.96        26%    +1.42
      ensemble         -4.05     36%        -2.98        26%    +1.07
      ... (all other base/_bc models -3.4 to -4.1, every one improved)

- **Still net-negative everywhere.** Best is `current_id` at −$0.50/trade. The grid fix
  removes about a quarter of the loss; it does not make Strategy 2 profitable.
- **Trade count rises and hit rate FALLS** (37% → ~27%) while $/trade improves: corrected
  interior-bucket probabilities are higher, so more edges clear and the marginal trades are
  cheaper longshots that pay more when they land. Read $/trade, not hit rate.
- This is not a fitted result — the grid fix has no free parameters, so there is nothing to
  overfit — but it is still one 21-day window, and the *level* should not be quoted as a
  forecast.
- ⚠ **The `model_pnl` cache is stale** (pre-fix), so `WA_TRADING_MODEL=auto_pnl` is selecting on
  old numbers. Re-run with `--persist` after the tracker restart, not before (these are
  in-sample).
- ⚠ The two modes cover slightly different rows: `--grid-fixed` skips NULLs and drops the
  `model_predictions` fallback. Same 10,164 (ticker,model) pairs scanned in both here, but
  check the counts before reading a difference.

## ⚠ SEPARATE BUG FOUND 2026-08-21 — **FIXED 2026-08-23** (kept for the diagnosis) — T_max was truncated
Independent of the basis question, **our stored `daily_tmax` is badly short of truth at
high-diurnal-range stations.** ⚠ Note the table below measures `daily_tmax` against the
**hourly-METAR max** (ingest fidelity), NOT against settlement — the 08-21 session believed
those were the same thing and they are not (see "SETTLEMENT BASIS"). The bug and the fix are
real and unaffected; only the "exact %" label is. Against *settlement* every station is
additionally ~0.81 °F low, by construction. n=147:

| city | exact | mean err | worst | days short ≥2 °F |
|---|---|---|---|---|
| MIA / LAX | 100% | +0.00 | 0 | 0/21 |
| NYC / CHI | 95% | −0.24 / −0.14 | −5 / −3 | 1/21 |
| PHIL | 90% | −0.67 | −11 | 2/21 |
| **AUS** | **52%** | **−7.24** | **−28** | **10/21** |
| **DEN** | **19%** | **−11.48** | **−24** | **17/21** |

**Root cause:** `metar_ingest`'s glitch guard (`spike_threshold_f = 5.0 °F` +
`confirmation_window_seconds`) requires a jump >5 °F to be re-confirmed **within 5 minutes** —
but METARs arrive ~hourly, so the confirmation can never arrive. At stations whose normal
morning warm-up exceeds 5 °F/hour (DEN, AUS) the guard rejects *ordinary diurnal warming*,
`validated_max_f` freezes near the morning value, and it never recovers. Log evidence:
`grep "unconfirmed temperature spike held pending" logs/tracker.out.log` — KDEN/KAUS dominate,
e.g. KDEN reading 96.98 °F rejected against `prior_max_f` 78.98.
This corrupts Strategy-1 lock-in and the intraday model's running-max input at 2 of 7 cities,
and is almost certainly a bigger error source than any model choice. **This is now backlog #2.**

## Strategy-1 lock-in monitoring (added 2026-08-23)
Strategy 1 has never fired in 4+ weeks of paper running, so it needed a way to be
*seen*. `_evaluate_lock_in` now pushes to ntfy the moment an order clears the risk gate.
- **Fires after `_submit` returns non-None**, i.e. after the risk gate, not merely when the
  temperature condition is met. An alert for an order that was never journaled would be a lie.
- **Payload:** ticker, side, the breached boundary (**cap** for certain-NO, **floor** for
  certain-YES — reporting the other one is meaningless), `observed_tmax_f`, the ask being
  crossed, size, limit, order id, status, and a `DRY-RUN`/`LIVE` marker in the title.
  Tagged `moneybag` so it is instantly distinct from a `warning` health push.
- **Runs via `asyncio.to_thread`.** `AlertManager.send` is a *blocking* 10 s-timeout HTTP
  POST; calling it inline would stall the event loop during a race against the market
  repricing the certainty away — exactly the latency the strategy exists to capture.
  Failures are caught and logged: alerting must never break the trade path.
- `AlertManager.send` gained an optional `tags=` (default `warning`, unchanged for callers).

### ⚠ `trades` CAN NEVER POPULATE IN DRY-RUN — this is structural, not a bug
`market_client.place_limit_order` returns `filled_count=0` on its dry-run branch, and
`_record_fill` (the only writer of `trades`) is reached only when `filled_count > 0`. So a
query against `trades` returns zero rows *by construction*. What does get written, in dry-run
and live alike, is **`open_limit_orders`** — journaled before transmit so a crash mid-flight
is recoverable, then status-updated; nothing ever deletes from it.
- **Ledger tagging needed no change**: `Strategy.LOCK_IN` (`'lock_in'`) already exists and is
  already set on the request, and `open_limit_orders.strategy` / `trades.strategy` already
  persist it. Verified by `tests/test_v30.py` rather than re-added.
- ⚠ **STALE AS WRITTEN — corrected 2026-09-10.** This said `open_limit_orders` is empty
  because nothing had ever triggered. It now holds **4,853 rows, every one `model_edge`**
  (4,458 canceled, 395 resting) — Strategy 2 fires constantly since the orderbook fix.
  What is still true, and is the point of the section: **not one row is tagged `lock_in`.**
  Strategy 1 has logged **273 `lock-in detected`** events and produced **zero orders**,
  which is exactly the backtest's finding — the certainty is already priced at 100c, or the
  sure side's book is empty, by the time we can act.
- **Audit:** `sqlite3 -header weather_arb.sqlite3 < scripts/query_lockins.sql` — lock-in
  orders (joined to the `daily_tmax` that triggered them), a status summary, confirmed fills
  (empty until live), and a strategy-mix check so a Strategy 2 row can never be misread as a
  lock-in. `price_cents` there is the *simulated* execution price (the marketable limit,
  capped at 98c), not a fill.

### ✅ ntfy CONFIGURED + VERIFIED 2026-08-24 — topic `weatherarb-<redacted>`
`.env` now exists (gitignored) with `WA_NTFY_TOPIC=weatherarb-<redacted>`. Verified
end-to-end: ntfy.sh returns **HTTP 200** and a server-side read-back confirms the messages
are held. Diagnostic: `.venv/bin/python test_push.py` — it sends through the real
`AlertManager` and then reads the topic back, so it distinguishes "we failed to send" from
"it sent and the phone is not subscribed". Two gotchas it exists to cover:
- **`AlertManager.send` swallows every exception** and returns `None`, so calling it proves
  nothing about delivery. Never treat "no traceback" as "it arrived".
- **ntfy's poll endpoint lags ~2-3 s** behind accepting a message, so a single immediate
  read-back reports a false negative. `test_push.py` retries for ~9 s.

⚠ **`.env` was created from `.env.example`, which ships `WA_ARB_EVAL_SECONDS=20`** against a
code default of 120. It is pinned back to **120** in `.env` — the cadence the tracker has run
at for 4+ weeks; 20 would be 6x the Kalshi orderbook load on an API that 429s under load.

### (historical) ntfy was NEVER configured before 2026-08-24
`WA_NTFY_TOPIC` appears only in a *comment* in the watchdog plist and in `.env.example`;
there is no `.env`, so `settings.ntfy_url` is `None` and every alert this system has ever
raised went to `logs/alerts.log` only. To actually get the lock-in push on your phone:

    cp .env.example .env     # then uncomment WA_NTFY_TOPIC=<your-topic>

`run.sh` sources `.env`, so the tracker picks it up on its next restart. Subscribe to the
same topic in the ntfy app.

## Critical facts / gotchas (do not relearn the hard way)
- **Verified settlement stations** (Kalshi `rules_primary` CLI codes, AND cross-checked
  2026-08-21 against the station TWC actually serves): NY→KNYC (Central Park), CHI→**KMDW
  (Midway, not O'Hare)**, MIA→KMIA, AUS→KAUS, DEN→KDEN, LAX→KLAX, PHIL→KPHL. Settlement
  authority is **The Weather Company**, and TWC's daily max == the hourly-METAR max we
  ingest (147/147 station-days). `market_discovery` blocks any series that drifts off either.
- **Climate day uses a FIXED Local-Standard-Time offset (no DST)** — this matches Kalshi's
  settlement window. Do not "fix" it to DST-aware local time.
- Kalshi high-temp markets are **range buckets** (floor/cap), not simple ≥X. `deterministic_lock_in`
  handles certain-YES (tail-high floor reached) and certain-NO (max climbed past a cap).
- **`slots=True` dataclass:** never read a field default via `ClassName.field` (returns the slot
  descriptor, not the value) — use a default instance. This bit `config.py` once.
- **IEM ASOS archive throttles bulk requests:** fetch one date-range per station with the
  `network=` param. **Kalshi public API 429s under load:** use backoff (see `backtest._kalshi_get`).
- METAR updates ~hourly, so quiet stretches with no new highs are normal, not a hang.
- WS/discovery/settlement loops must stay long-lived; an early `return` makes the supervisor
  think a task crashed and tears down (fixed — keep them parked on `stop_event` when idle).

## Testing
Offline, stubbed-HTTP async test scripts live in `tests/` (`smoke_test.py`, `test_glitch.py`,
`test_v2..v43.py` — **44 files**). Run each with the venv, e.g.
`PYTHONPATH=. .venv/bin/python tests/smoke_test.py`. **All 44 passed 2026-10-30.**
Compile-check: `python3 -m py_compile weather_arb/*.py`. There is no runner script; the
one-liner is:

    for t in $(ls tests/*.py | sort -V); do PYTHONPATH=. .venv/bin/python "$t" >/dev/null \
        2>&1 || echo "FAIL $t"; done

⚠ `timeout` is not installed on this Mac — don't wrap the loop in it (it fails every test).

## Conventions
- Commit messages end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`; push to
  `origin main`. Commit/push only when the user asks (they usually do here).
- Temp/scratch files go in the session scratchpad, not the repo.

## Next possible work

### CURRENT STATE (2026-08-21) — read this first
> ⚠ **Largely superseded by 2026-08-23.** Every number in this section was measured against a
> model that mis-priced **every** bucket (the six probabilities summed to 0.70) — that grid bug
> is now FIXED, so the model-choice spread, the threshold sweep, and the `_id` verdict all need
> re-measuring after the tracker restart. The `_id` Brier experiment clock **restarts** there.
> Also: `WA_TRADING_MODEL=auto` and the `_id` verdict both depend on `market_results`, which
> only started filling 2026-08-21.

**Intraday sharpening has been live 4 days (since 2026-08-17 04:35Z). Signal is positive but
not yet conclusive; measure it on BRIER, not P&L.**
- Health: 4 days continuous, 1 (benign) ERROR total, all 7 curves rebuild daily.
- **Matched-pair P&L** (same tickers, both models traded): `_id` beats its base in ALL FOUR --
  current +$0.86/trade, nbm +$1.95, gfs_mos +$2.58, ensemble +$0.59 (100-contract size).
- **Matched Brier** (n=138-150 settled): `_id` beats its base in ALL FOUR -- current
  .1161→.1110, nbm .1392→.1172, gfs_mos .1305→.1172, ensemble .1476→.1172. All `_id` variants
  converge to ~.117 because the intraday term dominates late in the day.
- **DO NOT read the backtest's raw net-P&L column as the verdict.** `_id` trades ~half as often
  (86 vs 174: no next-day entries), so its smaller total loss is partly just less exposure. On
  *unmatched* per-trade net, `_id` is slightly WORSE for 3 of 4 models. Only the matched
  comparison is apples-to-apples.
- **Everything is still net-negative.** Best is gfs_mos_id at -$3.16/trade. Sharpening reduces
  the loss; it does not create profit. Strategy 2 stays shadow regardless of the flag.
- **Time to a verdict** (paired, 80% power / 95% conf, accruing ~34 matched settled
  predictions/day): via Brier ~1 week more for nbm & ensemble (need n=353/423, have 138),
  ~4 weeks for gfs_mos (n=1183), impractical for current (n=13426 — effect too small).
  Via P&L: 5 weeks to ~4 YEARS. Per-trade P&L variance is far too high to gate on.
  => **Give it ~2 more weeks, then decide on Brier.**
- **THRESHOLD SWEEP RESULT (2026-08-21, `backtest --strategy 2 --sweep --days 7`): direction
  confirmed, profitability NOT.** Read the pooled fixed-threshold panel, not the per-model
  picked-config table (that table shows 15/15 models "improving", which is selection + regime).
  Pooled, nothing fitted, net $/trade:
      thresh   15%    20%    25%    30%    35%    40%    45%    50%
      TRAIN  -3.56  -3.85  -4.63  -3.77  -6.12  -4.97  -2.23  -0.37
      TEST   -3.06  -1.62  -0.98  +0.69  +3.02  +5.06  +5.63  +4.18
  Higher threshold is better in BOTH windows -> the 15% default really is too permissive
  (consistent with the longshot diagnosis). But **no threshold is profitable on TRAIN** —
  the strong TEST profit is a 3-day favorable regime, NOT a validated edge. Do not raise
  `WA_MODEL_EDGE_THRESHOLD` on this evidence. Re-run at --days 21+ once data allows; the
  train window here also predates the intraday feature (live 2026-08-17), so `_id` rows in
  it are thin.
- **Model choice is the SMALLEST lever** (measured 2026-08-21): all 15 model variants span only
  $3.42/trade, while threshold spans ~$5.6/trade. An oracle (perfect forecast) earns +$38.21/trade
  through this exact rule+fees, so the plumbing converts skill fine — but frontier/AI weather
  models (GraphCast/GenCast/Aurora/AIFS) are global GRIDDED ~0.25 deg products, and this needs
  station-level daily max at a specific ASOS site, which is exactly what NBM/MOS already
  downscale to. Don't chase models before the selection rule is settled.
- This makes backlog #2 (fix report.py) the unlock, not just a dashboard nicety: the Brier
  panel is exactly the verdict instrument and it is silently empty.
  Repro of the analysis: `scratchpad/analyze.py` + `brier.py` patterns — fetch settlements from
  Kalshi (as backtest does), match on ticker, compare base vs `_id`.

### PRIOR STATE (2026-08-16, after 3 weeks live paper data)
- Tracker: 3+ weeks continuous, no crashes. **635K** prediction-history snapshots; the
  `model_prediction_history` lead-time series (WA_PREDICTION_HISTORY_INTERVAL_SECONDS=1800) is populating.
- Models forecast WELL (Brier 0.14–0.19, ~80% directional acc vs Kalshi-sourced settlements).
- **Strategy 2 loses money.** Root cause found + partly fixed: the edge-threshold rule over-selects
  cheap longshot buckets where models are overconfident (≤20¢ YES entries hit ~2%). Opt-in probability
  recalibration (`calibration.py`, isotonic/PAVA) + price floor cut held-out losses **72% (−$9,871 → −$2,787
  on an 8-day holdout)** but it is STILL NET-NEGATIVE. Do NOT go live on Strategy 2. All fixes are opt-in/off.
- Diagnostic entry point: `backtest --strategy 2 --calibrate --min-price 15 --days 21`.

### ⚠ METAR SPIKE GUARD FIXED 2026-08-23 — `daily_tmax` truncation is resolved
The flat `>5 °F` step guard is replaced by a **kinematic (velocity) check**: warming is legal
if the air could plausibly have warmed that much *in the elapsed time*. Rule 1 of the glitch
guard is now `plausible_rise()`; rules 2 (5-min confirmation) and 3 (neighbour corroboration)
are unchanged.
- **Allowance** = `8.0 °F/hour × elapsed + 1.0 °F buffer`, hard-capped at **15 °F** however
  long the gap (without the cap, a data outage would make any spike legal), and a flat
  **2.0 °F** for sub-minute updates (SPECI bursts, where a rate is meaningless). **Cooling is
  never rate-limited** — it cannot inflate the daily max. All five knobs are `WA_*` env vars.
- **Measured on 30 days of real IEM observations, 7 stations, replaying both guards against
  the true daily max of the same series:**

      station   OLD mean err   NEW mean err   OLD days short ≥2F   NEW
      KDEN          5.81 °F        0.00 °F           35%            0%
      KAUS          2.39 °F        0.00 °F           13%            0%
      KMDW          0.10 °F        0.00 °F            3%            0%
      KNYC/KMIA/KLAX/KPHL — 0.00 both (they warm gently; never had the bug)

  Worst single day went from **−24 °F (DEN) / −28 °F (AUS)** to 0. On individual warming
  steps the old rule rejected 1.82% of real rises, the new one 0.36% — **5× fewer**.
- **The guard measures from the last TRUSTED reading, not the last raw one.** A reading held
  as an unconfirmed spike must not become the baseline that legitimises the next one —
  otherwise a 110 °F spike makes a following 108 °F look like a harmless *drop*. New
  `_StationTrack.trusted_temp_f/trusted_at` fields carry this; `tests/test_v26.py` locks it.
- **It is stricter than the old rule below ~30 min elapsed** (they cross at exactly 5 °F/30
  min) and looser above. 18% of real observation gaps are under 30 min, but this costs
  nothing: a rejected fast rise is held pending, `trusted` deliberately does *not* advance,
  so the next reading sees a longer elapsed and accepts. That self-healing is why the
  measured error is 0.00 and not merely "better".
- **Fallback:** with no trusted reading yet (first observation after a restart, max seeded
  from the DB) there is no velocity to compute, so it falls back to the old flat
  `spike_threshold_f` step. Conservative, and self-heals on the next observation.
  `spike_threshold_f` also still governs `onemin_ingest`'s sanity cap — it is not dead.
- ⚠ **Requires a tracker restart to take effect** (`launchctl unload` then `load`
  `com.weatherarb.tracker`). Not done — the tracker is still running the old guard.
- Note this repairs *future* readings only. Historical `daily_tmax` rows for DEN/AUS remain
  truncated; anything backtesting off that column is still reading the corrupted values.

### ⚠⚠ WHERE THE EDGE ACTUALLY IS — MEASURED 2026-08-25. READ BEFORE ANY MORE MODEL WORK.
**The market is efficient with respect to the information we can get. The only real edge in
this game is an OBSERVATION-LATENCY edge, and our data source cannot reach it.**

**1. The market is not slow — it reprices exactly when the hourly METAR lands.** Traced
`KXHIGHPHIL-26AUG03-T87` (settled YES) hour by hour. It was **21c** two hours before the
crossing, **55c** one hour before, and **99/100c** six minutes after the METAR published.
It is not "already priced hours ahead" (an earlier note here claimed that — wrong); it moves
*with* the observation, on the same feed we read. Simultaneous, so no edge.

**2. But the outcome is decided long BEFORE the METAR says so.** The 1-minute ASOS record
proves a bucket crossing a **median 48 minutes** earlier than the hourly METAR does
(664 crossings, 4 stations x 12 days; mean 67.5, p25 26, p75 68, p90 110):

      station   n    median lead
      KAUS     155     42 min
      KDEN     276     48 min
      KNYC     113     50 min
      KPHL     120     49 min
      >= 20 min of lead: 82% of crossings.  >= 40 min: 59%.

   On the PHIL day the temperature passed 87F at **20:04**; the METAR reported it at **20:54**.
   For those 50 minutes the outcome was certain and the market sat at ~55c. That is a
   ~44c/contract gross mispricing, open for most of an hour.

**3. ⚠⚠ ANSWERED 2026-09-08 — SEE `FEED-LATENCY.md`. THE LATENCY EDGE IS DEAD,
but the reasoning below is still wrong and the real reason is different.**
The blocker below generalises from ONE endpoint: `asos1min.py` is ~2 days stale,
but **`asos.py`** carries the 5-minute MADIS reports at ~18 min. That correction
stands and it does NOT help, because of a deeper problem nobody had spotted:
**the 5-minute MADIS `T`-group is WHOLE DEGREES CELSIUS padded to look like
tenths** — 100.0% of 17,007 sub-hourly T-groups end in `.0`, vs 8-12% (chance)
for routine METARs. So a sub-hourly reading carries a **1.8 °F** band, not 0.18,
and it barely leads the hourly METAR at all. Measured, n=539 certainty events,
5 cities, 45 days: median usable lead after latency **0 min**, and at t1 the sure
side is priced at exactly 100¢ in **505 of 505** cases (0% tradeable) against 7%
at t2. Reading the T-group as exact also manufactures false certainty: 4 of 18
sub-hourly-only lock-ins were **wrong** vs settlement (22%) before the fix, 0 of
539 after. Reproduce: `scripts/t1_price_history.py --days 45`.
⚠ An intermediate pass on 2026-09-08 claimed IEM gave "full T-group precision"
and a ~15-min window — that was wrong, and is retracted in `FEED-LATENCY.md`.

**3-superseded. The claim below generalises from ONE endpoint, and is wrong.**
`asos1min.py` really is ~2 days stale. But **`asos.py`** carries the same 5-minute
MADIS reports *with their `RMK T`-groups* and runs **~18 min** behind (measured by
first-sighting, n=36). Net usable window is **~15 min**, not ~48 — the 48 never
subtracted the cost of getting the data — and the sub-hourly channel proves the
settled max on 56/60 station-days against the hourly METAR's 28/60, so it roughly
**doubles** the days a lock exists. Coverage is **5 of 7 cities**: KNYC is sparse
and **KDEN has no sub-hourly feed at all**. ⚠ A separate trap: weather.gov's live
copies are **truncated before `RMK`** (0/174 rows carry a T-group vs 287/288 in
IEM's archive of the same reports), so that feed quantises to 1.8 °F and proved
the settled max on **0/60** days — and any backtest on the archive overstates what
production sees. Depth at t1 is now sampled by `scripts/t1_depth_probe.py`; the
first sweep (n=12) found nothing to buy, with a same-instant control showing
undecided markets at median depth 878 and already-certain ones at 0.

**3-old (superseded). ⚠ AND WE CANNOT REACH IT — the IEM 1-minute archive is ~2 DAYS stale, not ~1 h.**
Measured 2026-08-25: same-day and previous-day requests return **0 observations**; the newest
data available was 53 h old. The "~1 h lag" recorded elsewhere in this file is wrong.
- **`WA_ENABLE_1MIN_LOCKIN` is therefore structurally incapable of ever helping.**
  `onemin_ingest` polls `mesonet.agron.iastate.edu/.../asos1min.py` (line 46) — the very feed
  that is two days behind. It is not "unmeasured"; with this source it **cannot work**. Do
  not spend time measuring it. Replace the source or drop the feature.
- Hourly METAR, by contrast, is effectively real-time (issued ~:53, readable immediately).

**WHAT THIS MEANS STRATEGICALLY.** Everything measured here is consistent and points one way:
- The plumbing is fine — an oracle forecast earns **+$38.21/trade** through this exact rule
  and fee schedule.
- The forecast edge is ~zero because we price public NWS/NBM/MOS data into a market that is
  pricing the same public NWS/NBM/MOS data. There is no informational advantage by
  construction, which is why Strategy 2 loses, why fading it also loses, and why model choice
  is the smallest lever.
- The one genuine inefficiency is the ~48-minute observation window — and it is invisible to
  everyone using public feeds, which is exactly why the market stays at 55c during it.
- So an edge here is a **data-acquisition problem, not a modelling problem.** The open
  question is whether a low-latency sub-hourly ASOS feed exists at acceptable cost
  (candidates to price out: NOAA MADIS, Synoptic Data / MesoWest — **neither verified**).

**REPLICATED 2026-08-25 (n=247, 7 stations, 20 days). The edge survives — with one hard
blocker and one unresolved question.** For every detected lock-in we found t1 (1-minute
record proves certainty) and t2 (hourly METAR proves it), then read the book at each:

      ask at t1 (1-min proof)   mean 95.0c   median 100c
      ask just before t2        mean 98.4c   median 100c
      ask after t2              mean 100.0c  median 100c
      lead window               mean 72m     median 58m
      wrong calls               0 / 247

- **Median is 100c: 56% of windows have NO edge at all.** The mean is carried by a minority.
  The `KXHIGHPHIL-26AUG03` case (55c through the window) is an **outlier, not typical** — it
  was n=1 and it misled.
- **But 68 of 247 (28%) are genuinely discounted (<98c), mean 17.4c gross,** worth
  **~$1,183 per 100 contracts over 20 days** (~$1,775/month), spread across all 7 cities.
- **They are NOT illiquid.** Discounted windows sit in the MOST traded markets: median
  lifetime volume **15,387** contracts and peak open interest **~11,000**, versus 3,269 /
  2,823 for the no-edge (100c) windows. Zero of 138 sampled markets failed to trade.

**⚠ THE BLOCKER IS UNCHANGED: this needs a real-time sub-hourly feed, and IEM is ~2 days
stale.** Nothing above is reachable today.

**⚠ DEPTH — NOW BEING LOGGED (2026-08-25), not yet answered.** The ask at t1 is
*top-of-book close*; high lifetime volume does not prove size rested at that price, and the
candlestick endpoint carries no depth at all. So depth is now captured **live**:
`OrderBook.get_l2_sweep_metrics(side, target_size=100)` walks the real ask ladder and returns
`top_levels`, `vwap_cents`, `book_cleared`, `fillable_size`, `depth_size`, `top_ask_cents`.
**⚠ CORRECTED 2026-08-26 — as first shipped, this probe could never have answered the
question.** It sat BELOW the price-ceiling gate (`best.price_cents >= ceiling -> return`),
but `backtest --strategy 1` measures **420 of 421 lock-ins already at exactly 100c** against
a 98c ceiling — so it would have logged roughly **one sample a month**. It is now **above**
the gate: every DETECTED lock-in logs depth, tradeable or not, with a `priced_out` flag
recording which. Distinguish the two no-order cases by `top_ask_cents`: `null` = no quotes at
all, non-null with `priced_out: true` = quoted but above the ceiling.
A companion `lock-in detected` line fires on the same transition, **before** any
tradeability/risk gating, giving the first live corroboration of the backtest's 421-detections
claim (previously the log recorded nothing until an order cleared, so the live detection rate
had never been observed at all).
Both latch **once per market per climate day** (`_lockin_first_sighting`): `_evaluate_lock_in`
runs every ~120 s and a market stays certain for the rest of the day, so unlatched telemetry
would emit ~30 lines/hour/market and trip logrotate on the very history it is accumulating.
The two channels latch **separately** — "detect" fires before gating, "depth" needs an order
book that may not arrive for several cycles, and a shared latch would burn the depth sample on
a cycle that never probed. Locked by `tests/test_v33.py`.
The push still appends `L2 Depth: ... | VWAP(100): ...c`.
Analyse with `grep '"message":"lock-in L2 depth"' logs/tracker.out.log`.

**⚠ THE SCOPE LIMIT THIS DOES NOT FIX, and it is the important one.** The probe fires at
**t2** — METAR publication, when the live engine first learns of certainty. The ~17.4c
discount in the $1,183 estimate lives at **t1**, a median 48 min earlier, and is
*structurally unobservable* without a real-time sub-hourly feed. So this instrument measures
depth at the moment the edge is already gone. It can tell you whether a *t2* discount is
fillable; it **cannot** validate the t1 number. Treat "depth is being resolved" as false until
a t1-capable feed exists.

**⚠ Discard the first batch.** The 13 samples logged at the 2026-08-26T03:28Z restart are
2026-08-25 markets seen at ~22:30 LST, hours after they locked in, with quotes pulled — all
13 have empty books. They are artifacts of the restart re-arming the latch, not t2
observations. Real samples start with lock-ins detected during active trading.

**⚠ AND THE ILLUSION IS IN THE ANALYSIS, NOT THE ENGINE.** Worth understanding before
re-reading the $1,183 figure. The live engine never took the headline ask: it sizes with
`buyable_no_size(ceiling)` / `buyable_yes_size(ceiling)`, which **sums every level up to the
98c ceiling**, and pays the marketable limit. A book of `14c x1, 18c x12, 98c x200` produces
an order for **213 contracts at a ~87.6c VWAP**, not 100 at 14c. But `backtest._entry_cents`
reads *top-of-book ask only*, which is exactly where the 17.4c mean discount came from.
**So the $1,183/100-contracts estimate is optimistic by an unknown amount, and the L2 log is
what will quantify it.** Do not act on that number until real VWAPs have accrued.

**⚠ MEASUREMENT HEALTH WARNING.** This particular analysis produced three wrong intermediate
answers before landing: an out-of-range candle query returning 0 candles (read as "never
traded"), and then **the wrong field names — Kalshi's candles carry `volume_fp` and
`open_interest_fp`, NOT `volume`/`open_interest`**, which read as zero volume everywhere and
nearly killed a real finding. Anything here should be independently re-derived before money
moves.

### ⚠ "JUST INVERT IT" MEASURED 2026-08-25 — the answer is NO. Do not fade the signal.
Strategy 2 loses money, so the obvious question is whether taking the OPPOSITE side of every
trade wins. It does not, and the reason is structural: **inverting does not flip the sign of
the P&L, because you still pay the fee and you still cross the spread — in the other
direction.** If the rule buys YES at `yes_ask`, the inverse buys NO at `no_ask`
(= `100 - yes_bid`), not at `100 - yes_ask`. That gap is the spread, paid again.

21 days, corrected (grid-fixed) probabilities, size 100, 7,957 matched trades:

      variant                    $/trade   hit%   avg entry   fee/trade
      AS BUILT (follow signal)    -3.22    28.4%    30.8c       0.88
      INVERTED (fade signal)      -0.27    71.6%    71.0c       0.85
      sum of the two              -3.49                 <- would be 0 if inverting were free

The sum is the round-trip cost and it reconciles exactly: spread 1.88c + both fees
(0.88 + 0.85) = ~3.6c/contract vs the 3.49 observed. **Both directions cannot be near zero;
one of them must lose at least the round trip.**

**The signal is not backwards, it is nearly worthless.** Decomposed per contract:
- as built: pays 30.8c for something that wins 28.4% → −2.4c gross, −0.88c fee = **−3.28**
- inverted: pays 71.0c for something that wins 71.6% → **+0.6c gross**, −0.85c fee = **−0.25**

So fading does have a real +0.6c gross edge — this is the classic favorite-longshot bias,
prediction markets overpricing longshots — but Kalshi's fee is 0.85c/contract at those
prices, so the bias is **smaller than the cost of harvesting it**.

**And the −0.27 is not even stable.** Chronological split (train <2026-08-17, test after),
pooled fade: **TRAIN +1.03/trade → TEST −1.52/trade**. It flips sign. Bucketed by entry
price, 6 of 7 buckets flip sign between windows too; only the 0-50c bucket is positive in
both (+0.19 / +1.05), which is what you expect from ~7 buckets by chance. There is no
tradeable residue here.

### ⚠⚠ FAVORITE-LONGSHOT, CHASED EXCHANGE-WIDE — MEASURED 2026-09-10. H1 IS DEAD EVERYWHERE EXCEPT WEATHER.
The section above ends on a loose thread: the fade has a real **+0.6c gross** favorite-longshot
edge, killed by an **0.85c** fee at ~71c. But Kalshi's fee is **quadratic** — `coeff x P x (1-P)`
— so it collapses to **0.14c at 98c**. If the bias survives up there, it clears the fee by 5x.
`scripts/favorite_bias.py` tests exactly that, first on the 7 weather series and then across
Kalshi's full `/series` list. **The answer is no, and the reason is not the one anyone expected.**

Sampling rule (this is the whole ballgame): price is read at a **FIXED horizon before close**
(24/12/6/3h), never at a price the market *reached*. Sampling "markets that traded at 96c"
conditions on the outcome — every eventual-YES passes through 96c, only some eventual-NOs do —
and reports a wildly inflated settle rate. `price_as_of` is strictly backward-looking
(`_candle_index_at` returns the first candle at/AFTER a ts, i.e. look-ahead; do not use it here).

**RESULT — exchange-wide: 117 series, 12 categories, 3,999 markets, 2,058 event clusters,
20,662 observations, closes 2026-07-02..2026-09-09.** Every price bucket is net-negative:

      bucket    fee   spread      NET c/ct    (event-weighted; NET = won - paid - fee)
       1-5     0.12      1.4       -0.91
      36-50    1.65      6.2      -10.05
      81-90    0.84      9.4       -7.64
      96-99    0.14      6.1       -2.67   <- H1's band. -2.67c [-3.95, -1.39]

- **H1 (favorites 96-99c profitable) fails in all 12 categories.** The three positive point
  estimates — Entertainment +1.56, Mentions +1.00, Crypto +1.24 — all die on the loss gate below.
- **H2 (longshots 1-5c lose) passes pooled (-0.91c) and in 7 of 12 categories.** The bias is
  real and everywhere. It is still not tradeable, for the reason the section above gives:
  fading it means buying the other side at *its* ask.

**⚠ WEATHER IS THE LONE SURVIVOR, AND THAT INVERTS THE PREMISE.** The 7-series weather run
(463 clusters, 120d) clears all four gates: **+0.64c, Holm p=0.039, both halves positive
(+0.22 then +1.05), 13 events saw a loss.** It is **MARGINAL — do not trade it**: the
two-sided CI is [-0.07, +1.36] and the Wilson interval on the win rate is [-0.81, +1.33].
Both include zero. Weather was picked as the *control* — nobody is emotionally attached to
Tuesday's high in Austin — and it won. **Not sentiment: SPREAD.** Weather's 96-99c band is
median 1c / p90 **2c**; exchange-wide the same band is median 1c / p90 **10c**. But tightness
is necessary, not sufficient:

      <= 1c spread, exchange-wide   520 ev   -0.70c  [-1.93, +0.53]
      <= 1c spread, weather only    445 ev   +0.94c  [+0.39, +1.50]

Same execution cost, opposite results. **Cheap execution lets a bias show; it does not make one.**

**⚠⚠ THE FALSE POSITIVE THAT EVERY OTHER GUARD WAVED THROUGH — remember this shape.**
Entertainment's 96-99c band scored **+1.56c, CI [+1.32, +1.80], Holm-adjusted p = 0.0000** —
apparently the cleanest result on the exchange. It had **ONE event in 36 where anything lost.**
At a 98c ask the payoff is +2c or -98c, so with no losses the sample variance is the variance
of the WINS and the interval describes nothing; one more loss takes it to **-1.14c**.
**Holm cannot catch this — the problem is not too many comparisons, it is that the statistic
is degenerate.** Every band now reports its observed-loss count, a one-more-loss sensitivity,
and a Wilson interval that stays sane at zero failures, and is disowned under 5 observed losses.

**Four API/measurement traps this had to handle (all real, all verified):**
- **`fee_multiplier` is NOT always 1.** 19 MLB series charge HALF fee, 14 series charge NONE.
  Every observation carries `0.07 * fee_multiplier` from `/series`. Assuming 0.07 mispriced
  MLB by ~0.9c/contract at 50c. `fee_type` variants (`quadratic_with_maker_fees`) tax MAKERS;
  we cross the spread, so the taker leg stays quadratic.
- **Ticker parsing breaks the clustering.** `event_of()` (`SERIES-DATE-STRIKE`) is weather-only:
  it merges an entire season into one cluster on `KXNBASERIES-26-BOSLAL-BOS`. Use the market's
  own **`event_ticker`**; `event_of` is a documented fallback. Clustering already flipped this
  study's conclusion once (naive CI [+0.23,+1.32] "significant" -> clustered [-0.04,+1.39] not).
- **Kalshi's `status=settled` listing retains only ~70 DAYS.** `--days 120` silently returns the
  same ~70. Enumerating 2,781 series with `min_close_ts` at 2026-05-10 returned 513,243 markets
  whose EARLIEST close is 2026-06-30, with a cliff there (42 markets on 06-30, 4,636 on 07-04).
  **Every window study in this repo is capped at ten weeks.**
- **The ask is not a fill.** 43.9% of samples (46.8% in the favorite band) come from a bar where
  NOTHING traded. Restricting to traded bars moves the favorite band -2.67c -> -0.73c. Every
  positive number here is an **upper bound**: top-of-book, one contract deep, before impact.

**Scale gotchas if you re-run it:** the public API rate-limits at **~3 req/s** (concurrency 32
got 69 of 80 requests 429'd; raising fan-out 8->24 moved throughput zero). Global market
enumeration is useless — **one day settles ~150,000 markets, ~99.9% auto-generated
`KXMVECROSSCATEGORY` combos**. Enumerate per series and cache it (`--cache`, ~75 min for 14k
series); `--from-json` re-analyses without refetching.

Reproduce:

    PYTHONPATH=. .venv/bin/python scripts/favorite_bias.py --days 120                 # 7-series weather
    PYTHONPATH=. .venv/bin/python scripts/favorite_bias.py --universe all --days 120 \
        --cache /tmp/enum.json --json /tmp/obs.json                                    # exchange-wide, ~2h

**WHERE THIS LEAVES THE IDEA.** The favorite side is not mispriced enough to take, at any fee
level, anywhere but weather — and weather's edge is inside its own error bars. The only lever
left is **not paying the ask at all**, which is a depth-and-resting-order question this script
structurally cannot answer (and `--passive` already answered one version of it: NO). Do not
spend more on calibration curves. Weather's +0.64c needs ~10 more weeks before it is anything,
and the window cannot be extended backwards — only forwards from here.

### ⚠⚠ THE WEATHER EXCEPTION DID NOT REPLICATE — RE-MEASURED 2026-10-28. H1 IS NOW DEAD EVERYWHERE.
The section above left exactly one thread open: weather's 96-99c band at **+0.64c**, clearing
all four gates but with both the two-sided CI and the Wilson interval including zero. The note
said it "needs ~10 more weeks before it is anything, and the window cannot be extended
backwards — only forwards from here." It got **7 more weeks. It weakened.**

Re-ran `scripts/favorite_bias.py --days 120` on 2026-10-28. Kalshi's ~70-day retention means
the window is now **2026-08-22..2026-10-28**, which OVERLAPS the original Jul2–Sep9 window by
about 18 days. **Quoting the full-window number would be partly re-measuring the same data**,
so the honest test is the disjoint slice — markets closing after 2026-09-09, i.e. 73% of the
new sample and all of it genuinely new:

      sample                              ev    favorites 96-99c        p
      full window (27% overlap)          464    +0.70c [+0.03,+1.36]   0.021  **
      NEW DATA ONLY (after Sep 9)        340    +0.48c [-0.39,+1.35]   0.142  not significant

**Out-of-sample it does not clear significance, and the point estimate is drifting toward
zero:** +0.68c (original Jul–Sep) → +0.64c (re-measured same era) → **+0.48c (new data)**.
One more losing event takes the new-data figure to +0.19c. The `<=1c` spread bucket — the one
cell that previously excluded zero at +0.94c [+0.39,+1.50] — is now **+0.65c [-0.15,+1.46]**
and no longer excludes it either.

⚠ The in-run STABILITY panel still reports "both halves +0.72/+0.67, as predicted", because
it splits the *full* window and both halves contain overlap. **The disjoint test above is the
stronger one and it disagrees.** Do not read that panel as a replication.

**H2 replicates perfectly and remains unharvestable:** longshots -1.14c on new data alone,
p=0.0000, 350 clusters. Longshots really are overpriced; fading them still means buying the
other side at its own ask.

**VERDICT: treat H1 as dead, in weather too.** Two windows, the second mostly independent,
both marginal, the estimate shrinking as data accrues — that is what a null looks like when
you keep sampling it. The most likely truth is ~0 to +0.5c, smaller than the measurement
error and far below what unknown depth would eat. **Do not spend more on this.** Reproduce:
`PYTHONPATH=. .venv/bin/python scripts/favorite_bias.py --days 120`, then slice on
`close_ts > <end of the prior window>` before believing any headline.

### ⚠ PASSIVE PLACEMENT MEASURED 2026-08-23 — the answer is NO. Leave the flag off.
`WA_ENABLE_PASSIVE_MODEL_EDGE` had shipped but never been measured. It is now measured
(`backtest --strategy 2 --passive --days 21`, 10,164 (ticker,model) pairs, 840 tickers) and
it **costs money**. Do not turn it on.
- **The prize is tiny.** Mean spread on entered markets **2.04¢, median 1¢**. Ceiling on
  spread capture is **$1.04/trade** (100 contracts) against a **−$4.06 to −$4.38/trade** loss,
  so even perfect capture recovers ~25% of it. For scale the mean fee is $1.14/trade: the
  spread and the fee are the same order, and *both together* are about half the loss. The
  other half is the strategy being wrong, which no execution change touches.
- **Same-side execution delta is negative in 22 of 24 configs on TRAIN and 24 of 24 on TEST**
  (3 fill models x {add, only} x improve 0..3). The two TRAIN exceptions are the
  rest-at-the-bid corner (`improve=0`, +$0.13 `trade/only` and +$0.04 `through/only`) and
  neither survives TEST (−$0.27, −$0.23). The live flag's own mode (`add`) is the **worst**: −$0.65 to
  −$1.37/trade, every window, every fill model, every improve setting.
- **Why**, and this is the reusable insight: the taker rule is *already* patient. It only
  fires when the ask has fallen inside the fee-adjusted limit. A resting bid fills earlier, at
  a price inside the *then*-spread but ABOVE where the ask later falls. Passive placement does
  not capture the spread — it forfeits the wait, and the wait is worth more (0.2–1.4¢) than
  the spread (1¢ median). `add` is worst precisely because it only rests where the ask was NOT
  yet inside the limit, i.e. exactly the markets where waiting paid most.
- **Fill model matters and is NOT a simple optimism ordering.** `through` ⊂ `trade` (both
  print-based); `touch` is quote-based and overlaps rather than nests — a print can happen at
  the bid without the offer ever reaching us. Fill rates: touch 59–64%, trade 63–67%,
  through 54–56% (passive-only). All three agree on the sign.
- **The trap this nearly fell into** (same shape as the `_id` raw-net-P&L trap): pooled
  per-trade P&L showed passive *winning* by up to +$0.70/trade. That was **1.6–3.6% of rows
  where the two runs took OPPOSITE sides** and passive's side won 96–99%. Those are not
  execution — the two runs entered at *different snapshots*, so they acted on different
  forecasts. Quarantining them (panel 3 = same-side only) flips the sign. Internal check that
  the panel is honest: `Δ$/trade ≈ −entryΔ × size/100` to the cent, exactly as it must be when
  the only difference is the price paid.

### ⚠ LOOK-AHEAD BIAS FIXED 2026-08-23 — old Strategy-2 numbers were ~$0.81/trade optimistic
`_candle_series` passed `period_interval=60` believing it was seconds. Kalshi's units are
**minutes**, so every backtest was reading **hourly** bars and `_price_at` returned that bar's
*close* — the quote up to 59 minutes AFTER the prediction. Entries were priced with information
the decision could not have had. Now fixed to true 1-minute bars. Measured cost of the fix on
the taker baseline (14 days, same data): **−$2.99 → −$3.80 per trade**. Every Strategy-2 P&L
figure recorded in this file before 2026-08-23 is optimistic by roughly that much; the
*rankings* between models are probably unaffected, the *levels* are not.

### Ranked next optimizations (do ONE per fresh session; independent unless noted)
1. ~~**Intraday model → live forecast**~~ **DONE 2026-08-16, live since 2026-08-17 04:35Z**
   (see "Intraday sharpening" above). Shipped shadow-only; tracker restarted and verified —
   all 7 curves build in <3.5 min, `_id` rows logging, 0 errors. Live behavior matches the
   design: on 2026-08-16 late-day markets it cut unreachable longshots hard (AUS ≥105 °F with
   a 102 °F max: 0.243 → 0.003) and moved that mass into the bucket holding the running max
   (AUS B102.5: 0.299 → 0.673). That is the diagnosed failure mode being corrected — but it is
   NOT yet evidence of better accuracy. The `_id` variants need a few days of history +
   settlements before `backtest --strategy 2 --days N` can rule. **The next session's job is to
   read that verdict**, then flip `WA_ENABLE_INTRADAY_FORECAST` if it holds up.
2. ~~**Fix `report.py`**~~ **ROOT CAUSE FOUND + FIXED 2026-08-21.** `market_results` was empty
   because `SettlementReconciler` only graded markets we had *filled*, and dry-run fills nothing
   (0 trades in 4 weeks). So every consumer that joins it was silently dead: the Brier
   leaderboard, `best_model_by_brier`, and therefore **`WA_TRADING_MODEL=auto` — which has been
   falling back to `current` (the WORST model, -$4.00/trade vs gfs_mos_id -$1.23) for the entire
   run.** The reconciler now also grades every SHADOW-PREDICTED market
   (`predicted_tickers_without_result`, batch-capped by `settlement_grade_batch`). Needs a
   tracker restart to take effect; verify `market_results` starts filling, then re-check
   report.py’s panels.
3. ~~**Verify the settlement authority**~~ **DONE 2026-08-21** — see "SETTLEMENT BASIS" above.
   TWC == our hourly-METAR max exactly (147/147); rules matcher + authority gate fixed.

**DIAGNOSED 2026-08-24, NOT YET FIXED — both block a safe go-live:**

G1. ~~**`WA_DRY_RUN=false` is GLOBAL**~~ **FIXED 2026-08-24 — `WA_ALLOW_LIVE_MODEL_EDGE`
   (default false).** Backlog F is unblocked: you can now run Strategy 1 live with Strategy 2
   still shadowed. Truth table (`tests/test_v31.py`):

         dry_run   allow_live_model_edge | lock_in   model_edge
         true      (either)              | allowed   allowed     <- unchanged today
         false     false                 | allowed   BLOCKED     <- the safe cutover
         false     true                  | allowed   allowed     <- deliberate opt-in

   - Enforced in **`RiskManager.size_order`**, not the engine: it is the single approval
     authority every order path already passes through, so no future caller (passive
     placement, a new strategy) can route around it. The rejection names the flag to flip.
   - **Dry-run behaviour is byte-for-byte unchanged**, so paper measurement of Strategy 2
     keeps running — the gate only bites when real money is at stake.
   - `main.py` logs a `LIVE TRADING ENABLED — real money at risk` **warning** at startup
     listing `strategies_cleared_live`, so the posture is unmistakable in the log.
   - Historical note on why this mattered: flipping `WA_DRY_RUN=false` to trade Strategy 1
     would otherwise also have put **Strategy 2** live at **−$3.26/trade**.
   - ⚠⚠ **THE OLD "MITIGATING FACTOR" HERE IS NOW FALSE — RE-MEASURED 2026-09-10.**
     This used to read "live Strategy 2 has never produced an order in 4+ weeks
     (`grep -c '"message":"model-edge signal"` → 0; `open_limit_orders` is empty)."
     **That is no longer true, and it was never a safety property.** Measured today on
     the running tracker (up since 2026-09-02):

           model-edge signals in tracker.out.log : 5,398
           open_limit_orders rows               : 4,853  (4,458 canceled, 395 resting)
           of those tagged lock_in              :     0

     The cause is documented in CORRECTION 4 at the top of this file: `_orderbook_from_kalshi`
     was reading a key Kalshi no longer serves, so the engine saw an **empty book for every
     market** for the whole early paper run. That is why the count was 0. Once the book
     became readable (fixed 2026-08-26) Strategy 2 started firing constantly.
     **So flipping `WA_DRY_RUN=false` today WOULD immediately place live Strategy-2 orders
     at a measured −$3.26/trade if the gate below were open.** The protection is
     `WA_ALLOW_LIVE_MODEL_EDGE` (default false) and nothing else — do not re-derive comfort
     from an order count.
   - Fix shape: a `WA_ENABLE_MODEL_EDGE_TRADING` (default off) checked in
     `_evaluate_model_edge` before `_submit`, so Strategy 2 stays shadow while dry-run is off.
   - ⚠ Related trap for any go-live risk config: **fractional Kelly is MODEL_EDGE-only.**
     `risk_manager.py:169` gates on `strategy is Strategy.MODEL_EDGE` **and**
     `win_prob is not None`; lock-in orders are `LOCK_IN` and carry no `model_prob`, so
     `WA_KELLY_FRACTION` is a **no-op for Strategy 1**. The binding cap for a lock-in is
     `WA_MAX_FRACTION_PER_MARKET` (0.05). `WA_MAX_FRACTION_PER_GROUP` caps *exposure*, not
     loss, and CLAUDE.md's own recommendation is ~0.10, not 0.20.

G2. ~~**`backtest --strategy 1` is wrong in two ways**~~ **FIXED 2026-08-24 — and the
   corrected result is the most important number in this project. READ THIS BEFORE FUNDING.**

   **Strategy 1 is genuinely riskless and genuinely unprofitable: the market prices the
   certainty before we can act on it.** 30 days, all 7 cities, 100-contract size:

         lock-ins DETECTED (certainty proved)  : 421
           already priced at 100c (no edge)    : 420   (99.8%)
           tradeable below the 98c ceiling     :   1
         net on the one tradeable opportunity  : +$3.73
         correct / wrong calls                 : 421 / 0

   - **Every one of the 420 is at exactly 100c ask.** Not 98, not 99 — raising
     `lock_in_price_ceiling_cents` captures nothing. Verified against the real book on
     `KXHIGHPHIL-26AUG03-T87`: certainty proved at 20:54 UTC (running max 89 > floor 87),
     and the book 6 minutes later was **99c bid / 100c ask** — a live, tight, fully-priced
     market, not an empty one.
   - **The safety claim is now verified at scale: 421 detections, 0 wrong calls.** So
     "near-riskless arbitrage" is true about the RISK and false about the RETURN.
   - Extrapolated: ~$45/year at 100-contract size on a $10k bankroll (~0.45%/yr) before
     considering the capital parked. **This does not justify funding an account on Strategy
     1's merits alone.**
   - Asymmetry worth knowing: certain-**NO** can occasionally be bought cheap (the one
     tradeable case was `buy NO @ 96c`, i.e. yes_bid had fallen to 4). Certain-**YES** is
     essentially never available — it is offered at 100c the moment it is provable.
   - A second, structural limit on the YES side: a tail-high lock-in needs the **hourly
     METAR** max to strictly exceed the floor, but settlement is the CLI, which runs ~+0.83F
     higher. So on days like `KXHIGHPHIL-26JUL28-T84` (METAR max exactly 84.00, CLI 85,
     settled YES) the outcome is never *provable* from what we observe. Correct and
     conservative — but it removes opportunities.

   ✅ **CONFIRMED LIVE 2026-10-20 — the first Strategy-1 order in the project's history, and
   the prediction was almost exact.** After 273 detections and zero orders, one cleared the
   gate: `KXHIGHDEN-26OCT19-B69.5`, the [69,70] bucket, observed max **71.06F > cap 70** ->
   certain NO, bought NO x189 crossing the 97c ask. **Kalshi settled it `no`**
   (`expiration_value` 71.00), so the call was correct — 274 detections, 0 wrong.

         contracts 189   cost 18,508c (VWAP 97.93c)   payout 18,900c
         gross +392c     fee -28c                     NET +364c = +$3.64

   against the backtest's predicted **+$3.73** for the one tradeable lock-in in 30 days. The
   whole chain — detection, certainty rule, L2 depth probe, risk sizing, ntfy push — worked
   end to end on a real event at the predicted magnitude. **It also confirms the verdict
   rather than overturning it: six weeks of running, one opportunity, $3.64.**
   - The depth probe earned its keep and the "ask is not a fill" warning is now MEASURED, not
     argued: only **14 contracts** rested at the 97c top-of-book (`97c x14, 98c x175,
     99c x1486`), so the real VWAP was **97.93c, not 97c**. A backtest reading top-of-book
     only would have booked 189 @ 97c.
   - Asymmetry holds as described: this was certain-**NO** bought cheap. Certain-YES is still
     never available.

   **The two bugs that were fixed** (kept because the old numbers appear in earlier notes):
   - `_crossing` ignored `strike_type`, firing certain-YES at `running >= floor` when a
     `greater` tail settles YES only at `T >= F+1`. It now delegates to
     `WeatherMarket.deterministic_lock_in` — **the live rule itself** — so it can never drift
     from production again. The live engine was always correct; this was a measurement bug.
   - `net_usd` assumed settlement at 100c unconditionally; `matched_result` fed only the
     mismatch counter and never P&L, so wrong calls were booked as full wins.
   - Old (wrong) headline: 8 opportunities, **+$374.07**. Corrected: 1 opportunity, **+$3.73**.

(historical) the original diagnosis:
G2-old. **`backtest --strategy 1` is wrong in two independent ways — do not trust its P&L.**
   The 30-day run reports 8 opportunities and **+$374.07 net**. Both numbers are wrong.
   - **`_crossing` (`backtest.py:142`) ignores `strike_type`.** It fires certain-YES at
     `running >= floor`, but a `greater` tail settles YES only at `T >= floor+1` (the strict
     tails verified 420/420 in `settlement_basis`). Confirmed on `KXHIGHMIA-26JUL26-T94`:
     `strike_type=greater, floor=94`, CLI settled **94**, so the market settled **NO** (and
     `B93.5`={93,94} settled yes) — but the backtest called it a certain YES at 4¢.
     This produced **4 of the 8 "opportunities"**, all bogus.
     ✅ **The LIVE engine is correct** — `models.deterministic_lock_in` uses `>` for
     `STRIKE_GREATER`. This is a measurement bug, not a trading bug.
   - **`net_usd` never applies the official result.** `gross = (100 - entry) * size`
     unconditionally assumes settlement at 100¢; `matched_result` is recorded only to feed
     the mismatch counter and never touches P&L. So the 4 losing trades are booked as wins.
   - Correcting both, the live-relevant figure is roughly **+$99 across 4 real opportunities
     in 30 days**, and even that is optimistic: the live engine fires at `floor+1`, i.e.
     *later* than the backtest's `floor`, so real entries would be pricier.
   - Separately this sets the honest expectation for the incubation period: **~1 lock-in per
     week across all 7 cities**, and it has fired live **zero** times in 4+ weeks.

**NEW, and this outranks everything else on the list:**

0. ~~**Fix the half-degree bucket-grid misalignment**~~ **DONE 2026-08-23** — see "BUCKET GRID"
   above. Probabilities now sum to 1 exactly. Needs the tracker restart to reach live pricing,
   resets the `_id` Brier experiment, and is not yet measurable via `backtest --strategy 2`
   (stored probs are pre-fix).

0b. ~~**Strategy-2 entry timing**~~ **BUILT + MEASURED 2026-08-23. Verdict: the time-decay
   threshold is HARMFUL — leave it off. Persistence is mildly positive but unproven.**
   Both shipped opt-in, and deliberately behind **two separate flags**, because the halves
   point in opposite directions and one flag would force you to take the bad half to get the
   good one:
   - `WA_ENABLE_DYNAMIC_EDGE` — time-decay threshold. **Measured harmful. Keep off.**
   - `WA_ENABLE_EDGE_PERSISTENCE` — persistence gate. Mildly positive, unproven. Off.

   Knobs: `WA_DYNAMIC_EDGE_{MAX,MIN}_THRESHOLD` (0.25/0.05), `WA_DYNAMIC_EDGE_{START,END}_HOUR`
   (8/16 LST), `WA_EDGE_PERSIST_SECONDS` (900). Reproduce with
   `backtest --strategy 2 --sweep-dynamic --days 21 --grid-fixed`.

   **RESULT (21d, corrected probs, pooled, nothing fitted). Baseline = live flat 0.15:
   TRAIN −4.45/tr, TEST −1.80/tr. All 25 decay configs LOSE to it on TEST:**

         max\min   0.02    0.04    0.06    0.08    0.10      (TEST $/trade)
         0.15     -4.50   -4.33   -3.90   -3.29   -3.02
         0.25     -4.42   -3.82   -3.40   -2.67   -2.13
         0.35     -3.97   -3.58   -3.08   -2.27   -1.84   <- best, still < -1.80

   The gradient runs the WRONG WAY for the premise: **higher `min_threshold` is monotonically
   better** in both windows. Loosening late in the day costs money; the "late forecasts are
   converged so a small edge is safe" story is not what the data does. Consistent with the
   old (void) 2026-08-21 finding that higher thresholds win — that part survived the grid fix.

   **⚠ THE CONCLUSION FLIPS ON PRE-FIX PROBABILITIES — this vindicates doing the grid fix
   first.** Run WITHOUT `--grid-fixed` and the decay looks like a *winner*: baseline TEST
   −4.30, and (0.35, 0.06) scores −3.15, with the min-threshold gradient REVERSED. Tuning
   entry timing before fixing the bucket grid would have shipped exactly the wrong rule.

   **Persistence** (swept alone at the flat threshold, pooled):

         persist_s      0     900    1800    3600    7200   14400
         TRAIN $/tr  -4.45   -4.35  -4.35   -4.46   -3.82   -3.33
         TEST  $/tr  -1.80   -1.19  -1.19   -1.11   -1.31   -1.08

   Better than no gate at every window on TEST, and at the long end on TRAIN — but the TRAIN
   gradient is **not monotone** (-4.46 at 3600 is worse than -4.35 at 900), so the window is
   not resolved. Promising, not established.
   - **900 s and 1800 s are IDENTICAL** because `model_prediction_history` snapshots are ~30
     min apart (78.9% of gaps exactly 30 min): both mean "present at two consecutive
     snapshots". The backtest cannot resolve a gate finer than ~30 min.
   - Backtested persistence is therefore the **OPTIMISTIC bound** on the live gate: live the
     engine re-evaluates every 120 s and resets the timer on a mid-window flicker the
     backtest cannot see.
   - Everything is still net-negative. This changes the size of the loss, not its sign.

   Implementation notes worth keeping:
   - `get_dynamic_edge_threshold()` and `gross_edges()` are **module-level and pure** in
     `arb_engine`, for the same reason `edge_limit_prices` is: the sweep prices against the
     live rule instead of a re-derivation that would drift.
   - The threshold uses each **station's own LST offset** — the seven cities span UTC-5..-8,
     so one global clock would apply LA's morning threshold to NY's afternoon
     (`timeutils.local_hour_lst`, fractional so the decay is smooth, no DST).
   - Persistence gates **both** entry actions. A SELL here is buying NO, i.e. equally a new
     position; gating only `Action.BUY` would let NO entries in instantly.
   - A **side flip restarts the clock** — half a window of "buy YES" then half of "sell YES"
     is a model that cannot make up its mind, not a persistent signal. So does the edge
     vanishing, so a flicker never accumulates.
   - State is `{ticker: (first_seen_utc, action, climate_date)}`, evicted on climate-date
     rollover (a ticker belongs to exactly one day, so old entries are pure garbage).
   - `gross_edge` is explicit now: `yes_prob - ask/100` on the buy leg,
     `no_prob - no_ask/100` on the sell leg. Note `edge_limit_prices` never compared an edge —
     it folds the threshold into a fee-adjusted limit price, so `ask <= buy_limit` **is**
     `gross_edge >= threshold + fee` up to integer-cent rounding.

A. ~~**Fix the `metar_ingest` spike guard**~~ **DONE 2026-08-23 — replaced with a kinematic
   (rate) guard; measured error now 0.00 °F at every station.** See the verdict section below.
B. ~~**Re-derive `intraday_sigma_floor_f`**~~ **DONE 2026-08-23 — the floor is correct at 1.0;
   its rationale was never void.** The premise ("the basis gap is 0.00 °F") came from a
   circular measurement. Real answer: Kalshi settles on the NWS CLI (245/245), the residual vs
   our hourly METAR is mean +0.81 / RMS 1.08 / never negative, and the late-day model is a
   half-normal whose MLE scale is exactly that RMS. Kept at 1.0 (indistinguishable from 1.08
   out-of-sample). New command `settlement_basis` reproduces the whole derivation. TWC
   preliminary→final revisions turned out to be the wrong thing to chase — the CLI residual
   subsumes them. See "SETTLEMENT BASIS" and the sigma-floor bullet.

**Then the pre-existing list:**

C. **Fix `report.py`** — its calibration/Brier panels join `market_results`, which only started
   filling on 2026-08-21 (see #2). Re-check whether the panels now populate before rewriting.
D. **Rolling re-fit of calibration** — wire `backtest --strategy 2 --calibrate --persist` into the weekly cron
   so live curves (`model_calibration` table, `WA_ENABLE_PROBABILITY_CALIBRATION`) stay fresh.
E. **Threshold × price-floor sweep** — 15% `model_edge_threshold` is too permissive; re-run at `--days 21+`
   (the 2026-08-21 attempt was inconclusive: TRAIN never profitable).
F. **Consider running Strategy 1 (lock-in) live but keeping Strategy 2 shadow** until it's
   actually positive. ✅ **Unblocked**: (A) is fixed (kinematic guard) and lock-in was verified
   safe against the corrected settlement basis on 2026-08-23 — 274 certain-NO fires replayed
   against real settlements, **0 wrong**, minimum margin 1.0 °F. Both branches only need
   `settlement >= observed`, which `R >= 0` guarantees. ⚠ Still requires the tracker restart
   for the guard fix to actually reach `daily_tmax`.
G. ~~**Passive model-edge placement**~~ **DONE 2026-08-23 — measured, answer is NO** (see the
   verdict section above). `WA_ENABLE_PASSIVE_MODEL_EDGE` stays off.
H. **NEW, surfaced by (G): entry TIMING may be a real lever, unlike execution.** The
   opposite-side pairs in the passive run were markets where the taker entered early on a
   stale forecast and the passive path happened to enter late, after the forecast had
   converged to near-certainty (prob ~1.00, entry ~69¢) — and won 96–99%. That is
   `first_qualifying_edge` (enter at the FIRST qualifying snapshot) being the wrong rule, not
   passive placement being right. n is only 57–88, far too thin to act on, and it is
   confounded with the intraday feature. Testable cheaply: add an entry-delay / minimum-lead
   variant to `_simulate` and sweep it under the same train/test discipline. Note this is
   partly the same phenomenon as Strategy 1 — a converged forecast late in the climate day
   *is* a lock-in — so check the overlap before building anything.
I. **Consider whether `enable_passive_model_edge` should rest a 1¢ bid on everything.**
   `edge_limit_prices` clamps to [1,99], so `buy_limit >= 1` and `sell_limit <= 99` are
   *always* true. With the flag on, the passive branch therefore produces an order on
   essentially every market evaluated (5,460 vs 2,906 pairs ever rested, per the fill-rate
   panel). Harmless in dry-run, but live that is a lot of resting orders and a rate-limit
   question. Only relevant if the flag is ever reconsidered.

J. ~~**Harvest the favorite-longshot bias where the fee is small**~~ **DONE 2026-09-10 —
   measured exchange-wide, answer is NO.** See "FAVORITE-LONGSHOT, CHASED EXCHANGE-WIDE"
   above. H1 fails in all 12 categories; the one survivor (weather, +0.64c) is inside its
   own error bars. Do not spend more on calibration curves — the remaining lever is depth
   and resting orders, not pricing. ⚠ Two reusable facts from it that bite ANY window study
   in this repo: **Kalshi's settled-market listing retains only ~70 days** regardless of what
   you pass, and the public API rate-limits at **~3 req/s**.
   ✅ **FULLY CLOSED 2026-10-28** — the one open thread (weather's +0.64c) was re-tested after
   7 more weeks and **did not replicate out of sample** (+0.48c, p=0.14). See "THE WEATHER
   EXCEPTION DID NOT REPLICATE" above. H1 is dead everywhere; nothing here is worth revisiting.

### Earlier backlog (all DONE)
All previously-listed backlog items are now DONE: intraday model (`intraday.py`), Strategy-2
backtest (`backtest --strategy 2`), passive limit placement (opt-in `WA_ENABLE_PASSIVE_MODEL_EDGE`
— shipped, and as of 2026-08-23 MEASURED and rejected; see the verdict section above),
fractional-Kelly + correlated-group sizing (opt-in `WA_KELLY_FRACTION` / `WA_MAX_FRACTION_PER_GROUP`),
and log rotation (`logrotate.py` + daily plist). Remaining is judgment/decision work, not code:
going live (add Kalshi keys, flip `WA_DRY_RUN`, tune the opt-in knobs) and the ForecastEx items
(IBKR OAuth + verify `≥strike`/bucket edges). Wire the opt-in features on deliberately, one at a time.
**1-minute lock-in DONE** (`onemin_ingest.py`, opt-in `WA_ENABLE_1MIN_LOCKIN`, off by default):
feeds corroborated 1-minute ASOS spike highs into the same `update_tmax`/`on_new_high` path as the
METAR poller (only ever raises T_max toward the NWS-CLI basis — ⚠ which as of 2026-08-14 is
the WRONG direction: Kalshi settles on TWC == the hourly-METAR max, so this flag now biases
T_max +0.80 °F away from settlement. **Keep it off.**). Glitch guards: ≥`onemin_min_confirm`
minutes corroboration + reject candidates >`spike_threshold_f` above T_max. IEM 1-min lags ~1h
(accuracy not latency). Wired in `main._supervise` as task `onemin_augmenter` when the flag is on.
**Cross-venue (started, `cross_venue.py`):** static registry + classifier. A 2026-07-22
live-rules pass confirmed Polymarket stations (Gamma API: NYC=KLGA, CHI=KORD, LAX=KLAX; others
main-airport) and ForecastEx NYC=Central Park + CHI=Midway (contract terms) — so **Chicago is
basis, not arb** (FE Midway vs PM O'Hare), and the lockable set is **MIA/AUS/DEN/LAX/PHIL** only.
Quote clients DONE (`cross_venue_quotes.py` + `ibkr_client.py`): live Polymarket via Gamma API;
ForecastEx via authenticated IBKR Client Portal Web API (CPAPI) — discovery (`secdef/search` →
`strikes` → `info`, or explicit conid map), snapshot (fields 84 bid/86 ask), and a threshold→2°F-bin
synthesizer since ForecastEx daily-high are OPT *strike=threshold* contracts (bin[lo,hi]=(≥lo)−(≥hi+1),
crossing the spread twice). Monitor CLI + bucket-aligned lock scan. All read-only.
Remaining to trade it for real: (1) VERIFY the ≥strike semantics + bucket edges + the 5 ForecastEx
main-airport stations against live contract terms (still UNVERIFIED); (2) supply the gateway +
`WA_FORECASTEX_CONTRACTS_JSON` (or `_SYMBOLS_JSON`) and validate auto-discovery on a first live run;
(3) DONE — NWS-CLI-vs-WU divergence detector (`divergence.py`); natural follow-on is feeding the
1-minute running max into the Strategy-1 lock-in engine (metar_ingest uses hourly METARs today) so
Kalshi locks fire earlier than the hourly-watching crowd — the detector is that feature's intel layer.
Facts: Polymarket unauth via `gamma-api.polymarket.com/events?slug=<event-slug>` (markets[] carry
`groupItemTitle` + YES `bestBid`/`bestAsk`); Polymarket does NOT list Philadelphia (2026-07-22).
ForecastEx on IBKR = secType OPT, exchange FORECASTX, expiry MMMYY, strikes = temps; snapshot first
call only primes the stream. IBKR/ForecastEx web pages block WebFetch (403).
