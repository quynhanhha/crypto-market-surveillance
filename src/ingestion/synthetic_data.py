"""Deterministic synthetic account, order, and trade generation."""

from __future__ import annotations

import argparse
import importlib
import logging
import random
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

thresholds = importlib.import_module("src.config.thresholds")
ACCOUNT_COUNTS = thresholds.ACCOUNT_COUNTS
BASELINE_CANCELLED_ORDER_COUNT = thresholds.BASELINE_CANCELLED_ORDER_COUNT
BASELINE_TRADE_COUNT = thresholds.BASELINE_TRADE_COUNT
CANCEL_RATE_RANGES = thresholds.CANCEL_RATE_RANGES
FILL_RATE_RANGES = thresholds.FILL_RATE_RANGES
GENERATION_DAYS = thresholds.GENERATION_DAYS
GENERATION_END = thresholds.GENERATION_END
LINK_CONFIDENCE_RANGES = thresholds.LINK_CONFIDENCE_RANGES
ORDER_SIZE_PARAMS = thresholds.ORDER_SIZE_PARAMS
PUMP_BURST_COUNT = thresholds.PUMP_BURST_COUNT
RANDOM_SEED = thresholds.RANDOM_SEED
SPOOF_EVENT_COUNT = thresholds.SPOOF_EVENT_COUNT
SYMBOL_BASE_PRICES = thresholds.SYMBOL_BASE_PRICES
WASH_ROUND_TRIPS_PER_PAIR = thresholds.WASH_ROUND_TRIPS_PER_PAIR

_logging_utils = importlib.import_module("src.utils.logging")
configure_logging = _logging_utils.configure_logging

LOGGER = logging.getLogger(__name__)

DATA_DIR = PROJECT_ROOT / "data"
SCENARIO_MARKET_MAKER = "ACC_0061"
WASH_PAIRS = (("ACC_0076", "ACC_0077"), ("ACC_0078", "ACC_0079"))
SPOOF_ACCOUNT = "ACC_0080"
PUMP_ACCOUNTS = ("ACC_0080", "ACC_0081", "ACC_0082", "ACC_0083")


@dataclass
class IdCounters:
    """Sequential IDs for generated order and trade rows."""

    order: int = 1
    trade: int = 1

    def next_order(self) -> str:
        order_id = f"ORD_{self.order:06d}"
        self.order += 1
        return order_id

    def next_trade(self) -> str:
        trade_id = f"TRD_{self.trade:06d}"
        self.trade += 1
        return trade_id


def iso(value: datetime) -> str:
    """Return a UTC ISO timestamp with second precision."""
    return value.astimezone(UTC).replace(microsecond=0).isoformat()


def weighted_symbol(rng: np.random.Generator) -> str:
    """Choose a synthetic trading symbol."""
    return str(rng.choice(list(SYMBOL_BASE_PRICES), p=[0.35, 0.30, 0.20, 0.15]))


def account_size(account_type: str, rng: np.random.Generator) -> float:
    """Generate an account-type-aware order quantity."""
    params = ORDER_SIZE_PARAMS.get(account_type, ORDER_SIZE_PARAMS["active_retail"])
    return round(float(rng.lognormal(params["mu"], params["sigma"])), 6)


def price_for(symbol: str, timestamp: datetime, rng: np.random.Generator) -> float:
    """Generate a deterministic price around a synthetic baseline."""
    day_offset = (timestamp - (GENERATION_END - timedelta(days=GENERATION_DAYS))).days
    drift = 1.0 + (day_offset * 0.001)
    noise = float(rng.normal(0, 0.003))
    return round(SYMBOL_BASE_PRICES[symbol] * drift * (1.0 + noise), 6)


