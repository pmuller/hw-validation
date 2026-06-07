from __future__ import annotations

from pathlib import Path

from hw_validation.files import write_json
from hw_validation.json_types import JsonObject
from hw_validation.status import ValidationOutcome
from hw_validation.timeutil import utc_now


def write_result(
    path: Path,
    outcome: ValidationOutcome,
    label: str,
    run_directory: Path,
    duration_seconds: float,
    extra_fields: JsonObject | None = None,
    completed_reason: str = "completed",
) -> None:
    payload: JsonObject = {
        "status": outcome.status.value,
        "result": outcome.status.value,
        "exit_code": outcome.exit_code,
        "failures": outcome.failures,
        "warnings": outcome.warnings,
        "label": label,
        "run_directory": str(run_directory),
        "ended_at": utc_now(),
        "duration_seconds": duration_seconds,
        "completed_reason": completed_reason,
    }
    if extra_fields:
        payload.update(extra_fields)
    write_json(path, payload)
