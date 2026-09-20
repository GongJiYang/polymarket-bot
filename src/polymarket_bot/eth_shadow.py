"""Persistent, credential-free ETH five-minute shadow bot."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

from polymarket_bot.adapters.eth_public_data import CurrentEthSnapshotAdapter
from polymarket_bot.adapters.public_data import PublicDataError
from polymarket_bot.audit import JsonlAudit
from polymarket_bot.dashboard import DashboardPublisher
from polymarket_bot.live.official_chainlink import OfficialPriceUnavailable
from polymarket_bot.microstructure.models import MarketInterval
from polymarket_bot.runners import Decision, ShadowRunner, StrategyEngine
from polymarket_bot.strategies.eth_chainlink_terminal import (
    EthChainlinkTerminalStrategy,
)

BOT_ID = "eth-5m-chainlink-v3-shadow"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threshold", type=Decimal, default=Decimal("0.04"))
    parser.add_argument(
        "--quantity",
        type=Decimal,
        default=Decimal("5"),
        help="minimum visible share depth; no order can be submitted",
    )
    parser.add_argument("--monitor-seconds", type=int, default=3600)
    parser.add_argument("--sample-seconds", type=float, default=2.0)
    parser.add_argument("--audit", type=Path, required=True)
    return parser


def _print_decision(decision: Decision) -> None:
    candidate = decision.candidate
    print(
        json.dumps(
            {
                "asset": "ETH",
                "bot_id": BOT_ID,
                "mode": decision.mode,
                "market_id": decision.market_id,
                "observed_at": decision.observed_at.isoformat(),
                "strategy_id": EthChainlinkTerminalStrategy.strategy_id,
                "direction": candidate.direction if candidate else None,
                "max_price": str(candidate.max_price) if candidate else None,
                "expected_fill_price": (
                    str(candidate.expected_fill_price) if candidate else None
                ),
                "net_edge": str(candidate.net_edge) if candidate else None,
                "post_capability": False,
            },
            sort_keys=True,
        ),
        flush=True,
    )


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if (
        arguments.threshold <= 0
        or arguments.quantity <= 0
        or arguments.monitor_seconds <= 0
        or arguments.sample_seconds <= 0
    ):
        raise ValueError(
            "threshold, quantity, and monitoring durations must be positive"
        )

    shared_dashboard = DashboardPublisher.connect(open_browser=False)
    dashboard = DashboardPublisher(
        shared_dashboard.base_url,
        run_id=f"{BOT_ID}-{os.getpid()}",
    )
    print(f"dashboard: {dashboard.base_url}", file=sys.stderr, flush=True)
    engine = StrategyEngine((EthChainlinkTerminalStrategy(),))
    deadline = time.monotonic() + arguments.monitor_seconds

    with (
        CurrentEthSnapshotAdapter.connect(
            interval=MarketInterval.FIVE_MINUTES,
            threshold=arguments.threshold,
            quantity=arguments.quantity,
        ) as data,
        JsonlAudit(arguments.audit) as audit,
    ):
        runner = ShadowRunner(data, engine, audit, observer=dashboard.publish)
        while time.monotonic() < deadline:
            cycle_started = time.monotonic()
            try:
                _print_decision(runner.run_once())
            except (PublicDataError, OfficialPriceUnavailable) as exc:
                print(
                    f"waiting for complete ETH public snapshot: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
            sleep_seconds = arguments.sample_seconds - (
                time.monotonic() - cycle_started
            )
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
