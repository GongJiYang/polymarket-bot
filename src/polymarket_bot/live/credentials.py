"""Opaque credential handles and external signing boundaries.

Production providers resolve secrets outside the repository. Secret values redact their
string representation and may only be consumed through an explicit callback.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, TypeVar

T = TypeVar("T")


class CredentialError(RuntimeError):
    pass


class SecretValue:
    """Short-lived secret container that never renders its contents."""

    __slots__ = ("__value",)

    def __init__(self, value: str) -> None:
        if type(value) is not str or not value:
            raise ValueError("secret must be a non-empty string")
        self.__value = value

    def consume(self, consumer: Callable[[str], T]) -> T:
        if not callable(consumer):
            raise TypeError("consumer must be callable")
        return consumer(self.__value)

    def __repr__(self) -> str:
        return "SecretValue([REDACTED])"

    __str__ = __repr__


class CredentialProvider(Protocol):
    def resolve(self, label: str) -> SecretValue: ...


class ExternalSigner(Protocol):
    @property
    def address(self) -> str: ...

    def sign_typed_data(
        self, domain: dict[str, object], message: dict[str, object]
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class KeychainCredentialProvider:
    """Keychain adapter with an injected resolver, keeping OS access out of tests."""

    service: str
    resolver: Callable[[str, str], str]

    def __post_init__(self) -> None:
        if type(self.service) is not str or not self.service.strip():
            raise ValueError("service must not be blank")
        if not callable(self.resolver):
            raise TypeError("resolver must be callable")

    def resolve(self, label: str) -> SecretValue:
        if type(label) is not str or not label.strip():
            raise ValueError("label must not be blank")
        try:
            value = self.resolver(self.service, label)
        except Exception as exc:
            raise CredentialError("credential resolution failed") from exc
        return SecretValue(value)
