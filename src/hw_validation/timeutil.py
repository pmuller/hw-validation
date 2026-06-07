from __future__ import annotations

import time
from datetime import UTC, datetime


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def elapsed_seconds(started_monotonic: float) -> float:
    return round(time.monotonic() - started_monotonic, 3)
