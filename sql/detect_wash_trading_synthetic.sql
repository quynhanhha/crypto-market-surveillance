-- Wash Trading Detection (synthetic trade and account-link data)
-- Mirrors: src/detection/wash_trading.py
--
-- Pattern: linked account pairs trading back and forth with minimal net
-- directional exposure, suggesting artificial volume generation.
-- Thresholds from src/config/thresholds.py:
--   LINK_CONFIDENCE_THRESHOLD = 0.70
--   MIN_PAIR_TRADES           = 5
--   MIN_NOTIONAL              = 50 000
--   MAX_NET_POSITION_RATIO    = 0.10
--   TIME_WINDOW_HOURS         = 48

WITH linked_pairs AS (
    -- Normalise the pair so account_a is always the lexicographically smaller ID,
    -- matching the Python min/max normalisation in detect_wash_trading().
    SELECT
        MIN(account_id_a, account_id_b) AS account_a,
        MAX(account_id_a, account_id_b) AS account_b,
        link_type,
        confidence
    FROM account_links
    WHERE confidence >= 0.70            -- LINK_CONFIDENCE_THRESHOLD
),

pair_trades AS (
    -- Attach normalised pair key and link metadata to every relevant trade.
    SELECT
        MIN(t.buyer_account_id, t.seller_account_id) AS account_a,
        MAX(t.buyer_account_id, t.seller_account_id) AS account_b,
        lp.link_type,
        lp.confidence,
        t.symbol,
        t.timestamp,
        t.buyer_account_id,
        t.seller_account_id,
        t.quantity,
        t.notional_value
    FROM synthetic_trades t
    JOIN linked_pairs lp
      ON lp.account_a = MIN(t.buyer_account_id, t.seller_account_id)
     AND lp.account_b = MAX(t.buyer_account_id, t.seller_account_id)
),

window_stats AS (
    -- For each trade t1, collect all trades in the same pair+symbol within 48 h.
    -- This replicates _best_pair_window(): slide a 48-hour window starting at
    -- each trade and gather metrics.
    SELECT
        t1.account_a,
        t1.account_b,
        t1.symbol,
        t1.link_type,
        t1.confidence,
        t1.timestamp                                                          AS window_start,
        COUNT(*)                                                              AS trade_count,
        SUM(t2.notional_value)                                                AS total_notional,
        -- Net position ratio = |bought_qty - sold_qty| / total_qty for account_a.
        CASE
            WHEN SUM(t2.quantity) > 0
            THEN ABS(
                     SUM(CASE WHEN t2.buyer_account_id  = t1.account_a THEN t2.quantity ELSE 0 END)
                   - SUM(CASE WHEN t2.seller_account_id = t1.account_a THEN t2.quantity ELSE 0 END)
                 ) * 1.0 / SUM(t2.quantity)
            ELSE 1.0
        END                                                                   AS net_position_ratio,
        MAX(t2.timestamp)                                                     AS window_end
    FROM pair_trades t1
    JOIN pair_trades t2
      ON  t2.account_a  = t1.account_a
     AND  t2.account_b  = t1.account_b
     AND  t2.symbol     = t1.symbol
     AND  t2.timestamp >= t1.timestamp
     AND  (julianday(t2.timestamp) - julianday(t1.timestamp)) * 24.0 <= 48.0  -- TIME_WINDOW_HOURS
    GROUP BY
        t1.account_a, t1.account_b, t1.symbol,
        t1.link_type, t1.confidence, t1.timestamp
),

best_window AS (
    -- Pick the single best 48-hour window per pair+symbol:
    -- most trades first, highest notional as tiebreaker.
    SELECT
        account_a,
        account_b,
        symbol,
        link_type,
        confidence,
        trade_count,
        total_notional,
        net_position_ratio,
        window_start,
        window_end,
        ROW_NUMBER() OVER (
            PARTITION BY account_a, account_b, symbol
            ORDER BY trade_count DESC, total_notional DESC
        ) AS rn
    FROM window_stats
)

SELECT
    'Synthetic Wash Trading Pattern'                                       AS alert_type,
    account_a || '|' || account_b                                          AS account_id,
    symbol,
    link_type,
    ROUND(confidence, 4)                                                   AS link_confidence,
    trade_count,
    ROUND(total_notional, 2)                                               AS total_notional,
    ROUND(net_position_ratio, 4)                                           AS net_position_ratio,
    window_start                                                           AS start_time,
    window_end                                                             AS end_time,
    -- Severity score from severity.py: wash_trading_score()
    --   wash_trade_count_points : 5-9 trades -> +15, >= 10 -> +25
    --   linked_accounts_points  : always +20 (pair is in account_links)
    --   high_link_confidence    : confidence > 0.85 -> +10
    --   high_notional_points    : notional >= 50 000 -> +15
    CASE WHEN trade_count >= 10 THEN 25 WHEN trade_count >= 5 THEN 15 ELSE 0 END
    + 20
    + CASE WHEN confidence > 0.85 THEN 10 ELSE 0 END
    + CASE WHEN total_notional >= 50000 THEN 15 ELSE 0 END                 AS severity_score,
    CASE
        WHEN (CASE WHEN trade_count >= 10 THEN 25 WHEN trade_count >= 5 THEN 15 ELSE 0 END
              + 20
              + CASE WHEN confidence > 0.85 THEN 10 ELSE 0 END
              + CASE WHEN total_notional >= 50000 THEN 15 ELSE 0 END) >= 75 THEN 'High'
        WHEN (CASE WHEN trade_count >= 10 THEN 25 WHEN trade_count >= 5 THEN 15 ELSE 0 END
              + 20
              + CASE WHEN confidence > 0.85 THEN 10 ELSE 0 END
              + CASE WHEN total_notional >= 50000 THEN 15 ELSE 0 END) >= 45 THEN 'Medium'
        ELSE 'Low'
    END                                                                    AS severity
FROM best_window
WHERE rn = 1
  AND trade_count      >= 5          -- MIN_PAIR_TRADES
  AND total_notional   >= 50000      -- MIN_NOTIONAL
  AND net_position_ratio <= 0.10     -- MAX_NET_POSITION_RATIO
ORDER BY severity_score DESC, total_notional DESC;