def clustered_timestamp(rng: np.random.Generator) -> datetime:
    """Generate a timestamp using the Section 5 activity mixture model."""
    start = GENERATION_END - timedelta(days=GENERATION_DAYS)
    day = start + timedelta(days=int(rng.integers(0, GENERATION_DAYS)))
    bucket = float(rng.random())
    if bucket < 0.30:
        hour = truncated_normal_hour(rng, mean_hour=10.5)
    elif bucket < 0.60:
        hour = truncated_normal_hour(rng, mean_hour=16.0)
    elif bucket < 0.90:
        hour = float(rng.uniform(6, 22))
    else:
        hour = float(rng.choice([rng.uniform(0, 6), rng.uniform(22, 24)]))
    seconds = int(hour * 3600)
    return day + timedelta(seconds=seconds)


def truncated_normal_hour(rng: np.random.Generator, mean_hour: float) -> float:
    """Jitter around an active window midpoint with a 45 minute std dev."""
    while True:
        hour = float(rng.normal(mean_hour, 0.75))
        if 0 <= hour < 24:
            return hour


def generate_accounts(rng: np.random.Generator) -> pd.DataFrame:
    """Generate deterministic account profiles."""
    accounts: list[dict[str, Any]] = []
    account_number = 1
    for account_type, count in ACCOUNT_COUNTS.items():
        for _ in range(count):
            account_id = f"ACC_{account_number:04d}"
            account_number += 1
            avg_daily_volume = avg_daily_volume_for(account_type, rng)
            accounts.append(
                {
                    "account_id": account_id,
                    "account_type": account_type,
                    "created_at": iso(
                        GENERATION_END
                        - timedelta(days=int(rng.integers(30, 18 * 30)))
                    ),
                    "risk_tier": risk_tier_for(account_type, rng),
                    "jurisdiction": str(rng.choice(["Region_A", "Region_B", "Region_C"])),
                    "avg_daily_volume": round(avg_daily_volume, 2),
                }
            )
    return pd.DataFrame(accounts)


def avg_daily_volume_for(account_type: str, rng: np.random.Generator) -> float:
    """Generate a baseline daily notional volume by account type."""
    ranges = {
        "retail": (5_000, 40_000),
        "active_retail": (40_000, 180_000),
        "market_maker": (250_000, 900_000),
        "institutional": (500_000, 2_500_000),
        "suspicious": (80_000, 350_000),
    }
    low, high = ranges[account_type]
    return float(rng.uniform(low, high))


def risk_tier_for(account_type: str, rng: np.random.Generator) -> str:
    """Assign risk tiers using Section 5 account-type probabilities."""
    if account_type == "retail":
        return str(rng.choice(["Low", "Medium"], p=[0.80, 0.20]))
    if account_type == "active_retail":
        return str(rng.choice(["Medium", "High"], p=[0.60, 0.40]))
    if account_type == "market_maker":
        return "Medium"
    if account_type == "institutional":
        return str(rng.choice(["Low", "Medium"], p=[0.70, 0.30]))
    return "High"


