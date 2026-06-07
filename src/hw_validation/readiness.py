from __future__ import annotations

import json
import time
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from hw_validation.files import write_json, write_text
from hw_validation.json_types import JsonObject, JsonValue
from hw_validation.paths import path_is_within
from hw_validation.status import ExitCode, ResultStatus
from hw_validation.timeutil import elapsed_seconds, utc_now


def load_json(path: Path) -> JsonObject:
    with path.open("r", encoding="utf-8") as json_file:
        value = cast(object, json.load(json_file))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root is not an object: {path}")
    return cast(JsonObject, value)


def result_status(result_payload: JsonObject) -> ResultStatus:
    for status_key in ("status", "result"):
        status_value = result_payload.get(status_key)
        if isinstance(status_value, str) and status_value.upper() in {
            "PASS",
            "WARN",
            "FAIL",
        }:
            return ResultStatus(status_value.upper())
    exit_code = result_payload.get("exit_code")
    if exit_code in (1, 70):
        return ResultStatus.fail
    if exit_code == 2:
        return ResultStatus.warn
    failures = result_payload.get("failures")
    warnings = result_payload.get("warnings")
    if isinstance(failures, int) and failures > 0:
        return ResultStatus.fail
    if isinstance(warnings, int) and warnings > 0:
        return ResultStatus.warn
    return ResultStatus.pass_status


def result_record(path: Path, payload: JsonObject) -> JsonObject:
    return {
        "path": str(path),
        "status": result_status(payload).value,
        "label": payload.get("label", ""),
        "profile_run_id": payload.get("profile_run_id", ""),
        "profile_step_id": payload.get("profile_step_id", ""),
        "profile_step_fingerprint": payload.get("profile_step_fingerprint", ""),
        "failures": payload.get("failures", 0),
        "warnings": payload.get("warnings", 0),
        "exit_code": payload.get("exit_code", 0),
    }


def result_payload_is_component(payload: JsonObject) -> bool:
    return payload.get("result_type") != "profile"


def collect_result_files(
    log_root: Path, out_root: Path
) -> tuple[list[JsonObject], list[JsonObject]]:
    if not log_root.exists():
        raise FileNotFoundError(f"--log-root does not exist: {log_root}")
    results: list[JsonObject] = []
    read_errors: list[JsonObject] = []
    for result_path in sorted(log_root.rglob("result.json")):
        if path_is_within(result_path, out_root):
            continue
        try:
            payload = load_json(result_path)
            if result_payload_is_component(payload):
                results.append(result_record(result_path, payload))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            read_errors.append(
                {"path": str(result_path), "status": "FAIL", "error": str(error)}
            )
    return results, read_errors


def final_status(
    records: Sequence[JsonObject],
    read_errors: Sequence[JsonObject],
    missing_steps: Sequence[JsonObject],
) -> tuple[ResultStatus, int]:
    if (
        any(record.get("status") == "FAIL" for record in read_errors)
        or any(record.get("status") == "FAIL" for record in records)
        or any(step.get("required") is True for step in missing_steps)
    ):
        return ResultStatus.fail, ExitCode.hard_failure.code
    if (
        any(record.get("status") == "WARN" for record in read_errors)
        or any(record.get("status") == "WARN" for record in records)
        or missing_steps
    ):
        return ResultStatus.warn, ExitCode.warning.code
    return ResultStatus.pass_status, ExitCode.pass_status.code


def load_profile_manifest(log_root: Path) -> JsonObject | None:
    manifest_path = log_root / "profile_manifest.json"
    if not manifest_path.exists():
        return None
    return load_json(manifest_path)


def missing_manifest_steps(
    manifest: JsonObject | None, records: Sequence[JsonObject]
) -> list[JsonObject]:
    if manifest is None:
        return []
    profile_run_id = manifest.get("profile_run_id")
    if not isinstance(profile_run_id, str):
        return [invalid_manifest("profile_manifest.json has no valid profile_run_id")]
    profile_step_keys = result_profile_step_keys(records)
    steps = manifest.get("steps")
    missing_steps: list[JsonObject] = []
    if not isinstance(steps, list):
        return [
            {
                "id": "profile-manifest",
                "label": "profile_manifest.json",
                "title": "profile_manifest.json has no valid steps list",
                "required": True,
            }
        ]
    for step_number, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            missing_steps.append(malformed_manifest_step(step_number))
            continue
        step_id = step.get("id")
        label = step.get("label")
        profile_step_fingerprint = step.get("profile_step_fingerprint")
        if not (
            isinstance(step_id, str)
            and isinstance(label, str)
            and isinstance(profile_step_fingerprint, str)
        ):
            missing_steps.append(malformed_manifest_step(step_number))
            continue
        if (
            profile_run_id,
            step_id,
            label,
            profile_step_fingerprint,
        ) not in profile_step_keys:
            missing_steps.append(
                {
                    "id": step_id,
                    "label": label,
                    "title": str(step.get("title", "")),
                    "required": step.get("required") is True,
                }
            )
    return missing_steps


def result_profile_step_keys(
    records: Sequence[JsonObject],
) -> set[tuple[str, str, str, str]]:
    profile_step_keys: set[tuple[str, str, str, str]] = set()
    for record in records:
        profile_run_id = record.get("profile_run_id")
        step_id = record.get("profile_step_id")
        label = record.get("label")
        profile_step_fingerprint = record.get("profile_step_fingerprint")
        if (
            isinstance(profile_run_id, str)
            and isinstance(step_id, str)
            and isinstance(label, str)
            and isinstance(profile_step_fingerprint, str)
        ):
            profile_step_keys.add(
                (profile_run_id, step_id, label, profile_step_fingerprint)
            )
    return profile_step_keys


