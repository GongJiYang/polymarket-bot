# Repository Guidelines

## Project Overview

`polymarket-bot` is a Python bot for short-horizon Polymarket crypto Up/Down markets. It ingests public market/CLOB data and official Chainlink inputs, evaluates terminal-probability edge strategies, supports replay/shadow/calibration, and confines real-money execution to one bounded FAK POST path.

## Architecture & Data Flow

```text
public market + CLOB + official Chainlink
  -> adapters/public_data.py -> immutable StrategyContext
  -> StrategyEngine -> StrategyEvaluation / StrategyCandidate
  -> ReplayRunner | ShadowRunner | LiveRunner
  -> BoundedExecutionAdapter -> live/order_executor.py (sole SDK write boundary)
  -> JSONL audit + loopback dashboard
```

- `src/polymarket_bot/cli.py` is the composition root. Keep live construction, Keychain resolution, authorization, account/geoblock preflight, capacity setup, and resource lifetimes there.
- `src/polymarket_bot/runners.py` owns evaluation orchestration. `ShadowRunner` is read-only; `LiveRunner` confirms a stable signal and re-evaluates a final snapshot before execution.
- `src/polymarket_bot/adapters/` owns external boundaries. Public reads must fail closed on stale, malformed, ambiguous, inactive, or mismatched data.
- `src/polymarket_bot/live/order_executor.py` is the only production signing/POST boundary. Never add direct SDK order submission elsewhere.

## Key Directories

- `src/polymarket_bot/contracts.py` — immutable cross-layer contracts and `Protocol` dependency boundaries.
- `src/polymarket_bot/microstructure/` — frozen Pydantic market/book models and probability/volatility math.
- `src/polymarket_bot/strategies/` — pluggable strategy implementations; registration and live allowlisting are in `strategy_registry.py`.
- `src/polymarket_bot/adapters/` — public data adapters and bounded execution bridge.
- `src/polymarket_bot/live/` — authorization, credentials, account reads, capacity/debit sizing, official price stream, and SDK transport.
- `tests/` — self-contained pytest tests for safety contracts, adapters, strategy behavior, CLI, dashboard, and calibration.
- `docs/` — operational strategy/runbook material. Treat live-operation constraints there as load-bearing.

## Development Commands

```bash
uv sync --extra dev
uv run pytest
uv run pytest tests/test_terminal_strategy.py
uv run pytest tests/test_execution_adapter.py::test_adapter_freezes_intent_checks_capacity_and_posts_once
uv run polymarket-bot strategies
uv run polymarket-bot replay <input.jsonl> --strategy <id|all>
uv run polymarket-bot shadow --audit /tmp/<audit>.jsonl
uv run polymarket-bot calibrate /tmp/<audit>.jsonl
uv build
```

There is no configured formatter, linter, type checker, Makefile, task runner, CI workflow, or `scripts/` directory. Do not invent corresponding conventions.

## Code Conventions & Common Patterns

- Use `Decimal` for all prices, fees, sizes, balances, and P&L; never use `float` for financial values.
- Use timezone-aware UTC timestamps. Ingestion models enforce this; do not weaken it.
- Keep domain/state contracts immutable: frozen slot dataclasses at layer boundaries and frozen Pydantic models for external data.
- Prefer typed, boundary-specific exceptions (`PublicDataError`, `OfficialPriceUnavailable`, `ExecutionPreparationRejected`, etc.). Invalid, stale, incomplete, unauthorized, or unknown state must fail closed.
- Express non-trading strategy rejection through metrics and `candidate=None`; preserve explicit rejection receipts for pre-POST execution failures.
- Inject clocks, transports, HTTP/price clients, audit sinks, capacity budgets, and observers through constructors/protocols. Tests use local fakes and `monkeypatch`, not live services.
- Keep strategy IDs unique. A strategy must implement `evaluate(StrategyContext) -> StrategyEvaluation`, be registry-discoverable, and be in `LIVE_STRATEGY_IDS` before live mode can select it.
- Preserve final-snapshot identity checks and the shared session capacity ledger. Do not retry an unknown submit outcome or auto-approve allowance.

## Important Files

- `pyproject.toml` — Python/dependency/package/entry-point/pytest contract; keep `uv.lock` synchronized when dependencies change.
- `src/polymarket_bot/cli.py` — CLI wiring and live safety gates.
- `src/polymarket_bot/runners.py` — replay, shadow, and live lifecycle.
- `src/polymarket_bot/adapters/public_data.py` — GET-only market/book/fee/price ingestion and freshness validation.
- `src/polymarket_bot/adapters/execution.py` — final snapshot rebind and pre-POST safety checks.
- `src/polymarket_bot/live/bounded_bot.py` — authorization and bounded intent/debit mechanics.
- `src/polymarket_bot/live/order_executor.py` — protected FAK SDK transport.
- `src/polymarket_bot/live/official_chainlink.py` — official Chainlink source/config/history validation.
- `src/polymarket_bot/audit.py` — exclusive, flushed, fsync'd JSONL audit sink.

## Runtime/Tooling Preferences

- Require Python `>=3.11`; use `uv` and the committed `uv.lock`.
- Packaging uses Hatchling with the `src/` layout. Console commands are `polymarket-bot` and `polymarket-bot-eth-shadow`.
- Runtime dependencies are bounded in `pyproject.toml`, including `polymarket-client >=0.6,<0.7`; do not casually widen them.
- macOS Keychain is the only credential source. Never put private keys or relayer API keys in code, commands, Markdown, configuration, or audit JSONL.

## Testing & QA

- Pytest discovery is configured as `tests/`; use focused tests for every changed contract, then run the relevant suite.
- Tests construct deterministic UTC `StrategyContext`/book objects inline and use `Decimal`, narrow fakes, `monkeypatch`, `tmp_path`, `capsys`, `pytest.raises`, and parametrization. Match that style; there is no shared `conftest.py` fixture layer.
- Test observable safety behavior: exact rejection codes, `post_attempted`, receipt/order states, debit capacity, exact decimals, audit payloads, and stale/final-snapshot behavior.
- Live changes require coverage of authorization, freshness, allowance, capacity, confirmation, and unknown-submit behavior. Verify shadow/replay remain read-only.
- Live commands are deliberately guarded: use only with explicit operator authorization, a fresh `ARM <authorization-id>` confirmation, `--compliance-confirmed`, and `--submit`. A restart needs fresh authorization and a fresh audit file; never relax integrity/confirmation gates because signals are scarce.
