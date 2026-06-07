from __future__ import annotations

import json
import shlex
import sys
import tempfile
import time
from pathlib import Path

import pytest

import hw_validation.runner as runner_module
from hw_validation.runner import CommandRunner

STDIN_FAILURE_COMMAND = (
    sys.executable,
    "-c",
    "import sys; raise SystemExit(12 if sys.stdin.read(1) == '' else 0)",
)


def test_capture_closes_child_stdin() -> None:
    assert (
        CommandRunner(verbose=False)
        .capture("stdin_eof", STDIN_FAILURE_COMMAND, timeout_seconds=5)
        .return_code
        == 12
    )


def test_stream_closes_child_stdin() -> None:
    with tempfile.TemporaryDirectory() as directory_text:
        assert (
            CommandRunner(verbose=False)
            .stream(
                "stdin_eof",
                STDIN_FAILURE_COMMAND,
                Path(directory_text) / "stdout.txt",
                Path(directory_text) / "stderr.txt",
            )
            .return_code
            == 12
        )


def test_capture_records_exact_timing_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monotonic_values = iter((100.0, 101.25))
    utc_values = iter(("2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z"))

    def monotonic() -> float:
        return next(monotonic_values)

    def utc_now() -> str:
        return next(utc_values)

    monkeypatch.setattr(time, "monotonic", monotonic)
    monkeypatch.setattr(runner_module, "utc_now", utc_now)
    with tempfile.TemporaryDirectory() as directory_text:
        result = CommandRunner(Path(directory_text), verbose=False).capture(
            "hello", (sys.executable, "-c", "print('ok')")
        )
        assert (
            result.return_code,
            result.stdout,
            result.stderr,
            result.started_at,
            result.ended_at,
            result.elapsed_seconds,
        ) == (0, "ok\n", "", "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z", 1.25)
        assert json.loads(
            (Path(directory_text) / "0001_hello.meta.json").read_text()
        ) == {
            "command": shlex.join((sys.executable, "-c", "print('ok')")),
            "elapsed_seconds": 1.25,
            "ended_at": "2026-01-01T00:00:01Z",
            "name": "hello",
            "return_code": 0,
            "started_at": "2026-01-01T00:00:00Z",
            "stderr_path": "0001_hello.stderr",
            "stdout_path": "0001_hello.stdout",
        }


def test_dry_run_result_timing_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_module, "utc_now", lambda: "2026-01-01T00:00:00Z")
    result = CommandRunner(verbose=False, dry_run=True).capture(
        "dry", ("missing-command",)
    )
    assert (
        result.return_code,
        result.stdout,
        result.stderr,
        result.started_at,
        result.ended_at,
        result.elapsed_seconds,
    ) == (0, "", "", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", 0.0)
