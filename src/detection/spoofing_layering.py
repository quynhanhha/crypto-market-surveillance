"""Synthetic spoofing and layering detection."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pandas as pd

from src.config.thresholds import (
    LARGE_ORDER_MULTIPLIER,
    LINK_CONFIDENCE_THRESHOLD,
    MAX_CANCEL_SECONDS,
    MIN_NOTIONAL,
    MIN_REPEATED_EVENTS,
    OPPOSITE_TRADE_WINDOW_SECONDS,
)
from src.detection.severity import (
    make_dedup_key,
    severity_from_score,
    spoofing_layering_score,
)


ALERT_TYPE = "Synthetic Spoofing/Layering Pattern"
RECOMMENDED_FOLLOW_UP = "Review order-book intent, cancellations, and post-cancel executions."


def detect_spoofing_layering(
    synthetic_orders: pd.DataFrame,
    synthetic_trades: pd.DataFrame,
    accounts: pd.DataFrame,
    account_links: pd.DataFrame,
) -> pd.DataFrame:
    """Detect repeated large fast cancellations followed by opposite-side trades."""
    required_order_columns = {
        "timestamp",
        "account_id",
        "symbol",
        "side",
        "price",
        "quantity",
        "status",
        "submitted_at",
        "cancelled_at",
    }
    missing_order_columns = required_order_columns.difference(synthetic_orders.columns)
    if missing_order_columns:
        raise ValueError(f"Synthetic orders missing columns: {sorted(missing_order_columns)}")
    required_trade_columns = {
        "timestamp",
        "symbol",
        "buyer_account_id",
        "seller_account_id",
    }
    missing_trade_columns = required_trade_columns.difference(synthetic_trades.columns)
    if missing_trade_columns:
        raise ValueError(f"Synthetic trades missing columns: {sorted(missing_trade_columns)}")
    if "account_id" not in accounts.columns:
        raise ValueError("Accounts missing columns: ['account_id']")
    required_link_columns = {"account_id_a", "account_id_b", "link_type", "confidence"}
    missing_link_columns = required_link_columns.difference(account_links.columns)
    if missing_link_columns:
        raise ValueError(f"Account links missing columns: {sorted(missing_link_columns)}")
    if synthetic_orders.empty or synthetic_trades.empty or accounts.empty:
        return _empty_alerts()

    orders = synthetic_orders.copy()
    orders["timestamp"] = pd.to_datetime(orders["timestamp"], utc=True)
    orders["submitted_at"] = pd.to_datetime(orders["submitted_at"], utc=True)
    orders["cancelled_at"] = pd.to_datetime(orders["cancelled_at"], utc=True, errors="coerce")
    orders["price"] = pd.to_numeric(orders["price"], errors="coerce")
    orders["quantity"] = pd.to_numeric(orders["quantity"], errors="coerce")
    orders["notional_value"] = orders["price"] * orders["quantity"]
    orders["cancel_seconds"] = (orders["cancelled_at"] - orders["submitted_at"]).dt.total_seconds()
    quick_cancelled = (
        (orders["status"] == "cancelled")
        & orders["cancelled_at"].notna()
        & (orders["cancel_seconds"] <= MAX_CANCEL_SECONDS)
    )
    historical_average = (
        orders.loc[~quick_cancelled].groupby("account_id")["notional_value"].mean()
    )
    orders["avg_account_order_notional"] = orders["account_id"].map(historical_average)

    trades = synthetic_trades.copy()
    trades["timestamp"] = pd.to_datetime(trades["timestamp"], utc=True)

    cancelled = orders[
        quick_cancelled
        & orders["avg_account_order_notional"].notna()
        & (orders["notional_value"] >= orders["avg_account_order_notional"] * LARGE_ORDER_MULTIPLIER)
    ].copy()
    if cancelled.empty:
        return _empty_alerts()

    events = [
        event
        for _, order in cancelled.iterrows()
        if (event := _event_if_opposite_trade(order, trades)) is not None
    ]
    if not events:
        return _empty_alerts()

    events_frame = pd.DataFrame(events)
    linked_pairs = _qualifying_linked_pairs(account_links)
    alerts = [
        _alert_from_events(account_id, symbol, group, linked_pairs)
        for (account_id, symbol), group in events_frame.groupby(["account_id", "symbol"], sort=False)
        if len(group) >= MIN_REPEATED_EVENTS
    ]
    return pd.DataFrame(alerts)


def _qualifying_linked_pairs(account_links: pd.DataFrame) -> set[tuple[str, str]]:
    """Return normalized (account_a, account_b) pairs linked at or above the confidence threshold."""
    if account_links.empty:
        return set()
    qualifying = account_links[account_links["confidence"] >= LINK_CONFIDENCE_THRESHOLD]
    return {
        tuple(sorted((str(row["account_id_a"]), str(row["account_id_b"]))))
        for _, row in qualifying.iterrows()
    }


def _event_if_opposite_trade(order: pd.Series, trades: pd.DataFrame) -> dict[str, Any] | None:
    account_id = str(order["account_id"])
    symbol = str(order["symbol"])
    cancel_time = order["cancelled_at"]
    window_end = cancel_time + timedelta(seconds=OPPOSITE_TRADE_WINDOW_SECONDS)
    symbol_trades = trades[
        (trades["symbol"] == symbol)
        & (trades["timestamp"] >= cancel_time)
        & (trades["timestamp"] <= window_end)
    ]
    if str(order["side"]) == "sell":
        opposite_trades = symbol_trades[symbol_trades["buyer_account_id"] == account_id]
    else:
        opposite_trades = symbol_trades[symbol_trades["seller_account_id"] == account_id]
    if opposite_trades.empty:
        return None
    first_trade = opposite_trades.sort_values("timestamp").iloc[0]
    counterparty_account_id = (
        str(first_trade["seller_account_id"])
        if str(order["side"]) == "sell"
        else str(first_trade["buyer_account_id"])
    )
    return {
        "account_id": account_id,
        "symbol": symbol,
        "order_time": order["timestamp"],
        "cancel_time": cancel_time,
        "trade_time": first_trade["timestamp"],
        "cancel_seconds": float(order["cancel_seconds"]),
        "notional_value": float(order["notional_value"]),
        "counterparty_account_id": counterparty_account_id,
    }


def _alert_from_events(
    account_id: str,
    symbol: str,
    events: pd.DataFrame,
    linked_pairs: set[tuple[str, str]],
) -> dict[str, Any]:
    start_time = events["order_time"].min().isoformat()
    end_time = events["trade_time"].max().isoformat()
    repeated_count = int(len(events))
    total_notional = float(events["notional_value"].sum())
    average_cancel_seconds = float(events["cancel_seconds"].mean())
    linked_coordination_confirmed = any(
        tuple(sorted((account_id, str(counterparty)))) in linked_pairs
        for counterparty in events["counterparty_account_id"]
    )
    score = spoofing_layering_score(
        repeated_count=repeated_count,
        linked_coordination_confirmed=linked_coordination_confirmed,
        high_notional_confirmed=total_notional >= MIN_NOTIONAL,
    )
    severity = severity_from_score(score)
    return {
        "alert_type": ALERT_TYPE,
        "severity": severity,
        "severity_score": score,
        "exchange": None,
        "symbol": symbol,
        "start_time": start_time,
        "end_time": end_time,
        "account_id": account_id,
        "evidence_summary": (
            f"{account_id} had {repeated_count} large fast cancellations on {symbol} "
            f"followed by opposite-side trades."
        ),
        "recommended_follow_up": RECOMMENDED_FOLLOW_UP,
        "dedup_key": make_dedup_key(ALERT_TYPE, symbol, start_time, end_time, account_id),
        "created_at": end_time,
        "evidence": [
            _evidence(
                "large_cancelled_order_count",
                repeated_count,
                MIN_REPEATED_EVENTS,
                ">=",
                "Large fast cancellations with opposite-side trades.",
            ),
            _evidence(
                "average_cancel_seconds",
                average_cancel_seconds,
                MAX_CANCEL_SECONDS,
                "<=",
                "Average time from submit to cancellation.",
            ),
            _evidence(
                "opposite_side_trade_count",
                repeated_count,
                MIN_REPEATED_EVENTS,
                ">=",
                "Opposite-side trades after cancellation.",
            ),
            _evidence("notional_value", total_notional, MIN_NOTIONAL, ">=", "Total cancelled order notional."),
            _evidence(
                "linked_counterparty_confirmed",
                1.0 if linked_coordination_confirmed else 0.0,
                None,
                None,
                f"Opposite-side counterparty is a linked account (confidence >= {LINK_CONFIDENCE_THRESHOLD}).",
            ),
        ],
    }


def _evidence(
    metric_name: str,
    metric_value: object,
    threshold_value: object,
    comparison_operator: str | None,
    explanation: str,
) -> dict[str, Any]:
    return {
        "metric_name": metric_name,
        "metric_value": float(metric_value) if pd.notna(metric_value) else None,
        "threshold_value": float(threshold_value) if threshold_value is not None else None,
        "comparison_operator": comparison_operator,
        "explanation": explanation,
    }


def _empty_alerts() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "alert_type",
            "severity",
            "severity_score",
            "exchange",
            "symbol",
            "start_time",
            "end_time",
            "account_id",
            "evidence_summary",
            "recommended_follow_up",
            "dedup_key",
            "created_at",
            "evidence",
        ]
    )
