from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import cast

from hw_validation.json_types import JsonObject
from hw_validation.results import write_result
from hw_validation.status import ExitCode, ResultStatus, ValidationOutcome


def test_write_result_includes_duration_and_completion_reason() -> None:
    with tempfile.TemporaryDirectory() as directory_text:
        result_path = Path(directory_text) / "result.json"
        write_result(
            result_path,
            ValidationOutcome(ResultStatus.pass_status, ExitCode.pass_status.code),
            "label",
            Path(directory_text),
            12.345,
            {"started_at": "2026-01-01T00:00:00Z"},
            "completed",
        )
        payload = cast(JsonObject, json.loads(result_path.read_text(encoding="utf-8")))
        payload["ended_at"] = "DYNAMIC"
        assert payload == {
            "completed_reason": "completed",
            "duration_seconds": 12.345,
            "ended_at": "DYNAMIC",
            "exit_code": 0,
            "failures": 0,
            "label": "label",
            "result": "PASS",
            "run_directory": directory_text,
            "started_at": "2026-01-01T00:00:00Z",
            "status": "PASS",
            "warnings": 0,
        }
