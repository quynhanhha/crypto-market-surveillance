# Limitations

This is a surveillance analytics prototype, not a production control room, a trading bot, or an "AI fraud detection" system. This document collects what it does not do, in three groups: stated project-level limitations, per-rule detection gaps, and things observed in the code that aren't documented elsewhere.

## Project-level limitations

- Public market data cannot prove account ownership, coordination, or intent — it can only flag statistically unusual behavior. Alerts are surveillance leads, not accusations.
- Synthetic account activity is deliberately realistic but does not represent real accounts or transactions.
- Spoofing/Layering severity is capped at Low because `linked_coordination_confirmed` is hardcoded `False` — coordination between accounts is not modeled in the current synthetic dataset. Wiring this to `account_links` would unlock High-severity spoofing alerts.
- SQLite is appropriate for local and single-session demo use, not for multi-user or persistent production deployments (see `docs/architecture.md` for why SQLite was chosen).
- The fallback data path makes no distinction between transient API failures and permanent unavailability — there is no retry or backoff logic.
- API availability and symbol coverage depend entirely on the external CCXT exchange source (Coinbase by default).

## Per-rule detection gaps

Each detection rule is a fixed statistical threshold, not a learning model, and each has known blind spots:

| Rule | What it misses |
|---|---|
| Volume Spike | A sustained 2× volume lift that never produces a single-candle spike (e.g. chronic front-running); cross-asset volume divergence. |
| Price Anomaly | Slow drift that never exceeds 3σ on any single bar (e.g. 1% per candle for 20 candles); cross-symbol price dislocation. |
| Pump-and-Dump | Pumps that take longer than 3 candles to peak; dumps delayed beyond 6 candles after the peak. |
| Wash Trading | Wash trading routed through an account pair not present in `account_links`; a new account with no link record. |
| Spoofing/Layering | Cancels slower than 60 seconds; an adversary who routes the opposite trade through a linked account rather than the same account. |

## Explicitly out of scope (V1)

Per `docs/BUILD_SPEC.md`: WebSocket/streaming ingestion, user authentication, paid API integrations, a full backend API, a React frontend, Kafka or other event queues, ML-based detection models, and any system that makes an actual manipulation accusation or acts as a production compliance system.

## Observed but not previously documented

These aren't called out elsewhere in the docs, but are visible in the code and worth stating plainly:

- There is no authentication or authorization anywhere in `app.py` or `src/ui/` — anyone who can reach the app can view alerts and change their status.
- The app holds a single SQLite connection in `st.session_state` (`app.py`) — this is a single-user, single-session model with no multi-tenant isolation.
- `src/config/settings.py` is an empty placeholder — there is no environment-based configuration layer yet; all configuration is the static constants in `src/config/thresholds.py`.
- `surveillance.db` (and a dated backup file) are committed directly in the repository working tree rather than generated at runtime or excluded via `.gitignore`.
- Detection thresholds are static constants, not adaptive — there is no feedback loop from analyst dispositions back into the thresholds (tracked as future work in `README.md`).
- Market data refresh is a manual/timed poll (`_maybe_auto_refresh`, 60-second interval) via Streamlit rerun, not a real-time or streaming pipeline.
- Only one exchange (Coinbase) and three symbols (`BTC/USD`, `ETH/USD`, `SOL/USD`) are wired by default — market coverage is narrow.

## Future work

See `README.md` for the full list; the highlights are replacing SQLite with PostgreSQL for concurrent/persistent production use, wiring spoofing coordination confirmation to `account_links`, adding retry/backoff to the market data fallback, richer case management workflows, broader exchange/symbol coverage, and analyst-feedback-driven threshold tuning.
