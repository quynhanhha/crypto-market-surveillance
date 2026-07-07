"""Synthetic detection rule tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd

from src.detection.spoofing_layering import detect_spoofing_layering
from src.detection.wash_trading import detect_wash_trading


def empty_account_links() -> pd.DataFrame:
    """Return an account_links frame with no rows, for tests that don't exercise linking."""
    return pd.DataFrame(columns=["account_id_a", "account_id_b", "link_type", "confidence"])


def test_detect_wash_trading_returns_alert() -> None:
    """Linked round-trip trading with low net position change triggers."""
    trades = wash_trade_rows()
    links = pd.DataFrame(
        [
            {
                "link_id": 1,
                "account_id_a": "ACC_A",
                "account_id_b": "ACC_B",
                "link_type": "beneficial_ownership",
                "confidence": 0.90,
            }
        ]
    )

    alerts = detect_wash_trading(trades, links)

    assert len(alerts) == 1
    alert = alerts.iloc[0]
    assert alert["alert_type"] == "Synthetic Wash Trading Pattern"
    assert alert["account_id"] == "ACC_A|ACC_B"
    assert alert["severity_score"] == 60
    assert alert["severity"] == "Medium"
    assert {item["metric_name"] for item in alert["evidence"]} == {
        "trade_count",
        "notional_value",
        "net_position_ratio",
        "link_confidence",
    }


def test_detect_wash_trading_requires_confirmed_link() -> None:
    """Low-confidence account links do not trigger."""
    links = pd.DataFrame(
        [
            {
                "link_id": 1,
                "account_id_a": "ACC_A",
                "account_id_b": "ACC_B",
                "link_type": "historical_pattern",
                "confidence": 0.40,
            }
        ]
    )

    alerts = detect_wash_trading(wash_trade_rows(), links)

    assert alerts.empty


def test_detect_wash_trading_requires_linked_accounts() -> None:
    """Unlinked account pairs do not trigger even with round trips."""
    links = pd.DataFrame(
        columns=["link_id", "account_id_a", "account_id_b", "link_type", "confidence"]
    )

    alerts = detect_wash_trading(wash_trade_rows(), links)

    assert alerts.empty


def test_detect_wash_trading_requires_low_net_position_ratio() -> None:
    """One-sided trading fails the net-position-ratio condition."""
    links = pd.DataFrame(
        [
            {
                "link_id": 1,
                "account_id_a": "ACC_A",
                "account_id_b": "ACC_B",
                "link_type": "beneficial_ownership",
                "confidence": 0.90,
            }
        ]
    )
    trades = wash_trade_rows(round_trip=False)

    alerts = detect_wash_trading(trades, links)

    assert alerts.empty


def test_detect_wash_trading_score_increases_with_trade_count_and_notional() -> None:
    """Higher trade count/notional moves the central score upward."""
    links = pd.DataFrame(
        [
            {
                "link_id": 1,
                "account_id_a": "ACC_A",
                "account_id_b": "ACC_B",
                "link_type": "beneficial_ownership",
                "confidence": 0.90,
            }
        ]
    )

    lower = detect_wash_trading(wash_trade_rows(trade_count=6), links)
    higher = detect_wash_trading(wash_trade_rows(trade_count=10), links)

    assert lower.iloc[0]["severity_score"] < higher.iloc[0]["severity_score"]


def test_detect_spoofing_layering_returns_alert() -> None:
    """Repeated large cancellations followed by opposite trades trigger."""
    orders, trades, accounts = spoofing_rows(include_opposite_trades=True)

    alerts = detect_spoofing_layering(orders, trades, accounts, empty_account_links())

    assert len(alerts) == 1
    alert = alerts.iloc[0]
    assert alert["alert_type"] == "Synthetic Spoofing/Layering Pattern"
    assert alert["account_id"] == "ACC_S"
    assert alert["severity_score"] == 30
    assert alert["severity"] == "Low"
    assert {item["metric_name"] for item in alert["evidence"]} == {
        "large_cancelled_order_count",
        "average_cancel_seconds",
        "opposite_side_trade_count",
        "notional_value",
        "linked_counterparty_confirmed",
    }


def test_detect_spoofing_layering_requires_opposite_trades() -> None:
    """Large fast cancellations alone do not trigger spoofing/layering."""
    orders, trades, accounts = spoofing_rows(include_opposite_trades=False)

    alerts = detect_spoofing_layering(orders, trades, accounts, empty_account_links())

    assert alerts.empty


def test_detect_spoofing_layering_ignores_normal_cancellations() -> None:
    """Ordinary-sized cancellations do not trigger spoofing/layering."""
    orders, trades, accounts = spoofing_rows(include_opposite_trades=True)
    orders.loc[orders["order_id"].str.startswith("SPOOF_"), "quantity"] = 1.0

    alerts = detect_spoofing_layering(orders, trades, accounts, empty_account_links())

    assert alerts.empty


