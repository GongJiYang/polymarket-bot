from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from polymarket_bot.contracts import (
    ExecutionPreparationRejected,
    FeeMetadata,
    StrategyCandidate,
    StrategyContext,
    StrategyEvaluation,
)
from polymarket_bot.live.bounded_bot import (
    BoundedBotError,
    OrderDebitCapExceeded,
    SessionCapacityLedger,
)
from polymarket_bot.microstructure.models import (
    BookLevel,
    MarketInterval,
    MarketMetadata,
    OrderBook,
    OutcomeSide,
)
from polymarket_bot.runners import (
    CandidateRejectionReceipt,
    LiveRunner,
    ReplayRunner,
    ShadowRunner,
    StrategyEngine,
    StrategyOptimizer,
)
from polymarket_bot.strategies.polyrec_adapter import PolyrecImpulseStrategy
from polymarket_bot.strategy_registry import (
    StrategyRegistryError,
    load_strategies,
    select_live_strategies,
    select_strategies,
)

NOW = datetime(2026, 8, 25, 12, 1, tzinfo=timezone.utc)
WINDOW_START = NOW.replace(minute=0, second=0, microsecond=0)


def book(outcome: OutcomeSide, ask: str) -> OrderBook:
    token = "up-token" if outcome is OutcomeSide.UP else "down-token"
    return OrderBook(
        market_id="condition",
        token_id=token,
        outcome=outcome,
        sequence=1,
        bids=(BookLevel(price=Decimal(ask) - Decimal("0.01"), size=Decimal("20")),),
        asks=(BookLevel(price=Decimal(ask), size=Decimal("20")),),
        tick_size=Decimal("0.01"),
        exchange_at=NOW,
        received_at=NOW,
        tradable=True,
    )


def context(*, up: str = "0.45", down: str = "0.56") -> StrategyContext:
    market = MarketMetadata(
        market_id="condition",
        title="Bitcoin Up or Down",
        rules="Resolves from Chainlink BTC/USD",
        asset="BTC",
        interval=MarketInterval.FIVE_MINUTES,
        window_start=WINDOW_START,
        window_end=WINDOW_START + timedelta(minutes=5),
        up_token_id="up-token",
        down_token_id="down-token",
        outcomes=("Up", "Down"),
    )
    return StrategyContext(
        market=market,
        up_book=book(OutcomeSide.UP, up),
        down_book=book(OutcomeSide.DOWN, down),
        fee=FeeMetadata(Decimal("0"), Decimal("0"), Decimal("5")),
        source="https://data.chain.link/streams/btc-usd",
        opening=Decimal("70000"),
        prices=((NOW - timedelta(seconds=10), Decimal("70000")),),
        current_spot=(NOW, Decimal("70010")),
        threshold=Decimal("0.02"),
        quantity=Decimal("5"),
    )


class FixedStrategy:
    strategy_id = "fixed"

    def evaluate(self, value: StrategyContext) -> StrategyEvaluation:
        candidate = StrategyCandidate(
            strategy_id=self.strategy_id,
            direction="up",
            token_id=value.market.up_token_id,
            book=value.up_book,
            source=value.source,
            opening=value.opening,
            prices=value.prices,
            terminal_probability=Decimal("0.7"),
            volatility_per_sqrt_second=Decimal("0.001"),
            top_ask=Decimal("0.45"),
            max_price=Decimal("0.46"),
            expected_fill_price=Decimal("0.46"),
            fee_per_share=Decimal("0"),
            net_edge=Decimal("0.24"),
            signal_metric="test",
            signal_strength=Decimal("0.24"),
        )
        return StrategyEvaluation(strategy_id=self.strategy_id, candidate=candidate)


class Data:
    def snapshot(self) -> StrategyContext:
        return context()

    def final_snapshot(self, initial: StrategyContext) -> StrategyContext:
        return initial


class Audit:
    def __init__(self) -> None:
        self.events: list[object] = []

    def record(self, event: object) -> None:
        self.events.append(event)


class Prepared:
    intent_hash = "intent"
    maximum_all_in_debit = Decimal("2")


class Receipt:
    state = "FILLED"
    post_attempted = True


class Execution:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def prepare(self, candidate: StrategyCandidate, value: StrategyContext) -> Prepared:
        assert candidate.token_id == value.market.up_token_id
        self.calls.append("prepare")
        return Prepared()

    def submit(self, prepared: Prepared) -> Receipt:
        assert prepared.intent_hash == "intent"
        self.calls.append("submit")
        return Receipt()


