from __future__ import annotations

import tempfile
from pathlib import Path

from hw_validation.system_stress import (
    scan_kernel_logs,
    system_stress_kernel_scan_paths,
)


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


def test_scan_kernel_logs_prefers_since_start_journal() -> None:
    with tempfile.TemporaryDirectory() as directory_text:
        root = Path(directory_text)
        _ = (root / "dmesg.log").write_text(
            "old kernel: MCE: hardware error detected\n", encoding="utf-8"
        )
        _ = (root / "kernel_journal_since_start.log").write_text(
            "clean stress window\n", encoding="utf-8"
        )
        _ = (root / "kernel_journal_since_start.ok").write_text(
            "ok\n", encoding="utf-8"
        )
        assert scan_kernel_logs(root, False, False) == (0, 0)


def test_system_stress_kernel_scan_paths_exclude_historical_dmesg() -> None:
    with tempfile.TemporaryDirectory() as directory_text:
        root = Path(directory_text)
        _ = (root / "kernel_journal_since_start.log").write_text(
            "clean\n", encoding="utf-8"
        )
        _ = (root / "kernel_journal_since_start.ok").write_text(
            "ok\n", encoding="utf-8"
        )
        assert system_stress_kernel_scan_paths(root) == (
            root / "kernel_journal_since_start.log",
        )


def test_system_stress_kernel_scan_paths_fallback_when_since_capture_failed() -> None:
    with tempfile.TemporaryDirectory() as directory_text:
        root = Path(directory_text)
        _ = (root / "kernel_journal_since_start.log").write_text("", encoding="utf-8")
        assert system_stress_kernel_scan_paths(root) == (
            root / "kernel_journal.log",
            root / "dmesg.log",
        )
