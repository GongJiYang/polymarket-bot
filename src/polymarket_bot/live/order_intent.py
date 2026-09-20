"""Canonical offline limit-order intent construction and dry signing."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from polymarket_bot.live.authentication import canonical_json
from polymarket_bot.live.credentials import ExternalSigner


class OrderConstructionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class OrderIntent:
    market_id: str
    token_id: str
    side: str
    price: Decimal
    size: Decimal
    fee_rate: Decimal
    neg_risk: bool
    expiration: datetime
    intent_hash: str

    @property
    def maximum_loss(self) -> Decimal:
        return (
            self.price * self.size
            if self.side == "BUY"
            else (Decimal("1") - self.price) * self.size
        )


def build_order_intent(
    *,
    market_id: str,
    token_id: str,
    side: str,
    price: Decimal,
    size: Decimal,
    tick_size: Decimal,
    min_size: Decimal,
    fee_rate: Decimal,
    neg_risk: bool,
    expiration: datetime,
) -> OrderIntent:
    if side not in {"BUY", "SELL"} or not market_id or not token_id:
        raise OrderConstructionError("market, token, and BUY/SELL side are required")
    values = (price, size, tick_size, min_size, fee_rate)
    if any(type(value) is not Decimal or not value.is_finite() for value in values):
        raise OrderConstructionError("numeric values must be finite Decimal")
    if tick_size <= 0 or min_size <= 0 or size < min_size or fee_rate < 0:
        raise OrderConstructionError("tick, size, or fee constraints violated")
    rounded_price = (price / tick_size).to_integral_value(
        rounding=ROUND_DOWN
    ) * tick_size
    rounded_size = size.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
    if not Decimal("0") < rounded_price < Decimal("1") or rounded_size < min_size:
        raise OrderConstructionError("rounded order is invalid")
    if expiration.tzinfo is None or expiration.utcoffset() is None:
        raise OrderConstructionError("expiration must be timezone-aware")
    payload = {
        "market_id": market_id,
        "token_id": token_id,
        "side": side,
        "price": str(rounded_price),
        "size": str(rounded_size),
        "fee_rate": str(fee_rate),
        "neg_risk": neg_risk,
        "expiration": expiration.isoformat(),
    }
    digest = hashlib.sha256(canonical_json(payload)).hexdigest()
    return OrderIntent(
        market_id,
        token_id,
        side,
        rounded_price,
        rounded_size,
        fee_rate,
        neg_risk,
        expiration,
        digest,
    )


def dry_sign(
    intent: OrderIntent, signer: ExternalSigner, *, chain_id: int = 137
) -> str:
    if chain_id != 137:
        raise OrderConstructionError("chain_id must be Polygon mainnet 137")
    return signer.sign_typed_data(
        {"name": "Polymarket CTF Exchange", "chainId": chain_id},
        {"intent_hash": intent.intent_hash},
    )