class Budget:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def reserve(self, prepared: Prepared) -> None:
        assert prepared.maximum_all_in_debit == Decimal("2")
        self.calls.append("reserve")

    def release(self, prepared: Prepared) -> None:
        assert prepared.maximum_all_in_debit == Decimal("2")
        self.calls.append("release")

    def close(self, state: str) -> None:
        self.calls.append(f"close:{state}")


def test_capacity_ledger_restores_only_proven_pre_post_reservation() -> None:
    class Authorization:
        max_post_attempts = 1
        max_session_debit = Decimal("7")

    ledger = SessionCapacityLedger(Authorization())  # type: ignore[arg-type]
    prepared = Prepared()

    ledger.reserve(prepared)  # type: ignore[arg-type]
    assert ledger.state == (0, Decimal("5"), "intent", "RESERVED")
    ledger.release(prepared)  # type: ignore[arg-type]
    assert ledger.state == (1, Decimal("7"), None, None)

    ledger.reserve(prepared)  # type: ignore[arg-type]
    ledger.close("FILLED")
    assert ledger.state == (0, Decimal("5"), "intent", "FILLED")


def test_builtin_strategy_registry_preserves_trusted_strategies() -> None:
    registry = load_strategies(entry_points=())
    assert tuple(registry) == (
        "chainlink_terminal_spot_v9",
        "doge_chainlink_terminal_spot_v3",
        "eth_chainlink_terminal_spot_v5",
        "polyrec_impulse_fade_underdog_v1",
        "sol_chainlink_terminal_spot_v3",
        "xrp_chainlink_terminal_spot_v3",
    )
    assert len(select_strategies(registry, "all")) == 6
    assert tuple(
        strategy.strategy_id
        for strategy in select_live_strategies(registry, "all")
    ) == (
        "chainlink_terminal_spot_v9",
        "doge_chainlink_terminal_spot_v3",
        "eth_chainlink_terminal_spot_v5",
        "sol_chainlink_terminal_spot_v3",
        "xrp_chainlink_terminal_spot_v3",
    )
    with pytest.raises(StrategyRegistryError, match="not approved"):
        select_live_strategies(registry, "polyrec_impulse_fade_underdog_v1")


def test_polyrec_impulse_is_observable_but_never_executable() -> None:
    strategy = PolyrecImpulseStrategy()
    assert strategy.evaluate(context()).candidate is None
    evaluation = strategy.evaluate(context(up="0.41", down="0.56"))
    assert evaluation.candidate is None
    assert evaluation.metric("signal_active") is True
    assert evaluation.metric("execution_eligible") is False
    assert evaluation.metric("rejection_code") == "SHADOW_ONLY_UNVALIDATED"
    assert evaluation.metric("direction") == "up"
    assert evaluation.metric("signal_strength") == Decimal("0.04")
    assert evaluation.metric("ask_sum") == Decimal("0.97")


def test_candidate_rejects_incomparable_edge() -> None:
    candidate = FixedStrategy().evaluate(context()).candidate
    assert candidate is not None
    with pytest.raises(ValueError, match="net_edge must equal"):
        replace(candidate, net_edge=Decimal("0.25"))
    with pytest.raises(ValueError, match="model-favored"):
        replace(
            candidate,
            terminal_probability=Decimal("0.43"),
            net_edge=Decimal("-0.03"),
        )


def test_live_runner_reserves_capacity_before_sole_submission() -> None:
    calls: list[str] = []
    audit = Audit()
    decision = LiveRunner(
        Data(),
        StrategyEngine((FixedStrategy(),)),
        Execution(calls),
        Budget(calls),
        audit,
        clock=lambda: NOW,
        required_signal_confirmations=1,
    ).run_once()
    assert calls == ["prepare", "reserve", "submit", "close:FILLED"]
    assert decision.execution_state == "FILLED"
    assert audit.events == [decision]


def test_live_runner_releases_capacity_after_pre_post_rejection() -> None:
    calls: list[str] = []

    class PrePostRejected(Execution):
        def submit(self, prepared: Prepared) -> object:
            assert prepared.intent_hash == "intent"
            self.calls.append("submit")
            return CandidateRejectionReceipt(
                state="REJECTED",
                code="PRE_POST_LIQUIDITY_GONE",
                message="book moved before POST",
            )

    decision = LiveRunner(
        Data(),
        StrategyEngine((FixedStrategy(),)),
        PrePostRejected(calls),
        Budget(calls),
        Audit(),
        clock=lambda: NOW,
        required_signal_confirmations=1,
    ).run_once()

    assert calls == ["prepare", "reserve", "submit", "release"]
    assert decision.execution_state == "REJECTED"
    assert getattr(decision.receipt, "post_attempted") is False


