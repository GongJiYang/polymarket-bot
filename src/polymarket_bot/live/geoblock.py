"""Shared geoblock gate for Polymarket live operations.

Every order-phase path (LM094 dry confirmation, LM095 execution) MUST pass
``require_unblocked`` before touching the official API. The gate is fail-closed:
any transport error, unexpected payload shape, or a response that does not
explicitly set ``blocked == false`` aborts the operation.
"""

from __future__ import annotations

import requests


class GeoblockError(RuntimeError):
    pass


class GeoblockUnavailable(GeoblockError):
    """The official gate could not be reached or decoded."""


def require_unblocked() -> None:
    """Fail closed unless Polymarket explicitly reports the path unblocked."""
    try:
        response = requests.get(
            "https://polymarket.com/api/geoblock",
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise GeoblockUnavailable("official geoblock check failed") from exc
    if type(payload) is not dict or payload.get("blocked") is not False:
        raise GeoblockError("official geoblock did not explicitly permit access")
