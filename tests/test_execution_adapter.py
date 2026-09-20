from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Barrier

import pytest
from polymarket.errors import RequestRejectedError

from polymarket_bot.adapters.execution import BoundedExecutionAdapter, ExecutionAdapterError
from polymarket_bot.contracts import (
    ExecutionPreparationRejected,
    FeeMetadata,
    StrategyCandidate,
    StrategyContext,
)
from polymarket_bot.live.bounded_bot import LiveSessionAuthorization
from polymarket_bot.live.order_executor import (
    OfficialOrderError,
    OfficialOrderTransport,
    PreparedFakOrder,
    ProtectedFakReceipt,
)
from polymarket_bot.live.sdk_account_readonly import ReadOnlyAccountSnapshot
from polymarket_bot.microstructure.models import (
    BookLevel,
    MarketInterval,
    MarketMetadata,
    OrderBook,
    OutcomeSide,
)

NOW = datetime(2026, 8, 25, 12, 1, tzinfo=timezone.utc)
WALLET = "0x" + "1" * 40


def setup() -> tuple[StrategyCandidate, StrategyContext, LiveSessionAuthorization]:
    market = MarketMetadata(
        market_id="condition",
        title="Bitcoin Up or Down",
        rules="Chainlink",
        asset="BTC",
        interval=MarketInterval.FIVE_MINUTES,
        window_start=NOW.replace(minute=0, second=0, microsecond=0),
        window_end=NOW.replace(minute=5, second=0, microsecond=0),
        up_token_id="up",
        down_token_id="down",
        outcomes=("Up", "Down"),
    )
    up = OrderBook(
        market_id="condition",
        token_id="up",
        outcome=OutcomeSide.UP,
        sequence=1,
        bids=(BookLevel(price=Decimal("0.44"), size=Decimal("20")),),
        asks=(BookLevel(price=Decimal("0.45"), size=Decimal("20")),),
        tick_size=Decimal("0.01"),
        exchange_at=NOW,
        received_at=NOW,
        tradable=True,
    )
    down = up.model_copy(update={"token_id": "down", "outcome": OutcomeSide.DOWN})
    context = StrategyContext(
        market=market,
        up_book=up,
        down_book=down,
        fee=FeeMetadata(Decimal("0"), Decimal("0"), Decimal("5")),
        source="https://data.chain.link/streams/btc-usd",
        opening=Decimal("70000"),
        prices=((NOW - timedelta(seconds=10), Decimal("70000")),),
        current_spot=(NOW, Decimal("70010")),
        threshold=Decimal("0.04"),
        quantity=Decimal("5"),
        observed_at=(
            ("official_price", NOW),
            ("market", NOW),
            ("up_book", NOW),
            ("down_book", NOW),
            ("fee", NOW),
        ),
    )
    candidate = StrategyCandidate(
        strategy_id="fixed",
        direction="up",
        token_id="up",
        book=up,
        source=context.source,
        opening=context.opening,
        prices=context.prices,
        terminal_probability=Decimal("0.70"),
        volatility_per_sqrt_second=Decimal("0.001"),
        top_ask=Decimal("0.45"),
        max_price=Decimal("0.46"),
        expected_fill_price=Decimal("0.46"),
        fee_per_share=Decimal("0"),
        net_edge=Decimal("0.24"),
        signal_metric="test",
        signal_strength=Decimal("0.24"),
    )
    authorization = LiveSessionAuthorization.create(
        authorization_id="authorization",
        approved_by="operator",
        wallet=WALLET,
        market_family="BTC Up/Down 5m",
        model_version="fixed",
        minimum_threshold=Decimal("0.04"),
        max_order_debit=Decimal("6"),
        max_session_debit=Decimal("6"),
        max_post_attempts=1,
        approved_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=1),
    )
    return candidate, context, authorization