def test_live_runner_records_exhausted_session_capacity_without_submission() -> None:
    calls: list[str] = []

    class CapacityExhaustedBudget(Budget):
        def reserve(self, prepared: Prepared) -> None:
            self.calls.append("reserve")
            raise BoundedBotError("session capacity has already been consumed")

    audit = Audit()
    decision = LiveRunner(
        Data(),
        StrategyEngine((FixedStrategy(),)),
        Execution(calls),
        CapacityExhaustedBudget(calls),
        audit,
        clock=lambda: NOW,
        required_signal_confirmations=1,
    ).run_once()

    assert calls == ["prepare", "reserve"]
    assert decision.execution_state == "REJECTED"
    assert getattr(decision.receipt, "code") == "SESSION_CAPACITY_UNAVAILABLE"
    assert getattr(decision.receipt, "post_attempted") is False
    assert audit.events == [decision]

def test_live_runner_retries_after_each_pre_post_rejection() -> None:
    calls: list[str] = []

    class AdvancingData:
        def __init__(self) -> None:
            self.count = 0

        def snapshot(self) -> StrategyContext:
            self.count += 1
            value = context()
            return replace(
                value,
                current_spot=(
                    NOW + timedelta(seconds=self.count),
                    value.current_spot[1],
                ),
            )

        def final_snapshot(self, initial: StrategyContext) -> StrategyContext:
            return initial

    class PrePostRejected(Execution):
        def submit(self, prepared: Prepared) -> object:
            self.calls.append("submit")
            return CandidateRejectionReceipt(
                state="REJECTED",
                code="PRE_POST_LIQUIDITY_GONE",
                message="book moved before POST",
            )

    runner = LiveRunner(
        AdvancingData(),
        StrategyEngine((FixedStrategy(),)),
        PrePostRejected(calls),
        Budget(calls),
        Audit(),
        clock=lambda: NOW,
        required_signal_confirmations=1,
    )
    decisions = tuple(runner.run_once() for _ in range(4))

    assert calls == ["prepare", "reserve", "submit", "release"] * 4
    assert all(
        getattr(decision.receipt, "code") == "PRE_POST_LIQUIDITY_GONE"
        and getattr(decision.receipt, "post_attempted") is False
        for decision in decisions
    )


def test_live_runner_requires_two_independent_signal_timestamps() -> None:
    calls: list[str] = []
    audit = Audit()

    class AdvancingData:
        def __init__(self) -> None:
            self.index = 0

        def snapshot(self) -> StrategyContext:
            value = replace(
                context(),
                current_spot=(
                    NOW + timedelta(seconds=5 * self.index),
                    Decimal("70010"),
                ),
            )
            self.index += 1
            return value

        def final_snapshot(self, initial: StrategyContext) -> StrategyContext:
            return initial

    runner = LiveRunner(
        AdvancingData(),
        StrategyEngine((FixedStrategy(),)),
        Execution(calls),
        Budget(calls),
        audit,
        clock=lambda: NOW,
    )

    first = runner.run_once()
    assert first.candidate is None
    assert calls == []

    second = runner.run_once()
    assert second.execution_state == "FILLED"
    assert calls == ["prepare", "reserve", "submit", "close:FILLED"]


