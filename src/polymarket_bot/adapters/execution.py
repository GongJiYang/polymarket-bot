"""Fail-closed bridge from strategy decisions to one official-SDK FAK POST."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable, Protocol

from polymarket_bot.contracts import (
    ExecutionPreparationRejected,
    StrategyCandidate,
    StrategyContext,
)
from polymarket_bot.live.bounded_bot import (
    FinalDecisionSnapshot,
    ImmediateBuyIntent,
    LiveSessionAuthorization,
    build_immediate_buy_intent,
)
from polymarket_bot.live.order_executor import (
    OfficialOrderError,
    OfficialOrderTransport,
    PreparedFakOrder,
    ProtectedFakReceipt,
)
from polymarket_bot.live.sdk_account_readonly import ReadOnlyAccountSnapshot
from polymarket_bot.microstructure.models import MarketMetadata, OrderBook, OutcomeSide

USDC_SCALE = Decimal(1_000_000)
MAX_PRE_POST_BOOK_AGE = timedelta(seconds=2)
FINAL_OBSERVATION_NAMES = frozenset(
    {"official_price", "market", "up_book", "down_book", "fee"}
)


class ExecutionAdapterError(RuntimeError):
    """The external execution boundary cannot safely continue."""


class AccountReader(Protocol):
    def snapshot(self, *, execution_only: bool = False) -> ReadOnlyAccountSnapshot: ...


@dataclass(frozen=True, slots=True)
class BoundedPreparedOrder:
    intent_hash: str
    maximum_all_in_debit: Decimal
    sdk_order: PreparedFakOrder
    signing_started_at: datetime
    signing_completed_at: datetime


@dataclass(frozen=True, slots=True)
class BoundedSubmissionReceipt:
    state: str
    post_attempted: bool
    order_id: str | None
    making_amount: Decimal | None
    taking_amount: Decimal | None
    code: str | None = None
    message: str | None = None
    signing_started_at: datetime | None = None
    signing_completed_at: datetime | None = None
    pre_post_book_requested_at: datetime | None = None
    pre_post_book_received_at: datetime | None = None
    pre_post_book_exchange_at: datetime | None = None
    pre_post_book_adapter_received_at: datetime | None = None
    post_started_at: datetime | None = None
    post_response_at: datetime | None = None
    post_recheck_book_requested_at: datetime | None = None
    post_recheck_book_received_at: datetime | None = None
    post_recheck_book_exchange_at: datetime | None = None
    post_recheck_book_adapter_received_at: datetime | None = None
    post_recheck_top_ask: Decimal | None = None
    post_recheck_executable_depth: Decimal | None = None
    post_recheck_error: str | None = None


class BoundedExecutionAdapter:
    """Revalidate, freeze, sign, and POST one capacity-reserved BUY.

    The runner must reserve ``maximum_all_in_debit`` before calling ``submit``.
    This adapter performs no retries and has no allowance-approval operation.
    """

    def __init__(
        self,
        *,
        transport: OfficialOrderTransport,
        account: AccountReader,
        authorization: LiveSessionAuthorization,
        require_unblocked: Callable[[], None],
        read_book: Callable[[MarketMetadata, OutcomeSide], OrderBook],
        clock: Callable[[], datetime],
    ) -> None:
        self._transport = transport
        self._account = account
        self._authorization = authorization
        self._require_unblocked = require_unblocked
        self._read_book = read_book
        self._clock = clock
        self._intents: dict[
            str, tuple[ImmediateBuyIntent, MarketMetadata, OutcomeSide]
        ] = {}

    def prepare(
        self, candidate: StrategyCandidate, context: StrategyContext
    ) -> BoundedPreparedOrder:
        expected_market_family = (
            f"{context.market.asset} Up/Down {context.market.interval.value}"
        )
        if self._authorization.market_family != expected_market_family:
            raise ExecutionPreparationRejected(
                "AUTHORIZATION_MARKET_FAMILY_MISMATCH",
                "candidate market family does not match authorization",
            )
        if candidate.strategy_id != self._authorization.model_version:
            raise ExecutionPreparationRejected(
                "AUTHORIZATION_MODEL_MISMATCH",
                "candidate strategy does not match authorization",
            )
        if context.threshold != self._authorization.minimum_threshold:
            raise ExecutionPreparationRejected(
                "AUTHORIZATION_THRESHOLD_MISMATCH",
                "candidate threshold does not match authorization",
            )
        if (
            candidate.source != context.source
            or candidate.opening != context.opening
            or candidate.prices != context.prices
        ):
            raise ExecutionPreparationRejected(
                "CANDIDATE_INPUTS_MISMATCH",
                "candidate model inputs are not bound to the final context",
            )
        if candidate.book is not (
            context.up_book if candidate.direction == "up" else context.down_book
        ):
            raise ExecutionAdapterError("candidate book is not bound to the snapshot")
        def observed(call: Callable[[], object]) -> tuple[object, datetime]:
            value = call()
            return value, self._clock()

        with ThreadPoolExecutor(max_workers=2) as executor:
            geoblock_future = executor.submit(observed, self._require_unblocked)
            account_future = executor.submit(
                observed, lambda: self._account.snapshot(execution_only=True)
            )
            _, geoblock_at = geoblock_future.result()
            account_value, account_at = account_future.result()
        if not isinstance(account_value, ReadOnlyAccountSnapshot):
            raise ExecutionPreparationRejected(
                "FINAL_ACCOUNT_MALFORMED",
                "final account snapshot has an unexpected type",
            )
        account = account_value
        if account.wallet != self._authorization.wallet:
            raise ExecutionPreparationRejected(
                "FINAL_ACCOUNT_CHANGED",
                "account wallet does not match authorization",
            )
        observed_names = {name for name, _ in context.observed_at}
        if observed_names != FINAL_OBSERVATION_NAMES:
            raise ExecutionPreparationRejected(
                "FINAL_SNAPSHOT_INCOMPLETE",
                "final public observation set is incomplete",
            )
        now = self._clock()
        selected_book = candidate.book
        try:
            snapshot = FinalDecisionSnapshot.create(
                condition_id=context.market.market_id,
                token_id=candidate.token_id,
                window_start=context.market.window_start,
                window_end=context.market.window_end,
                resolution_source=context.source,
                opening_benchmark=context.opening,
                source_observations=context.prices,
                asks=tuple((level.price, level.size) for level in selected_book.asks),
                tick_size=selected_book.tick_size,
                minimum_size=context.fee.minimum_size,
                fee_rate=context.fee.rate,
                fee_exponent=context.fee.exponent,
                balance_raw=account.collateral_balance_raw,
                allowances_raw=account.collateral_allowances_raw,
                geoblock_blocked=False,
                observed_at=(
                    *context.observed_at,
                    ("geoblock", geoblock_at),
                    ("account", account_at),
                ),
                strategy_inputs=(
                    ("minimum_threshold", str(context.threshold)),
                    ("signal_depth_quantity", str(context.quantity)),
                    (
                        "target_all_in_debit",
                        str(self._authorization.max_order_debit),
                    ),
                    ("current_spot", str(context.current_spot[1])),
                    ("current_spot_at", context.current_spot[0].isoformat()),
                ),
                strategy_outputs=(
                    ("strategy_id", candidate.strategy_id),
                    ("direction", candidate.direction),
                    ("signal_metric", candidate.signal_metric),
                    ("signal_strength", str(candidate.signal_strength)),
                    ("terminal_probability", str(candidate.terminal_probability)),
                    (
                        "volatility_per_sqrt_second",
                        str(candidate.volatility_per_sqrt_second),
                    ),
                    ("top_ask", str(candidate.top_ask)),
                    ("max_price", str(candidate.max_price)),
                    ("expected_fill_price", str(candidate.expected_fill_price)),
                    ("fee_per_share", str(candidate.fee_per_share)),
                    ("net_edge", str(candidate.net_edge)),
                ),
            )
        except ValueError as exc:
            raise ExecutionPreparationRejected(
                "FINAL_SNAPSHOT_INVALID", str(exc)
            ) from exc
        intent = build_immediate_buy_intent(
            snapshot=snapshot,
            authorization=self._authorization,
            max_price=candidate.max_price,
            target_all_in_debit=self._authorization.max_order_debit,
            now=now,
        )
        required_raw = intent.maximum_all_in_debit * USDC_SCALE
        if account.collateral_balance_raw < required_raw:
            raise ExecutionPreparationRejected(
                "FINAL_BALANCE_INSUFFICIENT",
                "collateral balance is below the debit bound",
            )
        if not any(
            allowance >= required_raw
            for _, allowance in account.collateral_allowances_raw
        ):
            raise ExecutionPreparationRejected(
                "FINAL_ALLOWANCE_INSUFFICIENT",
                "collateral allowance is below the debit bound",
            )
        signing_started_at = self._clock()
        try:
            sdk_order = self._transport.prepare_protected_fak_buy(intent)
        except OfficialOrderError as exc:
            raise ExecutionPreparationRejected(
                "PREPARE_SIGNING_REJECTED", str(exc)
            ) from exc
        signing_completed_at = self._clock()
        outcome = OutcomeSide.UP if candidate.direction == "up" else OutcomeSide.DOWN
        self._intents[intent.intent_hash] = (intent, context.market, outcome)
        return BoundedPreparedOrder(
            intent_hash=intent.intent_hash,
            maximum_all_in_debit=intent.maximum_all_in_debit,
            sdk_order=sdk_order,
            signing_started_at=signing_started_at,
            signing_completed_at=signing_completed_at,
        )

    def submit(self, prepared: BoundedPreparedOrder) -> BoundedSubmissionReceipt:
        pending = self._intents.pop(prepared.intent_hash, None)
        if pending is None or prepared.sdk_order.intent_hash != prepared.intent_hash:
            raise ExecutionAdapterError("prepared order is unknown or already submitted")
        intent, market, outcome = pending
        now = self._clock()
        if now >= intent.expires_at or not self._authorization.is_valid_at(now):
            raise ExecutionAdapterError("authorization or intent expired after signing")
        pre_post_book_requested_at = self._clock()
        with ThreadPoolExecutor(max_workers=2) as executor:
            geoblock = executor.submit(self._require_unblocked)
            latest_book = executor.submit(self._read_book, market, outcome)
            geoblock.result()
            book = latest_book.result()
        pre_post_book_received_at = self._clock()
        checked_at = pre_post_book_received_at
        if (
            book.market_id != market.market_id
            or book.token_id != intent.token_id
            or book.outcome is not outcome
        ):
            raise ExecutionAdapterError("pre-post book identity changed")
        if book.tick_size != intent.tick_size:
            return BoundedSubmissionReceipt(
                state="REJECTED",
                post_attempted=False,
                order_id=None,
                making_amount=None,
                taking_amount=None,
                code="PRE_POST_TICK_CHANGED",
                message="tick size changed after the order was signed",
                signing_started_at=prepared.signing_started_at,
                signing_completed_at=prepared.signing_completed_at,
                pre_post_book_requested_at=pre_post_book_requested_at,
                pre_post_book_received_at=pre_post_book_received_at,
                pre_post_book_exchange_at=book.exchange_at,
                pre_post_book_adapter_received_at=book.received_at,
            )
        if (
            book.received_at > checked_at
            or book.exchange_at > checked_at
            or checked_at - book.received_at > MAX_PRE_POST_BOOK_AGE
            or checked_at - book.exchange_at > MAX_PRE_POST_BOOK_AGE
        ):
            return BoundedSubmissionReceipt(
                state="REJECTED",
                post_attempted=False,
                order_id=None,
                making_amount=None,
                taking_amount=None,
                code="PRE_POST_BOOK_STALE",
                message="last-moment order book is older than two seconds",
                signing_started_at=prepared.signing_started_at,
                signing_completed_at=prepared.signing_completed_at,
                pre_post_book_requested_at=pre_post_book_requested_at,
                pre_post_book_received_at=pre_post_book_received_at,
                pre_post_book_exchange_at=book.exchange_at,
                pre_post_book_adapter_received_at=book.received_at,
            )
        executable_depth = sum(
            (
                level.size
                for level in book.asks
                if level.price <= intent.max_price
            ),
            Decimal(0),
        )
        target_shares = prepared.sdk_order.taker_amount
        if not book.tradable or executable_depth < target_shares:
            return BoundedSubmissionReceipt(
                state="REJECTED",
                post_attempted=False,
                order_id=None,
                making_amount=None,
                taking_amount=None,
                code="PRE_POST_LIQUIDITY_GONE",
                message=(
                    f"executable depth {executable_depth} is below signed target "
                    f"{target_shares} at or below {intent.max_price}"
                ),
                signing_started_at=prepared.signing_started_at,
                signing_completed_at=prepared.signing_completed_at,
                pre_post_book_requested_at=pre_post_book_requested_at,
                pre_post_book_received_at=pre_post_book_received_at,
                pre_post_book_exchange_at=book.exchange_at,
                pre_post_book_adapter_received_at=book.received_at,
            )
        post_started_at = self._clock()
        receipt = self._transport.post_prepared_fak(prepared.sdk_order)
        post_response_at = self._clock()
        state = "FILLED" if receipt.accepted and receipt.status == "matched" else "REJECTED"
        post_recheck_book_requested_at = None
        post_recheck_book_received_at = None
        post_recheck_book_exchange_at = None
        post_recheck_book_adapter_received_at = None
        post_recheck_top_ask = None
        post_recheck_executable_depth = None
        post_recheck_error = None
        if state == "REJECTED":
            post_recheck_book_requested_at = self._clock()
            try:
                post_recheck = self._read_book(market, outcome)
                post_recheck_book_received_at = self._clock()
                if (
                    post_recheck.market_id != market.market_id
                    or post_recheck.token_id != intent.token_id
                    or post_recheck.outcome is not outcome
                ):
                    raise ExecutionAdapterError("post-rejection book identity changed")
                post_recheck_book_exchange_at = post_recheck.exchange_at
                post_recheck_book_adapter_received_at = post_recheck.received_at
                post_recheck_top_ask = (
                    post_recheck.asks[0].price if post_recheck.asks else None
                )
                post_recheck_executable_depth = sum(
                    (
                        level.size
                        for level in post_recheck.asks
                        if level.price <= intent.max_price
                    ),
                    Decimal(0),
                )
            except Exception as exc:
                post_recheck_book_received_at = self._clock()
                post_recheck_error = f"{type(exc).__name__}: {exc}"
        return BoundedSubmissionReceipt(
            state=state,
            post_attempted=True,
            order_id=receipt.order_id,
            making_amount=receipt.making_amount,
            taking_amount=receipt.taking_amount,
            code=receipt.code,
            message=receipt.message,
            signing_started_at=prepared.signing_started_at,
            signing_completed_at=prepared.signing_completed_at,
            pre_post_book_requested_at=pre_post_book_requested_at,
            pre_post_book_received_at=pre_post_book_received_at,
            pre_post_book_exchange_at=book.exchange_at,
            pre_post_book_adapter_received_at=book.received_at,
            post_started_at=post_started_at,
            post_response_at=post_response_at,
            post_recheck_book_requested_at=post_recheck_book_requested_at,
            post_recheck_book_received_at=post_recheck_book_received_at,
            post_recheck_book_exchange_at=post_recheck_book_exchange_at,
            post_recheck_book_adapter_received_at=post_recheck_book_adapter_received_at,
            post_recheck_top_ask=post_recheck_top_ask,
            post_recheck_executable_depth=post_recheck_executable_depth,
            post_recheck_error=post_recheck_error,
        )
