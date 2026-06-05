#!/usr/bin/env python3
"""Scan validation logs and classify hardware-validation findings."""

from __future__ import annotations

import argparse
import json
import re
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
class TriagePattern:
    name: str
    category: str
    severity: str
    expression: re.Pattern[str]


@dataclass(frozen=True, slots=True)
class TriageSettings:
    log_root: Path
    out_root: Path


class ValidationArgumentParser(argparse.ArgumentParser):
    @override
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


PATTERNS = (
    TriagePattern(
        "machine_check_exception",
        "cpu",
        "failure",
        re.compile(r"machine check exception", re.IGNORECASE),
    ),
    TriagePattern("mce", "cpu", "failure", re.compile(r"\bMCE\b", re.IGNORECASE)),
    TriagePattern(
        "hardware_error",
        "hardware",
        "failure",
        re.compile(r"hardware error", re.IGNORECASE),
    ),
    TriagePattern(
        "edac_uncorrected",
        "memory",
        "failure",
        re.compile(
            r"EDAC.*uncorrected|uncorrected.*EDAC|ECC uncorrected|uncorrected ECC",
            re.IGNORECASE,
        ),
    ),
    TriagePattern(
        "ecc_corrected",
        "memory",
        "warning",
        re.compile(
            r"ECC corrected|corrected ECC|EDAC.*corrected|corrected.*EDAC",
            re.IGNORECASE,
        ),
    ),
    TriagePattern("edac", "memory", "warning", re.compile(r"\bEDAC\b", re.IGNORECASE)),
    TriagePattern(
        "pcie_aer_fatal",
        "pcie",
        "failure",
        re.compile(r"PCIe AER fatal|AER.*fatal", re.IGNORECASE),
    ),
    TriagePattern(
        "pcie_aer_nonfatal",
        "pcie",
        "failure",
        re.compile(r"PCIe AER nonfatal|AER.*non.?fatal", re.IGNORECASE),
    ),
    TriagePattern(
        "pcie_aer_corrected",
        "pcie",
        "warning",
        re.compile(r"PCIe AER corrected|AER.*corrected", re.IGNORECASE),
    ),
    TriagePattern(
        "io_error", "storage", "failure", re.compile(r"\bI/O error\b", re.IGNORECASE)
    ),
    TriagePattern(
        "buffer_io_error",
        "storage",
        "failure",
        re.compile(r"buffer I/O error", re.IGNORECASE),
    ),
    TriagePattern(
        "blk_update_request",
        "storage",
        "failure",
        re.compile(r"blk_update_request", re.IGNORECASE),
    ),
    TriagePattern(
        "nvme_reset", "storage", "failure", re.compile(r"NVMe reset", re.IGNORECASE)
    ),
    TriagePattern(
        "nvme_timeout", "storage", "failure", re.compile(r"NVMe timeout", re.IGNORECASE)
    ),
    TriagePattern(
        "nvme_controller_down",
        "storage",
        "failure",
        re.compile(r"NVMe controller down", re.IGNORECASE),
    ),
    TriagePattern(
        "sata_link_reset",
        "storage",
        "failure",
        re.compile(
            r"SATA link reset|link is slow to respond|hard resetting link",
            re.IGNORECASE,
        ),
    ),
    TriagePattern(
        "ata_exception",
        "storage",
        "failure",
        re.compile(r"ATA exception", re.IGNORECASE),
    ),
    TriagePattern(
        "smart_failure",
        "storage",
        "failure",
        re.compile(r"SMART.*fail|SMART failure", re.IGNORECASE),
    ),
    TriagePattern(
        "fio_verify_failure",
        "storage",
        "failure",
        re.compile(r"fio.*verify.*fail|verify.*failed", re.IGNORECASE),
    ),
    TriagePattern(
        "badblocks_failure",
        "storage",
        "failure",
        re.compile(r"badblocks.*fail|bad blocks found", re.IGNORECASE),
    ),
    TriagePattern(
        "kernel_oops",
        "kernel",
        "failure",
        re.compile(r"kernel oops|Oops:", re.IGNORECASE),
    ),
    TriagePattern(
        "panic", "kernel", "failure", re.compile(r"\bpanic\b", re.IGNORECASE)
    ),
    TriagePattern(
        "kernel_bug", "kernel", "failure", re.compile(r"BUG:", re.IGNORECASE)
    ),
    TriagePattern(
        "hung_task", "kernel", "failure", re.compile(r"hung task", re.IGNORECASE)
    ),
    TriagePattern(
        "soft_lockup", "kernel", "failure", re.compile(r"soft lockup", re.IGNORECASE)
    ),
    TriagePattern(
        "hard_lockup", "kernel", "failure", re.compile(r"hard lockup", re.IGNORECASE)
    ),
    TriagePattern(
        "thermal_throttling",
        "thermal",
        "warning",
        re.compile(r"thermal throttling|throttled", re.IGNORECASE),
    ),
    TriagePattern(
        "critical_temperature",
        "thermal",
        "failure",
        re.compile(r"critical temperature|temperature above threshold", re.IGNORECASE),
    ),
    TriagePattern(
        "watchdog", "kernel", "failure", re.compile(r"watchdog", re.IGNORECASE)
    ),
    TriagePattern(
        "segfault", "process", "failure", re.compile(r"segfault", re.IGNORECASE)
    ),
    TriagePattern(
        "filesystem_read_only",
        "filesystem",
        "failure",
        re.compile(
            r"filesystem.*remount.*read-only|remounted read-only", re.IGNORECASE
        ),
    ),
    TriagePattern(
        "link_flap",
        "network",
        "failure",
        re.compile(r"link down.*link up|link up.*link down|link flap", re.IGNORECASE),
    ),
    TriagePattern(
        "network_driver_reset",
        "network",
        "failure",
        re.compile(r"network.*driver.*reset|NIC.*reset|adapter.*reset", re.IGNORECASE),
    ),
)


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