def test_authorization_normalizes_checksum_case_wallet() -> None:
    _, _, authorization = setup()
    mixed_case = "0x" + "Aa" * 20
    normalized = LiveSessionAuthorization.create(
        authorization_id=authorization.authorization_id,
        approved_by=authorization.approved_by,
        wallet=mixed_case,
        market_family=authorization.market_family,
        model_version=authorization.model_version,
        minimum_threshold=authorization.minimum_threshold,
        max_order_debit=authorization.max_order_debit,
        max_session_debit=authorization.max_session_debit,
        max_post_attempts=authorization.max_post_attempts,
        approved_at=authorization.approved_at,
        expires_at=authorization.expires_at,
    )

    assert normalized.wallet == mixed_case.lower()


@pytest.mark.parametrize(
    ("changes", "code"),
    (
        (
            {"market_family": "ETH Up/Down 5m"},
            "AUTHORIZATION_MARKET_FAMILY_MISMATCH",
        ),
        (
            {"model_version": "other"},
            "AUTHORIZATION_MODEL_MISMATCH",
        ),
        (
            {"minimum_threshold": Decimal("0.05")},
            "AUTHORIZATION_THRESHOLD_MISMATCH",
        ),
    ),
)
def test_adapter_rejects_candidate_outside_authorization(
    changes: dict[str, object], code: str
) -> None:
    candidate, context, authorization = setup()
    values = {
        name: getattr(authorization, name)
        for name in (
            "authorization_id",
            "approved_by",
            "wallet",
            "market_family",
            "model_version",
            "minimum_threshold",
            "max_order_debit",
            "max_session_debit",
            "max_post_attempts",
            "approved_at",
            "expires_at",
        )
    }
    changed = LiveSessionAuthorization.create(**(values | changes))
    adapter = BoundedExecutionAdapter(
        transport=Transport(),
        account=Account(),
        authorization=changed,
        require_unblocked=lambda: None,
        read_book=lambda market, outcome: context.up_book,
        clock=lambda: NOW,
    )

    with pytest.raises(ExecutionPreparationRejected) as caught:
        adapter.prepare(candidate, context)

    assert caught.value.code == code


def test_adapter_rejects_candidate_model_inputs_from_another_context() -> None:
    candidate, context, authorization = setup()
    changed = replace(candidate, opening=Decimal("69999"))
    adapter = BoundedExecutionAdapter(
        transport=Transport(),
        account=Account(),
        authorization=authorization,
        require_unblocked=lambda: None,
        read_book=lambda market, outcome: context.up_book,
        clock=lambda: NOW,
    )

    with pytest.raises(ExecutionPreparationRejected) as caught:
        adapter.prepare(changed, context)

    assert caught.value.code == "CANDIDATE_INPUTS_MISMATCH"


class Account:
    def __init__(self, allowance: str = "10000000") -> None:
        self.allowance = Decimal(allowance)

    def snapshot(self, *, execution_only: bool = False) -> ReadOnlyAccountSnapshot:
        assert execution_only
        return ReadOnlyAccountSnapshot(
            wallet=WALLET,
            wallet_type="DEPOSIT_WALLET",
            collateral_balance_raw=Decimal("10000000"),
            collateral_allowances_raw=(("spender", self.allowance),),
            positions=(),
            open_orders=(),
            account_trades=(),
            activity=(),
        )


class Transport:
    def __init__(self) -> None:
        self.posts = 0
        self.intent = None

    def prepare_protected_fak_buy(self, intent):
        self.intent = intent
        return PreparedFakOrder(
            intent_hash=intent.intent_hash,
            signed_order=object(),
            maker_amount=intent.principal_cap,
            taker_amount=intent.minimum_expected_fill_shares,
            fee_bound=intent.maximum_all_in_debit - intent.principal_cap,
            all_in_bound=intent.maximum_all_in_debit,
            max_price=intent.max_price,
        )

    def post_prepared_fak(self, prepared):
        self.posts += 1
        return ProtectedFakReceipt(
            accepted=True,
            order_id="order",
            status="matched",
            making_amount=Decimal("2.3"),
            taking_amount=Decimal("5"),
        )

