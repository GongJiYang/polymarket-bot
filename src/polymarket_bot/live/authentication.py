"""Deterministic L1/L2 authentication serialization with injected signing.

This module performs no network I/O and never owns a private key. It creates canonical
bytes for an external signer and derives request authentication with an opaque secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime

from polymarket_bot.live.credentials import ExternalSigner, SecretValue


class AuthenticationError(ValueError):
    pass


def canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AuthenticationError("payload is not canonical JSON") from exc


@dataclass(frozen=True, slots=True)
class L1Proof:
    signer: str
    timestamp: int
    signature: str


@dataclass(frozen=True, slots=True)
class L2Headers:
    api_key: str
    timestamp: str
    signature: str
    passphrase: str

    def __repr__(self) -> str:
        return "L2Headers([REDACTED])"


def create_l1_proof(
    signer: ExternalSigner, *, chain_id: int, timestamp: int, nonce: int = 0
) -> L1Proof:
    if chain_id != 137:
        raise AuthenticationError("chain_id must be Polygon mainnet 137")
    if (
        type(timestamp) is not int
        or timestamp < 0
        or type(nonce) is not int
        or nonce < 0
    ):
        raise AuthenticationError("timestamp and nonce must be non-negative integers")
    domain = {"name": "ClobAuthDomain", "version": "1", "chainId": chain_id}
    message = {"address": signer.address, "timestamp": str(timestamp), "nonce": nonce}
    signature = signer.sign_typed_data(domain, message)
    if type(signature) is not str or not signature:
        raise AuthenticationError("external signer returned no signature")
    return L1Proof(signer=signer.address, timestamp=timestamp, signature=signature)


def create_l2_headers(
    *,
    api_key: SecretValue,
    api_secret: SecretValue,
    passphrase: SecretValue,
    timestamp: datetime,
    method: str,
    path: str,
    body: object | None = None,
) -> L2Headers:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise AuthenticationError("timestamp must be timezone-aware")
    epoch_ms = str(int(timestamp.timestamp() * 1000))
    verb = method.strip().upper()
    if not verb or not path.startswith("/"):
        raise AuthenticationError("method and absolute API path are required")
    body_bytes = b"" if body is None else canonical_json(body)
    message = epoch_ms.encode() + verb.encode() + path.encode() + body_bytes

    def sign(secret: str) -> str:
        try:
            key = base64.urlsafe_b64decode(secret + "=" * (-len(secret) % 4))
        except Exception as exc:
            raise AuthenticationError("API secret is not base64url") from exc
        return (
            base64.urlsafe_b64encode(hmac.new(key, message, hashlib.sha256).digest())
            .decode()
            .rstrip("=")
        )

    signature = api_secret.consume(sign)
    return L2Headers(
        api_key=api_key.consume(lambda value: value),
        timestamp=epoch_ms,
        signature=signature,
        passphrase=passphrase.consume(lambda value: value),
    )
