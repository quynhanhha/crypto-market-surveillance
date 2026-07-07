"""Streamlit entrypoint for the surveillance dashboard."""

from __future__ import annotations

import logging
from pathlib import Path
import sqlite3
from time import monotonic

import pandas as pd
import streamlit as st

from src.detection.price_anomaly import detect_price_anomalies
from src.detection.pump_dump import detect_pump_dump_candidates
from src.detection.spoofing_layering import detect_spoofing_layering
from src.detection.volume_spike import detect_volume_spikes
from src.detection.wash_trading import detect_wash_trading
from src.ingestion.fetch_market_data import load_market_data
from src.storage.db import connect_sqlite
from src.storage.repositories import (
    fetch_alerts,
    fetch_market_candles,
    insert_alerts,
    insert_market_candles,
    insert_synthetic_tables,
    load_synthetic_tables,
)
from src.storage.schema import create_schema
from src.ui.components import filter_alerts, filter_candles, render_sidebar
from src.ui.pages import (
    alert_detail_page,
    daily_report_page,
    market_anomalies_page,
    methodology_page,
    overview_page,
    synthetic_cases_page,
)
from src.utils.logging import configure_logging


LOGGER = logging.getLogger(__name__)

APP_TITLE = "Crypto Market Surveillance Analytics"
PROJECT_ROOT = Path(__file__).resolve().parent
DB_PATH = PROJECT_ROOT / "surveillance.db"
DATA_DIR = PROJECT_ROOT / "data"
FALLBACK_PATH = DATA_DIR / "sample_market_candles.csv"
DEFAULT_EXCHANGE = "coinbase"
DEFAULT_SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD"]
DEFAULT_TIMEFRAME = "5m"
DEFAULT_LIMIT = 100
SIDEBAR_WIDTH_STYLE = """
<style>
[data-testid="stSidebar"] {
    min-width: 220px !important;
    max-width: 220px !important;
}
</style>
"""


def main() -> None:
    """Render the six-page Streamlit surveillance dashboard."""
    configure_logging()
    st.set_page_config(
        page_title="Crypto Market Surveillance",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.markdown(SIDEBAR_WIDTH_STYLE, unsafe_allow_html=True)
    conn = _connection()
    create_schema(conn)

    loaded = _safe_initialize_data(conn)
    if loaded is None:
        return
    all_candles = fetch_market_candles(conn)
    all_alerts = fetch_alerts(conn)
    controls = render_sidebar(all_candles, loaded["metadata"], all_alerts)
    _maybe_auto_refresh(controls["auto_refresh"])

    if controls["refresh"]:
        loaded = _safe_initialize_data(conn)
        if loaded is None:
            return
        all_candles = fetch_market_candles(conn)
        all_alerts = fetch_alerts(conn)

    filtered_candles = filter_candles(all_candles, controls)
    filtered_alerts = filter_alerts(all_alerts, controls)

    page = controls["page"]
    if page == "Overview":
        overview_page(filtered_alerts, filtered_candles, controls["symbol"])
    elif page == "Market Anomalies":
        market_anomalies_page(filtered_alerts)
    elif page == "Synthetic Surveillance Cases":
        synthetic_cases_page(conn, filtered_alerts, loaded["synthetic_tables"]["account_links"])
    elif page == "Alert Detail":
        alert_detail_page(conn, filtered_alerts, filtered_candles)
    elif page == "Daily Report":
        daily_report_page(filtered_alerts, filtered_candles, loaded["metadata"])
    else:
        methodology_page()


def _connection() -> sqlite3.Connection:
    if "db_conn" not in st.session_state:
        st.session_state.db_conn = connect_sqlite(DB_PATH)
    return st.session_state.db_conn


def _safe_initialize_data(conn: sqlite3.Connection) -> dict[str, object] | None:
    """Run `_initialize_data`, turning any failure into a user-facing error instead of a crash."""
    try:
        return _initialize_data(conn)
    except Exception:
        LOGGER.exception("Failed to load market data, seed synthetic data, or run detection rules")
        st.error(
            "Something went wrong while loading data or running detection rules. "
            "Try refreshing the page; if the problem persists, check the app logs."
        )
        st.stop()
        return None


def _initialize_data(conn: sqlite3.Connection) -> dict[str, object]:
    candles = load_market_data(
        exchange_id=DEFAULT_EXCHANGE,
        symbols=DEFAULT_SYMBOLS,
        timeframe=DEFAULT_TIMEFRAME,
        limit=DEFAULT_LIMIT,
        fallback_path=str(FALLBACK_PATH),
    )
    insert_market_candles(conn, candles)
    synthetic_tables = load_synthetic_tables(DATA_DIR)
    insert_synthetic_tables(conn, synthetic_tables)
    market_alerts = _market_alerts(candles)
    synthetic_alerts = _synthetic_alerts(synthetic_tables)
    alerts = pd.concat([market_alerts, synthetic_alerts], ignore_index=True)
    insert_alerts(conn, alerts)
    metadata = {
        "data_source": str(candles.attrs.get("data_source", "sample")),
        "api_status": str(candles.attrs.get("api_status", "unknown")),
        "fallback_reason": str(candles.attrs.get("fallback_reason", "")),
        "last_fetched_at": str(candles.attrs.get("last_fetched_at", "")),
        "latest_candle_timestamp": str(candles.attrs.get("latest_candle_timestamp", "")),
    }
    return {"metadata": metadata, "synthetic_tables": synthetic_tables}


def _market_alerts(candles: pd.DataFrame) -> pd.DataFrame:
    price_alerts = detect_price_anomalies(candles)
    volume_alerts = detect_volume_spikes(candles)
    pump_alerts = detect_pump_dump_candidates(candles, volume_alerts)
    return pd.concat([price_alerts, volume_alerts, pump_alerts], ignore_index=True)


def _synthetic_alerts(tables: dict[str, pd.DataFrame]) -> pd.DataFrame:
    wash_alerts = detect_wash_trading(
        tables["synthetic_trades"], tables["account_links"]
    )
    spoof_alerts = detect_spoofing_layering(
        tables["synthetic_orders"],
        tables["synthetic_trades"],
        tables["accounts"],
        tables["account_links"],
    )
    return pd.concat([wash_alerts, spoof_alerts], ignore_index=True)


def _maybe_auto_refresh(enabled: bool) -> None:
    if not enabled:
        st.session_state.last_auto_refresh = monotonic()
        return
    last_refresh = st.session_state.get("last_auto_refresh", 0.0)
    if monotonic() - last_refresh >= 60:
        st.session_state.last_auto_refresh = monotonic()
        st.rerun()


if __name__ == "__main__":
    main()
