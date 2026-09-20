"""Independent run modes over the same typed strategy engine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Iterable, Mapping, Sequence

from polymarket_bot.contracts import (
    AuditSink,
    CapacityBudget,
    ExecutionAdapter,
    ExecutionPreparationRejected,
    MarketDataAdapter,
    Strategy,
    StrategyCandidate,
    StrategyContext,
    StrategyEvaluation,
)
from polymarket_bot.live.bounded_bot import BoundedBotError, OrderDebitCapExceeded

REQUIRED_LIVE_SIGNAL_CONFIRMATIONS = 2
MINIMUM_LIVE_ORDER_LEAD = timedelta(seconds=45)
MINIMUM_TERMINAL_PROBABILITY = Decimal("0.60")
MAXIMUM_ENTRY_PRICE = Decimal("0.75")
MAXIMUM_PROBABILITY_PULLBACK = Decimal("0.03")




@dataclass(frozen=True, slots=True)
class Decision:
    mode: str
    observed_at: datetime
    market_id: str
    candidate: StrategyCandidate | None
    execution_state: str | None = None
    receipt: object | None = None
    strategy_evaluations: tuple[StrategyEvaluation, ...] = ()


@dataclass(frozen=True, slots=True)
class CandidateRejectionReceipt:
    state: str
    code: str
    message: str
    post_attempted: bool = False

@dataclass(frozen=True, slots=True)
class SignalObservation:
    source_at: datetime
    terminal_probability: Decimal
    max_price: Decimal



@dataclass(frozen=True, slots=True)
class EngineEvaluation:
    candidate: StrategyCandidate | None
    strategies: tuple[StrategyEvaluation, ...]


@dataclass(frozen=True, slots=True)
class OptimizationResult:
    parameters: tuple[tuple[str, str], ...]
    score: Decimal
    signal_count: int


class StrategyEngine:
    def __init__(self, strategies: Sequence[Strategy]) -> None:
        if not strategies:
            raise ValueError("at least one strategy is required")
        identities = tuple(strategy.strategy_id for strategy in strategies)
        if len(set(identities)) != len(identities):
            raise ValueError("strategy ids must be unique")
        self._strategies = tuple(strategies)

    def evaluate(self, context: StrategyContext) -> EngineEvaluation:
        evaluations = tuple(strategy.evaluate(context) for strategy in self._strategies)
        candidate = max(
            (
                evaluation.candidate
                for evaluation in evaluations
                if evaluation.candidate is not None
            ),
            key=lambda item: (item.net_edge, item.strategy_id),
            default=None,
        )
        return EngineEvaluation(candidate=candidate, strategies=evaluations)


class ShadowRunner:
    def __init__(
        self,
        data: MarketDataAdapter,
        engine: StrategyEngine,
        audit: AuditSink,
        *,
        clock: Callable[[], datetime] | None = None,
        observer: Callable[[Decision, StrategyContext], None] | None = None,
    ) -> None:
        self._data = data
        self._engine = engine
        self._audit = audit
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._observer = observer

    def run_once(self) -> Decision:
        context = self._data.snapshot()
        evaluation = self._engine.evaluate(context)
        decision = Decision(
            mode="shadow",
            observed_at=self._clock(),
            market_id=context.market.market_id,
            candidate=evaluation.candidate,
            strategy_evaluations=evaluation.strategies,
        )
        self._audit.record(decision)
        if self._observer is not None:
            self._observer(decision, context)
        return decision


class ReplayRunner:
    def __init__(self, engine: StrategyEngine) -> None:
        self._engine = engine

    def run(self, snapshots: Iterable[StrategyContext]) -> tuple[Decision, ...]:
        decisions: list[Decision] = []
        for context in snapshots:
            evaluation = self._engine.evaluate(context)
            decisions.append(
                Decision(
                    mode="replay",
                    observed_at=context.current_spot[0],
                    market_id=context.market.market_id,
                    candidate=evaluation.candidate,
                    strategy_evaluations=evaluation.strategies,
                )
            )
        return tuple(decisions)


class LiveRunner:
    """Require stable signals before at most one HTTP POST."""

    def __init__(
        self,
        data: MarketDataAdapter,
        engine: StrategyEngine,
        execution: ExecutionAdapter,
        budget: CapacityBudget,
        audit: AuditSink,
        *,
        clock: Callable[[], datetime] | None = None,
        observer: Callable[[Decision, StrategyContext], None] | None = None,
        required_signal_confirmations: int = REQUIRED_LIVE_SIGNAL_CONFIRMATIONS,
    ) -> None:
        self._data = data
        self._engine = engine
        self._execution = execution
        self._budget = budget
        self._audit = audit
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._observer = observer
        if required_signal_confirmations < 1:
            raise ValueError("required signal confirmations must be positive")
        self._required_signal_confirmations = required_signal_confirmations
        self._signal_key: tuple[str, str, str] | None = None
        self._signal_count = 0
        self._last_signal_source_at: datetime | None = None
        self._signal_observations: tuple[SignalObservation, ...] = ()

    def run_once(self) -> Decision:
        context = self._data.snapshot()
        evaluation = self._engine.evaluate(context)
        candidate, signal_rejection = self._confirmed_candidate(
            evaluation.candidate, context
        )
        if candidate is None:
            decision = Decision(
                mode="live",
                observed_at=self._clock(),
                market_id=context.market.market_id,
                candidate=evaluation.candidate if signal_rejection else None,
                execution_state="REJECTED" if signal_rejection else None,
                receipt=signal_rejection,
                strategy_evaluations=evaluation.strategies,
            )
            if signal_rejection is not None:
                self._reset_signal()
            self._record(decision, context)
            return decision

        final_context = self._data.final_snapshot(context)
        final_evaluation = self._engine.evaluate(final_context)
        final_candidate = final_evaluation.candidate
        final_rejection = self._final_signal_rejection(
            candidate, final_candidate, final_context
        )
        if final_rejection is not None:
            decision = Decision(
                mode="live",
                observed_at=self._clock(),
                market_id=final_context.market.market_id,
                candidate=final_candidate or candidate,
                execution_state="REJECTED",
                receipt=final_rejection,
                strategy_evaluations=final_evaluation.strategies,
            )
            self._reset_signal()
            self._record(decision, final_context)
            return decision
        assert final_candidate is not None
        try:
            prepared = self._execution.prepare(final_candidate, final_context)
            self._budget.reserve(prepared)
        except (BoundedBotError, ExecutionPreparationRejected) as exc:
            if isinstance(exc, OrderDebitCapExceeded):
                code = "ORDER_DEBIT_CAP_EXCEEDED"
            elif isinstance(exc, ExecutionPreparationRejected):
                code = exc.code
            else:
                code = "SESSION_CAPACITY_UNAVAILABLE"
            decision = Decision(
                mode="live",
                observed_at=self._clock(),
                market_id=final_context.market.market_id,
                candidate=final_candidate,
                execution_state="REJECTED",
                receipt=CandidateRejectionReceipt(
                    state="REJECTED",
                    code=code,
                    message=str(exc),
                ),
                strategy_evaluations=final_evaluation.strategies,
            )
            self._reset_signal()
            self._record(decision, final_context)
            return decision
        try:
            receipt = self._execution.submit(prepared)
        except BaseException:
            self._budget.close("UNKNOWN")
            raise
        state = getattr(receipt, "state", None)
        post_attempted = getattr(receipt, "post_attempted", None)
        if (
            state not in {"FILLED", "CANCELED", "REJECTED", "UNKNOWN"}
            or type(post_attempted) is not bool
            or (not post_attempted and state != "REJECTED")
        ):
            self._budget.close("UNKNOWN")
            raise RuntimeError("execution receipt has no safe terminal state")
        if post_attempted:
            self._budget.close(state)
        else:
            self._budget.release(prepared)
        decision = Decision(
            mode="live",
            observed_at=self._clock(),
            market_id=final_context.market.market_id,
            candidate=final_candidate,
            execution_state=state,
            receipt=receipt,
            strategy_evaluations=final_evaluation.strategies,
        )
        self._reset_signal()
        self._record(decision, final_context)
        return decision

    def _confirmed_candidate(
        self,
        candidate: StrategyCandidate | None,
        context: StrategyContext,
    ) -> tuple[StrategyCandidate | None, CandidateRejectionReceipt | None]:
        source_at = context.current_spot[0]
        key = (
            (
                context.market.market_id,
                candidate.strategy_id,
                candidate.direction,
            )
            if candidate is not None
            else None
        )
        if (
            self._last_signal_source_at is not None
            and source_at <= self._last_signal_source_at
        ):
            if key != self._signal_key:
                self._reset_signal()
            return None, None
        self._last_signal_source_at = source_at
        if self._clock() >= context.market.window_end - MINIMUM_LIVE_ORDER_LEAD:
            self._reset_signal()
            return None, None
        if key is None:
            self._reset_signal()
            return None, None
        reversal = (
            self._signal_key is not None
            and key[:2] == self._signal_key[:2]
            and key[2] != self._signal_key[2]
        )
        observation = SignalObservation(
            source_at=source_at,
            terminal_probability=candidate.terminal_probability,
            max_price=candidate.max_price,
        )
        if key != self._signal_key:
            self._signal_key = key
            self._signal_count = 1
            self._signal_observations = (observation,)
        else:
            self._signal_count += 1
            keep = self._required_signal_confirmations - 1
            prior = self._signal_observations[-keep:] if keep else ()
            self._signal_observations = (*prior, observation)
        if reversal:
            return None, CandidateRejectionReceipt(
                state="REJECTED",
                code="SIGNAL_DIRECTION_REVERSED",
                message="signal direction reversed inside the confirmation window",
            )
        if self._signal_count < self._required_signal_confirmations:
            return None, None
        rejection = self._stability_rejection(self._signal_observations)
        return (candidate, None) if rejection is None else (None, rejection)

    def _final_signal_rejection(
        self,
        initial: StrategyCandidate,
        final: StrategyCandidate | None,
        context: StrategyContext,
    ) -> CandidateRejectionReceipt | None:
        if final is None:
            return CandidateRejectionReceipt(
                state="REJECTED",
                code="FINAL_SIGNAL_DISAPPEARED",
                message="strategy signal disappeared in the final snapshot",
            )
        if (
            context.market.market_id != self._signal_key[0]
            or final.strategy_id != initial.strategy_id
            or final.direction != initial.direction
            or final.token_id != initial.token_id
        ):
            return CandidateRejectionReceipt(
                state="REJECTED",
                code="FINAL_SIGNAL_IDENTITY_CHANGED",
                message="market, strategy, token, or direction changed before signing",
            )
        keep = self._required_signal_confirmations - 1
        prior = self._signal_observations[-keep:] if keep else ()
        observations = (
            *prior,
            SignalObservation(
                source_at=context.current_spot[0],
                terminal_probability=final.terminal_probability,
                max_price=final.max_price,
            ),
        )
        return self._stability_rejection(observations)

    @staticmethod
    def _stability_rejection(
        observations: Sequence[SignalObservation],
    ) -> CandidateRejectionReceipt | None:
        probabilities = tuple(
            observation.terminal_probability for observation in observations
        )
        if any(value < MINIMUM_TERMINAL_PROBABILITY for value in probabilities):
            code, message = (
                "SIGNAL_PROBABILITY_BELOW_FLOOR",
                "terminal probability fell below 0.60",
            )
        elif any(
            observation.max_price > MAXIMUM_ENTRY_PRICE
            for observation in observations
        ):
            code, message = (
                "SIGNAL_PRICE_ABOVE_CAP",
                "maximum entry price rose above 0.75",
            )
        elif len(probabilities) >= 3 and all(
            later < earlier for earlier, later in zip(probabilities, probabilities[1:])
        ):
            code, message = (
                "SIGNAL_PROBABILITY_DECAY",
                "terminal probability declined throughout the confirmation window",
            )
        elif max(probabilities[:-1], default=probabilities[-1]) - probabilities[
            -1
        ] > MAXIMUM_PROBABILITY_PULLBACK:
            code, message = (
                "SIGNAL_PROBABILITY_PULLBACK",
                "terminal probability pulled back by more than 0.03",
            )
        else:
            return None
        return CandidateRejectionReceipt(
            state="REJECTED",
            code=code,
            message=message,
        )

    def _reset_signal(self) -> None:
        self._signal_key = None
        self._signal_count = 0
        self._signal_observations = ()

    def _record(self, decision: Decision, context: StrategyContext) -> None:
        self._audit.record(decision)
        if self._observer is not None:
            self._observer(decision, context)


class StrategyOptimizer:
    """Deterministic grid search over replay snapshots; never owns execution."""

    def optimize(
        self,
        grid: Iterable[Mapping[str, object]],
        snapshots: Sequence[StrategyContext],
        build: Callable[[Mapping[str, object]], Sequence[Strategy]],
        score: Callable[[tuple[Decision, ...]], Decimal],
    ) -> OptimizationResult:
        results: list[OptimizationResult] = []
        for parameters in grid:
            decisions = ReplayRunner(StrategyEngine(build(parameters))).run(snapshots)
            value = score(decisions)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError("optimizer score must be a finite Decimal")
            results.append(
                OptimizationResult(
                    parameters=tuple(
                        sorted((str(key), str(item)) for key, item in parameters.items())
                    ),
                    score=value,
                    signal_count=sum(item.candidate is not None for item in decisions),
                )
            )
        if not results:
            raise ValueError("optimizer grid must not be empty")
        return max(results, key=lambda item: (item.score, item.parameters))
