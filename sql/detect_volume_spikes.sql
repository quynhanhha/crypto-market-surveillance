-- Volume Spike Detection (market candle data)
-- Mirrors: src/detection/volume_spike.py
--
-- Pattern: a single candle's volume is unusually high relative to the
-- rolling 24-candle baseline computed from the previous 24 candles.
-- Thresholds from src/config/thresholds.py:
--   ROLLING_WINDOW        = 24
--   VOLUME_Z_THRESHOLD    = 3.0
--   MIN_VOLUME_MULTIPLIER = 2.5

WITH rolling_stats AS (
    SELECT
        id,
        exchange,
        symbol,
        timeframe,
        timestamp,
        volume,
        -- Rolling mean over the previous 24 candles (excludes current candle,
        -- matching shift(1).rolling(24, min_periods=24) in Python).
        -- Only computed when a full 24-candle window is available.
        AVG(volume) OVER (
            PARTITION BY exchange, symbol, timeframe
            ORDER BY timestamp
            ROWS BETWEEN 24 PRECEDING AND 1 PRECEDING
        ) AS rolling_mean_volume,
        -- Population stddev via the E[x²] - E[x]² identity.
        -- Python uses sample stddev (ddof=1); for n=24 the difference is ~2%.
        SQRT(
            AVG(volume * volume) OVER (
                PARTITION BY exchange, symbol, timeframe
                ORDER BY timestamp
                ROWS BETWEEN 24 PRECEDING AND 1 PRECEDING
            )
            - AVG(volume) OVER (
                PARTITION BY exchange, symbol, timeframe
                ORDER BY timestamp
                ROWS BETWEEN 24 PRECEDING AND 1 PRECEDING
            )
            * AVG(volume) OVER (
                PARTITION BY exchange, symbol, timeframe
                ORDER BY timestamp
                ROWS BETWEEN 24 PRECEDING AND 1 PRECEDING
            )
        ) AS rolling_std_volume,
        -- Count candles in the window to enforce min_periods = 24.
        COUNT(*) OVER (
            PARTITION BY exchange, symbol, timeframe
            ORDER BY timestamp
            ROWS BETWEEN 24 PRECEDING AND 1 PRECEDING
        ) AS window_candle_count
    FROM market_candles
    WHERE volume IS NOT NULL
),

z_scores AS (
    SELECT
        *,
        CASE
            WHEN rolling_std_volume > 0 AND window_candle_count = 24
            THEN (volume - rolling_mean_volume) / rolling_std_volume
            ELSE NULL
        END                                      AS volume_z_score,
        CASE
            WHEN rolling_mean_volume > 0
            THEN volume / rolling_mean_volume
            ELSE NULL
        END                                      AS volume_multiplier
    FROM rolling_stats
)

SELECT
    'Volume Spike'                                                         AS alert_type,
    exchange,
    symbol,
    timeframe,
    timestamp,
    ROUND(volume, 6)                                                       AS volume,
    ROUND(rolling_mean_volume, 6)                                          AS rolling_mean_volume,
    ROUND(rolling_std_volume, 6)                                           AS rolling_std_volume,
    ROUND(volume_z_score, 4)                                               AS volume_z_score,
    ROUND(volume_multiplier, 4)                                            AS volume_multiplier,
    -- Severity score from severity.py: volume_spike_score()
    --   primary_metric_z_score_points : z 3-4 -> +15, z 4-6 -> +25, z > 6 -> +40
    --   volume_multiplier_points      : multiplier >= 2.5 -> +15
    CASE
        WHEN ABS(volume_z_score) > 6 THEN 40
        WHEN ABS(volume_z_score) >= 4 THEN 25
        ELSE 15
    END
    + CASE WHEN volume_multiplier >= 2.5 THEN 15 ELSE 0 END               AS severity_score,
    CASE
        WHEN (CASE WHEN ABS(volume_z_score) > 6 THEN 40
                   WHEN ABS(volume_z_score) >= 4 THEN 25
                   ELSE 15 END
              + CASE WHEN volume_multiplier >= 2.5 THEN 15 ELSE 0 END) >= 75 THEN 'High'
        WHEN (CASE WHEN ABS(volume_z_score) > 6 THEN 40
                   WHEN ABS(volume_z_score) >= 4 THEN 25
                   ELSE 15 END
              + CASE WHEN volume_multiplier >= 2.5 THEN 15 ELSE 0 END) >= 45 THEN 'Medium'
        ELSE 'Low'
    END                                                                    AS severity
FROM z_scores
WHERE volume_z_score > 3.0          -- VOLUME_Z_THRESHOLD
  AND volume_z_score IS NOT NULL
ORDER BY volume_z_score DESC, timestamp;
