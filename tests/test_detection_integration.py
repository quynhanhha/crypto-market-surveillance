"""End-to-end detection and storage integration tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

import app
from src.detection.price_anomaly import detect_price_anomalies
from src.detection.pump_dump import detect_pump_dump_candidates
from src.detection.spoofing_layering import detect_spoofing_layering
from src.detection.volume_spike import detect_volume_spikes
from src.detection.wash_trading import detect_wash_trading
from src.ingestion.synthetic_data import generate_synthetic_dataset
from src.reporting.case_report import generate_case_report_pdf
from src.storage.db import connect_sqlite
from src.storage.repositories import (
    fetch_alert_evidence,
    insert_alerts,
    insert_market_candles,
    insert_synthetic_tables,
)
from src.storage.schema import create_schema


def test_database_initializes_and_seeds_synthetic_tables() -> None:
    """Repository helpers seed generated synthetic data into a fresh schema."""
    conn = connect_sqlite()
    create_schema(conn)
    inserted = insert_synthetic_tables(conn, generate_synthetic_dataset())

    assert inserted["accounts"] == 83
    assert inserted["synthetic_orders"] >= 8_000
    assert conn.execute("SELECT COUNT(*) FROM account_links").fetchone()[0] > 0


def test_detection_pipeline_inserts_deduplicated_alerts_and_preserves_status() -> None:
    """Running detection twice does not duplicate alerts or overwrite analyst status."""
    conn = connect_sqlite()
    create_schema(conn)
    candles = integration_market_candles()
    synthetic_tables = generate_synthetic_dataset()

    first_inserted = run_detection_pipeline(conn, candles, synthetic_tables)
    assert first_inserted >= 5
    assert conn.execute("SELECT COUNT(*) FROM alerts WHERE dedup_key IS NULL").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM alert_evidence").fetchone()[0] > 0

    alert_id = conn.execute("SELECT alert_id FROM alerts ORDER BY alert_id LIMIT 1").fetchone()[0]
    conn.execute("UPDATE alerts SET status = 'Under Review' WHERE alert_id = ?", (alert_id,))
    conn.commit()

    assert run_detection_pipeline(conn, candles, synthetic_tables) == 0
    row = conn.execute("SELECT status FROM alerts WHERE alert_id = ?", (alert_id,)).fetchone()
    assert row["status"] == "Under Review"


def test_case_report_generates_from_stored_alert_and_evidence() -> None:
    """A persisted alert and its evidence can feed case report generation."""
    conn = connect_sqlite()
    create_schema(conn)
    run_detection_pipeline(conn, integration_market_candles(), generate_synthetic_dataset())

    alert = pd.read_sql_query("SELECT * FROM alerts ORDER BY severity_score DESC LIMIT 1", conn).iloc[0]
    evidence = fetch_alert_evidence(conn, int(alert["alert_id"]))
    pdf = generate_case_report_pdf(alert, evidence)

    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 100


def test_dashboard_initialization_loads_sample_data_without_crash(monkeypatch) -> None:
    """The app initialization path loads sample-shaped data and inserts alerts."""
    conn = connect_sqlite()
    create_schema(conn)
    candles = integration_market_candles()

    monkeypatch.setattr(app, "load_market_data", lambda **_: candles)
    monkeypatch.setattr(app, "DATA_DIR", Path("data"))

    loaded = app._initialize_data(conn)

    assert "metadata" in loaded
    assert conn.execute("SELECT COUNT(*) FROM market_candles").fetchone()[0] == len(candles)
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] > 0


def run_detection_pipeline(
    conn,
    candles: pd.DataFrame,
    synthetic_tables: dict[str, pd.DataFrame],
) -> int:
    """Insert source data, run all five rules, and persist alerts."""
    insert_market_candles(conn, candles)
    insert_synthetic_tables(conn, synthetic_tables)
    volume_alerts = detect_volume_spikes(candles)
    alerts = pd.concat(
        [
            detect_price_anomalies(candles),
            volume_alerts,
            detect_pump_dump_candidates(candles, volume_alerts),
            detect_wash_trading(
                synthetic_tables["synthetic_trades"],
                synthetic_tables["account_links"],
            ),
            detect_spoofing_layering(
                synthetic_tables["synthetic_orders"],
                synthetic_tables["synthetic_trades"],
                synthetic_tables["accounts"],
                synthetic_tables["account_links"],
            ),
        ],
        ignore_index=True,
    )
    return insert_alerts(conn, alerts)


def integration_market_candles() -> pd.DataFrame:
    """Build deterministic candles that trigger all market detection rules."""
    return pd.concat(
        [
            market_candles_with_final_return("ETH/USD", 115.0),
            market_candles_with_volume("BTC/USD", 400.0),
            pump_dump_candles(),
        ],
        ignore_index=True,
    )


def market_candles_with_final_return(symbol: str, final_close: float) -> pd.DataFrame:
    """Build a market candle series with one configurable final close."""
    start = datetime(2026, 5, 23, tzinfo=UTC)
    rows = []
    for index in range(25):
        timestamp = start + timedelta(minutes=5 * index)
        close = 101.0 if index % 2 == 0 else 99.5
        if index == 24:
            close = final_close
        rows.append(market_candle(symbol, timestamp, close, 100.0))
    return pd.DataFrame(rows)


def market_candles_with_volume(symbol: str, final_volume: float) -> pd.DataFrame:
    """Build a stable market candle series with one configurable final volume."""
    start = datetime(2026, 5, 23, tzinfo=UTC)
    rows = []
    for index in range(25):
        timestamp = start + timedelta(minutes=5 * index)
        volume = 100 + (index % 2) if index < 24 else final_volume
        rows.append(market_candle(symbol, timestamp, 100.5, volume))
    return pd.DataFrame(rows)


def pump_dump_candles() -> pd.DataFrame:
    """Build candles with pump, volume spike, and reversal."""
    start = datetime(2026, 5, 23, tzinfo=UTC)
    rows = []
    for index in range(35):
        timestamp = start + timedelta(minutes=5 * index)
        close = 100.0
        volume = 100 + (index % 2)
        if index == 27:
            close = 106.0
            volume = 400.0
        elif index == 28:
            close = 102.0
        rows.append(market_candle("SOL/USD", timestamp, close, volume))
    return pd.DataFrame(rows)


def market_candle(
    symbol: str, timestamp: datetime, close: float, volume: float
) -> dict[str, object]:
    """Return one normalized market candle row."""
    return {
        "exchange": "coinbase",
        "symbol": symbol,
        "timeframe": "5m",
        "timestamp": timestamp.isoformat(),
        "open": 100.0,
        "high": max(100.0, close),
        "low": min(100.0, close),
        "close": close,
        "volume": volume,
        "fetched_at": "2026-05-23T00:00:00+00:00",
    }
