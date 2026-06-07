from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from hw_validation.files import write_json
from hw_validation.json_types import JsonObject, JsonValue


def write_timing_summary(run_directory: Path) -> None:
    write_json(run_directory / "timing_summary.json", timing_summary(run_directory))


def timing_summary(run_directory: Path) -> list[JsonValue]:
    records: list[JsonObject] = []
    for metadata_path in sorted(run_directory.rglob("*.meta.json")):
        record = read_timing_record(run_directory, metadata_path)
        if record is not None:
            records.append(record)
    return [
        cast(JsonValue, record)
        for record in sorted(records, key=elapsed_key, reverse=True)
    ]


def read_timing_record(run_directory: Path, metadata_path: Path) -> JsonObject | None:
    try:
        payload_object = cast(
            object, json.loads(metadata_path.read_text(encoding="utf-8"))
        )
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload_object, dict):
        return None
    payload = cast(dict[str, object], payload_object)
    elapsed_value = payload.get("elapsed_seconds")
    if not isinstance(elapsed_value, int | float):
        return None
    return_code_value = payload.get("return_code", 0)
    return {
        "name": str(payload.get("name", "")),
        "command": str(payload.get("command", "")),
        "return_code": return_code_value if isinstance(return_code_value, int) else 0,
        "elapsed_seconds": float(elapsed_value),
        "path": str(metadata_path.relative_to(run_directory)),
    }


def elapsed_key(record: JsonObject) -> float:
    elapsed_value = record.get("elapsed_seconds")
    return float(elapsed_value) if isinstance(elapsed_value, int | float) else 0.0