@pytest.mark.parametrize(
    ("probabilities", "expected_state", "expected_code"),
    (
        (
            ("0.80", "0.79", "0.78"),
            "REJECTED",
            "SIGNAL_PROBABILITY_DECAY",
        ),
        (
            ("0.80", "0.84", "0.80"),
            "REJECTED",
            "SIGNAL_PROBABILITY_PULLBACK",
        ),
        (
            ("0.65", "0.70", "0.85"),
            "FILLED",
            None,
        ),
    ),
)
def test_live_runner_probability_stability_confirmation(
    probabilities: tuple[str, ...],
    expected_state: str,
    expected_code: str | None,
) -> None:
    calls: list[str] = []

    class ProbabilityStrategy(FixedStrategy):
        def evaluate(self, value: StrategyContext) -> StrategyEvaluation:
            evaluation = super().evaluate(value)
            assert evaluation.candidate is not None
            probability = value.current_spot[1]
            candidate = replace(
                evaluation.candidate,
                terminal_probability=probability,
                net_edge=(
                    probability
                    - evaluation.candidate.max_price
                    - evaluation.candidate.fee_per_share
                ),
            )
            return replace(evaluation, candidate=candidate)

    class ProbabilityData:
        def __init__(self) -> None:
            self.index = 0

        def snapshot(self) -> StrategyContext:
            value = replace(
                context(),
                current_spot=(
                    NOW + timedelta(seconds=5 * self.index),
                    Decimal(probabilities[self.index]),
                ),
            )
            self.index += 1
            return value

        def final_snapshot(self, initial: StrategyContext) -> StrategyContext:
            return initial

    runner = LiveRunner(
        ProbabilityData(),
        StrategyEngine((ProbabilityStrategy(),)),
        Execution(calls),
        Budget(calls),
        Audit(),
        clock=lambda: NOW,
        required_signal_confirmations=3,
    )

    decisions = tuple(runner.run_once() for _ in probabilities)

    assert all(decision.execution_state is None for decision in decisions[:-1])
    assert decisions[-1].execution_state == expected_state
    assert getattr(decisions[-1].receipt, "code", None) == expected_code
    expected_calls = (
        ["prepare", "reserve", "submit", "close:FILLED"]
        if expected_state == "FILLED"
        else []
    )
    assert calls == expected_calls


def test_live_runner_rejects_direction_reversal_during_confirmation() -> None:
    calls: list[str] = []

    class DirectionStrategy(FixedStrategy):
        def evaluate(self, value: StrategyContext) -> StrategyEvaluation:
            evaluation = super().evaluate(value)
            assert evaluation.candidate is not None
            if value.current_spot[1] > 0:
                return evaluation
            candidate = replace(
                evaluation.candidate,
                direction="down",
                token_id=value.market.down_token_id,
                book=value.down_book,
            )
            return replace(evaluation, candidate=candidate)

    class DirectionData:
        def __init__(self) -> None:
            self.index = 0

        def snapshot(self) -> StrategyContext:
            spots = (Decimal("1"), Decimal("-1"))
            value = replace(
                context(),
                current_spot=(
                    NOW + timedelta(seconds=5 * self.index),
                    spots[self.index],
                ),
            )
            self.index += 1
            return value

        def final_snapshot(self, initial: StrategyContext) -> StrategyContext:
            return initial

    runner = LiveRunner(
        DirectionData(),
        StrategyEngine((DirectionStrategy(),)),
        Execution(calls),
        Budget(calls),
        Audit(),
        clock=lambda: NOW,
    )

    first, reversal = (runner.run_once() for _ in range(2))

    assert first.execution_state is None
    assert reversal.execution_state == "REJECTED"
    assert getattr(reversal.receipt, "code") == "SIGNAL_DIRECTION_REVERSED"
    assert calls == []


def test_live_runner_re_evaluates_strategy_on_final_snapshot() -> None:
    calls: list[str] = []

    class FinalSignalGone(FixedStrategy):
        def evaluate(self, value: StrategyContext) -> StrategyEvaluation:
            if value.current_spot[1] == Decimal("70009"):
                return StrategyEvaluation(strategy_id=self.strategy_id, candidate=None)
            return super().evaluate(value)

    class FinalData(Data):
        def final_snapshot(self, initial: StrategyContext) -> StrategyContext:
            return replace(
                initial,
                current_spot=(NOW + timedelta(seconds=1), Decimal("70009")),
            )

    decision = LiveRunner(
        FinalData(),
        StrategyEngine((FinalSignalGone(),)),
        Execution(calls),
        Budget(calls),
        Audit(),
        clock=lambda: NOW,
        required_signal_confirmations=1,
    ).run_once()

    assert decision.execution_state == "REJECTED"
    assert getattr(decision.receipt, "code") == "FINAL_SIGNAL_DISAPPEARED"
    assert calls == []


def test_live_runner_abandons_market_inside_close_time_guard() -> None:
    calls: list[str] = []
    runner = LiveRunner(
        Data(),
        StrategyEngine((FixedStrategy(),)),
        Execution(calls),
        Budget(calls),
        Audit(),
        clock=lambda: WINDOW_START + timedelta(minutes=4, seconds=15),
        required_signal_confirmations=1,
    )

    decision = runner.run_once()

    assert decision.candidate is None
    assert calls == []

def test_live_runner_does_not_recount_same_source_timestamp() -> None:
    calls: list[str] = []
    runner = LiveRunner(
        Data(),
        StrategyEngine((FixedStrategy(),)),
        Execution(calls),
        Budget(calls),
        Audit(),
        clock=lambda: NOW,
    )

    decisions = tuple(runner.run_once() for _ in range(6))

    assert all(decision.candidate is None for decision in decisions)
    assert calls == []