def generate_account_links(rng: np.random.Generator) -> pd.DataFrame:
    """Generate suspicious and non-suspicious account links."""
    links: list[dict[str, Any]] = []
    link_id = 1

    def add(a: str, b: str, link_type: str, minimum: float | None = None) -> None:
        nonlocal link_id
        low, high = LINK_CONFIDENCE_RANGES[link_type]
        confidence = max(low, minimum or low) + float(rng.random()) * (high - max(low, minimum or low))
        links.append(
            {
                "link_id": link_id,
                "account_id_a": min(a, b),
                "account_id_b": max(a, b),
                "link_type": link_type,
                "confidence": round(confidence, 4),
            }
        )
        link_id += 1

    for account_a, account_b in WASH_PAIRS:
        add(account_a, account_b, "beneficial_ownership", minimum=0.90)

    for index, account_a in enumerate(PUMP_ACCOUNTS):
        for account_b in PUMP_ACCOUNTS[index + 1 :]:
            add(account_a, account_b, "coordinated_timing", minimum=0.78)

    add(SPOOF_ACCOUNT, "ACC_0081", "historical_pattern", minimum=0.62)

    normal_pairs = [
        ("ACC_0003", "ACC_0014", "shared_infrastructure"),
        ("ACC_0008", "ACC_0031", "historical_pattern"),
        ("ACC_0011", "ACC_0044", "shared_infrastructure"),
        ("ACC_0018", "ACC_0052", "coordinated_timing"),
        ("ACC_0022", "ACC_0063", "historical_pattern"),
        ("ACC_0029", "ACC_0068", "shared_infrastructure"),
        ("ACC_0035", "ACC_0059", "coordinated_timing"),
        ("ACC_0041", "ACC_0065", "historical_pattern"),
        ("ACC_0048", "ACC_0071", "shared_infrastructure"),
        ("ACC_0055", "ACC_0074", "historical_pattern"),
        ("ACC_0006", "ACC_0026", "shared_infrastructure"),
        ("ACC_0017", "ACC_0037", "coordinated_timing"),
        ("ACC_0024", "ACC_0049", "historical_pattern"),
        ("ACC_0030", "ACC_0069", "shared_infrastructure"),
        ("ACC_0039", "ACC_0058", "historical_pattern"),
        ("ACC_0045", "ACC_0072", "coordinated_timing"),
    ]
    for account_a, account_b, link_type in normal_pairs:
        add(account_a, account_b, link_type)

    return pd.DataFrame(links)