def test_detect_spoofing_layering_requires_minimum_repeat_count() -> None:
    """Fewer than three repeated events do not trigger."""
    orders, trades, accounts = spoofing_rows(include_opposite_trades=True, spoof_count=2)

    alerts = detect_spoofing_layering(orders, trades, accounts, empty_account_links())

    assert alerts.empty


def test_detect_spoofing_layering_uses_historical_account_average() -> None:
    """Quick cancelled orders do not inflate the account average used for detection."""
    orders, trades, accounts = spoofing_rows(include_opposite_trades=True)
    orders.loc[orders["order_id"].str.startswith("SPOOF_"), "quantity"] = 10.0

    alerts = detect_spoofing_layering(orders, trades, accounts, empty_account_links())

    assert len(alerts) == 1
    assert alerts.iloc[0]["account_id"] == "ACC_S"


def test_detect_spoofing_layering_confirms_linked_counterparty() -> None:
    """A linked opposite-side counterparty raises severity above the unlinked baseline."""
    orders, trades, accounts = spoofing_rows(include_opposite_trades=True)
    links = pd.DataFrame(
        [
            {
                "link_id": 1,
                "account_id_a": "ACC_S",
                "account_id_b": "ACC_M",
                "link_type": "beneficial_ownership",
                "confidence": 0.90,
            }
        ]
    )

    alerts = detect_spoofing_layering(orders, trades, accounts, links)

    assert len(alerts) == 1
    alert = alerts.iloc[0]
    assert alert["severity_score"] == 50
    assert alert["severity"] == "Medium"
    linked_evidence = next(
        item for item in alert["evidence"] if item["metric_name"] == "linked_counterparty_confirmed"
    )
    assert linked_evidence["metric_value"] == 1.0


def wash_trade_rows(trade_count: int = 6, round_trip: bool = True) -> pd.DataFrame:
    """Return deterministic round-trip trades for a linked account pair."""
    start = datetime(2026, 5, 23, tzinfo=UTC)
    rows = []
    for index in range(trade_count):
        buyer, seller = (
            ("ACC_A", "ACC_B")
            if not round_trip or index % 2 == 0
            else ("ACC_B", "ACC_A")
        )
        rows.append(
            {
                "trade_id": f"TRD_{index}",
                "timestamp": (start + timedelta(hours=index)).isoformat(),
                "symbol": "ETH/USDT",
                "buyer_account_id": buyer,
                "seller_account_id": seller,
                "price": 1000.0,
                "quantity": 10.0,
                "notional_value": 10_000.0,
            }
        )
    return pd.DataFrame(rows)


def spoofing_rows(
    include_opposite_trades: bool,
    spoof_count: int = 3,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Return deterministic orders/trades for spoofing tests."""
    start = datetime(2026, 5, 23, tzinfo=UTC)
    orders = []
    trades = []
    for index in range(10):
        timestamp = start - timedelta(hours=10 - index)
        orders.append(
            {
                "order_id": f"BASE_{index}",
                "timestamp": timestamp.isoformat(),
                "account_id": "ACC_S",
                "symbol": "BTC/USDT",
                "side": "buy",
                "price": 100.0,
                "quantity": 1.0,
                "status": "filled",
                "submitted_at": timestamp.isoformat(),
                "cancelled_at": "",
                "filled_at": timestamp.isoformat(),
            }
        )
    for index in range(spoof_count):
        timestamp = start + timedelta(minutes=index)
        cancel_time = timestamp + timedelta(seconds=30)
        orders.append(
            {
                "order_id": f"SPOOF_{index}",
                "timestamp": timestamp.isoformat(),
                "account_id": "ACC_S",
                "symbol": "BTC/USDT",
                "side": "sell",
                "price": 100.0,
                "quantity": 200.0,
                "status": "cancelled",
                "submitted_at": timestamp.isoformat(),
                "cancelled_at": cancel_time.isoformat(),
                "filled_at": "",
            }
        )
        if include_opposite_trades:
            trades.append(
                {
                    "trade_id": f"TRD_{index}",
                    "timestamp": (cancel_time + timedelta(seconds=60)).isoformat(),
                    "symbol": "BTC/USDT",
                    "buyer_account_id": "ACC_S",
                    "seller_account_id": "ACC_M",
                    "price": 100.0,
                    "quantity": 1.0,
                    "notional_value": 100.0,
                }
            )
    if not trades:
        trades.append(
            {
                "trade_id": "TRD_NONE",
                "timestamp": (start + timedelta(hours=1)).isoformat(),
                "symbol": "BTC/USDT",
                "buyer_account_id": "ACC_X",
                "seller_account_id": "ACC_M",
                "price": 100.0,
                "quantity": 1.0,
                "notional_value": 100.0,
            }
        )
    accounts = pd.DataFrame(
        [
            {"account_id": "ACC_S", "avg_daily_volume": 100_000.0},
            {"account_id": "ACC_M", "avg_daily_volume": 100_000.0},
        ]
    )
    return pd.DataFrame(orders), pd.DataFrame(trades), accounts
