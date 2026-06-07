from __future__ import annotations

import pytest

from hw_validation.parsing import bandwidth_bits, duration_seconds
from hw_validation.system_stress import stress_ng_command


@pytest.mark.parametrize(
    ("duration_text", "seconds"),
    [
        ("30", 30),
        ("30s", 30),
        ("2m", 120),
        ("3h", 10_800),
        ("1d", 86_400),
    ],
)
def test_duration_seconds(duration_text: str, seconds: int) -> None:
    assert duration_seconds(duration_text) == seconds


@pytest.mark.parametrize(
    ("bandwidth_text", "bits"),
    [
        ("900M", 900_000_000),
        ("1G", 1_000_000_000),
        ("10", 10),
    ],
)
def test_bandwidth_bits(bandwidth_text: str, bits: int) -> None:
    assert bandwidth_bits(bandwidth_text) == bits


def test_stress_ng_command() -> None:
    assert stress_ng_command("8h", 75) == [
        "stress-ng",
        "--cpu",
        "0",
        "--cpu-method",
        "all",
        "--matrix",
        "0",
        "--vm",
        "2",
        "--vm-bytes",
        "75%",
        "--verify",
        "--metrics-brief",
        "--timeout",
        "8h",
    ]
