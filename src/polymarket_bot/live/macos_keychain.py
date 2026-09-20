"""Minimal macOS Keychain adapter with no shell or secret logging.

Secrets are retrieved with ``security find-generic-password`` using an argv
sequence.  They are never placed in command strings, logs, exceptions, or the
repository.  Callers receive the existing opaque ``SecretValue`` wrapper.
"""

from __future__ import annotations

import platform
import subprocess
from collections.abc import Callable, Sequence

from polymarket_bot.live.credentials import (
    CredentialError,
    KeychainCredentialProvider,
    SecretValue,
)

DEFAULT_KEYCHAIN_SERVICE = "forecasting-tools.polymarket"


def _run_security(argv: Sequence[str]) -> str:
    completed = subprocess.run(
        list(argv),
        check=True,
        capture_output=True,
        text=True,
        # The first read may display a macOS authorization dialog. Give the
        # user enough time to approve it while still keeping a finite bound.
        timeout=60,
    )
    return completed.stdout


def resolve_macos_keychain_password(
    service: str,
    account: str,
    *,
    runner: Callable[[Sequence[str]], str] = _run_security,
) -> str:
    """Return one generic-password value without exposing command output.

    ``service`` groups this application's records. ``account`` is a non-secret
    label such as ``signer-private-key``.  Any OS or Keychain error is replaced
    with a stable redacted exception.
    """
    if platform.system() != "Darwin":
        raise CredentialError("macOS Keychain is only available on macOS")
    if type(service) is not str or not service.strip():
        raise ValueError("service must not be blank")
    if type(account) is not str or not account.strip():
        raise ValueError("account must not be blank")
    try:
        value = runner(
            (
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                service,
                "-a",
                account,
                "-w",
            )
        ).rstrip("\r\n")
    except Exception as exc:
        raise CredentialError("credential resolution failed") from exc
    if not value:
        raise CredentialError("credential resolution failed")
    return value


def macos_keychain_provider(
    service: str = DEFAULT_KEYCHAIN_SERVICE,
) -> KeychainCredentialProvider:
    """Construct the production provider used by the account-only CLI."""
    return KeychainCredentialProvider(service, resolve_macos_keychain_password)


def resolve_required(provider: KeychainCredentialProvider, label: str) -> SecretValue:
    """Resolve a required Keychain item through the opaque provider boundary."""
    return provider.resolve(label)