def should_scan_file(file_path: Path, out_root: Path) -> bool:
    if path_is_within(file_path, out_root):
        return False
    if file_path.is_dir():
        return False
    return True


def finding(
    pattern: TriagePattern, file_path: Path, line_number: int, line: str
) -> JsonObject:
    return {
        "file_path": str(file_path),
        "line_number": line_number,
        "severity": pattern.severity,
        "category": pattern.category,
        "pattern": pattern.name,
        "matched_text": line.strip()[:1000],
    }


def scan_file(file_path: Path) -> tuple[list[JsonObject], JsonObject | None]:
    findings: list[JsonObject] = []
    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as log_file:
            for line_number, line in enumerate(log_file, start=1):
                for pattern in PATTERNS:
                    if pattern.expression.search(line):
                        findings.append(finding(pattern, file_path, line_number, line))
    except PermissionError as error:
        return findings, {
            "file_path": str(file_path),
            "line_number": 0,
            "severity": "failure",
            "category": "permission",
            "pattern": "permission_denied",
            "matched_text": str(error),
        }
    except OSError as error:
        return findings, {
            "file_path": str(file_path),
            "line_number": 0,
            "severity": "warning",
            "category": "read-error",
            "pattern": "read_error",
            "matched_text": str(error),
        }
    return findings, None


def scan_logs(settings: TriageSettings) -> list[JsonObject]:
    if not settings.log_root.exists():
        raise FileNotFoundError(f"--log-root does not exist: {settings.log_root}")
    all_findings: list[JsonObject] = []
    for file_path in sorted(settings.log_root.rglob("*")):
        if not should_scan_file(file_path, settings.out_root):
            continue
        findings, scan_error = scan_file(file_path)
        all_findings.extend(findings)
        if scan_error is not None:
            all_findings.append(scan_error)
    return all_findings


def status_for(findings: Sequence[JsonObject]) -> tuple[str, int]:
    has_failure = any(
        finding_item.get("severity") == "failure" for finding_item in findings
    )
    has_warning = any(
        finding_item.get("severity") == "warning" for finding_item in findings
    )
    if has_failure:
        return "FAIL", EXIT_FAIL
    if has_warning:
        return "WARN", EXIT_WARN
    return "PASS", EXIT_PASS