def invalid_manifest(title: str) -> JsonObject:
    return {
        "id": "profile-manifest",
        "label": "profile_manifest.json",
        "title": title,
        "required": True,
    }


def malformed_manifest_step(step_number: int) -> JsonObject:
    return {
        "id": "profile-manifest",
        "label": f"profile_manifest.json steps[{step_number}]",
        "title": "profile_manifest.json contains malformed step entry",
        "required": True,
    }


def run_report(log_root: Path, out_root: Path) -> int:
    out_root.mkdir(parents=True, exist_ok=True)
    started_monotonic = time.monotonic()
    started_at = utc_now()
    results: list[JsonObject]
    read_errors: list[JsonObject]
    try:
        results, read_errors = collect_result_files(log_root, out_root)
    except (FileNotFoundError, PermissionError, OSError) as error:
        results = []
        read_errors = [{"path": str(log_root), "status": "FAIL", "error": str(error)}]
    if not results and not read_errors:
        read_errors.append(
            {
                "path": str(log_root),
                "status": "WARN",
                "error": "No result.json files found",
            }
        )
    profile_manifest: JsonObject | None = None
    if log_root.exists():
        try:
            profile_manifest = load_profile_manifest(log_root)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            read_errors.append(
                {
                    "path": str(log_root / "profile_manifest.json"),
                    "status": "FAIL",
                    "error": str(error),
                }
            )
    missing_steps = missing_manifest_steps(profile_manifest, results)
    status, exit_code = final_status(results, read_errors, missing_steps)
    summary: JsonObject = {
        "status": status.value,
        "result": status.value,
        "exit_code": exit_code,
        "started_at": started_at,
        "ended_at": utc_now(),
        "duration_seconds": elapsed_seconds(started_monotonic),
        "completed_reason": "completed",
        "log_root": str(log_root),
        "out_root": str(out_root),
        "results": [cast(JsonValue, result) for result in results],
        "read_errors": [cast(JsonValue, read_error) for read_error in read_errors],
        "missing_steps": [cast(JsonValue, step) for step in missing_steps],
        "profile_manifest": profile_manifest or {},
        "supporting_artifacts": collect_supporting_artifacts(log_root, out_root)
        if log_root.exists()
        else {},
    }
    write_json(out_root / "readiness_report.json", summary)
    write_json(out_root / "result.json", summary)
    write_text(out_root / "readiness_report.md", markdown_report(summary))
    return exit_code


def collect_supporting_artifacts(log_root: Path, out_root: Path) -> JsonObject:
    return {
        "audit_manifests": collect_paths(log_root, out_root, "manifest.json"),
        "audit_inventories": collect_paths(log_root, out_root, "inventory.tsv"),
        "triage_summaries": collect_paths(log_root, out_root, "triage_summary.json"),
    }


def collect_paths(log_root: Path, out_root: Path, pattern: str) -> list[JsonValue]:
    return [
        str(path)
        for path in sorted(log_root.rglob(pattern))
        if not path_is_within(path, out_root)
    ]


def markdown_report(summary: JsonObject) -> str:
    result_records = [record for record in cast(list[JsonObject], summary["results"])]
    read_errors = [record for record in cast(list[JsonObject], summary["read_errors"])]
    missing_steps = [
        record for record in cast(list[JsonObject], summary["missing_steps"])
    ]
    lines = [
        "# Hardware Validation Readiness Report",
        "",
        f"Final status: {summary['status']}",
        f"Result files: {len(result_records)}",
        "",
        "| Status | Label | Failures | Warnings | Exit Code | Path |",
        "|---|---|---:|---:|---:|---|",
    ]
    for record in result_records:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(record.get("status", "")),
                    markdown_cell(str(record.get("label", ""))),
                    str(record.get("failures", "")),
                    str(record.get("warnings", "")),
                    str(record.get("exit_code", "")),
                    markdown_cell(str(record.get("path", ""))),
                ]
            )
            + " |"
        )
    if not result_records:
        lines.append(
            "| WARN | no result files | 0 | 1 | 2 | No validation result files found. |"
        )
    if missing_steps:
        lines.extend(
            [
                "",
                "## Missing Required Steps",
                "",
                "| Required | Step | Label | Title |",
                "|---|---|---|---|",
            ]
        )
        for step in missing_steps:
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(step.get("required", "")),
                        markdown_cell(str(step.get("id", ""))),
                        markdown_cell(str(step.get("label", ""))),
                        markdown_cell(str(step.get("title", ""))),
                    ]
                )
                + " |"
            )
    if read_errors:
        lines.extend(
            [
                "",
                "## Read Errors",
                "",
                "| Status | Path | Error |",
                "|---|---|---|",
            ]
        )
        for read_error in read_errors:
            lines.append(
                "| "
                + " | ".join(
                    [
                        markdown_cell(str(read_error.get("status", ""))),
                        markdown_cell(str(read_error.get("path", ""))),
                        markdown_cell(str(read_error.get("error", ""))),
                    ]
                )
                + " |"
            )
    return "\n".join(lines) + "\n"


def markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")[:500]
