# polymarket-bot

Bounded Polymarket strategy runner for short-horizon crypto Up/Down markets.

This repository is experimental trading infrastructure. It is not financial advice, does not guarantee execution or profit, and should not be used with money that cannot be lost.

## Status

The project supports replay, shadow, and guarded live-mode plumbing. Live trading is **disabled by default** and requires explicit operator authorization, a fresh authorization ID, compliance confirmation, and `--submit`.

The BTC live path previously experienced a real post-entry lifecycle failure: an entry was confirmed, but the local position manager exited before its exit rules could run. The accounting and settlement handling have since been repaired and covered by tests, but the system has not earned a reliability or profitability claim. Treat live mode as untrusted until independently validated with shadow runs and a small, manually supervised amount.

## Features

- Chainlink BTC/USD terminal-probability strategy for five-minute Up/Down markets.
- BTC volatility stress gate using a 1.25x effective-volatility scenario.
- Public-data fail-closed checks for freshness, identity, completeness, and source consistency.
- Bounded FAK execution through the official Polymarket client.
- Explicit authorization, session debit, order debit, and POST-count limits.
- Durable wallet-level position journal with atomic writes, fsync, and process locking.
- BUY/SELL pending states and exact-order confirmed-fill reconciliation.
- Position policy: net take profit 5%, net stop loss 8%, 30-second timeout, and at most three SELL attempts.
- Replay and shadow tooling for non-submitting validation.

## Safety model

- Private keys and relayer API keys are read from macOS Keychain. They must never be committed, placed in commands, or written to Markdown or audit files.
- Unknown POST outcomes are not retried automatically.
- A fresh authorization ID and audit file are required after every restart or recovery.
- A local stop-loss is not an exchange-hosted stop order. Process failure, stale data, unavailable settlement reads, market expiry, or insufficient liquidity can prevent an exit.
- PnL estimates use conservative fee bounds. The SDK does not expose the exact fee amount for every individual trade.
- A test pass is not evidence of profitability or live reliability.

## Requirements

- macOS or another POSIX environment with file locking support.
- Python 3.11 or newer.
- `uv`.
- A Polymarket account configured according to the operator's own compliance and authorization requirements.
- `polymarket-client` 0.6.x.

## Install

```bash
uv sync --extra dev
```

Run the test suite:

```bash
uv run pytest -q
```

Build the package:

```bash
uv build
```

## Commands

List registered strategies:

```bash
uv run polymarket-bot strategies
```

Run replay or shadow workflows using their respective input and audit arguments:

```bash
uv run polymarket-bot replay <input.jsonl> --strategy <strategy-id>
uv run polymarket-bot shadow --audit /tmp/polymarket-shadow.jsonl
```

Inspect live options without starting a session:

```bash
uv run polymarket-bot live --help
```

## Live mode

Do not copy this template without replacing every placeholder and independently checking the limits:

```bash
uv run polymarket-bot live \
  --strategy chainlink_terminal_spot_v9 \
  --wallet <deposit-wallet> \
  --relayer-api-key-address <signer-address> \
  --authorization-id <fresh-authorization-id> \
  --approved-by <operator> \
  --authorization-expires-at <UTC-expiry> \
  --quantity 5 \
  --max-order-debit 4 \
  --max-session-debit 4 \
  --monitor-seconds 86000 \
  --sample-seconds 2 \
  --audit /tmp/<fresh-audit-name>.jsonl \
  --compliance-confirmed \
  --submit
```

After startup, the process requires the exact ARM confirmation printed in the terminal. Never reuse an authorization ID or audit file. Use `--resume-position` only to recover a journaled position with a fresh authorization; recovery never buys a new position.

## Strategies

| Strategy | Scope | Notes |
|---|---|---|
| `chainlink_terminal_spot_v9` | BTC | Chainlink terminal probability with BTC stress gate |
| `eth_chainlink_terminal_spot_v5` | ETH | Chainlink terminal strategy |
| `sol_chainlink_terminal_spot_v3` | SOL | Alternate Chainlink terminal strategy |
| `xrp_chainlink_terminal_spot_v3` | XRP | Alternate Chainlink terminal strategy |
| `doge_chainlink_terminal_spot_v3` | DOGE | Alternate Chainlink terminal strategy |
| `polyrec_impulse_fade_underdog_v1` | Research | Adapter for the Polyrec impulse signal |

## Repository layout

```text
src/polymarket_bot/
  adapters/       Public-data and execution boundaries
  live/           Authorization, bounded execution, SDK transport, position journal
  microstructure/ Probability, order-book, and market models
  strategies/     Strategy implementations
  cli.py          Command composition root
  runners.py      Replay, shadow, and live orchestration
tests/            Safety and behavior tests
docs/             Strategy and operations documentation
```

## Related project

This bot was developed from the author's related `forecasting-tools` work. Network-operation notes remain in that project; bot-specific strategy and lifecycle documentation lives under `docs/` here.

## License

No license has been declared yet. All rights remain with the repository owner unless a license file is added.
