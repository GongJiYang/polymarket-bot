"""Single-use, expiring approvals for exact immutable order intents."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from polymarket_bot.live.order_intent import OrderIntent


class ApprovalError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    intent_hash: str
    approved_by: str
    approved_at: datetime
    expires_at: datetime


class ApprovalService:
    def __init__(self, *, maximum_ttl: timedelta = timedelta(minutes=5)) -> None:
        if not timedelta(0) < maximum_ttl <= timedelta(minutes=15):
            raise ValueError("maximum_ttl must be positive and at most 15 minutes")
        self._maximum_ttl = maximum_ttl
        self._consumed: set[str] = set()

    def display(self, intent: OrderIntent) -> dict[str, str]:
        return {
            "market": intent.market_id,
            "side": intent.side,
            "price": str(intent.price),
            "size": str(intent.size),
            "fee_rate": str(intent.fee_rate),
            "maximum_loss": str(intent.maximum_loss),
            "intent_hash": intent.intent_hash,
        }

    def approve(
        self, intent: OrderIntent, *, approved_by: str, now: datetime, ttl: timedelta
    ) -> ApprovalRecord:
        if not approved_by.strip() or not timedelta(0) < ttl <= self._maximum_ttl:
            raise ApprovalError("approver and bounded TTL are required")
        return ApprovalRecord(intent.intent_hash, approved_by, now, now + ttl)

    def consume(
        self, intent: OrderIntent, approval: ApprovalRecord, *, now: datetime
    ) -> None:
        if approval.intent_hash != intent.intent_hash or now >= approval.expires_at:
            raise ApprovalError("approval does not match intent or has expired")
        if approval.intent_hash in self._consumed:
            raise ApprovalError("approval has already been consumed")
        self._consumed.add(approval.intent_hash)