def count_by_key(findings: Sequence[JsonObject], key: str) -> JsonObject:
    counts: dict[str, int] = {}
    for finding_item in findings:
        value = finding_item.get(key)
        if isinstance(value, str):
            counts[value] = counts.get(value, 0) + 1
    return {count_key: count for count_key, count in sorted(counts.items())}


def markdown_summary(summary: JsonObject, findings: Sequence[JsonObject]) -> str:
    lines = [
        "# Log Triage Summary",
        "",
        f"Status: {summary['status']}",
        f"Failures: {summary['failures']}",
        f"Warnings: {summary['warnings']}",
        "",
        "| Severity | Category | File | Line | Matched Text |",
        "|---|---|---|---:|---|",
    ]
    for finding_item in findings:
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_cell(str(finding_item.get("severity", ""))),
                    markdown_cell(str(finding_item.get("category", ""))),
                    markdown_cell(str(finding_item.get("file_path", ""))),
                    markdown_cell(str(finding_item.get("line_number", ""))),
                    markdown_cell(str(finding_item.get("matched_text", ""))),
                ]
            )
            + " |"
        )
    if not findings:
        lines.append("| pass | none |  |  | No findings. |")
    return "\n".join(lines) + "\n"


def markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")[:500]


def run_triage(settings: TriageSettings) -> int:
    settings.out_root.mkdir(parents=True, exist_ok=True)
    started_at = utc_now()
    findings: list[JsonObject]
    try:
        findings = scan_logs(settings)
    except (FileNotFoundError, PermissionError, OSError) as error:
        scan_error: JsonObject = {
            "file_path": str(settings.log_root),
            "line_number": 0,
            "severity": "failure",
            "category": "setup",
            "pattern": "scan_error",
            "matched_text": str(error),
        }
        findings = [
            scan_error,
        ]
    status, exit_code = status_for(findings)
    summary: JsonObject = {
        "status": status,
        "result": status,
        "exit_code": exit_code,
        "started_at": started_at,
        "ended_at": utc_now(),
        "log_root": str(settings.log_root),
        "out_root": str(settings.out_root),
        "failures": sum(
            1 for finding_item in findings if finding_item.get("severity") == "failure"
        ),
        "warnings": sum(
            1 for finding_item in findings if finding_item.get("severity") == "warning"
        ),
        "counts_by_category": count_by_key(findings, "category"),
        "counts_by_pattern": count_by_key(findings, "pattern"),
        "findings": cast(JsonValue, findings),
    }
    write_json(settings.out_root / "triage_summary.json", summary)
    write_json(settings.out_root / "result.json", summary)
    write_text(
        settings.out_root / "triage_summary.md", markdown_summary(summary, findings)
    )
    print(f"{utc_now()} [INFO] RESULT={status} out_root={settings.out_root}")
    return exit_code


def parse_arguments(arguments: Sequence[str]) -> TriageSettings:
    parser = ValidationArgumentParser(
        description="Scan validation logs for hardware findings."
    )
    require_argument_action(
        parser.add_argument(
            "--log-root", required=True, help="Required absolute log root to scan."
        )
    )
    require_argument_action(
        parser.add_argument(
            "--out-root", required=True, help="Required absolute output root."
        )
    )
    namespace = parser.parse_args(arguments)
    try:
        return TriageSettings(
            log_root=require_absolute_path(cast(str, namespace.log_root), "--log-root"),
            out_root=require_absolute_path(cast(str, namespace.out_root), "--out-root"),
        )
    except ValueError as error:
        parser.error(str(error))


def main(arguments: Sequence[str] | None = None) -> int:
    try:
        return run_triage(
            parse_arguments(sys.argv[1:] if arguments is None else arguments)
        )
    except PermissionError as error:
        print(f"Permission denied: {error}", file=sys.stderr)
        return EXIT_TOOLING


if __name__ == "__main__":
    raise SystemExit(main())
