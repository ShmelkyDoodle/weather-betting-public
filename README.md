> **This is a public, read-only snapshot** of a private development repository,
> mirrored on 2026-11-08 for external review (including by other LLMs/agents
> that cannot access the private original). It is **not kept in sync** — later
> commits to the private repo will not appear here unless re-mirrored.
>
> Excluded from this mirror (never tracked by git in the first place):
> the runtime SQLite ledger, logs, `.env` credentials, and any API keys. The
> project has never had live Kalshi credentials installed; it has run
> continuously in paper/dry-run mode.
>
> Two ntfy.sh topic names have been replaced with placeholders: an illustrative
> one in `.env.example` and the deploy plist comment, and — in `CLAUDE.md` and
> `HANDOFF.md` — the project's actual configured alert topic, which those
> documents named in prose. The latter now reads `weatherarb-<redacted>`. The
> topic it replaced should be treated as compromised and rotated; an ntfy topic
> is a shared secret, so anyone holding the string can both read the alert
> stream and publish to it.
>
> Start with `CLAUDE.md` for the project's own "read this first" handoff
> summary (safety posture, corrections to earlier claims, current status),
> then this README for architecture. `AUDIT-2026-10-30.md` holds the most
> recent independent audit (settlement-basis re-verification at scale).

---

# weather-arb

A highly concurrent, `asyncio`-based engine that executes **low-risk weather
derivative arbitrage** on prediction markets (Kalshi, extensible to Polymarket)
using official meteorological observations (METAR/ASOS) and short-range
forecasts (NBM/HRRR via the NWS gridpoint product).

The system is **limit-order only** by construction, converts every internal
timestamp to UTC, buckets observations into NWS **Local-Standard-Time climate
days** (no DST bugs), and defaults to a safe **dry-run** mode until credentials
are supplied.

> ⚠️ Trading prediction markets carries financial risk and is subject to your
> local laws and each venue's terms of service. This is engineering
> infrastructure for authorized, self-directed trading — validate thoroughly in
> dry-run and the exchange's demo environment before risking capital.

## Architecture

```
                        ┌──────────────────────────┐
 NOAA AWC / IEM  ─────► │ metar_ingest.MetarPoller │──┐  validated new T_max
 (METAR/ASOS)           └──────────────────────────┘  │  (event callback)
                                                       ▼
 NWS gridpoint   ─────► ┌──────────────────────────┐  ┌────────────────────────┐
 (NBM/HRRR)             │ forecast_ingest.Forecast │─►│  arb_engine.Arbitrage  │
                        │ Client (P[Tmax>=strike]) │  │  Engine                │
                        └──────────────────────────┘  │  • Strategy 1 lock-in  │
                                                       │  • Strategy 2 edge     │
 Kalshi REST + WS ────► ┌──────────────────────────┐  └───────────┬────────────┘
 (order books)          │ market_client.Kalshi     │◄─────────────┤ limit orders
                        │ Client (RSA-signed)      │              ▼
                        └──────────────────────────┘  ┌────────────────────────┐
                                                       │ risk_manager.RiskMgr   │
 SQLite (WAL)   ◄─────► db.Database  ◄─────────────────┤ (caps, limit-only)     │
 ledger + Tmax                                         └────────────────────────┘
                        main.Application: reconcile → supervise → graceful shutdown
```

| Module | Responsibility |
| --- | --- |
| `config.py` | Typed settings + environment loading. |
| `timeutils.py` | UTC helpers and LST climate-day resolution. |
| `models.py` | Immutable domain models, enums, order-book math. |
| `logging_setup.py` | Structured JSON logging. |
| `db.py` | `aiosqlite` WAL ledger, atomic `T_max` upserts, reconciliation queries. |
| `metar_ingest.py` | METAR poller, sensor-glitch protection, running `T_max`. |
| `forecast_ingest.py` | NWS/NBM gridpoint fetch, truncated-normal exceedance probability. |
| `market_client.py` | Kalshi REST + WebSocket, RSA-PSS signing, rate limiting. |
| `risk_manager.py` | Per-market bankroll caps, limit-only enforcement, sizing. |
| `arb_engine.py` | Strategy 1 (deterministic lock-in) + Strategy 2 (model edge). |
| `main.py` | Orchestration, crash-recovery reconciliation, signal handling. |

