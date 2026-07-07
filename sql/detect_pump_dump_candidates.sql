-- Pump-and-Dump Candidate Detection (market candle data)
-- Mirrors: src/detection/pump_dump.py
--
-- Pattern: price rises >= 5% over 3 candles, volume z-score > 3.0 at the
-- peak, then price reverses >= 3% within the following 6 candles.
-- All three conditions must hold simultaneously to fire an alert.
-- Thresholds from src/config/thresholds.py:
--   ROLLING_WINDOW       = 24
--   VOLUME_Z_THRESHOLD   = 3.0
--   PUMP_RETURN_THRESHOLD = 0.05
--   PUMP_WINDOW          = 3
--   REVERSAL_THRESHOLD   = -0.03
--   REVERSAL_WINDOW      = 6

WITH numbered_candles AS (
    -- Assign a sequential row number per series so we can reference offsets.
    SELECT
        exchange,
        symbol,
        timeframe,
        timestamp,
        close,
        volume,
        ROW_NUMBER() OVER (
            PARTITION BY exchange, symbol, timeframe
            ORDER BY timestamp
        ) AS rn
    FROM market_candles
    WHERE close  > 0
      AND volume IS NOT NULL
),

with_volume_stats AS (
    SELECT
        *,
        AVG(volume) OVER (
            PARTITION BY exchange, symbol, timeframe
            ORDER BY rn
            ROWS BETWEEN 24 PRECEDING AND 1 PRECEDING
        ) AS rolling_mean_volume,
        SQRT(
            AVG(volume * volume) OVER (
                PARTITION BY exchange, symbol, timeframe
                ORDER BY rn
                ROWS BETWEEN 24 PRECEDING AND 1 PRECEDING
            )
            - AVG(volume) OVER (
                PARTITION BY exchange, symbol, timeframe
                ORDER BY rn
                ROWS BETWEEN 24 PRECEDING AND 1 PRECEDING
            )
            * AVG(volume) OVER (
                PARTITION BY exchange, symbol, timeframe
                ORDER BY rn
                ROWS BETWEEN 24 PRECEDING AND 1 PRECEDING
            )
        ) AS rolling_std_volume,
        COUNT(*) OVER (
            PARTITION BY exchange, symbol, timeframe
            ORDER BY rn
            ROWS BETWEEN 24 PRECEDING AND 1 PRECEDING
        ) AS window_candle_count
    FROM numbered_candles
),

with_z_and_pump AS (
    SELECT
        *,
        CASE
            WHEN rolling_std_volume > 0 AND window_candle_count = 24
            THEN (volume - rolling_mean_volume) / rolling_std_volume
            ELSE NULL
        END AS volume_z_score,
        -- Pump return = (close_peak - close_3_candles_ago) / close_3_candles_ago.
        -- LAG(close, 3) gives the close 3 rows before (= PUMP_WINDOW).
        LAG(close, 3) OVER (
            PARTITION BY exchange, symbol, timeframe ORDER BY rn
        ) AS close_3_ago
    FROM with_volume_stats
),

with_pump_return AS (
    SELECT
        *,
        CASE
            WHEN close_3_ago > 0
            THEN (close - close_3_ago) / close_3_ago
            ELSE NULL
        END AS pump_return,
        -- Reversal: minimum close in the next 6 candles after the peak.
        MIN(close) OVER (
            PARTITION BY exchange, symbol, timeframe
            ORDER BY rn
            ROWS BETWEEN 1 FOLLOWING AND 6 FOLLOWING   -- REVERSAL_WINDOW
        ) AS reversal_close
    FROM with_z_and_pump
),

with_reversal_return AS (
    SELECT
        *,
        CASE
            WHEN close > 0 AND reversal_close IS NOT NULL
            THEN (reversal_close - close) / close
            ELSE NULL
        END AS reversal_return
    FROM with_pump_return
)

SELECT
    'Pump-and-Dump Candidate'                                              AS alert_type,
    exchange,
    symbol,
    timeframe,
    -- The pump window spans from the candle 3 rows before (pump start) to peak.
    -- The alert window extends to the reversal low.
    LAG(timestamp, 3) OVER (
        PARTITION BY exchange, symbol, timeframe ORDER BY rn
    )                                                                      AS pump_start_time,
    timestamp                                                              AS peak_time,
    ROUND(close, 6)                                                        AS peak_close,
    ROUND(pump_return * 100, 4)                                            AS pump_return_pct,
    ROUND(volume_z_score, 4)                                               AS peak_volume_z_score,
    ROUND(reversal_return * 100, 4)                                        AS reversal_return_pct,
    ROUND(reversal_close, 6)                                               AS reversal_close,
    -- Severity score from severity.py: pump_dump_score()
    --   pump_return_points     : 5-8% -> +15, > 8% -> +25
    --   volume_multiplier_pts  : volume confirmed -> +15  (always true here)
    --   rapid_reversal_points  : reversal confirmed -> +15 (always true here)
    --   pump_all_conditions    : all conditions met -> +10 (always true here)
    --   multiple_rules_points  : cross-rule Volume Spike overlap -> +25
    --     (set to 0 in this standalone query; the Python checks existing_alerts)
    CASE WHEN pump_return > 0.08 THEN 25 ELSE 15 END    -- pump_return_points
    + 15                                                 -- volume_multiplier_points (confirmed)
    + 15                                                 -- rapid_reversal_points (confirmed)
    + 10                                                 -- pump_all_conditions_points (confirmed)
    + 0                                                  -- multiple_rules_points (not wired here)
                                                                           AS severity_score,
    CASE
        WHEN (CASE WHEN pump_return > 0.08 THEN 25 ELSE 15 END + 40) >= 75 THEN 'High'
        WHEN (CASE WHEN pump_return > 0.08 THEN 25 ELSE 15 END + 40) >= 45 THEN 'Medium'
        ELSE 'Low'
    END                                                                    AS severity
FROM with_reversal_return
WHERE pump_return    >= 0.05        -- PUMP_RETURN_THRESHOLD
  AND volume_z_score >  3.0        -- VOLUME_Z_THRESHOLD
  AND reversal_return <= -0.03     -- REVERSAL_THRESHOLD
  AND pump_return    IS NOT NULL
  AND volume_z_score IS NOT NULL
  AND reversal_return IS NOT NULL
ORDER BY pump_return DESC, timestamp;