def test_live_runner_resets_streak_on_duplicate_source_rejection() -> None:
    calls: list[str] = []

    class ConditionalStrategy(FixedStrategy):
        def evaluate(self, value: StrategyContext) -> StrategyEvaluation:
            if value.current_spot[1] == Decimal("70009"):
                return StrategyEvaluation(strategy_id=self.strategy_id, candidate=None)
            return super().evaluate(value)

    class SequencedData:
        def __init__(self) -> None:
            self.values = iter(
                (
                    (0, "70010"),
                    (0, "70009"),
                    (5, "70010"),
                    (10, "70010"),
                )
            )

        def snapshot(self) -> StrategyContext:
            seconds, spot = next(self.values)
            return replace(
                context(),
                current_spot=(NOW + timedelta(seconds=seconds), Decimal(spot)),
            )

        def final_snapshot(self, initial: StrategyContext) -> StrategyContext:
            return initial

    runner = LiveRunner(
        SequencedData(),
        StrategyEngine((ConditionalStrategy(),)),
        Execution(calls),
        Budget(calls),
        Audit(),
        clock=lambda: NOW,
    )

    first_three = tuple(runner.run_once() for _ in range(3))
    assert all(decision.candidate is None for decision in first_three)
    assert calls == []

    fourth = runner.run_once()
    assert fourth.execution_state == "FILLED"
    assert calls == ["prepare", "reserve", "submit", "close:FILLED"]


def test_live_runner_audits_candidate_rejected_by_order_cap() -> None:
    calls: list[str] = []
    audit = Audit()

    class AboveCap(Execution):
        def prepare(
            self, candidate: StrategyCandidate, value: StrategyContext
        ) -> Prepared:
            raise OrderDebitCapExceeded("candidate exceeds the all-in debit cap")

    decision = LiveRunner(
        Data(),
        StrategyEngine((FixedStrategy(),)),
        AboveCap(calls),
        Budget(calls),
        audit,
        clock=lambda: NOW,
        required_signal_confirmations=1,
    ).run_once()

    assert calls == []
    assert decision.execution_state == "REJECTED"
    assert getattr(decision.receipt, "code") == "ORDER_DEBIT_CAP_EXCEEDED"
    assert audit.events == [decision]


def test_live_runner_monitors_after_typed_preparation_rejection() -> None:
    calls: list[str] = []

    class SigningRejected(Execution):
        def prepare(
            self, candidate: StrategyCandidate, value: StrategyContext
        ) -> Prepared:
            raise ExecutionPreparationRejected(
                "PREPARE_SIGNING_REJECTED",
                "signed order falls below target shares",
            )

    decision = LiveRunner(
        Data(),
        StrategyEngine((FixedStrategy(),)),
        SigningRejected(calls),
        Budget(calls),
        Audit(),
        clock=lambda: NOW,
        required_signal_confirmations=1,
    ).run_once()

    assert calls == []
    assert decision.execution_state == "REJECTED"
    assert getattr(decision.receipt, "code") == "PREPARE_SIGNING_REJECTED"
    assert getattr(decision.receipt, "post_attempted") is False


def test_live_runner_consumes_budget_on_unknown_submission() -> None:
    calls: list[str] = []

    class Unknown(Execution):
        def submit(self, prepared: Prepared) -> Receipt:
            self.calls.append("submit")
            raise TimeoutError("unknown")

    with pytest.raises(TimeoutError, match="unknown"):
        LiveRunner(
            Data(),
            StrategyEngine((FixedStrategy(),)),
            Unknown(calls),
            Budget(calls),
            Audit(),
            clock=lambda: NOW,
            required_signal_confirmations=1,
        ).run_once()
    assert calls == ["prepare", "reserve", "submit", "close:UNKNOWN"]


def test_shadow_replay_and_optimizer_never_require_execution_adapter() -> None:
    audit = Audit()
    shadow = ShadowRunner(Data(), StrategyEngine((FixedStrategy(),)), audit, clock=lambda: NOW)
    assert shadow.run_once().mode == "shadow"
    decisions = ReplayRunner(StrategyEngine((FixedStrategy(),))).run((context(),))
    assert decisions[0].candidate is not None
    result = StrategyOptimizer().optimize(
        ({"weight": 1}, {"weight": 2}),
        (context(),),
        lambda _: (FixedStrategy(),),
        lambda values: Decimal(len(values)),
    )
    assert result.score == Decimal("1")
    assert result.signal_count == 1
