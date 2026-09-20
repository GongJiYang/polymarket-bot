"""Fail-closed operating modes for the Polymarket live boundary."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from enum import IntEnum
from polymarket_bot.live.bounded_bot import LiveSessionAuthorization


class LiveMode(IntEnum):
    """Modes ordered by increasing live capability."""

    OFF = 0
    ACCOUNT_READ_ONLY = 1
    CANCEL_ONLY = 2
    SHADOW = 3
    CONFIRM_EACH = 4
    BOUNDED_AUTO = 5




class InvalidModeTransition(ValueError):
    """A requested transition violated the no-skip or fail-closed policy."""


class ModeStateMachine:
    """In-memory, fail-closed mode state.

    Every new process constructs this object in ``OFF``. State is deliberately
    not restored by the constructor. Any rejected upgrade resets the machine to
    ``OFF`` so an exception cannot leave uncertain live capability enabled.
    """

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._mode = LiveMode.OFF
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._live_session_authorization: LiveSessionAuthorization | None = None

    @property
    def mode(self) -> LiveMode:
        return self._mode

    @property
    def live_session_authorization(self) -> LiveSessionAuthorization | None:
        return self._live_session_authorization

    def upgrade(
        self,
        target: LiveMode,
        *,
        authorization: LiveSessionAuthorization | None = None,
    ) -> LiveMode:
        """Upgrade by exactly one level, failing closed on every rejection."""
        try:
            target = self._validated_mode(target)
            if target.value != self._mode.value + 1:
                raise InvalidModeTransition("upgrades must advance exactly one level")
            if target is LiveMode.BOUNDED_AUTO:
                if authorization is None:
                    raise InvalidModeTransition(
                        "BOUNDED_AUTO requires an independent authorization object"
                    )
                now = self._clock()
                if now.tzinfo is None or now.utcoffset() is None:
                    raise InvalidModeTransition(
                        "clock must return a timezone-aware time"
                    )
                if now.utcoffset().total_seconds() != 0:
                    raise InvalidModeTransition("clock must return UTC")
                if not authorization.is_valid_at(now):
                    raise InvalidModeTransition(
                        "BOUNDED_AUTO authorization has expired"
                    )
            elif authorization is not None:
                raise InvalidModeTransition(
                    "authorization is only accepted for BOUNDED_AUTO"
                )
        except (TypeError, ValueError):
            self.fail_closed()
            raise

        self._mode = target
        self._live_session_authorization = authorization
        return self._mode

    def downgrade(self, target: LiveMode) -> LiveMode:
        """Reduce capability to any strictly lower valid mode."""
        try:
            target = self._validated_mode(target)
            if target.value >= self._mode.value:
                raise InvalidModeTransition(
                    "downgrades must strictly reduce capability"
                )
        except (TypeError, ValueError):
            self.fail_closed()
            raise

        self._mode = target
        self._live_session_authorization = None
        return self._mode

    def fail_closed(self) -> LiveMode:
        """Clear all authorization and return to the process default."""
        self._mode = LiveMode.OFF
        self._live_session_authorization = None
        return self._mode

    @staticmethod
    def _validated_mode(value: LiveMode) -> LiveMode:
        if not isinstance(value, LiveMode):
            raise TypeError("target must be a LiveMode")
        return value
