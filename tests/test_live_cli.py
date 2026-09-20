from __future__ import annotations

from contextlib import nullcontext

from types import SimpleNamespace

import pytest

from polymarket_bot import cli
from polymarket_bot.cli import _live_decision_exit_code, main
from polymarket_bot.strategies.alt_chainlink_terminal import (
    DogeChainlinkTerminalStrategy,
    SolChainlinkTerminalStrategy,
    XrpChainlinkTerminalStrategy,
)
from polymarket_bot.strategies.chainlink_terminal import ChainlinkTerminalStrategy
from polymarket_bot.strategies.eth_chainlink_terminal import (
    EthChainlinkTerminalStrategy,
)


BASE_ARGS = [
    "live",
    "--wallet",
    "0x" + "11" * 20,
    "--relayer-api-key-address",
    "0x" + "22" * 20,
    "--authorization-id",
    "test-session",
    "--approved-by",
    "tester",
    "--authorization-expires-at",
    "2099-01-01T00:00:00Z",
    "--audit",
    "/tmp/polymarket-bot-live-cli-test.jsonl",
]


def test_live_requires_compliance_confirmation_before_credentials() -> None:
    with pytest.raises(ValueError, match="compliance confirmation"):
        main(BASE_ARGS + ["--submit"])


def test_live_requires_explicit_submit_before_credentials() -> None:
    with pytest.raises(ValueError, match="explicit --submit"):
        main(BASE_ARGS + ["--compliance-confirmed"])


def test_shadow_rejects_invalid_monitor_arguments_before_public_data() -> None:
    with pytest.raises(ValueError, match="monitor-seconds"):
        main(
            [
                "shadow",
                "--audit",
                "/tmp/polymarket-bot-shadow-cli-test.jsonl",
                "--monitor-seconds",
                "-1",
            ]
        )
    with pytest.raises(ValueError, match="sample-seconds"):
        main(
            [
                "shadow",
                "--audit",
                "/tmp/polymarket-bot-shadow-cli-test.jsonl",
                "--sample-seconds",
                "0",
            ]
        )

def test_shadow_monitor_continues_after_temporary_public_data_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TemporarilyUnavailable:
        def run_once(self) -> None:
            raise cli.OfficialPriceUnavailable("volatility history is incomplete")

    monotonic_values = iter((0.0, 0.0, 1.0))
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(
        cli.DashboardPublisher,
        "connect",
        lambda **_: SimpleNamespace(base_url="http://127.0.0.1:8765", publish=lambda _: None),
    )
    monkeypatch.setattr(
        cli.CurrentBtcSnapshotAdapter,
        "connect",
        lambda **_: nullcontext(object()),
    )
    monkeypatch.setattr(cli, "JsonlAudit", lambda _: nullcontext(object()))
    monkeypatch.setattr(cli, "ShadowRunner", lambda *_args, **_kwargs: TemporarilyUnavailable())

    assert (
        main(
            [
                "shadow",
                "--audit",
                "/tmp/polymarket-bot-shadow-cli-test.jsonl",
                "--monitor-seconds",
                "1",
            ]
        )
        == 0
    )

def test_live_rejects_shadow_only_strategy_before_credentials(capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        main(
            BASE_ARGS
            + [
                "--strategy",
                "polyrec_impulse_fade_underdog_v1",
                "--compliance-confirmed",
                "--submit",
            ]
        )
    assert raised.value.code == 2
    assert "not approved for live execution" in capsys.readouterr().err


def test_live_authorization_binds_selected_asset_and_strategy(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def capture_authorization(**values: object) -> object:
        captured.update(values)
        raise RuntimeError("authorization captured")

    monkeypatch.setattr(
        cli.LiveSessionAuthorization,
        "create",
        capture_authorization,
    )
    with pytest.raises(RuntimeError, match="authorization captured"):
        main(BASE_ARGS + ["--compliance-confirmed", "--submit"])

    assert captured["market_family"] == "BTC Up/Down 5m"
    assert captured["model_version"] == ChainlinkTerminalStrategy.strategy_id


def test_live_authorization_binds_all_assets_to_one_session(monkeypatch) -> None:
    captured: list[dict[str, object]] = []
    strategy_types = (
        ChainlinkTerminalStrategy,
        EthChainlinkTerminalStrategy,
        SolChainlinkTerminalStrategy,
        XrpChainlinkTerminalStrategy,
        DogeChainlinkTerminalStrategy,
    )

    def capture_authorization(**values: object) -> object:
        captured.append(values)
        if len(captured) == len(strategy_types):
            raise RuntimeError("authorizations captured")
        return object()

    monkeypatch.setattr(
        cli.LiveSessionAuthorization,
        "create",
        capture_authorization,
    )
    strategy_args = [
        value
        for strategy_type in strategy_types
        for value in ("--strategy", strategy_type.strategy_id)
    ]
    with pytest.raises(RuntimeError, match="authorizations captured"):
        main(
            BASE_ARGS
            + strategy_args
            + [
                "--compliance-confirmed",
                "--submit",
            ]
        )

    assert tuple(
        (
            item["authorization_id"],
            item["market_family"],
            item["model_version"],
        )
        for item in captured
    ) == tuple(
        (
            f"test-session-{strategy_type.asset.lower()}",
            f"{strategy_type.asset} Up/Down 5m",
            strategy_type.strategy_id,
        )
        for strategy_type in strategy_types
    )


def test_live_loop_continues_after_pre_post_rejection() -> None:
    decision = SimpleNamespace(
        candidate=object(),
        receipt=SimpleNamespace(post_attempted=False),
        execution_state="REJECTED",
    )

    assert _live_decision_exit_code(decision) is None  # type: ignore[arg-type]



def test_live_loop_stops_cleanly_when_session_capacity_is_unavailable() -> None:
    decision = SimpleNamespace(
        candidate=object(),
        receipt=SimpleNamespace(
            code="SESSION_CAPACITY_UNAVAILABLE",
            post_attempted=False,
        ),
        execution_state="REJECTED",
    )

    assert _live_decision_exit_code(decision) == 0  # type: ignore[arg-type]

@pytest.mark.parametrize(
    ("state", "exit_code"),
    (("FILLED", 0), ("REJECTED", 2), ("UNKNOWN", 2)),
)
def test_live_loop_stops_after_post_attempt(
    state: str,
    exit_code: int,
) -> None:
    decision = SimpleNamespace(
        candidate=object(),
        receipt=SimpleNamespace(post_attempted=True),
        execution_state=state,
    )

    assert _live_decision_exit_code(decision) == exit_code  # type: ignore[arg-type]


