"""Fail-closed order execution and the sole official-SDK write adapter.

All production signing, posting, and cancellation calls stay confined to this
module. Callers receive only order identities and redacted lifecycle facts.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_UP, Decimal
from typing import Any

from polymarket.errors import RateLimitError, RequestRejectedError, TransportError

from polymarket_bot.live.approval import ApprovalRecord, ApprovalService
from polymarket_bot.live.order_intent import OrderIntent
from polymarket_bot.live.bounded_bot import (
    ImmediateBuyIntent,
    worst_fee_ratio,
)


class ExecutionBlocked(RuntimeError):
    pass


class OfficialOrderError(RuntimeError):
    """Redacted failure from the official order transport."""


class SettlementReadUnavailable(OfficialOrderError):
    """Transient GET failure; no statement about settlement or POST outcome."""


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    intent_hash: str
    order_id: str
    recovered_after_timeout: bool = False


@dataclass(frozen=True, slots=True)
class SubmissionReceipt:
    order_id: str
    status: str
    making_amount: Decimal
    taking_amount: Decimal


@dataclass(frozen=True, slots=True)
class ProtectedFakReceipt:
    accepted: bool
    code: str | None = None
    message: str | None = None
    order_id: str | None = None
    status: str | None = None
    making_amount: Decimal | None = None
    taking_amount: Decimal | None = None
    average_price: Decimal | None = None
    trade_ids: tuple[str, ...] = ()
    transaction_hashes: tuple[str, ...] = ()

@dataclass(frozen=True, slots=True)
class PreparedFakOrder:
    intent_hash: str
    signed_order: Any
    maker_amount: Decimal
    taker_amount: Decimal
    fee_bound: Decimal
    all_in_bound: Decimal
    max_price: Decimal


@dataclass(frozen=True, slots=True)
class PreparedFakSell:
    signed_order: Any
    shares: Decimal
    min_price: Decimal



class IdempotentOrderExecutor:
    def __init__(
        self,
        *,
        preflight: Callable[[OrderIntent], dict[str, object]],
        submit: Callable[[OrderIntent], str],
        lookup: Callable[[str], str | None],
        approvals: ApprovalService,
    ) -> None:
        self._preflight = preflight
        self._submit = submit
        self._lookup = lookup
        self._approvals = approvals
        self._results: dict[str, ExecutionResult] = {}

    def execute(
        self, intent: OrderIntent, approval: ApprovalRecord, *, now
    ) -> ExecutionResult:
        if intent.intent_hash in self._results:
            return self._results[intent.intent_hash]
        if now.tzinfo is None or now.utcoffset() is None or now >= intent.expiration:
            raise ExecutionBlocked("order intent has expired or clock is invalid")
        checks = self._preflight(intent)
        required = {"geoblock", "market", "book", "balance", "allowance", "risk"}
        boolean_checks = required - {"geoblock"}
        if (
            type(checks) is not dict
            or set(checks) != required
            or type(checks["geoblock"]) is not dict
            or checks["geoblock"] != {"blocked": False}
            or any(checks[key] is not True for key in boolean_checks)
        ):
            raise ExecutionBlocked("exact preflight checks did not all pass")
        self._approvals.consume(intent, approval, now=now)
        try:
            order_id = self._submit(intent)
            recovered = False
        except TimeoutError:
            order_id = self._lookup(intent.intent_hash)
            recovered = True
            if order_id is None:
                raise ExecutionBlocked(
                    "submission outcome unknown; blind retry forbidden"
                )
        if type(order_id) is not str or not order_id:
            raise ExecutionBlocked("transport returned no order identity")
        result = ExecutionResult(intent.intent_hash, order_id, recovered)
        self._results[intent.intent_hash] = result
        return result


class OfficialOrderTransport:
    """Narrow official-SDK adapter for one post-only GTD limit order.

    A signed order is created exactly once for each immutable intent. If the
    POST transport fails, ``recover_submission`` only searches for that exact
    order among open orders; it never retries the POST.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self._intents: dict[str, OrderIntent] = {}
        self._signed_orders: dict[str, Any] = {}
        self._receipts: dict[str, SubmissionReceipt] = {}
        self._pending_sells: dict[int, PreparedFakSell] = {}

    def prepare_protected_fak_sell(
        self, *, token_id: str, shares: Decimal, min_price: Decimal
    ) -> PreparedFakSell:
        """Sign a reduce-only caller-bound SELL; never recover allowances."""
        if (
            not token_id or not shares.is_finite() or shares <= 0
            or shares != shares.quantize(Decimal("0.01"))
            or not min_price.is_finite() or not Decimal(0) < min_price < Decimal(1)
        ):
            raise OfficialOrderError("invalid protected SELL bounds")
        try:
            signed = self._client.create_market_order(
                token_id=token_id, side="SELL", shares=shares,
                min_price=min_price, order_type="FAK",
            )
        except Exception as exc:
            raise OfficialOrderError("official SDK could not sign protected SELL") from exc
        maker = getattr(signed, "maker_amount", None)
        taker = getattr(signed, "taker_amount", None)
        if (
            type(maker) is not int or type(taker) is not int
            or maker != shares * Decimal(1_000_000) or taker <= 0
            or Decimal(taker) / Decimal(maker) < min_price
            or str(getattr(signed, "token_id", "")) != token_id
            or getattr(signed, "side", None) != "SELL"
        ):
            raise OfficialOrderError("signed SELL violates position or price bounds")
        prepared = PreparedFakSell(signed, shares, min_price)
        self._pending_sells[id(prepared)] = prepared
        return prepared

    def post_prepared_sell(self, prepared: PreparedFakSell) -> ProtectedFakReceipt:
        """Consume the signed order before POST; uncertainty is never retried."""
        if self._pending_sells.pop(id(prepared), None) is not prepared:
            raise OfficialOrderError("SELL is unknown or already submitted")
        try:
            response = self._client.post_order(prepared.signed_order)
        except Exception as exc:
            raise OfficialOrderError("SELL outcome unknown; reconciliation required") from exc
        shares = getattr(response, "making_amount", None)
        gross = getattr(response, "taking_amount", None)
        order_id = getattr(response, "order_id", None)
        if (
            getattr(response, "ok", None) is not True
            or getattr(response, "status", None) != "matched"
            or not isinstance(order_id, str) or not order_id
            or not isinstance(shares, Decimal) or not shares.is_finite()
            or not Decimal(0) < shares <= prepared.shares
            or not isinstance(gross, Decimal) or not gross.is_finite() or gross <= 0
            or not prepared.min_price <= gross / shares <= Decimal(1)
        ):
            raise OfficialOrderError("SELL response ambiguous; reconciliation required")
        return ProtectedFakReceipt(
            accepted=True, order_id=order_id, status="matched",
            making_amount=shares, taking_amount=gross, average_price=gross / shares,
        )

    def confirmed_order_amounts(
        self, *, order_id: str, token_id: str, market_id: str, side: str
    ) -> tuple[Decimal, Decimal] | None:
        """Read exact taker fills; matched/mined trades are not settled inventory."""
        try:
            trades = self._client.list_account_trades(
                token_id=token_id, market=market_id,
            ).iter_items()
            seen: dict[str, tuple] = {}
            shares, gross = Decimal(0), Decimal(0)
            for index, trade in enumerate(trades):
                if index >= 1000:
                    raise OfficialOrderError("exact-order trade lookup exceeded bound")
                if str(getattr(trade, "taker_order_id", "")) != order_id:
                    continue
                if (
                    getattr(trade, "trader_side", None) != "TAKER"
                    or str(getattr(trade, "token_id", "")) != token_id
                    or getattr(trade, "side", None) != side
                    or str(getattr(trade, "condition_id", "")) != market_id
                ):
                    raise OfficialOrderError("exact-order trade identity mismatch")
                trade_id = str(getattr(trade, "id", ""))
                if not trade_id:
                    raise OfficialOrderError("trade has no identity")
                fingerprint = (getattr(trade, "status", None), getattr(trade, "size", None),
                               getattr(trade, "price", None))
                if trade_id in seen:
                    if seen[trade_id] != fingerprint:
                        raise OfficialOrderError("conflicting duplicate settlement trade")
                    continue
                seen[trade_id] = fingerprint
                status = getattr(trade, "status", None)
                if status == "FAILED":
                    raise OfficialOrderError("order contains failed settlement; reconcile")
                if status != "CONFIRMED":
                    return None
                size, price = getattr(trade, "size", None), getattr(trade, "price", None)
                if (
                    not isinstance(size, Decimal) or not size.is_finite() or size <= 0
                    or not isinstance(price, Decimal) or not price.is_finite()
                    or not Decimal(0) < price <= Decimal(1)
                ):
                    raise OfficialOrderError("trade amounts are malformed")
                shares += size
                gross += size * price
            return (shares, gross) if seen else None
        except OfficialOrderError:
            raise
        except (TimeoutError, TransportError, RateLimitError) as exc:
            raise SettlementReadUnavailable("settlement read temporarily unavailable") from exc
        except Exception as exc:
            raise OfficialOrderError("exact-order settlement read failed") from exc

    def submit_intent(self, intent: OrderIntent) -> str:
        """Submit one post-only GTD limit intent."""
        return self._submit_limit_intent(intent, post_only=True)

    def submit_marketable_intent(self, intent: OrderIntent) -> str:
        """Submit one bounded, non-post-only GTD limit intent."""
        return self._submit_limit_intent(intent, post_only=False)

    def _submit_limit_intent(self, intent: OrderIntent, *, post_only: bool) -> str:
        try:
            signed = self._signed_orders.get(intent.intent_hash)
            if signed is None:
                signed = self._client.create_limit_order(
                    token_id=intent.token_id,
                    price=intent.price,
                    size=intent.size,
                    side=intent.side,
                    post_only=post_only,
                    expiration=int(intent.expiration.timestamp()),
                )
                self._signed_orders[intent.intent_hash] = signed
                self._intents[intent.intent_hash] = intent
            response = self._client.post_order(signed)
        except Exception as exc:
            if _is_transport_failure(exc):
                raise TimeoutError(
                    "official order submission outcome is unknown"
                ) from exc
            raise OfficialOrderError("official SDK rejected order submission") from exc
        if getattr(response, "ok", None) is not True:
            code = getattr(response, "code", "unknown")
            raise OfficialOrderError(f"official CLOB rejected order: {code}")
        order_id = getattr(response, "order_id", None)
        status = getattr(response, "status", None)
        making_amount = getattr(response, "making_amount", None)
        taking_amount = getattr(response, "taking_amount", None)
        if (
            type(order_id) is not str
            or not order_id
            or type(status) is not str
            or not isinstance(making_amount, Decimal)
            or not isinstance(taking_amount, Decimal)
        ):
            raise OfficialOrderError("official CLOB returned a malformed acceptance")
        self._receipts[order_id] = SubmissionReceipt(
            order_id=order_id,
            status=status,
            making_amount=making_amount,
            taking_amount=taking_amount,
        )
        return order_id

    def prepare_protected_fak_buy(
        self, intent: ImmediateBuyIntent
    ) -> PreparedFakOrder:
        """Sign exactly once, then verify the resulting wire amounts before POST."""
        if intent.intent_hash in self._signed_orders:
            raise OfficialOrderError("protected FAK intent was already signed")
        try:
            signed = self._client.create_market_order(
                token_id=intent.token_id,
                side="BUY",
                amount=intent.principal_cap,
                max_spend=intent.sdk_max_spend,
                max_price=intent.max_price,
                order_type="FAK",
            )
        except Exception as exc:
            raise OfficialOrderError("official SDK could not sign protected FAK") from exc
        maker_raw = getattr(signed, "maker_amount", None)
        taker_raw = getattr(signed, "taker_amount", None)
        if (
            type(maker_raw) is not int
            or maker_raw <= 0
            or type(taker_raw) is not int
            or taker_raw <= 0
            or str(getattr(signed, "token_id", "")) != intent.token_id
            or getattr(signed, "side", None) != "BUY"
        ):
            raise OfficialOrderError("signed protected FAK fields are malformed")
        maker = Decimal(maker_raw) / Decimal(1_000_000)
        taker = Decimal(taker_raw) / Decimal(1_000_000)
        if maker > intent.principal_cap:
            raise OfficialOrderError("signed maker collateral exceeds principal cap")
        if taker < intent.minimum_expected_fill_shares:
            raise OfficialOrderError("signed order falls below target shares")
        maximum_fee_ratio = worst_fee_ratio(
            max_price=intent.max_price,
            tick=intent.tick_size,
            rate=intent.fee_rate,
            exponent=intent.fee_exponent,
        )
        fee_bound = (maker * maximum_fee_ratio).quantize(
            Decimal("0.000001"), rounding=ROUND_UP
        )
        all_in = maker + fee_bound
        if all_in > intent.maximum_all_in_debit:
            raise OfficialOrderError("signed all-in debit exceeds authorization cap")
        self._signed_orders[intent.intent_hash] = signed
        return PreparedFakOrder(
            intent_hash=intent.intent_hash,
            signed_order=signed,
            maker_amount=maker,
            taker_amount=taker,
            fee_bound=fee_bound,
            all_in_bound=all_in,
            max_price=intent.max_price,
        )

    def post_prepared_fak(self, prepared: PreparedFakOrder) -> ProtectedFakReceipt:
        """Issue the sole POST. No transport failure is retried."""
        if self._signed_orders.get(prepared.intent_hash) is not prepared.signed_order:
            raise OfficialOrderError("prepared FAK is not bound to this transport")
        try:
            response = self._client.post_order(prepared.signed_order)
        except RateLimitError:
            return ProtectedFakReceipt(
                accepted=False,
                code="rate_limited",
                message="official CLOB rejected FAK",
            )
        except RequestRejectedError as exc:
            if 400 <= exc.status < 500 and exc.status != 408:
                return ProtectedFakReceipt(
                    accepted=False,
                    code=exc.code or f"http_{exc.status}",
                    message=str(exc) or "official CLOB rejected FAK",
                )
            raise OfficialOrderError(
                "official FAK POST failed; outcome is unknown and retry is forbidden"
            ) from exc
        except Exception as exc:
            raise OfficialOrderError(
                "official FAK POST failed; outcome is unknown and retry is forbidden"
            ) from exc
        return self._parse_protected_fak_response(
            response, max_price=prepared.max_price
        )

    def _parse_protected_fak_response(
        self, response: Any, *, max_price: Decimal
    ) -> ProtectedFakReceipt:
        if getattr(response, "ok", None) is False:
            return ProtectedFakReceipt(
                accepted=False,
                code=str(getattr(response, "code", "unknown")),
                message=str(getattr(response, "message", "")),
            )
        order_id = getattr(response, "order_id", None)
        status = getattr(response, "status", None)
        if (
            getattr(response, "ok", None) is not True
            or type(order_id) is not str
            or not order_id
            or type(status) is not str
            or not status
        ):
            raise OfficialOrderError("official CLOB returned a malformed FAK response")
        if status != "matched":
            try:
                self._client.cancel_order(order_id)
            except Exception as exc:
                raise OfficialOrderError(
                    "FAK returned a non-matched status and defensive cancellation failed"
                ) from exc
            raise OfficialOrderError(
                "FAK returned a non-matched status; cancelled defensively"
            )
        making_amount = getattr(response, "making_amount", None)
        taking_amount = getattr(response, "taking_amount", None)
        if (
            not isinstance(making_amount, Decimal)
            or not making_amount.is_finite()
            or making_amount < 0
            or not isinstance(taking_amount, Decimal)
            or not taking_amount.is_finite()
            or taking_amount <= 0
        ):
            raise OfficialOrderError("official CLOB returned malformed FAK fill amounts")
        average_price = making_amount / taking_amount
        if average_price > max_price:
            raise OfficialOrderError("reported FAK fill exceeded the signed maximum price")
        return ProtectedFakReceipt(
            accepted=True,
            order_id=order_id,
            status=status,
            making_amount=making_amount,
            taking_amount=taking_amount,
            average_price=average_price,
            trade_ids=tuple(str(value) for value in getattr(response, "trade_ids", ())),
            transaction_hashes=tuple(
                str(value) for value in getattr(response, "transactions_hashes", ())
            ),
        )

    def submit_protected_fak_buy(
        self, *, token_id: str, max_spend: Decimal, max_price: Decimal
    ) -> ProtectedFakReceipt:
        """Sign and post one price-protected FAK BUY without allowance recovery."""
        if (
            type(token_id) is not str
            or not token_id
            or not isinstance(max_spend, Decimal)
            or not max_spend.is_finite()
            or max_spend <= 0
            or not isinstance(max_price, Decimal)
            or not max_price.is_finite()
            or not Decimal(0) < max_price < Decimal(1)
        ):
            raise OfficialOrderError("protected FAK parameters are invalid")
        try:
            signed = self._client.create_market_order(
                token_id=token_id,
                side="BUY",
                amount=max_spend,
                max_spend=max_spend,
                max_price=max_price,
                order_type="FAK",
            )
            response = self._client.post_order(signed)
        except Exception as exc:
            raise OfficialOrderError(
                "official FAK submission failed; outcome may be unknown and was not retried"
            ) from exc

        if getattr(response, "ok", None) is False:
            return ProtectedFakReceipt(
                accepted=False,
                code=str(getattr(response, "code", "unknown")),
                message=str(getattr(response, "message", "")),
            )
        order_id = getattr(response, "order_id", None)
        status = getattr(response, "status", None)
        if (
            getattr(response, "ok", None) is not True
            or type(order_id) is not str
            or not order_id
            or type(status) is not str
            or not status
        ):
            raise OfficialOrderError("official CLOB returned a malformed FAK response")
        if status != "matched":
            try:
                self._client.cancel_order(order_id)
            except Exception as exc:
                raise OfficialOrderError(
                    "FAK returned a non-matched status and defensive cancellation failed"
                ) from exc
            raise OfficialOrderError(
                "FAK returned a non-matched status; cancelled defensively"
            )

        making_amount = getattr(response, "making_amount", None)
        taking_amount = getattr(response, "taking_amount", None)
        if (
            not isinstance(making_amount, Decimal)
            or not making_amount.is_finite()
            or making_amount < 0
            or not isinstance(taking_amount, Decimal)
            or not taking_amount.is_finite()
            or taking_amount <= 0
        ):
            raise OfficialOrderError(
                "official CLOB returned malformed FAK fill amounts"
            )
        average_price = making_amount / taking_amount
        if average_price > max_price:
            raise OfficialOrderError(
                "reported FAK fill exceeded the signed maximum price"
            )
        return ProtectedFakReceipt(
            accepted=True,
            order_id=order_id,
            status=status,
            making_amount=making_amount,
            taking_amount=taking_amount,
            average_price=average_price,
            trade_ids=tuple(str(value) for value in getattr(response, "trade_ids", ())),
            transaction_hashes=tuple(
                str(value) for value in getattr(response, "transactions_hashes", ())
            ),
        )

    def filled_quantity(
        self, *, order_id: str, token_id: str, market_id: str
    ) -> Decimal:
        """Return the deduplicated quantity matched to one exact order."""
        try:
            trades = self._client.list_account_trades(
                token_id=token_id,
                market=market_id,
            ).iter_items()
            total = Decimal(0)
            seen: set[str] = set()
            for trade in trades:
                trade_id = str(getattr(trade, "id", ""))
                if not trade_id or trade_id in seen:
                    continue
                seen.add(trade_id)
                quantity = Decimal(0)
                if (
                    str(getattr(trade, "taker_order_id", "")) == order_id
                    and getattr(trade, "trader_side", None) == "TAKER"
                ):
                    quantity = getattr(trade, "size", Decimal(0))
                else:
                    for maker_order in getattr(trade, "maker_orders", ()):
                        if (
                            str(getattr(maker_order, "order_id", "")) == order_id
                            and str(getattr(maker_order, "token_id", "")) == token_id
                        ):
                            quantity += getattr(
                                maker_order, "matched_amount", Decimal(0)
                            )
                if not isinstance(quantity, Decimal) or quantity < 0:
                    raise OfficialOrderError(
                        "official CLOB returned a malformed fill quantity"
                    )
                total += quantity
        except OfficialOrderError:
            raise
        except Exception as exc:
            raise OfficialOrderError(
                "official SDK exact-order fill lookup failed"
            ) from exc
        return total

    def conditional_balance_allowance(
        self, *, token_id: str
    ) -> tuple[int, tuple[int, ...]]:
        """Read one conditional-token balance and its existing allowances."""
        try:
            value = self._client.get_balance_allowance(
                asset_type="CONDITIONAL",
                token_id=token_id,
            )
            balance = getattr(value, "balance", None)
            allowances = getattr(value, "allowances", None)
        except Exception as exc:
            raise OfficialOrderError(
                "official SDK conditional balance lookup failed"
            ) from exc
        if (
            type(balance) is not int
            or balance < 0
            or not isinstance(allowances, dict)
            or any(
                type(amount) is not int or amount < 0 for amount in allowances.values()
            )
        ):
            raise OfficialOrderError(
                "official CLOB returned malformed conditional balance data"
            )
        return balance, tuple(allowances.values())

    def recover_submission(self, intent_hash: str) -> str | None:
        intent = self._intents.get(intent_hash)
        if intent is None:
            return None
        try:
            matches = [
                order
                for order in self._client.list_open_orders(
                    token_id=intent.token_id,
                    market=intent.market_id,
                ).iter_items()
                if _open_order_matches(order, intent)
            ]
        except Exception as exc:
            raise OfficialOrderError(
                "could not reconcile an unknown submission outcome"
            ) from exc
        if len(matches) > 1:
            raise OfficialOrderError(
                "submission reconciliation is ambiguous; manual review required"
            )
        if not matches:
            return None
        order_id = getattr(matches[0], "id", None)
        if type(order_id) is not str or not order_id:
            raise OfficialOrderError("reconciled order has no identity")
        return order_id

    def receipt(self, order_id: str) -> SubmissionReceipt | None:
        return self._receipts.get(order_id)

    def cancel_exact(self, order_id: str) -> bool:
        """Cancel one known order; never broad-cancels."""
        try:
            response = self._client.cancel_order(order_id=order_id)
        except Exception as exc:
            raise OfficialOrderError("official SDK exact-order cancel failed") from exc
        canceled = tuple(str(value) for value in getattr(response, "canceled", ()))
        not_canceled = getattr(response, "not_canceled", {})
        if order_id in canceled:
            return True
        if isinstance(not_canceled, dict) and order_id in {
            str(value) for value in not_canceled
        }:
            return False
        raise OfficialOrderError("official CLOB returned a malformed cancel response")


def _open_order_matches(order: Any, intent: OrderIntent) -> bool:
    expires_at = getattr(order, "expires_at", None)
    return (
        str(getattr(order, "condition_id", "")) == intent.market_id
        and str(getattr(order, "token_id", "")) == intent.token_id
        and getattr(order, "side", None) == intent.side
        and getattr(order, "price", None) == intent.price
        and getattr(order, "original_size", None) == intent.size
        and expires_at is not None
        and int(expires_at.timestamp()) == int(intent.expiration.timestamp())
    )


def _is_transport_failure(exc: Exception) -> bool:
    return isinstance(exc, (TimeoutError, TransportError))
