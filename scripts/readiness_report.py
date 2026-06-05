#!/usr/bin/env python3
"""Build a final PASS/WARN/FAIL readiness report from validation artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn, cast, override

type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)
type JsonObject = dict[str, JsonValue]

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_WARN = 2
EXIT_USAGE = 64
EXIT_TOOLING = 70


@dataclass(frozen=True, slots=True)
class ReportSettings:
    log_root: Path
    out_root: Path


class ValidationArgumentParser(argparse.ArgumentParser):
    @override
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as text_file:
        print(text, file=text_file, end="")


def write_json(path: Path, payload: JsonValue) -> None:
    write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def require_argument_action(action: argparse.Action) -> None:
    if not action.dest:
        raise RuntimeError("argparse returned an action without a destination")


def require_absolute_path(path_text: str, argument_name: str) -> Path:
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{argument_name} must be an absolute path")
    return path.resolve(strict=False)


def path_is_within(path: Path, possible_parent: Path) -> bool:
    return path.is_relative_to(possible_parent)


def load_json(path: Path) -> JsonObject:
    with path.open("r", encoding="utf-8") as json_file:
        value = cast(object, json.load(json_file))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root is not an object: {path}")
    return cast(JsonObject, value)


def result_status(result_payload: JsonObject) -> str:
    for status_key in ("status", "result"):
        status_value = result_payload.get(status_key)
        if isinstance(status_value, str):
            normalized_status = status_value.upper()
            if normalized_status in {"PASS", "WARN", "FAIL"}:
                return normalized_status
    exit_code = result_payload.get("exit_code")
    if exit_code in (1, 70):
        return "FAIL"
    if exit_code == 2:
        return "WARN"
    failures = result_payload.get("failures")
    warnings = result_payload.get("warnings")
    if isinstance(failures, int) and failures > 0:
        return "FAIL"
    if isinstance(warnings, int) and warnings > 0:
        return "WARN"
    return "PASS"


def result_record(path: Path, payload: JsonObject) -> JsonObject:
    return {
        "path": str(path),
        "status": result_status(payload),
        "label": payload.get("label", ""),
        "failures": payload.get("failures", 0),
        "warnings": payload.get("warnings", 0),
        "exit_code": payload.get("exit_code", 0),
    }


def collect_result_files(
    settings: ReportSettings,
) -> tuple[list[JsonObject], list[JsonObject]]:
    if not settings.log_root.exists():
        raise FileNotFoundError(f"--log-root does not exist: {settings.log_root}")
    results: list[JsonObject] = []
    read_errors: list[JsonObject] = []
    for result_path in sorted(settings.log_root.rglob("result.json")):
        if path_is_within(result_path, settings.out_root):
            continue
        try:
            results.append(result_record(result_path, load_json(result_path)))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            read_errors.append(
                {
                    "path": str(result_path),
                    "status": "FAIL",
                    "error": str(error),
                }
            )
    return results, read_errors


def collect_supporting_artifacts(settings: ReportSettings) -> JsonObject:
    manifest_paths = [
        str(path)
        for path in sorted(settings.log_root.rglob("manifest.json"))
        if not path_is_within(path, settings.out_root)
    ]
    inventory_paths = [
        str(path)
        for path in sorted(settings.log_root.rglob("inventory.tsv"))
        if not path_is_within(path, settings.out_root)
    ]
    triage_paths = [
        str(path)
        for path in sorted(settings.log_root.rglob("triage_summary.json"))
        if not path_is_within(path, settings.out_root)
    ]
    return {
        "audit_manifests": cast(JsonValue, manifest_paths),
        "audit_inventories": cast(JsonValue, inventory_paths),
        "triage_summaries": cast(JsonValue, triage_paths),
    }


def final_status(
    records: Sequence[JsonObject], read_errors: Sequence[JsonObject]
) -> tuple[str, int]:
    if any(record.get("status") == "FAIL" for record in read_errors) or any(
        record.get("status") == "FAIL" for record in records
    ):
        return "FAIL", EXIT_FAIL
    if any(record.get("status") == "WARN" for record in read_errors) or any(
        record.get("status") == "WARN" for record in records
    ):
        return "WARN", EXIT_WARN
    return "PASS", EXIT_PASS


def markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")[:500]


def markdown_report(summary: JsonObject) -> str:
    result_records = [record for record in cast(list[JsonObject], summary["results"])]
    lines = [
        "# Hardware Validation Readiness Report",
        "",
        f"Final status: {summary['status']}",
        f"Result files: {len(result_records)}",
        f"Read errors: {len(cast(list[JsonObject], summary['read_errors']))}",
        "",
        "| Status | Label | Failures | Warnings | Exit Code | Path |",
        "|---|---|---:|---:|---:|---|",
    ]
    for record in result_records:
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_cell(str(record.get("status", ""))),
                    markdown_cell(str(record.get("label", ""))),
                    markdown_cell(str(record.get("failures", ""))),
                    markdown_cell(str(record.get("warnings", ""))),
                    markdown_cell(str(record.get("exit_code", ""))),
                    markdown_cell(str(record.get("path", ""))),
                ]
            )
            + " |"
        )
    if not result_records:
        lines.append(
            "| WARN | no result files | 0 | 1 | 2 | No validation result files found. |"
        )
    return "\n".join(lines) + "\n"


def run_report(settings: ReportSettings) -> int:
    settings.out_root.mkdir(parents=True, exist_ok=True)
    started_at = utc_now()
    results: list[JsonObject]
    read_errors: list[JsonObject]
    try:
        results, read_errors = collect_result_files(settings)
    except (FileNotFoundError, PermissionError, OSError) as error:
        results = []
        read_error: JsonObject = {
            "path": str(settings.log_root),
            "status": "FAIL",
            "error": str(error),
        }
        read_errors = [
            read_error,
        ]
    if not results and not read_errors:
        read_error = {
            "path": str(settings.log_root),
            "status": "WARN",
            "error": "No result.json files found",
        }
        read_errors.append(
            read_error,
        )
    status, exit_code = final_status(results, read_errors)
    summary: JsonObject = {
        "status": status,
        "result": status,
        "exit_code": exit_code,
        "started_at": started_at,
        "ended_at": utc_now(),
        "log_root": str(settings.log_root),
        "out_root": str(settings.out_root),
        "results": cast(JsonValue, results),
        "read_errors": cast(JsonValue, read_errors),
        "supporting_artifacts": collect_supporting_artifacts(settings)
        if settings.log_root.exists()
        else {},
        "final_rule": "PASS requires no failures and no unresolved serious warnings. WARN requires human review. FAIL means at least one hard failure.",
    }
    write_json(settings.out_root / "readiness_report.json", summary)
    write_json(settings.out_root / "result.json", summary)
    write_text(settings.out_root / "readiness_report.md", markdown_report(summary))
    print(f"{utc_now()} [INFO] RESULT={status} out_root={settings.out_root}")
    return exit_code


def parse_arguments(arguments: Sequence[str]) -> ReportSettings:
    parser = ValidationArgumentParser(
        description="Build a hardware validation readiness report."
    )
    require_argument_action(
        parser.add_argument(
            "--log-root", required=True, help="Required absolute log root to summarize."
        )
    )
    require_argument_action(
        parser.add_argument(
            "--out-root", required=True, help="Required absolute output root."
        )
    )
    namespace = parser.parse_args(arguments)
    try:
        return ReportSettings(
            log_root=require_absolute_path(cast(str, namespace.log_root), "--log-root"),
            out_root=require_absolute_path(cast(str, namespace.out_root), "--out-root"),
        )
    except ValueError as error:
        parser.error(str(error))


def main(arguments: Sequence[str] | None = None) -> int:
    try:
        return run_report(
            parse_arguments(sys.argv[1:] if arguments is None else arguments)
        )
    except PermissionError as error:
        print(f"Permission denied: {error}", file=sys.stderr)
        return EXIT_TOOLING


if __name__ == "__main__":
    raise SystemExit(main())