def generate_baseline_activity(
    accounts: pd.DataFrame, rng: np.random.Generator, counters: IdCounters
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Generate baseline filled/cancelled orders and matched trades."""
    orders: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    market_makers = account_ids(accounts, "market_maker")

    for _ in range(BASELINE_TRADE_COUNT):
        timestamp = clustered_timestamp(rng)
        symbol = weighted_symbol(rng)
        price = price_for(symbol, timestamp, rng)
        buyer = choose_account(accounts, rng, exclude=(), rate_ranges=FILL_RATE_RANGES)
        seller = choose_account(accounts, rng, exclude=(buyer,), rate_ranges=FILL_RATE_RANGES)
        if rng.random() < 0.35:
            seller = str(rng.choice(market_makers))
        quantity = account_size(account_type(accounts, buyer), rng)
        orders.append(filled_order(counters, buyer, symbol, "buy", price, quantity, timestamp))
        orders.append(filled_order(counters, seller, symbol, "sell", price, quantity, timestamp))
        trades.append(
            trade_row(counters, timestamp, symbol, buyer, seller, price, quantity, "baseline")
        )

    for _ in range(BASELINE_CANCELLED_ORDER_COUNT):
        timestamp = clustered_timestamp(rng)
        account_id = choose_account(accounts, rng, exclude=(), rate_ranges=CANCEL_RATE_RANGES)
        symbol = weighted_symbol(rng)
        quantity = account_size(account_type(accounts, account_id), rng)
        price = price_for(symbol, timestamp, rng)
        orders.append(
            cancelled_order(
                counters,
                account_id,
                symbol,
                str(rng.choice(["buy", "sell"])),
                price,
                quantity,
                timestamp,
                int(rng.integers(15, 600)),
                "baseline_cancel",
            )
        )

    return orders, trades


def choose_account(
    accounts: pd.DataFrame,
    rng: np.random.Generator,
    exclude: Iterable[str],
    rate_ranges: dict[str, tuple[float, float]] | None = None,
) -> str:
    """Choose an account with broad account-type activity weights."""
    excluded = set(exclude)
    eligible = accounts[~accounts["account_id"].isin(excluded)]
    activity_weights = eligible["account_type"].map(
        {
            "retail": 1.0,
            "active_retail": 2.0,
            "market_maker": 3.0,
            "institutional": 0.5,
            "suspicious": 1.3,
        }
    )
    if rate_ranges is None:
        weights = activity_weights
    else:
        rate_weights = eligible["account_type"].map(
            lambda value: sum(rate_ranges[str(value)]) / 2
        )
        weights = activity_weights * rate_weights
    probabilities = weights / weights.sum()
    return str(rng.choice(eligible["account_id"].to_numpy(), p=probabilities.to_numpy()))


def account_ids(accounts: pd.DataFrame, account_type_value: str) -> list[str]:
    """Return account IDs for an account type."""
    return accounts.loc[accounts["account_type"] == account_type_value, "account_id"].tolist()


def account_type(accounts: pd.DataFrame, account_id: str) -> str:
    """Return account type for one account."""
    row = accounts.loc[accounts["account_id"] == account_id].iloc[0]
    return str(row["account_type"])


def filled_order(
    counters: IdCounters,
    account_id: str,
    symbol: str,
    side: str,
    price: float,
    quantity: float,
    timestamp: datetime,
    scenario: str = "baseline",
) -> dict[str, Any]:
    """Create a filled synthetic order row."""
    return {
        "order_id": counters.next_order(),
        "timestamp": iso(timestamp),
        "account_id": account_id,
        "symbol": symbol,
        "side": side,
        "price": round(price, 6),
        "quantity": round(quantity, 6),
        "status": "filled",
        "submitted_at": iso(timestamp - timedelta(seconds=10)),
        "cancelled_at": "",
        "filled_at": iso(timestamp),
    }


def cancelled_order(
    counters: IdCounters,
    account_id: str,
    symbol: str,
    side: str,
    price: float,
    quantity: float,
    timestamp: datetime,
    cancel_seconds: int,
    scenario: str,
) -> dict[str, Any]:
    """Create a cancelled synthetic order row."""
    return {
        "order_id": counters.next_order(),
        "timestamp": iso(timestamp),
        "account_id": account_id,
        "symbol": symbol,
        "side": side,
        "price": round(price, 6),
        "quantity": round(quantity, 6),
        "status": "cancelled",
        "submitted_at": iso(timestamp),
        "cancelled_at": iso(timestamp + timedelta(seconds=cancel_seconds)),
        "filled_at": "",
    }


def trade_row(
    counters: IdCounters,
    timestamp: datetime,
    symbol: str,
    buyer: str,
    seller: str,
    price: float,
    quantity: float,
    scenario: str,
) -> dict[str, Any]:
    """Create a synthetic trade row."""
    return {
        "trade_id": counters.next_trade(),
        "timestamp": iso(timestamp),
        "symbol": symbol,
        "buyer_account_id": buyer,
        "seller_account_id": seller,
        "price": round(price, 6),
        "quantity": round(quantity, 6),
        "notional_value": round(price * quantity, 2),
    }


def inject_wash_trading(
    orders: list[dict[str, Any]], trades: list[dict[str, Any]], counters: IdCounters
) -> None:
    """Inject beneficial-ownership wash trading pairs."""
    symbol = "ETH/USDT"
    base_time = GENERATION_END - timedelta(days=5)
    local_rng = np.random.default_rng(RANDOM_SEED + 10)
    py_rng = random.Random(RANDOM_SEED + 10)

    for pair_index, (account_a, account_b) in enumerate(WASH_PAIRS):
        for cycle in range(WASH_ROUND_TRIPS_PER_PAIR):
            if py_rng.random() < 0.20:
                continue
            timestamp = base_time + timedelta(
                hours=pair_index * 8 + cycle * 4,
                seconds=int(local_rng.integers(15, 600)),
            )
            price = price_for(symbol, timestamp, local_rng)
            quantity = round(8.0 * (1.0 + float(local_rng.uniform(-0.08, 0.08))), 6)
            reverse_quantity = round(quantity * float(local_rng.uniform(0.95, 1.03)), 6)
            reverse_time = timestamp + timedelta(seconds=int(local_rng.integers(25, 600)))
            orders.append(filled_order(counters, account_a, symbol, "buy", price, quantity, timestamp, "wash_trading"))
            orders.append(filled_order(counters, account_b, symbol, "sell", price, quantity, timestamp, "wash_trading"))
            trades.append(trade_row(counters, timestamp, symbol, account_a, account_b, price, quantity, "wash_trading"))
            orders.append(filled_order(counters, account_b, symbol, "buy", price, reverse_quantity, reverse_time, "wash_trading"))
            orders.append(filled_order(counters, account_a, symbol, "sell", price, reverse_quantity, reverse_time, "wash_trading"))
            trades.append(trade_row(counters, reverse_time, symbol, account_b, account_a, price, reverse_quantity, "wash_trading"))


def inject_spoofing_layering(
    accounts: pd.DataFrame,
    orders: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    counters: IdCounters,
) -> None:
    """Inject repeated large cancelled orders and opposite-side trades."""
    symbol = "BTC/USDT"
    local_rng = np.random.default_rng(RANDOM_SEED + 20)
    base_time = GENERATION_END - timedelta(days=3, hours=6)
    avg_order_notional = historical_average_order_notional(orders, SPOOF_ACCOUNT)
    event_offsets = sorted(int(offset) for offset in local_rng.choice(range(0, 361), size=SPOOF_EVENT_COUNT, replace=False))

    for event_index in range(SPOOF_EVENT_COUNT):
        timestamp = base_time + timedelta(minutes=event_offsets[event_index])
        price = price_for(symbol, timestamp, local_rng)
        large_notional = avg_order_notional * float(local_rng.uniform(4.8, 7.8))
        large_quantity = large_notional / price
        side = "sell" if event_index % 2 == 0 else "buy"
        opposite_side = "buy" if side == "sell" else "sell"
        cancel_seconds = int(local_rng.choice([18, 24, 33, 41]))
        for layer in range(3):
            orders.append(
                cancelled_order(
                    counters,
                    SPOOF_ACCOUNT,
                    symbol,
                    side,
                    price * (1.0 + layer * 0.001),
                    large_quantity * (1.0 - layer * 0.05),
                    timestamp + timedelta(seconds=layer * 6),
                    cancel_seconds + layer * 4,
                    "spoofing_layering",
                )
            )
        trade_time = timestamp + timedelta(seconds=cancel_seconds + int(local_rng.integers(60, 160)))
        trade_quantity = round((avg_order_notional / price) * float(local_rng.uniform(0.8, 1.4)), 6)
        market_maker_side = "sell" if opposite_side == "buy" else "buy"
        orders.append(filled_order(counters, SPOOF_ACCOUNT, symbol, opposite_side, price, trade_quantity, trade_time, "spoofing_layering"))
        orders.append(filled_order(counters, SCENARIO_MARKET_MAKER, symbol, market_maker_side, price, trade_quantity, trade_time, "spoofing_layering"))
        buyer, seller = (
            (SPOOF_ACCOUNT, SCENARIO_MARKET_MAKER)
            if opposite_side == "buy"
            else (SCENARIO_MARKET_MAKER, SPOOF_ACCOUNT)
        )
        trades.append(trade_row(counters, trade_time, symbol, buyer, seller, price, trade_quantity, "spoofing_layering"))


def historical_average_order_notional(orders: Iterable[dict[str, Any]], account_id: str) -> float:
    """Return an account's average notional from already-generated history."""
    notionals = [
        float(order["price"]) * float(order["quantity"])
        for order in orders
        if order["account_id"] == account_id
    ]
    if not notionals:
        raise ValueError(f"No historical synthetic orders for account: {account_id}")
    return float(np.mean(notionals))


def inject_pump_pressure(
    accounts: pd.DataFrame,
    orders: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    counters: IdCounters,
) -> None:
    """Inject coordinated buy bursts."""
    symbol = "SOL/USDT"
    local_rng = np.random.default_rng(RANDOM_SEED + 30)
    base_time = GENERATION_END - timedelta(days=2, hours=12)

    for burst_index in range(PUMP_BURST_COUNT):
        burst_start = base_time + timedelta(hours=burst_index * 3)
        for account_id in PUMP_ACCOUNTS:
            offset = int(local_rng.integers(0, 90))
            timestamp = burst_start + timedelta(seconds=offset)
            price = price_for(symbol, timestamp, local_rng)
            avg_daily_volume = float(
                accounts.loc[accounts["account_id"] == account_id, "avg_daily_volume"].iloc[0]
            )
            baseline_quantity = (avg_daily_volume / 35) / SYMBOL_BASE_PRICES[symbol]
            quantity = round(baseline_quantity * float(local_rng.uniform(2.0, 3.0)), 6)
            orders.append(filled_order(counters, account_id, symbol, "buy", price, quantity, timestamp, "pump_pressure"))
            orders.append(filled_order(counters, SCENARIO_MARKET_MAKER, symbol, "sell", price, quantity, timestamp, "pump_pressure"))
            trades.append(trade_row(counters, timestamp, symbol, account_id, SCENARIO_MARKET_MAKER, price, quantity, "pump_pressure"))


def generate_synthetic_dataset(seed: int = RANDOM_SEED) -> dict[str, pd.DataFrame]:
    """Generate all deterministic synthetic sample tables."""
    random.seed(seed)
    np.random.seed(seed)
    rng = np.random.default_rng(seed)
    counters = IdCounters()

    accounts = generate_accounts(rng)
    account_links = generate_account_links(rng)
    orders, trades = generate_baseline_activity(accounts, rng, counters)
    inject_wash_trading(orders, trades, counters)
    inject_spoofing_layering(accounts, orders, trades, counters)
    inject_pump_pressure(accounts, orders, trades, counters)

    return {
        "accounts": accounts.sort_values("account_id").reset_index(drop=True),
        "account_links": account_links.sort_values("link_id").reset_index(drop=True),
        "synthetic_orders": pd.DataFrame(orders).sort_values("order_id").reset_index(drop=True),
        "synthetic_trades": pd.DataFrame(trades).sort_values("trade_id").reset_index(drop=True),
    }


def export_synthetic_data(output_dir: Path = DATA_DIR, seed: int = RANDOM_SEED) -> dict[str, Path]:
    """Export deterministic synthetic tables to CSV files."""
    from src.ingestion.fetch_market_data import export_sample_market_candles

    output_dir.mkdir(parents=True, exist_ok=True)
    dataset = generate_synthetic_dataset(seed=seed)
    paths = {
        "accounts": output_dir / "sample_accounts.csv",
        "account_links": output_dir / "sample_account_links.csv",
        "synthetic_orders": output_dir / "sample_synthetic_orders.csv",
        "synthetic_trades": output_dir / "sample_synthetic_trades.csv",
    }
    for table_name, path in paths.items():
        dataset[table_name].to_csv(path, index=False)
    paths["market_candles"] = export_sample_market_candles(
        output_path=output_dir / "sample_market_candles.csv",
        seed=seed,
    )
    return paths


def main() -> None:
    """Run the synthetic data CLI."""
    configure_logging()
    parser = argparse.ArgumentParser(description="Generate deterministic synthetic data.")
    parser.add_argument("--export", action="store_true", help="Export generated CSV files.")
    parser.add_argument("--seed", type=int, default=RANDOM_SEED, help="Deterministic seed.")
    args = parser.parse_args()
    if args.export:
        paths = export_synthetic_data(seed=args.seed)
        for path in paths.values():
            LOGGER.info("Exported %s", path)
    else:
        dataset = generate_synthetic_dataset(seed=args.seed)
        for table_name, frame in dataset.items():
            LOGGER.info("%s: %d rows", table_name, len(frame))


if __name__ == "__main__":
    main()