## Strategies

**Strategy 1 — Intraday `T_max` Lock-In (deterministic).**
The validated daily maximum is monotonic. Once it meets/exceeds a contract's
strike, that contract *will* settle YES. If YES is still buyable below the
ceiling (default 98¢), the engine sweeps the cheap offers with an aggressive
**marketable limit** capped at the ceiling — never a market order, never above
the cap. A deterministic `client_order_id` (`s1-<ticker>-<climate_date>`) makes
re-triggering idempotent.

**Strategy 2 — Model / Implied-Probability Discrepancy.**
The NBM/HRRR gridpoint forecast yields the day's forecast high `mu`; forecast
error is modeled as Gaussian with lead-time-scaled `sigma`, **truncated below at
the temperature already observed** (the final max can't be lower than what's
happened). When `P(Tmax>=strike)` diverges from the market's implied probability
by more than the EV edge (default 15 pts), a bounded limit order is posted on the
favorable side at a price that preserves the edge. Stale model-edge orders are
cancelled/replaced as the estimate moves.

## Sensor-glitch protection

A new daily high is only *validated* (and only then moves `T_max` / fires
lock-in) when at least one holds:
1. the jump above the prior max is small (`≤ spike_threshold_f`);
2. it is **confirmed by a later reading within 5 minutes**; or
3. it is **corroborated by a nearby station** (same metro, ≤ 60 km).
Physically impossible readings (dew point ≫ air temp, out-of-envelope) are
rejected and logged with `accepted = 0`.

## Install & run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # edit as needed
set -a; source .env; set +a

