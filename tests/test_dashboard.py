from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.request import ProxyHandler, build_opener

from polymarket_bot.contracts import FeeMetadata, StrategyContext, StrategyEvaluation
from polymarket_bot.dashboard import (
    DashboardPublisher,
    _DashboardServer,
    build_dashboard_event,
    main,
)
from polymarket_bot.microstructure.models import (
    BookLevel,
    MarketInterval,
    MarketMetadata,
    OrderBook,
    OutcomeSide,
)
from polymarket_bot.runners import Decision

NOW = datetime(2026, 8, 26, 12, 2, tzinfo=timezone.utc)


def _context(asset: str = "BTC") -> StrategyContext:
    market = MarketMetadata(
        market_id="condition",
        title=f"{asset} Up or Down",
        rules="Chainlink",
        asset=asset,
        interval=MarketInterval.FIVE_MINUTES,
        window_start=NOW - timedelta(minutes=2),
        window_end=NOW + timedelta(minutes=3),
        up_token_id="up",
        down_token_id="down",
        outcomes=("Up", "Down"),
    )

    def book(token: str, side: OutcomeSide, ask: str) -> OrderBook:
        return OrderBook(
            market_id=market.market_id,
            token_id=token,
            outcome=side,
            sequence=1,
            bids=(BookLevel(price=Decimal(ask) - Decimal("0.01"), size=Decimal("20")),),
            asks=(BookLevel(price=Decimal(ask), size=Decimal("20")),),
            tick_size=Decimal("0.01"),
            exchange_at=NOW,
            received_at=NOW,
            tradable=True,
        )

    return StrategyContext(
        market=market,
        up_book=book("up", OutcomeSide.UP, "0.45"),
        down_book=book("down", OutcomeSide.DOWN, "0.55"),
        fee=FeeMetadata(Decimal("0"), Decimal("0"), Decimal("5")),
        source=f"https://data.chain.link/streams/{asset.lower()}-usd",
        opening=Decimal("70000"),
        prices=(
            (NOW - timedelta(seconds=5), Decimal("70000")),
            (NOW, Decimal("70010")),
        ),
        current_spot=(NOW, Decimal("70010")),
        threshold=Decimal("0.04"),
        quantity=Decimal("5"),
    )


def _decision(
    observed_at: datetime = NOW,
    strategy_id: str = "chainlink_terminal_spot_v9",
) -> Decision:
    evaluation = StrategyEvaluation(
        strategy_id=strategy_id,
        candidate=None,
        metrics=(
            ("up_terminal_probability", Decimal("0.70")),
            ("down_terminal_probability", Decimal("0.30")),
            ("volatility_per_sqrt_second", Decimal("0.001")),
            ("up_top_ask", Decimal("0.45")),
            ("up_max_price", Decimal("0.46")),
            ("up_fee_per_share", Decimal("0.001")),
            ("up_net_edge", Decimal("0.239")),
            ("up_available_depth", Decimal("20")),
            ("up_eligible", True),
            ("selected_direction", None),
        ),
    )
    return Decision(
        mode="shadow",
        observed_at=observed_at,
        market_id="condition",
        candidate=None,
        strategy_evaluations=(evaluation,),
    )


def test_loopback_dashboard_keeps_latest_observation_per_bot() -> None:
    server = _DashboardServer(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    first = DashboardPublisher(f"http://127.0.0.1:{port}", run_id="first")
    second = DashboardPublisher(f"http://127.0.0.1:{port}", run_id="second")
    try:
        assert first.healthy()
        assert main(["--host", "127.0.0.1", "--port", str(port)]) == 0
        first.publish(_decision(), _context())
        first.publish(_decision(NOW + timedelta(seconds=1)), _context())
        second.publish(_decision(), _context())
        opener = build_opener(ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/api/snapshot") as response:
            snapshot = json.load(response)
        with opener.open(f"http://127.0.0.1:{port}/") as response:
            dashboard = response.read().decode()
        assert "全部策略评估指标" in dashboard
        assert "Chainlink 官方价格路径" in dashboard
        assert snapshot["generation"] == 3
        assert {run["run_id"] for run in snapshot["runs"]} == {"first", "second"}
        first_run = next(run for run in snapshot["runs"] if run["run_id"] == "first")
        assert first_run["observed_at"] == (NOW + timedelta(seconds=1)).isoformat()
        assert (
            first_run["evaluations"]["chainlink_terminal_spot_v9"][
                "up_terminal_probability"
            ]
            == "0.70"
        )
        assert first_run["prices"][-1] == [NOW.isoformat(), "70010"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_dashboard_selects_eth_strategy_metrics_for_eth_bot() -> None:
    event = build_dashboard_event(
        _decision(strategy_id="eth_chainlink_terminal_spot_v5"),
        _context("ETH"),
        run_id="eth-shadow",
        process_id=42,
    )

    assert event["market"]["asset"] == "ETH"  # type: ignore[index]
    assert event["chainlink_strategy_id"] == "eth_chainlink_terminal_spot_v5"


def test_dashboard_rejects_non_loopback_urls() -> None:
    try:
        DashboardPublisher("http://example.com:8765")
    except ValueError as exc:
        assert "loopback" in str(exc)
    else:
        raise AssertionError("non-loopback dashboard URL was accepted")
