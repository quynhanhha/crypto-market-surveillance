# Architecture

This document describes how the system is put together: the module layout, the data flow from raw market data to a rendered alert, and the boundary rules that keep the codebase from tangling detection logic into the UI.

## Entry points

There are two independent runnable entry points. They never share a process.

- **`app.py`** — the Streamlit dashboard. `main()` (line 58) is the sole startup path, guarded by `if __name__ == "__main__":`. Run with `streamlit run app.py`.
- **`src/ingestion/synthetic_data.py`** — a standalone CLI for generating and exporting the deterministic synthetic dataset. Run with `python3 -m src.ingestion.synthetic_data --export`.

## Module map and data flow

```text
Public Exchange API (CCXT / Coinbase) or sample CSV
                 |
                 v
       Market Ingestion Layer
       src/ingestion/fetch_market_data.py   (live fetch + 3-layer fallback)
       src/ingestion/synthetic_data.py      (deterministic synthetic accounts/orders/trades)
                 |
                 v
     SQLite Surveillance Database
     src/storage/db.py          (connection helper, PRAGMA foreign_keys = ON)
     src/storage/schema.py      (executes sql/create_tables.sql)
     src/storage/repositories.py (all reads/writes — no SQL in app.py or src/ui/)
                 |
        +--------+--------+
        |                 |
        v                 v
 Market Detection    Synthetic Scenario Detection
 src/detection/       src/detection/
  price_anomaly.py     wash_trading.py
  volume_spike.py      spoofing_layering.py
  pump_dump.py
        |                 |
        +--------+--------+
                 |
                 v
        src/detection/severity.py   (shared scoring — no per-rule severity logic elsewhere)
                 |
                 v
      Alerts, Evidence, Cases (SQLite tables, deduplicated)
                 |
        +--------+--------+
        |                 |
        v                 v
  Streamlit Dashboard   Reporting
  app.py                src/reporting/case_report.py    (PDF via reportlab)
  src/ui/pages.py        src/reporting/daily_summary.py  (CSV/PDF daily digest)
  src/ui/components.py
  src/ui/charts.py
```

## Module boundary rules

Enforced by `tests/test_architecture_rules.py` and stated in `AGENTS.md`:

| Rule | Enforced in |
|---|---|
| Detection logic lives only in `src/detection/` — `src/ui/` may not import it | `tests/test_architecture_rules.py::test_ui_does_not_import_detection_or_severity_modules` |
| Severity scoring lives only in `src/detection/severity.py` | `tests/test_architecture_rules.py::test_ui_does_not_contain_rule_or_threshold_logic` |
| All thresholds live only in `src/config/thresholds.py` — no magic numbers elsewhere | same test, via `THRESHOLD_CONSTANT_NAMES` |
| Database access lives only in `src/storage/` — no SQL in `app.py` or UI files | convention, not currently a standalone test |

## Startup sequence (`app.py`)

1. `main()` calls `configure_logging()` once, then `st.set_page_config()`.
2. `_connection()` opens (or reuses, via `st.session_state`) a single SQLite connection and runs `create_schema()`.
3. `_initialize_data()`:
   - Loads market candles via `load_market_data()` (live CCXT fetch, or the fallback chain described in `docs/limitations.md`), inserts them.
   - Loads the committed synthetic tables (`accounts`, `account_links`, `synthetic_orders`, `synthetic_trades`) and inserts them.
   - Runs all five detection functions and concatenates their alerts.
   - Inserts alerts with `ON CONFLICT(dedup_key) DO NOTHING` so re-running the pipeline never clobbers analyst-set status.
4. `render_sidebar()` drives page selection and filtering across six pages: Overview, Market Anomalies, Synthetic Surveillance Cases, Alert Detail, Daily Report, Methodology.

## Database

SQLite, chosen for two reasons: Streamlit Community Cloud's filesystem is ephemeral with no managed database service, and all writes happen once at startup from committed CSVs with no concurrent writers — SQLite's single-writer model plus `check_same_thread=False` is sufficient. See `docs/limitations.md` for the tradeoffs this brings.

Core tables (full DDL in `sql/create_tables.sql`):

- `accounts` → `account_links`, `synthetic_orders`, `synthetic_trades`
- `alerts` (`UNIQUE(dedup_key)`) → `alert_evidence`, `cases`
- `market_candles` (`UNIQUE(exchange, symbol, timeframe, timestamp)`, no FK to other tables)

`alerts.account_id` is plain TEXT, not a FK — it may hold a pipe-delimited pair (e.g. `ACC_0076|ACC_0077`) for wash-trading alerts that jointly flag two accounts. `alert_evidence` is a child table rather than a JSON blob so evidence stays queryable in SQL.

**Alert deduplication**: `dedup_key = sha256(f"{alert_type}|{symbol}|{start_time}|{end_time}|{account_id}").hexdigest()[:32]`, enforced by a `UNIQUE` constraint. `start_time`/`end_time` are always candle timestamps, never a fetch or insert timestamp — this is what makes the key stable across re-runs on the same data.

## Detection rules

Each Python rule in `src/detection/` has a SQL mirror in `sql/detect_*.sql` (used for SQL-only analysis, not executed by the app itself):

| Rule | File | Trigger condition |
|---|---|---|
| Volume Spike | `volume_spike.py` | `(volume − rolling_mean) / rolling_std > 3.0` over a 24-candle baseline |
| Price Anomaly | `price_anomaly.py` | `|return_z_score| > 3.0`, bidirectional |
| Pump-and-Dump | `pump_dump.py` | ≥5% pump over 3 candles + volume z-score >3.0 at peak + ≥3% reversal within 6 candles |
| Wash Trading (synthetic) | `wash_trading.py` | linked account pair (confidence ≥0.70), ≥5 trades / ≥$50,000 notional / net position ratio ≤0.10 within a 48h window |
| Spoofing/Layering (synthetic) | `spoofing_layering.py` | ≥3 "cancel within 60s at ≥4× normal size, followed by an opposite trade within 180s" events per account/symbol |

Exact thresholds live in `src/config/thresholds.py`; scoring lives in `src/detection/severity.py`. See `README.md` for the full per-rule breakdown and severity point table, and `docs/limitations.md` for what each rule is known not to catch.

## Config

`src/config/thresholds.py` holds every detection threshold and synthetic-generation parameter (seed, account counts, window sizes). There is no environment-based runtime configuration layer — deployment-level settings (DB path, default exchange/symbols/timeframe) are module-level constants in `app.py`.
