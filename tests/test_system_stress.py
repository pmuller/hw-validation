from __future__ import annotations

import tempfile
from pathlib import Path

from hw_validation.system_stress import scan_kernel_logs


def test_scan_kernel_logs_detects_abbreviated_mce() -> None:
    with tempfile.TemporaryDirectory() as directory_text:
        _ = (Path(directory_text) / "kernel_journal.log").write_text(
            "kernel: MCE: hardware error detected\n", encoding="utf-8"
        )
        assert scan_kernel_logs(Path(directory_text), False, False) == (1, 0)


def test_scan_kernel_logs_does_not_match_mce_inside_words() -> None:
    with tempfile.TemporaryDirectory() as directory_text:
        _ = (Path(directory_text) / "kernel_journal.log").write_text(
            "kernel: MCEs are not reported\n", encoding="utf-8"
        )
        assert scan_kernel_logs(Path(directory_text), False, False) == (0, 0)