python -m weather_arb.main
```

Without `KALSHI_API_KEY_ID` / `KALSHI_PRIVATE_KEY_PATH` the process runs in
**dry-run**: real weather + public market data flow through the full pipeline,
orders are risk-checked and journaled to SQLite, but nothing is transmitted.
Set credentials and `WA_DRY_RUN=false` to trade live (start on Kalshi's demo
endpoints).

Type-check:

```bash
mypy weather_arb
```

## Validate before going live: 30-day paper-trading

The safest way to trust the strategies is to run them in **dry-run** for a month
against real data and measure the result. Everything needed is built in.

**1. Market discovery is automatic.** With `enable_discovery` on (default), the
system scans the configured Kalshi high-temp series each cycle, parses each
range bucket's `floor_strike`/`cap_strike`/`strike_type`, tags it with its
settlement date, and feeds the engine — no daily ticker pasting.

The default series→station mappings were **verified from each series'
`rules_primary` text via Kalshi's live API** (the actual contract language):

| Series | Settles on | ICAO |
|---|---|---|
| `KXHIGHNY` | Central Park, NY | `KNYC` |
| `KXHIGHCHI` | Chicago Midway | `KMDW` |
| `KXHIGHMIA` | Miami International | `KMIA` |
| `KXHIGHAUS` | Austin-Bergstrom | `KAUS` |
| `KXHIGHDEN` | Denver | `KDEN` |
| `KXHIGHLAX` | Los Angeles (LAX) | `KLAX` |
| `KXHIGHPHIL` | Philadelphia International | `KPHL` |

> Chicago settles on **Midway**, not the NWS-official O'Hare — an easy, costly
> wrong assumption. Kalshi settlement uses **Local Standard Time** days (not
> DST), which is exactly why the climate-day logic uses a fixed standard offset.
> Override with `WA_WEATHER_SERIES_JSON`; only series whose station is also in
> `stations` are traded.

**Mappings are self-verified so they can't silently drift.** Kalshi could change
a reference station, so two layers guard against it:

- **The running app re-checks every series once per UTC day** against its live
  `rules_primary` text and **auto-disables** (never trades) any series whose
  station no longer matches — logging a loud `SETTLEMENT MAPPING MISMATCH` — and
  re-enables it automatically once it verifies clean again.
- **A standalone command** for on-demand or scheduled checks:
  ```bash
  python -m weather_arb.verify_mappings          # prints OK/FAIL per series; exits non-zero on any doubt
  ```
  A daily `launchd` job ([deploy/com.weatherarb.verify.plist](deploy/com.weatherarb.verify.plist))
  runs it each morning and appends results to `logs/verify.log`.

**2. Buckets are handled correctly.** Kalshi high-temp events are mutually
exclusive range buckets, so lock-in works both ways: once `T_max ≥ floor` of a
tail-high bucket it's a certain **YES** (buy YES); once `T_max` climbs above any
bucket's `cap` that bucket is a certain **NO** (buy NO). Strategy 2 computes the
proper *bucket* probability (`P(floor ≤ T_max ≤ cap)`), truncated at the temp
already observed.

**3. Settlement closes the loop.** A background reconciler polls settled markets
you've (paper) traded, records the YES/NO result, and logs realized P&L per
market. Trades stay immutable; P&L is a `trades × market_results` join.

**4. Read the track record any time** (WAL allows concurrent reads):

```bash
python -m weather_arb.report            # human-readable P&L, hit rate, per-day
python -m weather_arb.report --json     # machine-readable
python -m weather_arb.report --html && open report.html   # visual dashboard
```

The `--html` view is a self-contained page (no external assets): a traffic-light
🟢/🟡/🔴 verdict computed from your numbers, stat tiles, a cumulative-P&L chart, a
per-day bar chart, a Strategy-2 **calibration diagram** (predicted vs. realized
win rate), a plain-English reading of your results, and the 4-week operating
plan. It works from an empty ledger (shows "too early to judge") onward.

### What to bet, and when

`signals` prints, in plain English, the bets the engine would place *right now* —
so you can place them by hand on Kalshi instead of (or before) letting it
auto-trade. It runs the identical decision logic, reads the live ledger, and
**places nothing**:

```bash
python -m weather_arb.signals          # grouped: "locked in — place now" vs "forecast edge — no rush"
python -m weather_arb.signals --json
```

Each line names the market, the side, the limit price ("or better"), why, and
the timing. **Lock-in** bets say "place now" (the temperature already settled the
outcome); **forecast-edge** bets say "no rush" (a resting limit; the edge is the
forecast, not speed). A quiet result (no bets) is normal and correct when nothing
has crossed a line and prices are fair.

The report is **net of estimated Kalshi trading fees** (`fee = coeff·C·P·(1−P)`,
default coeff 0.07) — the same fee model tightens the Strategy-2 edge test so a
nominal edge that fees would erase is never taken, and paper P&L reflects what
live trading would actually net. It also prints a **Strategy-2 calibration**
table with a **Brier score**: every model-edge order records the modeled
probability that the side it took wins, and the report buckets those against
realized outcomes. If your "70%" trades win ≈70% of the time (and the Brier
score sits well below 0.25), the forecast model is trustworthy; if not, it
isn't — that's the number that tells you whether Strategy 2 is real edge or
noise. A **heartbeat** log line (every `WA_HEARTBEAT_SECONDS`) shows markets
tracked, working orders, and running net P&L while it runs.

**5. Keep it alive for the month.** A `launchd` job restarts it across crashes
and reboots:

```bash
mkdir -p logs
cp deploy/com.weatherarb.tracker.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.weatherarb.tracker.plist
tail -f logs/tracker.out.log            # watch it
launchctl unload ~/Library/LaunchAgents/com.weatherarb.tracker.plist   # stop
```

After ~30 days, `report` gives you realized P&L and hit rate per strategy. If
Strategy 1 (deterministic) shows steady small gains and Strategy 2's hit rate
tracks its modeled probabilities, the edge is real — then consider going live on
Kalshi's demo endpoints first.

### Retrospective backtest (Strategy 1)

`backtest` replays past **settled** markets to measure the deterministic lock-in
edge without waiting weeks. For each market it reconstructs the day's running max
from the IEM ASOS archive, finds when the max made the contract a certain YES/NO,
reads the Kalshi candlestick price at that moment, and settles at 100¢:

```bash
python -m weather_arb.backtest --days 30 --size 100
```

It reports opportunities found, average entry price, edge per contract, net P&L,
and **settlement mismatches** (deterministic call vs. official result — should be
~0, which validates the reconstruction). Caveats: candlestick OHLC approximates
fills (no book depth), and only Strategy 1 is covered (a Strategy 2 backtest needs
archived forecasts). Lock-in edges are *rare but real* — most crossings get priced
to ~99¢ instantly; the backtest surfaces the ones that lagged.

## Choosing the forecast model (measured, not guessed)

Several forecast models predict every market side-by-side, logged each day to the
ledger: `current` (NWS gridpoint), `nbm` and `gfs_mos` (station-specific MOS from
IEM), `ensemble` (their mean, sigma widened by disagreement — a *real* spread),
`climo` (a **climatological base rate** — the windowed multi-year distribution of
the daily high for this station and calendar date, from the IEM archive), and
`blend` (forecast × climatology). Bias-corrected `*_bc` and intraday-sharpened
`*_id` variants ride along too (plus `intraday`, the remaining-rise estimate
standing alone). Trading runs on the selected model (`current` until the
leaderboard is meaningful); the rest are **shadow predictions**. Once markets
settle, the report ranks them by **Brier score** so the run tells you which model
actually predicts best:

```
forecast model comparison (settled markets — lower Brier is better):
  model          n     Brier   dir.acc
  ensemble      35     0.165      77%
  nbm           35     0.171      74%
  current       35     0.183      71%
  -> best so far: ensemble (Brier 0.165)
