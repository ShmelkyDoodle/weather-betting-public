-- Strategy 1 (deterministic lock-in) audit: every lock-in the engine has journaled.
--
--   sqlite3 -header -column weather_arb.sqlite3 < scripts/query_lockins.sql
--
-- ⚠ READ THIS FIRST -- why this reads `open_limit_orders`, not `trades`:
--
-- In dry-run the `trades` table can NEVER populate. `market_client.place_limit_order`
-- returns `filled_count=0` on its dry-run branch, and `ArbitrageEngine._record_fill`
-- (the only writer of `trades`) is reached only when `filled_count > 0`. So a query
-- against `trades` returns zero rows by construction, not because nothing fired.
--
-- What DOES get written, in dry-run and live alike, is `open_limit_orders`: the
-- engine journals the order *before* transmitting so a crash mid-flight is
-- recoverable, then updates its status. Nothing ever deletes from it. That makes it
-- the honest record of "Strategy 1 decided to trade", which is what we want to audit.
--
-- `price_cents` is therefore the *simulated execution price*: the marketable limit
-- the engine would have crossed with (capped at risk.lock_in_price_ceiling_cents,
-- 98c), not a confirmed fill. Section 3 shows real fills, and stays empty until
-- WA_DRY_RUN is turned off.

.headers on
.mode box

.print ''
.print '=== 1. LOCK-IN ORDERS (Strategy 1) ==='

SELECT
    substr(o.created_at_utc, 1, 19)                   AS ts_utc,
    o.ticker                                          AS ticker,
    o.side                                            AS side,
    o.action                                          AS action,
    o.count                                           AS size,
    o.price_cents                                     AS limit_c,
    printf('%.2f', o.count * o.price_cents / 100.0)   AS notional_usd,
    o.status                                          AS status,
    o.filled_count                                    AS filled,
    -- The daily max that made settlement certain, and the bucket it breached.
    -- Joined on the ticker's own climate day (the YYMMMDD token), so a lock-in is
    -- always shown against the temperature that actually triggered it.
    printf('%.1f', t.tmax_f)                          AS tmax_f,
    o.exchange_order_id                               AS exch_id
FROM open_limit_orders o
LEFT JOIN daily_tmax t
       ON t.climate_date = printf('20%s-%s-%s',
              substr(o.ticker, instr(o.ticker, '-') + 1, 2),
              CASE substr(o.ticker, instr(o.ticker, '-') + 3, 3)
                  WHEN 'JAN' THEN '01' WHEN 'FEB' THEN '02' WHEN 'MAR' THEN '03'
                  WHEN 'APR' THEN '04' WHEN 'MAY' THEN '05' WHEN 'JUN' THEN '06'
                  WHEN 'JUL' THEN '07' WHEN 'AUG' THEN '08' WHEN 'SEP' THEN '09'
                  WHEN 'OCT' THEN '10' WHEN 'NOV' THEN '11' ELSE '12' END,
              substr(o.ticker, instr(o.ticker, '-') + 6, 2))
      AND t.icao = CASE substr(o.ticker, 1, instr(o.ticker, '-') - 1)
              WHEN 'KXHIGHNY'   THEN 'KNYC' WHEN 'KXHIGHCHI'  THEN 'KMDW'
              WHEN 'KXHIGHMIA'  THEN 'KMIA' WHEN 'KXHIGHAUS'  THEN 'KAUS'
              WHEN 'KXHIGHDEN'  THEN 'KDEN' WHEN 'KXHIGHLAX'  THEN 'KLAX'
              WHEN 'KXHIGHPHIL' THEN 'KPHL' END
WHERE o.strategy = 'lock_in'
ORDER BY o.created_at_utc DESC;


.print ''
.print '=== 2. SUMMARY BY STATUS ==='

SELECT
    status                                            AS status,
    count(*)                                          AS orders,
    sum(count)                                        AS contracts,
    printf('%.2f', sum(count * price_cents / 100.0))  AS notional_usd,
    min(substr(created_at_utc, 1, 10))                AS first_day,
    max(substr(created_at_utc, 1, 10))                AS last_day
FROM open_limit_orders
WHERE strategy = 'lock_in'
GROUP BY status
ORDER BY orders DESC;


.print ''
.print '=== 3. CONFIRMED FILLS (empty while WA_DRY_RUN is on -- see header) ==='

SELECT
    substr(executed_at_utc, 1, 19)                    AS ts_utc,
    ticker                                            AS ticker,
    side                                              AS side,
    action                                            AS action,
    count                                             AS size,
    price_cents                                       AS price_c,
    printf('%.2f', notional_usd)                      AS notional_usd
FROM trades
WHERE strategy = 'lock_in'
ORDER BY executed_at_utc DESC;


.print ''
.print '=== 4. STRATEGY MIX (is anything mislabelled?) ==='

-- Every journaled order by strategy, so a Strategy 2 row can never be mistaken for
-- a lock-in. 'lock_in' and 'model_edge' are the only values models.Strategy emits
-- for automated orders; anything else here is a bug worth chasing.
SELECT
    'open_limit_orders' AS tbl, strategy AS strategy, count(*) AS n
FROM open_limit_orders GROUP BY strategy
UNION ALL
SELECT
    'trades' AS tbl, strategy AS strategy, count(*) AS n
FROM trades GROUP BY strategy
ORDER BY tbl, n DESC;
