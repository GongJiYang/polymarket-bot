"""Strict account identity parsing for Polygon Polymarket wallets."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class IdentityError(ValueError):
    pass


class WalletType(IntEnum):
    EOA = 0
    POLY_PROXY = 1
    GNOSIS_SAFE = 2
    DEPOSIT_WALLET = 3


@dataclass(frozen=True, slots=True)
class AccountIdentity:
    signer: str
    maker: str
    funder: str
    wallet_type: WalletType
    chain_id: int
    signature_type: int

    def __post_init__(self) -> None:
        addresses = tuple(
            _address(value, name)
            for name, value in (
                ("signer", self.signer),
                ("maker", self.maker),
                ("funder", self.funder),
            )
        )
        object.__setattr__(self, "signer", addresses[0])
        object.__setattr__(self, "maker", addresses[1])
        object.__setattr__(self, "funder", addresses[2])
        if type(self.wallet_type) is not WalletType:
            raise IdentityError("wallet_type must be WalletType")
        if self.chain_id != 137:
            raise IdentityError("chain_id must be Polygon mainnet 137")
        if self.signature_type != int(self.wallet_type):
            raise IdentityError("signature_type does not match wallet_type")
        if self.wallet_type is WalletType.EOA:
            if len(set(addresses)) != 1:
                raise IdentityError("EOA signer, maker, and funder must match")
        elif addresses[1] != addresses[2] or addresses[0] == addresses[1]:
            raise IdentityError(
                "smart wallet requires maker=funder distinct from signer"
            )


def _address(value: str, name: str) -> str:
    if type(value) is not str:
        raise IdentityError(f"{name} must be an address")
    normalized = value.strip().lower()
    if len(normalized) != 42 or not normalized.startswith("0x"):
        raise IdentityError(f"{name} must be a 20-byte hex address")
    try:
        int(normalized[2:], 16)
    except ValueError as exc:
        raise IdentityError(f"{name} must be a 20-byte hex address") from exc
    return normalized