def test_transport_preserves_definitive_clob_rejection_message() -> None:
    signed_order = object()

    class RejectingClient:
        def post_order(self, signed: object) -> object:
            assert signed is signed_order
            raise RequestRejectedError(
                "no orders found to match",
                status=400,
                code=None,
            )

    transport = OfficialOrderTransport(RejectingClient())
    transport._signed_orders["intent"] = signed_order
    receipt = transport.post_prepared_fak(
        PreparedFakOrder(
            intent_hash="intent",
            signed_order=signed_order,
            maker_amount=Decimal("2.3"),
            taker_amount=Decimal("5"),
            fee_bound=Decimal("0"),
            all_in_bound=Decimal("2.3"),
            max_price=Decimal("0.46"),
        )
    )

    assert receipt.accepted is False
    assert receipt.code == "http_400"
    assert receipt.message == "no orders found to match"



def test_adapter_freezes_intent_checks_capacity_and_posts_once() -> None:
    candidate, context, authorization = setup()
    transport = Transport()
    checks: list[str] = []
    adapter = BoundedExecutionAdapter(
        transport=transport,
        account=Account(),
        authorization=authorization,
        require_unblocked=lambda: checks.append("geoblock"),
        read_book=lambda market, outcome: context.up_book,
        clock=lambda: NOW,
    )
    prepared = adapter.prepare(candidate, context)
    assert prepared.maximum_all_in_debit == Decimal("6.000000")
    assert transport.intent is not None
    assert transport.intent.minimum_expected_fill_shares == Decimal("13.043478")
    assert transport.intent.minimum_expected_fill_shares != context.quantity
    receipt = adapter.submit(prepared)
    assert receipt.state == "FILLED"
    assert receipt.post_attempted is True
    assert sorted(checks) == ["geoblock", "geoblock"]
    assert transport.posts == 1
    assert prepared.signing_started_at == NOW
    assert prepared.signing_completed_at == NOW
    assert receipt.pre_post_book_requested_at == NOW
    assert receipt.pre_post_book_received_at == NOW
    assert receipt.pre_post_book_exchange_at == NOW
    assert receipt.pre_post_book_adapter_received_at == NOW
    assert receipt.post_started_at == NOW
    assert receipt.post_response_at == NOW
    assert receipt.post_recheck_book_requested_at is None
    with pytest.raises(ExecutionAdapterError, match="already submitted"):
        adapter.submit(prepared)
    assert transport.posts == 1


def test_adapter_targets_six_dollars_including_worst_case_fee() -> None:
    candidate, context, authorization = setup()
    context = replace(
        context,
        fee=FeeMetadata(Decimal("0.25"), Decimal("2"), Decimal("5")),
    )
    transport = Transport()
    adapter = BoundedExecutionAdapter(
        transport=transport,
        account=Account(),
        authorization=authorization,
        require_unblocked=lambda: None,
        read_book=lambda market, outcome: context.up_book,
        clock=lambda: NOW,
    )

    prepared = adapter.prepare(candidate, context)

    assert Decimal("0") <= Decimal("6") - prepared.maximum_all_in_debit < Decimal("0.011")
    assert transport.intent is not None
    assert transport.intent.principal_cap < Decimal("6")
    assert transport.intent.principal_cap == transport.intent.principal_cap.quantize(
        Decimal("0.01")
    )
    assert transport.posts == 0


def test_adapter_never_auto_approves_allowance() -> None:
    candidate, context, authorization = setup()
    adapter = BoundedExecutionAdapter(
        transport=Transport(),
        account=Account(allowance="0"),
        authorization=authorization,
        require_unblocked=lambda: None,
        read_book=lambda market, outcome: context.up_book,
        clock=lambda: NOW,
    )
    with pytest.raises(ExecutionPreparationRejected) as caught:
        adapter.prepare(candidate, context)
    assert caught.value.code == "FINAL_ALLOWANCE_INSUFFICIENT"


def test_adapter_rejects_stale_final_public_observation() -> None:
    candidate, context, authorization = setup()
    stale = NOW - timedelta(seconds=3)
    context = replace(
        context,
        observed_at=tuple((name, stale) for name, _ in context.observed_at),
    )
    adapter = BoundedExecutionAdapter(
        transport=Transport(),
        account=Account(),
        authorization=authorization,
        require_unblocked=lambda: None,
        read_book=lambda market, outcome: context.up_book,
        clock=lambda: NOW,
    )

    with pytest.raises(ExecutionPreparationRejected) as caught:
        adapter.prepare(candidate, context)

    assert caught.value.code == "FINAL_SNAPSHOT_INVALID"


