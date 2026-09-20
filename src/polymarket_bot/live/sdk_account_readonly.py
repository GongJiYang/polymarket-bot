"""Read-only account facade over Polymarket's maintained unified Python SDK.

The facade deliberately does not expose the underlying ``SecureClient`` or any
order, cancellation, signing, allowance, or wallet-transaction method.  SDK
construction is isolated behind an injected factory so unit tests never need a
private key or network connection.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from time import sleep
from typing import Any, Protocol

from polymarket_bot.live.account_identity import _address
from polymarket_bot.live.credentials import SecretValue


class OfficialAccountReadError(RuntimeError):
    """A stable, secret-free failure at the official SDK boundary."""


class _Paginator(Protocol):
    def __iter__(self) -> Iterator[object]: ...


class _SecureReadClient(Protocol):
    wallet: str
    wallet_type: str

    def get_balance_allowance(self, *, asset_type: str) -> object: ...
    def list_positions(self, *, user: str, page_size: int) -> _Paginator: ...
    def list_open_orders(self) -> _Paginator: ...
    def list_account_trades(self) -> _Paginator: ...
    def list_activity(self, *, user: str, page_size: int) -> _Paginator: ...
    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ReadOnlyAccountSnapshot:
    wallet: str
    wallet_type: str
    collateral_balance_raw: Decimal
    collateral_allowances_raw: tuple[tuple[str, Decimal], ...]
    positions: tuple[dict[str, Any], ...]
    open_orders: tuple[dict[str, Any], ...]
    account_trades: tuple[dict[str, Any], ...]
    activity: tuple[dict[str, Any], ...]
    mode: str = "ACCOUNT_READ_ONLY"
    execution_eligible: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "wallet", _address(self.wallet, "wallet"))
        if self.mode != "ACCOUNT_READ_ONLY" or self.execution_eligible is not False:
            raise ValueError("account snapshots must remain read-only")


def create_official_secure_client(
    *,
    private_key: SecretValue,
    wallet: str,
    relayer_api_key: SecretValue | None = None,
    relayer_api_key_address: str | None = None,
) -> _SecureReadClient:
    """Create the official SDK client while keeping secrets callback-scoped."""
    try:
        from polymarket import RelayerApiKey, SecureClient
    except ImportError as exc:
        raise OfficialAccountReadError(
            "polymarket-client is not installed; install the locked project dependencies"
        ) from exc

    if (relayer_api_key is None) != (relayer_api_key_address is None):
        raise OfficialAccountReadError(
            "relayer API key and address must be supplied together"
        )
    normalized_wallet = _address(wallet, "wallet")

    def construct(key: str) -> _SecureReadClient:
        kwargs: dict[str, object] = {
            "private_key": key,
            "wallet": normalized_wallet,
        }
        if relayer_api_key is not None and relayer_api_key_address is not None:
            normalized_relayer_address = _address(
                relayer_api_key_address, "relayer_api_key_address"
            )
            kwargs["api_key"] = relayer_api_key.consume(
                lambda value: RelayerApiKey(
                    key=value,
                    address=normalized_relayer_address,
                )
            )
        return SecureClient.create(**kwargs)

    try:
        return private_key.consume(construct)
    except OfficialAccountReadError:
        raise
    except Exception as exc:
        raise OfficialAccountReadError(
            "official account authentication failed"
        ) from exc


class OfficialAccountReadOnlyClient:
    """Narrow read-only SDK facade with bounded pagination and strict identity."""

    def __init__(
        self,
        client: _SecureReadClient,
        *,
        expected_wallet: str,
        max_pages: int = 100,
        page_size: int = 100,
    ) -> None:
        if type(max_pages) is not int or not 1 <= max_pages <= 1000:
            raise ValueError("max_pages must be between 1 and 1000")
        if type(page_size) is not int or not 1 <= page_size <= 500:
            raise ValueError("page_size must be between 1 and 500")
        self._client = client
        self._wallet = _address(expected_wallet, "expected_wallet")
        self._max_pages = max_pages
        self._page_size = page_size

    def snapshot(self, *, execution_only: bool = False) -> ReadOnlyAccountSnapshot:
        """Retry transient SDK reads while preserving fail-closed validation."""
        for attempt in range(3):
            try:
                return self._snapshot_once(execution_only=execution_only)
            except OfficialAccountReadError as exc:
                if str(exc) != "official account read failed" or attempt == 2:
                    raise
                sleep(0.25 * (attempt + 1))
        raise AssertionError("unreachable")

    def _snapshot_once(
        self, *, execution_only: bool = False
    ) -> ReadOnlyAccountSnapshot:
        """Read balance and positions; omit unrelated feeds during execution."""
        try:
            resolved = _address(self._client.wallet, "resolved_wallet")
            if resolved != self._wallet:
                raise OfficialAccountReadError(
                    "official SDK resolved a different account wallet"
                )
            wallet_type = self._client.wallet_type
            if wallet_type not in {
                "EOA",
                "POLY_PROXY",
                "GNOSIS_SAFE",
                "DEPOSIT_WALLET",
            }:
                raise OfficialAccountReadError(
                    "official SDK returned unknown wallet type"
                )

            collateral = self._client.get_balance_allowance(asset_type="COLLATERAL")
            balance = self._nonnegative_decimal(
                getattr(collateral, "balance", None), "collateral balance"
            )
            raw_allowances = getattr(collateral, "allowances", None)
            if not isinstance(raw_allowances, Mapping):
                raise OfficialAccountReadError("collateral allowances are malformed")
            allowances = tuple(
                sorted(
                    (
                        str(spender),
                        self._nonnegative_decimal(value, "collateral allowance"),
                    )
                    for spender, value in raw_allowances.items()
                )
            )
            return ReadOnlyAccountSnapshot(
                wallet=resolved,
                wallet_type=wallet_type,
                collateral_balance_raw=balance,
                collateral_allowances_raw=allowances,
                positions=self._pages(
                    self._client.list_positions(
                        user=self._wallet, page_size=self._page_size
                    ),
                    "positions",
                ),
                open_orders=(
                    ()
                    if execution_only
                    else self._pages(self._client.list_open_orders(), "open orders")
                ),
                account_trades=(
                    ()
                    if execution_only
                    else self._pages(
                        self._client.list_account_trades(), "account trades"
                    )
                ),
                activity=(
                    ()
                    if execution_only
                    else self._pages(
                        self._client.list_activity(
                            user=self._wallet, page_size=self._page_size
                        ),
                        "activity",
                    )
                ),
            )
        except OfficialAccountReadError:
            raise
        except Exception as exc:
            raise OfficialAccountReadError("official account read failed") from exc

    def close(self) -> None:
        try:
            self._client.close()
        except Exception as exc:
            raise OfficialAccountReadError(
                "official account client cleanup failed"
            ) from exc

    def _pages(self, paginator: _Paginator, name: str) -> tuple[dict[str, Any], ...]:
        records: list[dict[str, Any]] = []
        for index, page in enumerate(paginator):
            if index >= self._max_pages:
                raise OfficialAccountReadError(f"{name} exceeded pagination bound")
            items = getattr(page, "items", None)
            if not isinstance(items, (tuple, list)):
                raise OfficialAccountReadError(f"{name} page is malformed")
            records.extend(self._record(item, name) for item in items)
        return tuple(records)

    @staticmethod
    def _record(item: object, name: str) -> dict[str, Any]:
        dump = getattr(item, "model_dump", None)
        if not callable(dump):
            raise OfficialAccountReadError(f"{name} record is malformed")
        value = dump(mode="json")
        if type(value) is not dict:
            raise OfficialAccountReadError(f"{name} record is malformed")
        return value

    @staticmethod
    def _nonnegative_decimal(value: object, name: str) -> Decimal:
        try:
            result = Decimal(str(value))
        except Exception as exc:
            raise OfficialAccountReadError(f"{name} is malformed") from exc
        if not result.is_finite() or result < 0:
            raise OfficialAccountReadError(f"{name} is malformed")
        return result
