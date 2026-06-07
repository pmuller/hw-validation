from __future__ import annotations

import re

DURATION_PATTERN = re.compile(r"^(?P<value>[0-9]+)(?P<unit>[smhd]?)$")
BANDWIDTH_PATTERN = re.compile(r"^(?P<value>[0-9]+)(?P<unit>[KkMmGgTt]?)$")


def duration_seconds(duration_text: str) -> int:
    match = DURATION_PATTERN.match(duration_text)
    if match is None:
        raise ValueError("duration must be an integer with s, m, h, or d suffix")
    value = int(match.group("value"))
    unit = match.group("unit")
    multipliers = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
    seconds = value * multipliers[unit]
    if seconds <= 0:
        raise ValueError("duration must be greater than zero")
    return seconds


def bandwidth_bits(bandwidth_text: str) -> int:
    match = BANDWIDTH_PATTERN.match(bandwidth_text)
    if match is None:
        raise ValueError("bandwidth must be an integer with K, M, G, or T suffix")
    value = int(match.group("value"))
    unit = match.group("unit").lower()
    multipliers = {
        "": 1,
        "k": 1000,
        "m": 1_000_000,
        "g": 1_000_000_000,
        "t": 1_000_000_000_000,
    }
    return value * multipliers[unit]
