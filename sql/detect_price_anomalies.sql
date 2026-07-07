-- Price Anomaly Detection (market candle data)
-- Mirrors: src/detection/price_anomaly.py
--
-- Pattern: a single candle's open-to-close return deviates significantly
-- from the rolling 24-candle baseline.  Catches both sharp drops and spikes.
-- Thresholds from src/config/thresholds.py:
--   ROLLING_WINDOW     = 24
--   RETURN_Z_THRESHOLD = 3.0

WITH candle_returns AS (
    -- Candle return = (close - open) / open, matching the Python implementation.
    -- Guards: open must be positive and both prices finite.
    SELECT
        id,
        exchange,
        symbol,
        timeframe,
        timestamp,
        open,
        close,
        (close - open) * 1.0 / open AS candle_return
    FROM market_candles
    WHERE open  > 0
      AND close IS NOT NULL
      AND open  IS NOT NULL
),

rolling_stats AS (
    SELECT
        *,
        -- Rolling mean of the previous 24 candle returns (shift(1).rolling(24)).
        AVG(candle_return) OVER (
            PARTITION BY exchange, symbol, timeframe
            ORDER BY timestamp
            ROWS BETWEEN 24 PRECEDING AND 1 PRECEDING
        ) AS rolling_mean_return,
        -- Population stddev; Python uses sample stddev (ddof=1), ~2% difference for n=24.
        SQRT(
            AVG(candle_return * candle_return) OVER (
                PARTITION BY exchange, symbol, timeframe
                ORDER BY timestamp
                ROWS BETWEEN 24 PRECEDING AND 1 PRECEDING
            )
            - AVG(candle_return) OVER (
                PARTITION BY exchange, symbol, timeframe
                ORDER BY timestamp
                ROWS BETWEEN 24 PRECEDING AND 1 PRECEDING
            )
            * AVG(candle_return) OVER (
                PARTITION BY exchange, symbol, timeframe
                ORDER BY timestamp
                ROWS BETWEEN 24 PRECEDING AND 1 PRECEDING
            )
        ) AS rolling_std_return,
        COUNT(*) OVER (
            PARTITION BY exchange, symbol, timeframe
            ORDER BY timestamp
            ROWS BETWEEN 24 PRECEDING AND 1 PRECEDING
        ) AS window_candle_count
    FROM candle_returns
),

z_scores AS (
    SELECT
        *,
        CASE
            WHEN rolling_std_return > 0 AND window_candle_count = 24
            THEN (candle_return - rolling_mean_return) / rolling_std_return
            ELSE NULL
        END AS return_z_score
    FROM rolling_stats
)

SELECT
    'Price Anomaly'                                                        AS alert_type,
    exchange,
    symbol,
    timeframe,
    timestamp,
    ROUND(open, 6)                                                         AS open_price,
    ROUND(close, 6)                                                        AS close_price,
    ROUND(candle_return * 100, 4)                                          AS return_pct,
    ROUND(rolling_mean_return, 6)                                          AS rolling_mean_return,
    ROUND(rolling_std_return, 6)                                           AS rolling_std_return,
    ROUND(return_z_score, 4)                                               AS return_z_score,
    CASE WHEN return_z_score > 0 THEN 'up' ELSE 'down' END                AS direction,
    -- Severity score from severity.py: price_anomaly_score()
    --   primary_metric_z_score_points: |z| 3-4 -> +15, |z| 4-6 -> +25, |z| > 6 -> +40
    CASE
        WHEN ABS(return_z_score) > 6 THEN 40
        WHEN ABS(return_z_score) >= 4 THEN 25
        ELSE 15
    END                                                                    AS severity_score,
    CASE
        WHEN (CASE WHEN ABS(return_z_score) > 6 THEN 40
                   WHEN ABS(return_z_score) >= 4 THEN 25
                   ELSE 15 END) >= 75 THEN 'High'
        WHEN (CASE WHEN ABS(return_z_score) > 6 THEN 40
                   WHEN ABS(return_z_score) >= 4 THEN 25
                   ELSE 15 END) >= 45 THEN 'Medium'
        ELSE 'Low'
    END                                                                    AS severity
FROM z_scores
WHERE ABS(return_z_score) > 3.0    -- RETURN_Z_THRESHOLD
  AND return_z_score IS NOT NULL
ORDER BY ABS(return_z_score) DESC, timestamp;