def test_adapter_observes_geoblock_and_account_concurrently() -> None:
    candidate, context, authorization = setup()
    rendezvous = Barrier(2, timeout=1)

    class ConcurrentAccount(Account):
        def snapshot(self, *, execution_only: bool = False) -> ReadOnlyAccountSnapshot:
            rendezvous.wait()
            return super().snapshot(execution_only=execution_only)

    def require_unblocked() -> None:
        rendezvous.wait()

    adapter = BoundedExecutionAdapter(
        transport=Transport(),
        account=ConcurrentAccount(),
        authorization=authorization,
        require_unblocked=require_unblocked,
        read_book=lambda market, outcome: context.up_book,
        clock=lambda: NOW,
    )

    prepared = adapter.prepare(candidate, context)

    assert prepared.intent_hash


def test_adapter_classifies_signing_rejection_before_capacity_reservation() -> None:
    candidate, context, authorization = setup()

    class SigningRejectedTransport(Transport):
        def prepare_protected_fak_buy(self, intent):
            raise OfficialOrderError("signed order falls below target shares")

    adapter = BoundedExecutionAdapter(
        transport=SigningRejectedTransport(),
        account=Account(),
        authorization=authorization,
        require_unblocked=lambda: None,
        read_book=lambda market, outcome: context.up_book,
        clock=lambda: NOW,
    )

    with pytest.raises(ExecutionPreparationRejected) as caught:
        adapter.prepare(candidate, context)

    assert caught.value.code == "PREPARE_SIGNING_REJECTED"


def test_adapter_skips_post_when_last_moment_depth_has_disappeared() -> None:
    candidate, context, authorization = setup()
    thin_book = context.up_book.model_copy(
        update={
            "asks": (BookLevel(price=Decimal("0.45"), size=Decimal("1")),),
        }
    )
    transport = Transport()
    adapter = BoundedExecutionAdapter(
        transport=transport,
        account=Account(),
        authorization=authorization,
        require_unblocked=lambda: None,
        read_book=lambda market, outcome: thin_book,
        clock=lambda: NOW,
    )

    receipt = adapter.submit(adapter.prepare(candidate, context))

    assert receipt.state == "REJECTED"
    assert receipt.code == "PRE_POST_LIQUIDITY_GONE"
    assert receipt.post_attempted is False
    assert "executable depth 1" in str(receipt.message)
    assert transport.posts == 0


def test_adapter_rechecks_book_after_definitive_no_match() -> None:
    candidate, context, authorization = setup()
    books_read = 0

    class NoMatchTransport(Transport):
        def post_prepared_fak(self, prepared):
            self.posts += 1
            return ProtectedFakReceipt(
                accepted=False,
                code="http_400",
                message="no orders found to match with FAK order",
            )

    def read_book(_market, _outcome):
        nonlocal books_read
        books_read += 1
        if books_read == 1:
            return context.up_book
        return context.up_book.model_copy(
            update={
                "asks": (
                    BookLevel(price=Decimal("0.47"), size=Decimal("20")),
                )
            }
        )

    transport = NoMatchTransport()
    adapter = BoundedExecutionAdapter(
        transport=transport,
        account=Account(),
        authorization=authorization,
        require_unblocked=lambda: None,
        read_book=read_book,
        clock=lambda: NOW,
    )

    receipt = adapter.submit(adapter.prepare(candidate, context))

    assert receipt.state == "REJECTED"
    assert receipt.code == "http_400"
    assert receipt.post_attempted is True
    assert receipt.post_recheck_book_requested_at == NOW
    assert receipt.post_recheck_book_received_at == NOW
    assert receipt.post_recheck_top_ask == Decimal("0.47")
    assert receipt.post_recheck_executable_depth == Decimal("0")
    assert receipt.post_recheck_error is None
    assert books_read == 2
    assert transport.posts == 1
