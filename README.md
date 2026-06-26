# Crypto Market Surveillance Analytics

A Streamlit surveillance dashboard that turns public OHLCV market candles and deterministic synthetic account-level scenarios into explainable alerts, evidence packages, severity scores, and PDF case reports — mirroring the workflow an analyst or investigator would use in a real control environment.

Demo: https://crypto-market-surveillance.streamlit.app

## Project Overview

Market abuse investigations require two different views of the market: public behavior (price and volume shocks that can indicate manipulation) and account-level behavior (linked trading, rapid cancellations, coordinated activity that can indicate intent). Public exchange APIs provide the first view but not the second.

This project combines live OHLCV data from a CCXT-compatible exchange with deterministic synthetic account, order, and trade data to demonstrate a complete surveillance stack: five detection rules, a shared severity model, deduplicated SQLite alert storage, and a Streamlit dashboard with alert triage, evidence review, and PDF case report export.

## Architecture

```text
Public Exchange API or Sample Market Data
                 |
                 v
         Market Ingestion Layer
         src/ingestion/fetch_market_data.py
                 |
                 v
       SQLite Surveillance Database
       src/storage/db.py + repositories.py
                 |
        +--------+--------+
        |                 |
        v                 v
 Market Detection     Synthetic Scenario
  Rules + Severity     Detection Rules
  src/detection/       src/detection/
        |                 |
        +--------+--------+
                 |
                 v
      Alerts, Evidence, and Cases
                 |
                 v
     Streamlit Dashboard and PDF Reports
     src/ui/          src/reporting/
```

**Module boundary rule** (enforced by `tests/test_architecture_rules.py`): UI files may not import `src/detection/` or `src/detection/severity`. Key files by responsibility:

| File | Responsibility |
|---|---|
| `src/config/thresholds.py` | All detection thresholds and generation parameters — no magic numbers elsewhere |
| `src/detection/severity.py` | All scoring functions and severity label mapping — no per-rule severity logic elsewhere |
| `src/storage/repositories.py` | All database reads and writes — no SQL in `app.py` or UI files |
| `src/ingestion/fetch_market_data.py` | Live fetch, normalization, and deterministic fallback |

## Data Sources and Real vs Synthetic Boundary

- **Real market data**: public OHLCV candles for BTC/USD, ETH/USD, and SOL/USD fetched from a Coinbase-compatible exchange via CCXT.
- **Sample market data**: committed CSV at `data/sample_market_candles.csv`, used when live data cannot be fetched.
- **Synthetic surveillance data**: deterministic accounts, account links, orders, and trades generated with `seed=42`. Approximates realistic account behavior across five account types (retail, active retail, market maker, institutional, suspicious) with embedded pump-and-dump, wash-trading, and spoofing scenarios.

> Public exchange APIs expose market-level data but not private account identifiers or order lifecycle data. Real market data is used for price and volume monitoring. Synthetic account-level data demonstrates surveillance investigation workflows. The synthetic data does not represent real accounts or transactions.

## Detection Rules

All thresholds are in `src/config/thresholds.py`. Detection implementations are in `src/detection/`.

### Rule 1 — Volume Spike (`src/detection/volume_spike.py`)

**Inputs**: `market_candles` — columns `exchange`, `symbol`, `timeframe`, `timestamp`, `volume`.

**Baseline**: per (exchange, symbol, timeframe) group, `shift(1).rolling(24).mean()` and `.std()`. The `shift(1)` excludes the current candle from its own baseline.

**Trigger**: `(volume − rolling_mean) / rolling_std > 3.0`

**Corroboration**: `volume / rolling_mean >= 2.5` — boosts severity score but is not a gate; an alert fires on z-score alone.

**What this misses**: a sustained 2× volume lift that never produces a single-candle spike (e.g., chronic front-running); cross-asset volume divergence.

---

### Rule 2 — Price Anomaly (`src/detection/price_anomaly.py`)

**Inputs**: `market_candles` — columns `exchange`, `symbol`, `timeframe`, `timestamp`, `open`, `close`.

**Return**: `(close − open) / open` per candle. Rows with `open <= 0` or non-finite values are dropped before analysis.

**Baseline**: same `shift(1).rolling(24)` structure as Volume Spike.

**Trigger**: `|return_z_score| > 3.0` — bidirectional, catches both crashes and pumps.