```

Switch the trading model to the winner later by pointing Strategy 2 at it — the
`ForecastClient` already computes all of them.

### Intraday sharpening (`*_id`)

The static forecast scales sigma by **lead time only** (~4.5 °F live), which is a
poor description of uncertainty *within* the target day: at 4pm the day's max is
all but decided, yet a lead-time sigma still smears probability across buckets
that will never settle — and cheap far-out buckets are exactly where Strategy 2
was measured to lose money. `intraday.py` learns the **remaining rise**
(`daily_max − running_max_at_hour_h`) per Local-Standard-Time hour from the IEM
archive; it collapses toward 0 after peak heating.

`ForecastClient` blends that into each model's `(mu, sigma)` — the mean is
precision-weighted (the forecast leads in the morning, the observation takes over
after peak) while the sigma is the **sharper of the two, not** the Bayesian
product rule, because the two errors describe the same air mass and are not
independent. Curves build lazily in the background (~45s per station per day
against the IEM archive) and a lookup never blocks an evaluation cycle; until one
lands, and on any failure, the engine simply forecasts unsharpened.

The `*_id` variants are logged and graded like any other shadow model. Set
`WA_ENABLE_INTRADAY_FORECAST=true` to let Strategy 2 actually trade them — and
until then an `auto` pick that lands on an `_id` model falls back to its base, so
the flag genuinely controls what reaches the order path.

## Data sources

- **Observations:** NOAA Aviation Weather Center METAR API
  (`aviationweather.gov`, default) or Iowa Environmental Mesonet (`iem`).
- **Forecasts:** NWS gridpoint API (`api.weather.gov`), populated by NBM and, at
  short lead times, HRRR. Raw GRIB decoding is intentionally avoided.
- **Markets:** Kalshi trade-api v2 (REST + WebSocket).

## Cross-venue basis monitor

Kalshi isn't the only venue for a city's daily high. IBKR **ForecastEx** and
**Polymarket** (US access via QCEX) run comparable markets; **Betfair** /
**Smarkets** run UK/EU novelty weather books. But "NYC high today" is *not* one
event across venues — they differ on three settlement axes:

| Axis | Kalshi | ForecastEx | Polymarket |
|---|---|---|---|
| **station** (NYC / CHI) | Central Park / Midway | **Central Park / Midway** (same as Kalshi) | **LaGuardia / O'Hare** (the outlier) |
| **data source** | next-day NWS **Climate Report** (catches 1-min / 6-hr highs) | **Weather Underground** METAR maxima | **Weather Underground** METAR maxima |
| **day window** | **Local Standard Time** (no DST) | local wall clock (DST) | local wall clock (DST) |

Because Kalshi differs from everyone on *source* + *day-window*, **Kalshi-vs-other
is always a basis (relative-value) trade, never a lock**. A genuinely lockable
cross-book arbitrage only exists between two venues matching on *all* axes — in
practice **ForecastEx ↔ Polymarket** on the five cities where they share a station:
**MIA, AUS, DEN, LAX, PHIL**. NYC and Chicago drop out because Polymarket is the
station outlier (LaGuardia / O'Hare) while Kalshi *and* ForecastEx both use
Central Park / Midway (confirmed in a 2026-07-22 live-rules pass).

```
python -m weather_arb.cross_venue                 # US venues (default)
python -m weather_arb.cross_venue --scope global  # + Betfair/Smarkets
python -m weather_arb.cross_venue --json
```

The report prints each venue's per-city station/source/window, classifies every
pair (`ARB?` candidate / `basis` / `----` none), and lists the lockable-arb
candidates. Stations are largely confirmed (Polymarket via its Gamma API /
resolution text; ForecastEx Central Park + Midway via its contract terms); rows
still flagged **UNVERIFIED** are ForecastEx's five main-airport stations awaiting
a per-city contract-terms pull — plus the **bucket edges** (Polymarket uses 2°F
bins with threshold tails), which must match before an `ARB?` pair is truly
fungible. Toggle the scope via `WA_VENUE_SCOPE` or `--scope`.

### Live quote monitor

`cross_venue_quotes` is the read-only network layer: it pulls live top-of-book
per **bucket** and scans the arb-eligible pairs for `yes_ask(A) + no_ask(B) < 100¢`
net of fees. Nothing is placed.

```
python -m weather_arb.cross_venue_quotes                 # candidate cities, today
python -m weather_arb.cross_venue_quotes --date 2026-07-22 --city LAX
python -m weather_arb.cross_venue_quotes --all --json
```

- **Kalshi** — live via the public trade-api (no key); each bucket market quotes
  `yes_ask`/`no_ask` in cents directly. Shown for basis awareness — the classifier
  keeps Kalshi *basis* (NWS-CLI + LST day, and its bucket edges are offset from
  Polymarket's), so the lock scan never pairs it.
- **Polymarket** — live via the public Gamma API (`gamma-api.polymarket.com`, no
  key). Each city/day event lists one market per bucket with `bestBid`/`bestAsk`
  on the YES token, so `YES_ask = bestAsk` and `NO_ask = 1 − bestBid`.
- **ForecastEx** — **no public feed**; quotes come through an authenticated IBKR
  Client Portal Web API session (see setup below). On IBKR, ForecastEx daily-high
  contracts are **options whose strikes are temperature thresholds** ("high ≥ X"),
  not 2°F bins. So the monitor pulls the threshold ladder and **synthesizes** the
  Polymarket bins from it: bin `[lo,hi] = (≥lo) − (≥hi+1)`. That synthesis crosses
  a bid/ask *twice*, so a synthesized-bin-vs-Polymarket-bin lock is structurally
  hard — it needs a real ForecastEx mispricing, not just a wide market. Without a
  gateway the monitor runs Polymarket-only and finds no locks (a lock needs two books).
- Live check (2026-07-22): **Polymarket does not list Philadelphia**, so the
  practical live candidate set is **MIA / AUS / DEN / LAX** even though PHIL's
  stations would match. Bucket alignment is strict — only identical `(lo, hi)`
  edges are compared, so mismatched bucketing is never treated as fungible.

**ForecastEx setup (one-time + a daily login):**
1. Download IBKR's **Client Portal Gateway** and run it (`bin/run.sh root/conf.yaml`).
2. Open `https://localhost:5000`, accept the self-signed cert, and **log into your
   IBKR account** (the session must be live for market data).
