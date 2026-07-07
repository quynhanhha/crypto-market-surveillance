-- Spoofing / Layering Detection (synthetic order and trade data)
-- Mirrors: src/detection/spoofing_layering.py
--
-- Pattern: an account repeatedly places large orders then cancels them
-- quickly, followed by an opposite-side trade in the same symbol.
-- Thresholds from src/config/thresholds.py:
--   LARGE_ORDER_MULTIPLIER        = 4.0
--   MAX_CANCEL_SECONDS            = 60
--   OPPOSITE_TRADE_WINDOW_SECONDS = 180
--   MIN_REPEATED_EVENTS           = 3
--   MIN_NOTIONAL                  = 50 000

WITH account_baseline AS (
    -- Per-account average notional of all non-quick-cancelled orders.
    -- Quick-cancelled = status 'cancelled' AND cancelled within 60 s.
    -- Excluded from the baseline so spoof orders do not inflate it.
    SELECT
        account_id,
        AVG(price * quantity) AS avg_order_notional
    FROM synthetic_orders
    WHERE NOT (
        status = 'cancelled'
        AND cancelled_at IS NOT NULL
        AND (julianday(cancelled_at) - julianday(submitted_at)) * 86400.0 <= 60.0
    )
    GROUP BY account_id
),

large_fast_cancels AS (
    -- Cancelled orders that were (a) cancelled within 60 s of submission
    -- and (b) at least 4x the account's own historical average notional.
    SELECT
        o.order_id,
        o.account_id,
        o.symbol,
        o.side,
        o.price * o.quantity                                               AS notional,
        o.submitted_at,
        o.cancelled_at,
        (julianday(o.cancelled_at) - julianday(o.submitted_at)) * 86400.0 AS cancel_seconds,
        o.timestamp                                                        AS order_timestamp
    FROM synthetic_orders o
    JOIN account_baseline ab ON ab.account_id = o.account_id
    WHERE o.status = 'cancelled'
      AND o.cancelled_at IS NOT NULL
      AND (julianday(o.cancelled_at) - julianday(o.submitted_at)) * 86400.0 <= 60.0  -- MAX_CANCEL_SECONDS
      AND o.price * o.quantity >= 4.0 * ab.avg_order_notional                        -- LARGE_ORDER_MULTIPLIER
),

matched_events AS (
    -- For each large fast cancellation, find the first opposite-side trade
    -- by the same account within 180 s of the cancellation.
    --   Sell cancel -> account appears as buyer in the subsequent trade.
    --   Buy  cancel -> account appears as seller in the subsequent trade.
    SELECT
        lfc.account_id,
        lfc.symbol,
        lfc.order_timestamp,
        lfc.cancelled_at,
        lfc.cancel_seconds,
        lfc.notional,
        MIN(t.timestamp) AS trade_timestamp
    FROM large_fast_cancels lfc
    JOIN synthetic_trades t
      ON t.symbol    = lfc.symbol
     AND t.timestamp >= lfc.cancelled_at
     AND (julianday(t.timestamp) - julianday(lfc.cancelled_at)) * 86400.0 <= 180.0  -- OPPOSITE_TRADE_WINDOW_SECONDS
     AND (
           (lfc.side = 'sell' AND t.buyer_account_id  = lfc.account_id)
        OR (lfc.side = 'buy'  AND t.seller_account_id = lfc.account_id)
     )
    GROUP BY
        lfc.order_id, lfc.account_id, lfc.symbol,
        lfc.order_timestamp, lfc.cancelled_at, lfc.cancel_seconds, lfc.notional
),

alert_candidates AS (
    -- Aggregate per account+symbol; fire when at least 3 matched events exist.
    SELECT
        account_id,
        symbol,
        COUNT(*)             AS event_count,
        AVG(cancel_seconds)  AS avg_cancel_seconds,
        SUM(notional)        AS total_cancel_notional,
        MIN(order_timestamp) AS start_time,
        MAX(trade_timestamp) AS end_time
    FROM matched_events
    GROUP BY account_id, symbol
    HAVING COUNT(*) >= 3    -- MIN_REPEATED_EVENTS
)

SELECT
    'Synthetic Spoofing/Layering Pattern'                              AS alert_type,
    account_id,
    symbol,
    event_count,
    ROUND(avg_cancel_seconds, 1)                                       AS avg_cancel_seconds,
    ROUND(total_cancel_notional, 2)                                    AS total_cancel_notional,
    start_time,
    end_time,
    -- Severity score from severity.py: spoof_repeated_events_points + high_notional_points.
    -- linked_coordination_confirmed is FALSE on synthetic data (no wire to account_links);
    -- adding +20 there would push the score into Medium / High territory.
    CASE WHEN event_count > 5 THEN 25 ELSE 15 END
    + CASE WHEN total_cancel_notional >= 50000 THEN 15 ELSE 0 END      AS severity_score,
    CASE
        WHEN (CASE WHEN event_count > 5 THEN 25 ELSE 15 END
              + CASE WHEN total_cancel_notional >= 50000 THEN 15 ELSE 0 END) >= 75 THEN 'High'
        WHEN (CASE WHEN event_count > 5 THEN 25 ELSE 15 END
              + CASE WHEN total_cancel_notional >= 50000 THEN 15 ELSE 0 END) >= 45 THEN 'Medium'
        ELSE 'Low'
    END                                                                AS severity
FROM alert_candidates
ORDER BY severity_score DESC, total_cancel_notional DESC;
