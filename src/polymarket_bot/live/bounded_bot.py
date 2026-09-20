"""Immutable contracts and one-shot capacity control for bounded live sessions."""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, ROUND_UP, Decimal, InvalidOperation
from typing import Any

from polymarket_bot.live.account_identity import _address
from polymarket_bot.live.authentication import canonical_json
from polymarket_bot.live.transaction_cost import fee_per_share

ZERO = Decimal(0)
ONE = Decimal(1)
USDC_SCALE = Decimal(1_000_000)
MARKET_BUY_AMOUNT_QUANTUM = Decimal("0.01")
MAX_SNAPSHOT_SKEW = timedelta(seconds=2)
MAX_INTENT_TTL = timedelta(seconds=5)


class BoundedBotError(RuntimeError):
    """A stable fail-closed error at the bounded-session boundary."""


class OrderDebitCapExceeded(BoundedBotError):
    """The candidate cannot buy the required shares within its all-in cap."""


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be UTC")
    return value


def _positive(value: Decimal, name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= ZERO:
        raise ValueError(f"{name} must be a finite positive Decimal")
    return value


def _json_value(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return _utc(value, "hashed datetime").isoformat()
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def _hash(payload: dict[str, object]) -> str:
    return hashlib.sha256(canonical_json(_json_value(payload))).hexdigest()


@dataclass(frozen=True, slots=True)
class LiveSessionAuthorization:
    authorization_id: str
    approved_by: str
    wallet: str
    market_family: str
    model_version: str
    minimum_threshold: Decimal
    max_order_debit: Decimal
    max_session_debit: Decimal
    max_post_attempts: int
    approved_at: datetime
    expires_at: datetime
    authorization_hash: str

    @classmethod
    def create(
        cls,
        *,
        authorization_id: str,
        approved_by: str,
        wallet: str,
        market_family: str,
        model_version: str,
        minimum_threshold: Decimal,
        max_order_debit: Decimal,
        max_session_debit: Decimal,
        max_post_attempts: int,
        approved_at: datetime,
        expires_at: datetime,
    ) -> "LiveSessionAuthorization":
        payload = cls._payload(
            authorization_id,
            approved_by,
            wallet,
            market_family,
            model_version,
            minimum_threshold,
            max_order_debit,
            max_session_debit,
            max_post_attempts,
            approved_at,
            expires_at,
        )
        return cls(
            authorization_id=authorization_id,
            approved_by=approved_by,
            wallet=_address(wallet, "wallet"),
            market_family=market_family,
            model_version=model_version,
            minimum_threshold=minimum_threshold,
            max_order_debit=max_order_debit,
            max_session_debit=max_session_debit,
            max_post_attempts=max_post_attempts,
            approved_at=approved_at,
            expires_at=expires_at,
            authorization_hash=_hash(payload),
        )

    @staticmethod
    def _payload(
        authorization_id: str,
        approved_by: str,
        wallet: str,
        market_family: str,
        model_version: str,
        minimum_threshold: Decimal,
        max_order_debit: Decimal,
        max_session_debit: Decimal,
        max_post_attempts: int,
        approved_at: datetime,
        expires_at: datetime,
    ) -> dict[str, object]:
        return {
            "authorization_id": authorization_id.strip(),
            "approved_by": approved_by.strip(),
            "wallet": _address(wallet, "wallet"),
            "market_family": market_family.strip(),
            "model_version": model_version.strip(),
            "minimum_threshold": str(minimum_threshold),
            "max_order_debit": str(max_order_debit),
            "max_session_debit": str(max_session_debit),
            "max_post_attempts": max_post_attempts,
            "approved_at": _utc(approved_at, "approved_at").isoformat(),
            "expires_at": _utc(expires_at, "expires_at").isoformat(),
        }

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.authorization_id,
                self.approved_by,
                self.market_family,
                self.model_version,
            )
        ):
            raise ValueError("authorization identity fields must not be empty")
        _positive(self.minimum_threshold, "minimum_threshold")
        _positive(self.max_order_debit, "max_order_debit")
        _positive(self.max_session_debit, "max_session_debit")
        if self.max_order_debit > self.max_session_debit:
            raise ValueError("max_order_debit exceeds max_session_debit")
        if type(self.max_post_attempts) is not int or self.max_post_attempts != 1:
            raise ValueError("max_post_attempts must equal one")
        if _utc(self.expires_at, "expires_at") <= _utc(self.approved_at, "approved_at"):
            raise ValueError("authorization expiry must follow approval")
        expected = _hash(
            self._payload(
                self.authorization_id,
                self.approved_by,
                self.wallet,
                self.market_family,
                self.model_version,
                self.minimum_threshold,
                self.max_order_debit,
                self.max_session_debit,
                self.max_post_attempts,
                self.approved_at,
                self.expires_at,
            )
        )
        if self.authorization_hash != expected:
            raise ValueError("authorization_hash does not cover the authorization")

    def is_valid_at(self, instant: datetime) -> bool:
        try:
            return self.approved_at <= _utc(instant, "instant") < self.expires_at
        except ValueError:
            return False


