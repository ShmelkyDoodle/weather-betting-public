# Data coverage, sampling frequency, and timestamp anomalies

Measured **2026-11-09** directly against the live ledger (opened `mode=ro`; the
tracker was writing throughout, which WAL permits without blocking either side).
Every number below is reproducible from the companion export
(`weather_arb_export.sqlite3`) — the queries are stated so they can be re-run.

The reviewer's four questions, answered in order: coverage, missing periods,
sampling frequency, records affected by the timestamp issue.

---

## 0. What the export contains

| table | rows | why it's here |
|---|---:|---|
| `daily_forecasts` | 3,118 | requested |
| `model_prediction_history` | 4,170,039 | requested — 90% of the export's bytes |
| `model_predictions` | 63,702 | requested |
| `station_logs` | 27,664 | requested |
| `source_observations` | 159,625 | requested |
| `market_results` | 4,620 | **added** — Kalshi's own payout record |
| `lockin_events` | 2,155 | **added** — the graded detection ledger |
| `daily_tmax` | 999 | **added** — the exact input the lock-in rule consumes |

The last three were not requested but are **required** to check the reported
findings empirically rather than just the data quality. `market_results` is the
ground truth every audit claim was scored against (Kalshi's own `result` per
ticker); `lockin_events` is where the "0 wrong in 940 graded detections" figure
comes from, with a `call_correct` column already graded against settlement; and
`daily_tmax` is what the live engine actually reads. Without them the findings
are unverifiable from this export. Counts drift upward between snapshots — the
tracker is live — so these differ slightly from figures quoted in
`AUDIT-2026-10-30.md`, which was run 2026-10-30.

