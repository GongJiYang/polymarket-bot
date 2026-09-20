"""Immutable data contracts for read-only Polymarket microstructure ingestion."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PositiveDecimal = Annotated[Decimal, Field(gt=Decimal("0"), allow_inf_nan=False)]
NonNegativeDecimal = Annotated[Decimal, Field(ge=Decimal("0"), allow_inf_nan=False)]
ProbabilityDecimal = Annotated[
    Decimal,
    Field(ge=Decimal("0"), le=Decimal("1"), allow_inf_nan=False),
]


class FrozenModel(BaseModel):
    """Base contract that rejects undeclared data and cannot be mutated."""

    model_config = ConfigDict(
        allow_inf_nan=False,
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class SpotConnectionState(StrEnum):
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    DISCONNECTED = "disconnected"


class SpotTick(FrozenModel):
    source: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    bid: PositiveDecimal
    ask: PositiveDecimal
    last: PositiveDecimal | None = None
    exchange_at: datetime
    received_at: datetime
    sequence: int | None = Field(default=None, ge=0)

    @field_validator("source", "symbol")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text fields must not be blank")
        return value

    @field_validator("exchange_at", "received_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return require_utc(value)

    @model_validator(mode="after")
    def _exchange_not_after_receipt(self) -> "SpotTick":
        if self.exchange_at > self.received_at:
            raise ValueError("exchange_at cannot be after received_at")
        if self.bid > self.ask:
            raise ValueError("bid cannot exceed ask")
        return self

    @property
    def mid(self) -> Decimal:
        """Return the deterministic top-of-book midpoint, never an inferred last."""

        return (self.bid + self.ask) / Decimal("2")


class SpotSourceStatus(FrozenModel):
    source: str = Field(min_length=1)
    state: SpotConnectionState
    latest_tick: SpotTick | None = None
    changed_at: datetime
    failure: str | None = None

    @field_validator("changed_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return require_utc(value)


class MarketInterval(StrEnum):
    FIVE_MINUTES = "5m"
    FIFTEEN_MINUTES = "15m"

    @property
    def seconds(self) -> int:
        return 300 if self is MarketInterval.FIVE_MINUTES else 900


class WindowPosition(StrEnum):
    CURRENT = "current"
    NEXT = "next"


class OutcomeSide(StrEnum):
    UP = "up"
    DOWN = "down"


class MarketMetadata(FrozenModel):
    market_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    rules: str = Field(min_length=1)
    asset: str = Field(min_length=1)
    interval: MarketInterval
    window_start: datetime
    window_end: datetime
    up_token_id: str = Field(min_length=1)
    down_token_id: str = Field(min_length=1)
    outcomes: tuple[str, str]
    active: bool = True

    @field_validator("window_start", "window_end")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return require_utc(value)

    @field_validator(
        "market_id", "title", "rules", "asset", "up_token_id", "down_token_id"
    )
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("market metadata text must not be blank")
        return value

    @model_validator(mode="after")
    def _valid_window_and_tokens(self) -> "MarketMetadata":
        if self.window_end <= self.window_start:
            raise ValueError("window_end must be after window_start")
        if (
            self.window_end - self.window_start
        ).total_seconds() != self.interval.seconds:
            raise ValueError("window length does not match interval")
        if self.window_start.timestamp() % self.interval.seconds != 0:
            raise ValueError("window_start is not aligned to interval")
        if self.up_token_id == self.down_token_id:
            raise ValueError("outcome token ids must be distinct")
        normalized = tuple(item.strip().lower() for item in self.outcomes)
        if normalized != ("up", "down"):
            raise ValueError("outcomes must be ordered exactly as Up, Down")
        return self


class DiscoveredMarket(FrozenModel):
    metadata: MarketMetadata
    position: WindowPosition
    discovered_at: datetime

    @field_validator("discovered_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return require_utc(value)


class ReferenceSourceSpec(FrozenModel):
    source: str = Field(min_length=1)
    asset: str = Field(min_length=1)
    interval: MarketInterval
    window_start: datetime

    @field_validator("window_start")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return require_utc(value)


class WindowReference(FrozenModel):
    market_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    asset: str = Field(min_length=1)
    interval: MarketInterval
    window_start: datetime
    opening_price: PositiveDecimal
    exchange_at: datetime
    received_at: datetime

    @field_validator("window_start", "exchange_at", "received_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return require_utc(value)

    @model_validator(mode="after")
    def _valid_timestamps(self) -> "WindowReference":
        if self.exchange_at != self.window_start:
            raise ValueError("reference exchange_at must equal the window start")
        if self.exchange_at > self.received_at:
            raise ValueError("exchange_at cannot be after received_at")
        return self


class ClockReading(FrozenModel):
    server_at: datetime
    local_midpoint_at: datetime
    observed_at: datetime
    round_trip_seconds: NonNegativeDecimal
    server_processing_seconds: NonNegativeDecimal
    network_seconds: NonNegativeDecimal
    drift_seconds: Decimal

    @field_validator("server_at", "local_midpoint_at", "observed_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return require_utc(value)

    @model_validator(mode="after")
    def _metrics_are_consistent(self) -> "ClockReading":
        if self.round_trip_seconds != (
            self.server_processing_seconds + self.network_seconds
        ):
            raise ValueError("round trip must equal processing plus network time")
        calculated_drift = Decimal(
            str((self.server_at - self.local_midpoint_at).total_seconds())
        )
        if self.drift_seconds != calculated_drift:
            raise ValueError("drift does not match server and midpoint timestamps")
        return self


class BookSide(StrEnum):
    BID = "bid"
    ASK = "ask"


class BookEventType(StrEnum):
    SNAPSHOT = "snapshot"
    DELTA = "delta"
    TICK_SIZE_CHANGE = "tick_size_change"
    RECONNECT = "reconnect"


class BookLevel(FrozenModel):
    price: ProbabilityDecimal
    size: PositiveDecimal


class BookChange(FrozenModel):
    side: BookSide
    price: ProbabilityDecimal
    size: NonNegativeDecimal


class BookEvent(FrozenModel):
    event_type: BookEventType
    market_id: str = Field(min_length=1)
    token_id: str | None = None
    sequence: int | None = Field(default=None, ge=0)
    bids: tuple[BookLevel, ...] = ()
    asks: tuple[BookLevel, ...] = ()
    changes: tuple[BookChange, ...] = ()
    tick_size: PositiveDecimal | None = None
    exchange_at: datetime
    received_at: datetime

    @field_validator("exchange_at", "received_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return require_utc(value)

    @model_validator(mode="after")
    def _shape_matches_type(self) -> "BookEvent":
        if self.exchange_at > self.received_at:
            raise ValueError("exchange_at cannot be after received_at")
        if self.event_type is BookEventType.RECONNECT:
            if self.token_id is not None or self.sequence is not None:
                raise ValueError("reconnect must not target a token or sequence")
            if self.bids or self.asks or self.changes or self.tick_size is not None:
                raise ValueError("reconnect cannot carry book data")
            return self
        if self.token_id is None or self.sequence is None:
            raise ValueError("book events require token_id and sequence")
        if self.event_type is BookEventType.SNAPSHOT:
            if not self.bids or not self.asks or self.changes:
                raise ValueError("snapshot requires both sides and no changes")
        elif self.event_type is BookEventType.DELTA:
            if not self.changes or self.bids or self.asks:
                raise ValueError("delta requires changes only")
        elif self.event_type is BookEventType.TICK_SIZE_CHANGE:
            if self.tick_size is None or self.bids or self.asks or self.changes:
                raise ValueError("tick size event requires tick_size only")
        return self


class OrderBook(FrozenModel):
    market_id: str = Field(min_length=1)
    token_id: str = Field(min_length=1)
    outcome: OutcomeSide
    sequence: int = Field(ge=0)
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    tick_size: PositiveDecimal
    exchange_at: datetime
    received_at: datetime
    tradable: bool
    reason: str | None = None

    @field_validator("exchange_at", "received_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return require_utc(value)

    @model_validator(mode="after")
    def _book_invariants(self) -> "OrderBook":
        if self.exchange_at > self.received_at:
            raise ValueError("exchange_at cannot be after received_at")
        if any(a.price >= b.price for b, a in zip(self.bids, self.bids[1:])):
            raise ValueError("bids must be strictly descending")
        if any(a.price >= b.price for a, b in zip(self.asks, self.asks[1:])):
            raise ValueError("asks must be strictly ascending")
        if (
            self.tradable
            and self.bids
            and self.asks
            and self.bids[0].price >= self.asks[0].price
        ):
            raise ValueError("book cannot be crossed or locked")
        if self.tradable and (
            not self.bids or not self.asks or self.reason is not None
        ):
            raise ValueError("tradable books require both sides and no failure reason")
        if not self.tradable and not self.reason:
            raise ValueError("untradable books require a reason")
        return self


class CombinedSnapshot(FrozenModel):
    market: MarketMetadata
    spot: SpotTick
    reference: WindowReference
    up_book: OrderBook
    down_book: OrderBook
    created_at: datetime
    max_source_age_seconds: NonNegativeDecimal
    max_time_skew_seconds: NonNegativeDecimal

    @field_validator("created_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return require_utc(value)


JsonObject = dict[str, Any]


def require_utc(value: datetime) -> datetime:
    """Reject naive timestamps and normalize aware values to UTC."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)
