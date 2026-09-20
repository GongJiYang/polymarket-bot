"""Small JSONL audit sink with explicit flush semantics."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, is_dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TextIO


def _json(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")  # type: ignore[union-attr]
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if hasattr(value, "value"):
        return value.value  # type: ignore[union-attr]
    raise TypeError(f"cannot serialize {type(value).__name__}")


class JsonlAudit:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._stream: TextIO = path.open("x", encoding="utf-8")

    def record(self, event: object) -> None:
        self._stream.write(json.dumps(event, default=_json, sort_keys=True) + "\n")
        self._stream.flush()
        os.fsync(self._stream.fileno())

    def close(self) -> None:
        self._stream.close()

    def __enter__(self) -> "JsonlAudit":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