⚠ **Nine stations appear, seven are traded.** `station_logs` carries `KBOS` and
`KORD` alongside the seven Kalshi settlement stations (`KNYC KMDW KMIA KAUS
KDEN KLAX KPHL`). Those two are ingested for neighbour-corroboration and
cross-venue comparison (`KORD` is Polymarket's Chicago station), not traded.
Grouping by station without excluding them will overcount markets by two.

---

## 1. Coverage — complete, no missing days in any table

```sql
SELECT min(climate_date), max(climate_date), count(DISTINCT climate_date) FROM <t>;
```

| table | first | last | days present / span | missing |
|---|---|---|---:|---:|
| `station_logs` | 2026-07-21 | 2026-11-08 | 111 / 111 | **0** |
| `source_observations` | 2026-08-24 | 2026-11-08 | 77 / 77 | **0** |
| `daily_forecasts` | 2026-07-21 | 2026-11-09 | 112 / 112 | **0** |
| `model_prediction_history` | 2026-07-24 | 2026-11-09 | 109 / 109 | **0** |
| `model_predictions` | 2026-07-21 | 2026-11-09 | 112 / 112 | **0** |

Per station, `station_logs` is complete on all 111 days for all nine stations:

      KAUS 111/111  2,929 rows  26.4/day      KMIA 111/111  3,036  27.4/day
      KBOS 111/111  3,009 rows  27.1/day      KNYC 111/111  3,370  30.4/day
      KDEN 111/111  2,924 rows  26.3/day      KORD 111/111  3,159  28.5/day
      KLAX 111/111  2,921 rows  26.3/day      KPHL 111/111  3,338  30.1/day
      KMDW 111/111  2,978 rows  26.8/day

26–30 observations/station/day is the expected rate for hourly METAR plus
SPECIs. `KNYC` and `KPHL` run highest because they issue more SPECIs.

**The different start dates are features shipping, not outages.**
`source_observations` begins 2026-08-24 because the multi-source observation
recorder was added then; `model_prediction_history` begins 2026-07-24 because
the snapshot writer was added two days after the tracker first started.

## 2. Missing periods — none at day granularity, and no intra-day outage ≥ 2h

      missing climate_dates, station_logs, 2026-07-21..2026-11-08 : none
      snapshot gaps >= 120 min within any (ticker, model) series   : 0
      snapshot gaps >= 120 min pooled across all series            : 0

This is worth stating plainly because the project's history contains a
multi-week silent failure (the 2026-08 order-book outage) that a heartbeat check
missed entirely. Coverage being complete here is *not* evidence that the data is
correct — see §4 — only that rows exist for every period.

## 3. Sampling frequency — 30 min, and tighter than previously documented

⚠ **Measure this per `(ticker, model)` series, not pooled.** Pooled across all
series the modal gap is 30 min at only **13.2%**, because ~15 models × ~840
tickers are written on staggered schedules and pooling interleaves them. That
pooled figure is an artifact of the grouping, not a finding.

Within a series (n = 4,106,979 gaps):

      gap        count        share
       30 min  3,607,554     87.84%
       31 min    250,474      6.10%
       32 min    191,959      4.67%
       60 min     23,375      0.57%     <- one skipped tick
       58-62 min  20,839      0.51%
      >= 120 min        0      0.00%

**98.6% of gaps fall in 30–32 minutes.** The configured interval is
`WA_PREDICTION_HISTORY_INTERVAL_SECONDS=1800`; the 1–2 minute spread above it is
scan duration, not drift.

⚠ **This revises a figure in `CLAUDE.md`,** which states "78.9% of gaps exactly
30 min". Measured now: **87.8%**. The original was computed on a 21-day window in
August; this is 109 days. The *conclusion* that claim supported is unaffected —
the backtest still cannot resolve a persistence gate finer than ~30 min, because
the floor on resolvable granularity is the 30-minute interval itself, not the
percentage that hits it exactly.

## 4. Records affected by the timestamp issue

⚠ **I do not know which issue was meant** — the phrase came in a request, not
from a specific finding in this repo. Three distinct candidates exist, with very
different blast radii. All three are counted here so the question is answerable
whichever was intended.

### 4a. `prob_grid_fixed` is NULL on 276,864 rows (6.64%) — all of them recent

```sql
SELECT substr(created_at_utc,1,7) mon, sum(prob_grid_fixed IS NULL), count(*)
FROM model_prediction_history GROUP BY mon;
```

      2026-07        48 /   210,696   ( 0.0%)
      2026-08       143 / 1,112,985   ( 0.0%)
      2026-09         0 / 1,239,669   ( 0.0%)
      2026-10         0 / 1,269,355   ( 0.0%)
      2026-11   276,864 /   337,334   (82.1%)   <-- all of it here

**This is the documented silent-skip hazard, firing exactly as predicted.**
`scripts/backfill_grid_probs.py` reprices history onto the corrected bucket grid
into `prob_grid_fixed`, and the weekly job passes a **fixed** historical constant
`--corrected-since 2026-08-24T00:05:22+00:00`. Rows created after the last weekly
run have `prob` already correct but `prob_grid_fixed` still NULL — and
`backtest --strategy 2 --grid-fixed` **reads only `prob_grid_fixed`, so it
silently skips them.** `CLAUDE.md` warns about this in the BUCKET GRID section.

So: **not data corruption, and not a bug — a staleness window.** It closes every
Monday 08:00 when `weekly.sh` runs, and reopens as new rows accrue. The practical
consequence is that any `--grid-fixed` backtest run late in the week is blind to
the most recent ~1 week of rows without announcing it. Right now that is 276,864
rows / 6 days.

The 191 older NULLs (48 in July, 143 in August) are the rows the original
backfill could not invert; `AUDIT`-era notes record 980,749/980,940 = 99.98%
repriced, consistent with this.

### 4b. Five rows carry a timestamp with no microsecond component

```sql
SELECT id, ticker, model, created_at_utc FROM model_prediction_history
WHERE length(created_at_utc) = 25;
```

      640064   KXHIGHMIA-26AUG15-B94.5   ensemble     2026-08-14T18:26:29+00:00
      2231886  KXHIGHCHI-26SEP22-B66.5   ensemble_bc  2026-09-22T23:23:42+00:00
      2481618  KXHIGHPHIL-26SEP28-B65.5  nbm_id       2026-09-29T00:45:04+00:00
      2826160  KXHIGHAUS-26OCT07-B89.5   current_id   2026-10-07T09:22:29+00:00
      3744340  KXHIGHMIA-26OCT30-B86.5   current      2026-10-29T21:34:38+00:00

Cause: `datetime.isoformat()` **omits** the fractional part when microseconds are
exactly zero. At 4.17M rows the expected count of such timestamps is ~4.2, and 5
were observed — so this is chance, not a code path.

**Severity: cosmetic for sorting, fatal for naive parsing.** These strings still
sort and compare correctly as ISO-8601 text, so any query using string
comparison is unaffected. But
`datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%f%z")` with a fixed format **raises
`ValueError` on these five rows**. Use `datetime.fromisoformat()`, which handles
both. The same pattern applies to every `*_at_utc` column in the schema.

Format audit across all seven timestamp columns — otherwise uniform:

| column | format | n |
|---|---|---:|
| `daily_forecasts.recorded_at_utc` | `…%f+00:00` (len 32) | 3,118 |
| `model_prediction_history.created_at_utc` | len 32 / **len 25** | 4,170,034 / **5** |
| `model_predictions.created_at_utc` | len 32 | 63,702 |
| `station_logs.observed_at_utc` | len 25 (minute-resolution, by design) | 27,664 |
| `station_logs.inserted_at_utc` | len 32 | 27,664 |
| `source_observations.observed_at_utc` | len 25 (by design) | 159,625 |
| `source_observations.received_at_utc` | len 32 | 159,625 |

All are UTC with an explicit `+00:00` offset. `observed_at_utc` columns carry no
microseconds because observation validity times are minute-resolution — that is
correct, not an anomaly.

### 4c. The look-ahead bias in backtest pricing — affects no rows in this export

`_candle_series` passed `period_interval=60` believing the unit was seconds;
Kalshi's unit is **minutes**, so every pre-fix backtest read **hourly** bars and
`_price_at` returned that bar's *close* — a quote up to 59 minutes **after** the
decision. Fixed 2026-08-23. Measured cost on the taker baseline: **−$2.99 →
−$3.80 per trade**.

This never touched the database: it was a bug in how Kalshi candlesticks were
requested at analysis time. **Rows affected in these tables: zero.** But every
Strategy-2 P&L figure recorded before 2026-08-23 is optimistic by roughly that
much, and the fix is why the levels in older notes disagree with newer ones.

---

## 5. One independent corroboration worth having

`source_observations.precision_f` records the resolution of each reading as
served (`1.8` = whole degrees Celsius, `0.18` = tenths):

      nws5min      precision 1.8   138,608    (97.6%)
      nws5min      precision 0.18    3,470
      nws_hourly   precision 0.18   15,167    (86.4%)
      nws_hourly   precision 1.8     2,380

This is direct empirical support for the `FEED-LATENCY.md` finding that the
5-minute feed is quantized to whole degrees Celsius, and therefore
**structurally incapable** of proving 1 °F bucket certainty — which is why that
channel produced 31 wrong lock-in calls (3.34%) in `lockin_events` while the
hourly METAR channel produced 0 in 940. Cross-check:

```sql
SELECT source, count(*), sum(call_correct = 0) FROM lockin_events GROUP BY source;
```

The 5-minute channel is wired to a shadow-only code path and cannot submit an
order (`arb_engine.py`, `observe_shadow_lockin`).

---

## Reproducing any of this

```bash
sqlite3 weather_arb_export.sqlite3
```

The export is a plain SQLite 3 database with the original schemas and indexes
intact. No credentials, account identifiers, order history, or P&L are included —
and none ever existed: the system has run in dry-run throughout with no Kalshi
credentials installed, so `trades` is empty by construction.