3. Set `IBKR_WEB_API_BASE=https://localhost:5000/v1/api` and `IBKR_VERIFY_SSL=false`.
4. Point it at the contracts: paste today's threshold conids via
   `WA_FORECASTEX_CONTRACTS_JSON` (`{"LAX":[[76,conid],…]}`, most reliable — conids
   roll daily) **or** give a per-city underlying symbol via `WA_FORECASTEX_SYMBOLS_JSON`
   for auto-discovery. The `≥strike` semantics and bucket edges are still
   `UNVERIFIED` — confirm them on your first live run before trusting a lock.

## NWS-vs-WU settlement divergence (the differentiated edge)

Kalshi settles on the next-day **NWS Climate Report**, derived from the full
**1-minute ASOS** record; Polymarket/ForecastEx settle on **Weather Underground**,
the max of the *hourly* METARs. A brief spike between the top-of-hour reports lands
in Kalshi's max but not WU's — so the two settle **different whole degrees**, hence
different buckets. Your tracker already ingests high-frequency obs, so you can see
the NWS-basis max before the CLI publishes and before the hourly-watching crowd
reprices. `divergence` quantifies it (read-only):

```
python -m weather_arb.divergence                 # yesterday, all 7 cities (snapshot)
python -m weather_arb.divergence --days 14        # frequency table: how often it fires
python -m weather_arb.divergence --date 2026-07-20 --city DEN --json
python -m weather_arb.divergence --watch          # live detector: poll + alert on a new divergence
```

