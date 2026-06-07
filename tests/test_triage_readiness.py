from __future__ import annotations

import json
import tempfile
from pathlib import Path

from hw_validation.readiness import run_report
from hw_validation.triage import run_triage


def test_triage_status_matrix() -> None:
    with tempfile.TemporaryDirectory() as directory_text:
        root = Path(directory_text)
        assert (
            run_triage(write_log(root / "pass", "clean log\n"), root / "pass-out") == 0
        )
        assert (
            run_triage(
                write_log(root / "warn", "ECC corrected on DIMM A1\n"),
                root / "warn-out",
            )
            == 2
        )
        assert (
            run_triage(
                write_log(root / "fail", "NVMe timeout on controller\n"),
                root / "fail-out",
            )
            == 1
        )


def test_readiness_status_matrix() -> None:
    with tempfile.TemporaryDirectory() as directory_text:
        root = Path(directory_text)
        assert (
            run_report(write_result(root / "pass", "PASS", 0), root / "pass-out") == 0
        )
        assert (
            run_report(write_result(root / "warn", "WARN", 2), root / "warn-out") == 2
        )
        assert (
            run_report(write_result(root / "fail", "FAIL", 1), root / "fail-out") == 1
        )


def write_log(root: Path, text: str) -> Path:
    root.mkdir(parents=True)
    log_path = root / "kernel.log"
    _ = log_path.write_text(text, encoding="utf-8")
    assert log_path.read_text(encoding="utf-8") == text
    return root


def write_result(root: Path, status: str, exit_code: int) -> Path:
    result_directory = root / "component"
    result_directory.mkdir(parents=True)
    payload = (
        json.dumps(
            {
                "status": status,
                "result": status,
                "exit_code": exit_code,
                "failures": 1 if status == "FAIL" else 0,
                "warnings": 1 if status == "WARN" else 0,
            }
        )
        + "\n"
    )
    result_path = result_directory / "result.json"
    _ = result_path.write_text(payload, encoding="utf-8")
    assert result_path.read_text(encoding="utf-8") == payload
    return root