**What this misses**: slow drift that never exceeds 3σ on any single bar (e.g., 1% per candle for 20 candles); cross-symbol price dislocation.

---

### Rule 3 — Pump-and-Dump Candidate (`src/detection/pump_dump.py`)

**Inputs**: `market_candles` — columns `exchange`, `symbol`, `timeframe`, `timestamp`, `close`, `volume`.

**All three conditions must hold simultaneously**:

1. `pump_return = (close[peak] − close[peak − 3]) / close[peak − 3] >= 0.05` — +5% over 3 candles (`PUMP_WINDOW = 3`)
2. `volume_z_score at peak > 3.0` — same 24-candle rolling baseline
3. `reversal_return = (min_close in next 6 candles − close[peak]) / close[peak] <= −0.03` — at least −3% reversal within 6 candles (`REVERSAL_WINDOW = 6`)

**Cross-rule confirmation**: if a Volume Spike alert already fired on the same symbol and overlapping window, `multiple_rules_confirmed = True` adds +25 points to the score.

**What this misses**: pumps that take more than 3 candles to peak; dumps delayed beyond 6 candles after the peak.

---

### Rule 4 — Synthetic Wash Trading Pattern (`src/detection/wash_trading.py`)

**Inputs**: `synthetic_trades` + `account_links`.

**Prerequisites**: the (buyer, seller) pair must have an `account_links` row with `confidence >= 0.70` (`LINK_CONFIDENCE_THRESHOLD`).

**Within a sliding 48-hour window** (`TIME_WINDOW_HOURS = 48`), all of:
- `trade_count >= 5` (`MIN_PAIR_TRADES`)
- `notional_value >= $50,000` (`MIN_NOTIONAL`)
- `net_position_ratio = |bought_qty − sold_qty| / total_qty <= 0.10` (`MAX_NET_POSITION_RATIO`)

The algorithm selects the best window per pair (most trades, then highest notional).

**What this misses**: wash trading routed through an account pair not present in `account_links`; a new account with no link record.

---

### Rule 5 — Synthetic Spoofing/Layering Pattern (`src/detection/spoofing_layering.py`)

**Inputs**: `synthetic_orders` + `synthetic_trades` + `accounts`.

**A "large fast cancel"** satisfies all of:
- `status == 'cancelled'`
- `cancelled_at − submitted_at <= 60 seconds` (`MAX_CANCEL_SECONDS`)
- `notional >= 4× the account's own historical average notional` on its non-cancelled orders (`LARGE_ORDER_MULTIPLIER = 4.0`)

**Each qualifying cancel** must be followed by an opposite-side trade by the same account within 180 seconds (`OPPOSITE_TRADE_WINDOW_SECONDS`).

**Fires per (account_id, symbol)** when there are `>= 3` qualifying cancel-then-trade events (`MIN_REPEATED_EVENTS`).

**What this misses**: cancels slower than 60 seconds; adversaries who route the opposite trade through a linked account rather than the same account.

## Severity Scoring

All scoring lives in `src/detection/severity.py`. No rule file computes its own severity.

**Label thresholds**: `score >= 75` → High | `score >= 45` → Medium | else → Low

**Point components**:

| Component | Condition | Points |
|---|---|---|
| z-score band | \|z\| ∈ [3, 4) | 15 |
| z-score band | \|z\| ∈ [4, 6) | 25 |
| z-score band | \|z\| > 6 | 40 |
| Volume multiplier corroboration | >= 2.5× rolling mean | 15 |
| High notional | >= $50,000 | 15 |
| Linked accounts confirmed | always True when wash trading fires | 20 |
| High link confidence | > 0.85 | 10 |
| Rapid reversal confirmed | always True when P&D fires | 15 |
| Pump return (strong) | > 8% | 25 |
| Pump return (moderate) | 5–8% | 15 |
| All conditions confirmed | always True when P&D fires | 10 |
| Multiple rules confirmed | cross-rule Volume Spike overlap | 25 |
| Spoofing repeat events | 3–5 events | 15 |
| Spoofing repeat events | > 5 events | 25 |
| Wash trade count | 5–9 trades | 15 |
| Wash trade count | >= 10 trades | 25 |

**Practical score ceilings** (useful when reading alerts):