`--watch` is the live detector: each cycle it recomputes today's running divergence
per city and fires a push alert (ntfy + `alerts.log`, reusing the watchdog infra)
the moment a divergence or 2°F bin-flip *newly* appears — the actionable "the higher
Kalshi bucket is underpriced right now" signal — and stays quiet otherwise. Since the
IEM 1-minute archive lags real time by up to ~an hour, detection carries that latency
(still well before the next-day CLI); a lagging 1-minute read floors to the METAR max
(flagged `1-min floor`) rather than inventing a value.

- `nws_max_f` = rounded max of the 1-minute ASOS series (Kalshi basis);
  `wu_max_f` = rounded hourly-METAR max (Polymarket basis); `delta_f` is the edge.
- **Measured over 14 days: the NWS max exceeded the hourly-METAR max on ~72% of
  city-days and flipped a Polymarket 2°F bin on ~70%** — Denver ~100%, Chicago ~92%.
  That's a settlement-basis divergence (an upper bound on the *tradeable* edge —
  you still need the market mispriced and a fill).
- The CLI can never be below WU (it sees the METARs too), so `nws_max = max(1-min,
  METAR)`; that also degrades gracefully when the 1-minute archive gaps through the
  peak (flagged `gappy` / `1-min floor`, never a bogus negative delta).
- Same-station cities (**MIA/AUS/DEN/LAX/PHIL**) are a clean cross-venue divergence.
  For **NYC/CHI** Polymarket settles at a *different, warmer* station (LaGuardia/O'Hare
  vs Central Park/Midway), so snapshot/`--json` also report a station-aware
  `pm_delta` — which can **reverse** the naive edge (e.g. NYC 2026-07-22: Kalshi
  +1°F on the source effect, but LaGuardia ran 2°F hotter, so *Polymarket* settled
  the higher bucket). Don't trade NYC/CHI off the same-station number alone.

## Extending to Polymarket

`market_client.MarketClient` is an abstract base; add a `PolymarketClient`
implementing the same methods (EIP-712 order signing against the CLOB) and pass
it to `ArbitrageEngine` — no engine/risk changes required.

## Notes & safety rails

- All order placement flows through `RiskManager.size_order`, which enforces the
  per-market cap **and** the limit-only invariant (defense-in-depth with the
  type system).
- Intent is journaled to the ledger *before* transmission, so a crash mid-flight
  is reconciled against the exchange on the next startup.
- `SIGINT`/`SIGTERM` trigger a graceful drain: stop generating orders, cancel
  still-resting limit orders, close sockets and the DB.
```