@dataclass(frozen=True, slots=True)
class FinalDecisionSnapshot:
    condition_id: str
    token_id: str
    window_start: datetime
    window_end: datetime
    resolution_source: str
    opening_benchmark: Decimal
    source_observations: tuple[tuple[datetime, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]
    tick_size: Decimal
    minimum_size: Decimal
    fee_rate: Decimal
    fee_exponent: Decimal
    balance_raw: Decimal
    allowances_raw: tuple[tuple[str, Decimal], ...]
    geoblock_blocked: bool
    observed_at: tuple[tuple[str, datetime], ...]
    strategy_inputs: tuple[tuple[str, str], ...]
    strategy_outputs: tuple[tuple[str, str], ...]
    snapshot_hash: str

    @classmethod
    def create(cls, **values: Any) -> "FinalDecisionSnapshot":
        payload = cls._payload(values)
        return cls(**values, snapshot_hash=_hash(payload))

    @staticmethod
    def _payload(values: dict[str, Any]) -> dict[str, object]:
        return {key: value for key, value in values.items() if key != "snapshot_hash"}

    def __post_init__(self) -> None:
        if not self.condition_id or not self.token_id or not self.resolution_source:
            raise ValueError("snapshot identities must not be empty")
        if "chain.link" not in self.resolution_source.lower():
            raise ValueError("snapshot resolution source must be Chainlink")
        _utc(self.window_start, "window_start")
        _utc(self.window_end, "window_end")
        if self.window_start >= self.window_end:
            raise ValueError("market window is invalid")
        _positive(self.opening_benchmark, "opening_benchmark")
        _positive(self.tick_size, "tick_size")
        _positive(self.minimum_size, "minimum_size")
        if self.fee_rate < ZERO or self.fee_exponent < ZERO:
            raise ValueError("fee metadata must be nonnegative")
        if self.balance_raw < ZERO or not self.allowances_raw:
            raise ValueError("account capacity is unavailable")
        if self.geoblock_blocked is not False:
            raise ValueError("geoblock must be exactly unblocked")
        if not self.source_observations or not self.asks or not self.observed_at:
            raise ValueError("snapshot data must be complete")
        source_times = [
            _utc(at, "source observed_at") for at, _ in self.source_observations
        ]
        if any(now <= before for before, now in zip(source_times, source_times[1:])):
            raise ValueError("source observations must be strictly ordered")
        times = [_utc(at, f"{name} observed_at") for name, at in self.observed_at]
        if max(times) - min(times) > MAX_SNAPSHOT_SKEW:
            raise ValueError("final observation skew exceeds two seconds")
        expected = _hash(
            self._payload(
                {field: getattr(self, field) for field in self.__dataclass_fields__}
            )
        )
        if self.snapshot_hash != expected:
            raise ValueError("snapshot_hash does not cover the final snapshot")


@dataclass(frozen=True, slots=True)
class ImmediateBuyIntent:
    token_id: str
    condition_id: str
    side: str
    principal_cap: Decimal
    sdk_max_spend: Decimal
    max_price: Decimal
    tick_size: Decimal
    minimum_expected_fill_shares: Decimal
    fee_rate: Decimal
    fee_exponent: Decimal
    maximum_all_in_debit: Decimal
    decision_snapshot_hash: str
    authorization_hash: str
    expires_at: datetime
    intent_hash: str

    def __post_init__(self) -> None:
        if self.side != "BUY":
            raise ValueError("bounded intent side must be BUY")
        for name in (
            "principal_cap",
            "sdk_max_spend",
            "max_price",
            "tick_size",
            "minimum_expected_fill_shares",
            "maximum_all_in_debit",
        ):
            _positive(getattr(self, name), name)
        if not ZERO < self.max_price < ONE:
            raise ValueError("max_price must be between zero and one")
        _utc(self.expires_at, "expires_at")




@dataclass(frozen=True, slots=True)
class BuyDebitSizing:
    max_price: Decimal
    principal_cap: Decimal
    minimum_fill_shares: Decimal
    maximum_all_in_debit: Decimal


def worst_fee_ratio(
    *, max_price: Decimal, tick: Decimal, rate: Decimal, exponent: Decimal
) -> Decimal:
    _positive(tick, "tick")
    points = int((max_price / tick).to_integral_value(rounding=ROUND_DOWN))
    if points < 1 or points > 100_000:
        raise BoundedBotError("executable fee range is invalid")
    return max(
        fee_per_share(tick * index, rate, exponent) / (tick * index)
        for index in range(1, points + 1)
    )
def size_buy_debit(
    *,
    max_price: Decimal,
    tick: Decimal,
    rate: Decimal,
    exponent: Decimal,
    target_all_in_debit: Decimal,
) -> BuyDebitSizing:
    rounded_price = (max_price / tick).to_integral_value(
        rounding=ROUND_DOWN
    ) * tick
    if rounded_price <= ZERO:
        raise BoundedBotError("maximum price rounds to zero")
    target = _positive(target_all_in_debit, "target_all_in_debit")
    maximum_fee_ratio = worst_fee_ratio(
        max_price=rounded_price,
        tick=tick,
        rate=rate,
        exponent=exponent,
    )
    # Match the official SDK's two-decimal FAK BUY collateral rounding.
    principal = (target / (ONE + maximum_fee_ratio)).quantize(
        MARKET_BUY_AMOUNT_QUANTUM, rounding=ROUND_DOWN
    )
    minimum_shares = (principal / rounded_price).quantize(
        Decimal("0.000001"), rounding=ROUND_DOWN
    )
    maximum_all_in = (principal * (ONE + maximum_fee_ratio)).quantize(
        Decimal("0.000001"), rounding=ROUND_UP
    )
    if maximum_all_in > target:
        raise BoundedBotError("debit quantization exceeded the target")
    return BuyDebitSizing(
        max_price=rounded_price,
        principal_cap=principal,
        minimum_fill_shares=minimum_shares,
        maximum_all_in_debit=maximum_all_in,
    )




def build_immediate_buy_intent(
    *,
    snapshot: FinalDecisionSnapshot,
    authorization: LiveSessionAuthorization,
    max_price: Decimal,
    target_all_in_debit: Decimal,
    now: datetime,
) -> ImmediateBuyIntent:
    _utc(now, "now")
    if not authorization.is_valid_at(now):
        raise BoundedBotError("session authorization is not valid")
    sizing = size_buy_debit(
        max_price=max_price,
        tick=snapshot.tick_size,
        rate=snapshot.fee_rate,
        exponent=snapshot.fee_exponent,
        target_all_in_debit=target_all_in_debit,
    )
    if target_all_in_debit > authorization.max_order_debit:
        raise OrderDebitCapExceeded(
            "target all-in debit exceeds the authorized order debit cap"
        )
    if sizing.minimum_fill_shares < snapshot.minimum_size:
        raise BoundedBotError("target debit buys less than the market minimum")
    expires_at = min(
        now + MAX_INTENT_TTL, authorization.expires_at, snapshot.window_end
    )
    payload = {
        "token_id": snapshot.token_id,
        "condition_id": snapshot.condition_id,
        "side": "BUY",
        "principal_cap": str(sizing.principal_cap),
        "sdk_max_spend": str(sizing.maximum_all_in_debit),
        "max_price": str(sizing.max_price),
        "tick_size": str(snapshot.tick_size),
        "minimum_expected_fill_shares": str(sizing.minimum_fill_shares),
        "fee_rate": str(snapshot.fee_rate),
        "fee_exponent": str(snapshot.fee_exponent),
        "maximum_all_in_debit": str(sizing.maximum_all_in_debit),
        "decision_snapshot_hash": snapshot.snapshot_hash,
        "authorization_hash": authorization.authorization_hash,
        "expires_at": expires_at.isoformat(),
    }
    return ImmediateBuyIntent(
        **payload
        | {
            "principal_cap": sizing.principal_cap,
            "sdk_max_spend": sizing.maximum_all_in_debit,
            "max_price": sizing.max_price,
            "tick_size": snapshot.tick_size,
            "minimum_expected_fill_shares": sizing.minimum_fill_shares,
            "fee_rate": snapshot.fee_rate,
            "fee_exponent": snapshot.fee_exponent,
            "maximum_all_in_debit": sizing.maximum_all_in_debit,
            "expires_at": expires_at,
            "intent_hash": _hash(payload),
        }
    )


class SessionCapacityLedger:
    """Atomically reserve the sole POST and release proven pre-POST rejects."""

    def __init__(self, authorization: LiveSessionAuthorization) -> None:
        self._lock = threading.Lock()
        self._remaining_posts = authorization.max_post_attempts
        self._remaining_debit = authorization.max_session_debit
        self._reserved_intent_hash: str | None = None
        self._reserved_debit: Decimal | None = None
        self._terminal_state: str | None = None

    @property
    def state(self) -> tuple[int, Decimal, str | None, str | None]:
        with self._lock:
            return (
                self._remaining_posts,
                self._remaining_debit,
                self._reserved_intent_hash,
                self._terminal_state,
            )

    def reserve(self, intent: ImmediateBuyIntent) -> None:
        with self._lock:
            if (
                self._terminal_state is not None
                or self._reserved_intent_hash is not None
            ):
                raise BoundedBotError("session capacity has already been consumed")
            if (
                self._remaining_posts != 1
                or intent.maximum_all_in_debit > self._remaining_debit
            ):
                raise BoundedBotError("session capacity is insufficient")
            self._remaining_posts = 0
            self._remaining_debit -= intent.maximum_all_in_debit
            self._reserved_intent_hash = intent.intent_hash
            self._reserved_debit = intent.maximum_all_in_debit
            self._terminal_state = "RESERVED"

    def release(self, intent: ImmediateBuyIntent) -> None:
        with self._lock:
            if (
                self._terminal_state != "RESERVED"
                or self._reserved_intent_hash != intent.intent_hash
                or self._reserved_debit != intent.maximum_all_in_debit
            ):
                raise BoundedBotError("cannot release an unmatched reservation")
            self._remaining_posts += 1
            self._remaining_debit += self._reserved_debit
            self._reserved_intent_hash = None
            self._reserved_debit = None
            self._terminal_state = None


    def close(self, state: str) -> None:
        with self._lock:
            if self._reserved_intent_hash is None:
                raise BoundedBotError("cannot close an unreserved session")
            if not state.strip():
                raise ValueError("terminal state must not be empty")
            self._terminal_state = state