| Rule | Max score | Max label |
|---|---|---|
| Volume Spike | 55 (40 + 15) | Medium |
| Price Anomaly | 40 | Low |
| Pump-and-Dump, no cross-rule | 65 | Medium |
| Pump-and-Dump, with cross-rule | 90 | High |
| Wash Trading | 70 (25 + 20 + 10 + 15) | Medium |
| Spoofing/Layering | 40 (25 + 0 + 15) | Low |

Note: Spoofing/Layering severity is capped at Low because `linked_coordination_confirmed` is hardcoded `False` — the synthetic dataset has no coordination signal between accounts in the spoofing scenario. A production deployment would wire this to the `account_links` table.

## Database Schema

SQLite schema is in `sql/create_tables.sql` and initialized by `src/storage/schema.py`. `PRAGMA foreign_keys = ON` is set at every connection in `src/storage/db.py`.

```
accounts (account_id TEXT PK)
  account_type, risk_tier, jurisdiction, avg_daily_volume
  │
  ├── account_links (link_id PK)
  │     account_id_a → accounts, account_id_b → accounts
  │     link_type: shared_infrastructure | coordinated_timing | beneficial_ownership | historical_pattern
  │     confidence: REAL 0.0–1.0
  │
  ├── synthetic_orders (order_id TEXT PK)
  │     account_id → accounts
  │     side, price, quantity, status, submitted_at, cancelled_at (nullable), filled_at (nullable)
  │
  └── synthetic_trades (trade_id TEXT PK)
        buyer_account_id → accounts, seller_account_id → accounts
        symbol, price, quantity, notional_value

alerts (alert_id PK, UNIQUE dedup_key)
  alert_type, severity, severity_score, exchange, symbol, account_id (TEXT, not FK)
  start_time, end_time, status (New | Under Review | Escalated | Closed)
  │
  ├── alert_evidence (evidence_id PK)
  │     alert_id → alerts
  │     metric_name, metric_value, threshold_value, comparison_operator, explanation
  │
  └── cases (case_id PK)
        alert_id → alerts
        case_status, analyst_note, classification, created_at, updated_at

market_candles (id PK)
  UNIQUE(exchange, symbol, timeframe, timestamp) — no FK to other tables
```

**Design notes**:
- `alerts.account_id` is plain TEXT (not a FK) and may be a pipe-delimited pair like `ACC_0076|ACC_0077` for wash-trading alerts where two accounts are jointly flagged.
- `alert_evidence` is a child table rather than a JSON blob so individual metrics can be queried or filtered in SQL without application-side deserialization.
- There is no `ON DELETE CASCADE`. Deleting an account requires removing its orders and trades first; the FK constraint will reject the deletion otherwise.

## Alert Deduplication

The dedup key is a 32-character prefix of a SHA-256 hash built from five fields:

```
sha256(f"{alert_type}|{symbol}|{start_time}|{end_time}|{account_id}").hexdigest()[:32]
```

The `alerts` table enforces `UNIQUE(dedup_key)`. Every insert uses `ON CONFLICT(dedup_key) DO NOTHING`. If the same alert re-fires (e.g., after a cold start rebuilds the database), the original row is preserved — including any analyst status already set (Under Review, Escalated, etc.).

Key: `start_time` and `end_time` are candle timestamps, never `fetched_at` or `datetime.utcnow()`. This makes the key stable across re-runs on the same data.

Tested in `tests/test_alert_dedup.py`: ON CONFLICT skip, analyst status preservation on re-insert, FK rejection of orphaned evidence, cross-process hash determinism, distinct symbols produce distinct keys.

## Fallback Data Logic

`load_market_data()` in `src/ingestion/fetch_market_data.py` wraps `fetch_ohlcv()` in a broad `except Exception` (`# noqa: BLE001` — intentionally covers all CCXT and runtime failures). Fallback also fires when the live response is empty.

**Three-layer fallback behavior**:

1. Read `data/sample_market_candles.csv` if it exists and is non-empty.
2. If missing or empty — call `generate_sample_market_candles(seed=42)` (100 5-minute periods, Coinbase, `numpy.default_rng(42)`) and write the CSV.
3. If the CSV exists but fails to parse or has wrong schema — catch the error, regenerate, re-read.

After loading, `candles.attrs` carries: `data_source="sample"`, `api_status="unavailable"`, `fallback_reason=<exception string>`. The UI reads these attrs to display a data-source banner.

**Known limitation**: there is no distinction between a transient rate-limit (retry-able in seconds) and a permanent failure — both produce the same static fallback with no retry. A production deployment would add exponential backoff and staleness checks on the fallback data.

## Database Choice

SQLite is used for this project for two concrete reasons:

1. **Deployment**: Streamlit Community Cloud has an ephemeral filesystem with no managed database. SQLite is a file next to the app — zero infrastructure, no connection strings, no migrations tool required.
2. **Write pattern**: all writes happen at initialization from committed CSVs. There are no concurrent writers during normal use. SQLite's single-writer model is sufficient and `check_same_thread=False` handles Streamlit's threading model.

For a production deployment with concurrent ingestion workers and persistent storage across sessions, replace `connect_sqlite()` in `src/storage/db.py` with a PostgreSQL connection pool. The schema and repository layer are otherwise compatible.

## How to Run Locally

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m streamlit run app.py
```

## Testing

```bash
python3 -m pytest
python3 -m ruff check .
```

84 tests across 16 files:

| File | Tests | What it covers |
|---|---|---|
| `test_volume_spike.py` | 4 | fires above threshold, ignores below, baseline excludes current candle via `shift(1)`, score scales with z-score band |
| `test_price_anomaly.py` | 4 | fires on extreme return, ignores normal, handles zero open and NaN safely, negative return triggers |
| `test_pump_dump.py` | 5 | all conditions fire, missing reversal suppresses, missing volume suppresses, cross-rule score boost |
| `test_synthetic_rules.py` | 10 | wash trading (fires / no link / low net position / low confidence) and spoofing (fires / repeat count / opposite trades / per-account baseline / normal cancel rates) |
| `test_alert_dedup.py` | 5 | ON CONFLICT skip, FK rejects orphan evidence, status preserved on re-insert, different symbols produce different keys, cross-process hash determinism |
| `test_market_ingestion.py` | 13 | 6 distinct fallback trigger paths (exchange error, rate limit, empty response, malformed rows, unsupported exchange, unsupported symbol), bad fallback CSV regeneration, normalization rejects non-finite values, determinism |
| `test_detection_integration.py` | 4 | full pipeline: DB init → seed synthetic tables → detect → dedup → PDF report |
| `test_severity.py` | 4 | z-score band breakpoints, severity label thresholds, dedup key field sensitivity, rule-specific helper existence |
| `test_synthetic_detection.py` | 7 | seeded data is deterministic, correct account mix (83 accounts), pump/spoof/wash scenarios are embedded |
| `test_case_report.py` | 8 | PDF bytes returned, CSV formatting, timestamp normalization, severity score in output |
| `test_dashboard_helpers.py` | 8 | filter logic, overview totals by severity and type, table rendering |
| `test_architecture_rules.py` | 2 | UI files do not import `src/detection/` or `src/detection/severity` |
| Other (schema, storage, skeleton) | 10 | tables created, FK enforcement, candle dedup on insert, Streamlit entrypoint exists |

## Limitations

- This is a surveillance prototype, not a production control room.
- Public market data cannot prove account ownership, coordination, or intent — it can only flag statistically unusual behavior.
- Synthetic account activity is deliberately realistic but does not represent real accounts or transactions.
- Spoofing/Layering severity is capped at Low because coordination between accounts is not modeled in the current synthetic dataset.
- SQLite is appropriate for local and single-session demo use, not for multi-user or persistent production deployments.
- The fallback data path makes no distinction between transient API failures and permanent unavailability — no retry logic exists.
- API availability and symbol coverage depend on the external CCXT exchange source.

## Future Work

- Replace SQLite with a persistent production database (PostgreSQL).
- Wire `linked_coordination_confirmed` in the spoofing rule to the `account_links` table to enable High-severity spoofing alerts.
- Add exponential backoff and staleness gating to the market data fallback path.
- Add richer case management and analyst workflow controls.
- Integrate more exchange venues and deeper market coverage.
- Extend entity resolution and linkage analysis for account networks.
- Add feedback loops so analyst dispositions can improve alert prioritization over time.

## What This Demonstrates

- Surveillance workflow that separates public market monitoring (price/volume rules) from private-account abuse scenarios (wash trading, spoofing).
- Explainable detection logic with exact thresholds, per-alert evidence tables, and recommended analyst follow-up.
- Shared severity model across five rule types so alerts are prioritized on a common scale.
- Deduplicated alert storage that preserves analyst state across pipeline re-runs.
- Honest documentation of deployment constraints and known gaps in the detection model.
