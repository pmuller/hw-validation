from __future__ import annotations

import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from hw_validation.files import write_json, write_text
from hw_validation.json_types import JsonObject
from hw_validation.paths import path_is_within
from hw_validation.status import ExitCode, ResultStatus
from hw_validation.timeutil import elapsed_seconds, utc_now


@dataclass(frozen=True, slots=True)
class TriagePattern:
    name: str
    category: str
    severity: str
    expression: re.Pattern[str]


PATTERNS = (
    TriagePattern(
        "machine_check_exception",
        "cpu",
        "failure",
        re.compile(r"machine check exception", re.IGNORECASE),
    ),
    TriagePattern(
        "mce",
        "cpu",
        "failure",
        re.compile(
            r"\bMCE\b.*(?:error|exception|hardware)|(?:error|exception|hardware).*\bMCE\b",
            re.IGNORECASE,
        ),
    ),
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
        re.compile(r"SATA link reset|hard resetting link", re.IGNORECASE),
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
TRIAGE_FILE_MARKERS = (
    "badblocks",
    "dmesg",
    "edac",
    "fio",
    "journal",
    "kernel",
    "mcelog",
    "nvme",
    "ras",
    "smartctl",
)
TRIAGE_EXCLUDED_SUFFIXES = (".meta.json",)
BENIGN_LINE_PATTERNS = (
    re.compile(r"\bno\s+mce\s+errors?\b", re.IGNORECASE),
    re.compile(r"\bno\s+errors?\s+to\s+report\b", re.IGNORECASE),
    re.compile(r"\bedac drivers are loaded\b", re.IGNORECASE),
)


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


def scan_file(file_path: Path) -> list[JsonObject]:
    findings: list[JsonObject] = []
    with file_path.open("r", encoding="utf-8", errors="replace") as log_file:
        for line_number, line in enumerate(log_file, start=1):
            if benign_line(line):
                continue
            for pattern in PATTERNS:
                if pattern.expression.search(line):
                    findings.append(finding(pattern, file_path, line_number, line))
    return findings


def benign_line(line: str) -> bool:
    return any(pattern.search(line) for pattern in BENIGN_LINE_PATTERNS)


def triage_candidate(file_path: Path) -> bool:
    path_text = str(file_path).lower()
    if any(path_text.endswith(suffix) for suffix in TRIAGE_EXCLUDED_SUFFIXES):
        return False
    return any(marker in path_text for marker in TRIAGE_FILE_MARKERS)


def scan_logs(log_root: Path, out_root: Path) -> list[JsonObject]:
    if not log_root.exists():
        raise FileNotFoundError(f"--log-root does not exist: {log_root}")
    findings: list[JsonObject] = []
    for file_path in sorted(log_root.rglob("*")):
        if (
            file_path.is_dir()
            or path_is_within(file_path, out_root)
            or not triage_candidate(file_path)
        ):
            continue
        try:
            findings.extend(scan_file(file_path))
        except PermissionError as error:
            findings.append(
                {
                    "file_path": str(file_path),
                    "line_number": 0,
                    "severity": "failure",
                    "category": "permission",
                    "pattern": "permission_denied",
                    "matched_text": str(error),
                }
            )
        except OSError as error:
            findings.append(
                {
                    "file_path": str(file_path),
                    "line_number": 0,
                    "severity": "warning",
                    "category": "read-error",
                    "pattern": "read_error",
                    "matched_text": str(error),
                }
            )
    return findings


def status_for(findings: Sequence[JsonObject]) -> tuple[ResultStatus, int]:
    if any(finding_item.get("severity") == "failure" for finding_item in findings):
        return ResultStatus.fail, ExitCode.hard_failure.code
    if any(finding_item.get("severity") == "warning" for finding_item in findings):
        return ResultStatus.warn, ExitCode.warning.code
    return ResultStatus.pass_status, ExitCode.pass_status.code


def count_by_key(findings: Sequence[JsonObject], key: str) -> JsonObject:
    counts: dict[str, int] = {}
    for finding_item in findings:
        value = finding_item.get(key)
        if isinstance(value, str):
            counts[value] = counts.get(value, 0) + 1
    return {count_key: count for count_key, count in sorted(counts.items())}


def run_triage(log_root: Path, out_root: Path) -> int:
    out_root.mkdir(parents=True, exist_ok=True)
    started_monotonic = time.monotonic()
    started_at = utc_now()
    findings: list[JsonObject]
    try:
        findings = scan_logs(log_root, out_root)
    except (FileNotFoundError, PermissionError, OSError) as error:
        findings = [log_root_error_finding(log_root, error)]
    status, exit_code = status_for(findings)
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
        "failures": sum(
            1 for finding_item in findings if finding_item.get("severity") == "failure"
        ),
        "warnings": sum(
            1 for finding_item in findings if finding_item.get("severity") == "warning"
        ),
        "counts_by_category": count_by_key(findings, "category"),
        "counts_by_pattern": count_by_key(findings, "pattern"),
        "findings": [finding_item for finding_item in findings],
    }
    write_json(out_root / "triage_summary.json", summary)
    write_json(out_root / "result.json", summary)
    write_text(out_root / "triage_summary.md", markdown_summary(summary, findings))
    return exit_code


def log_root_error_finding(log_root: Path, error: OSError) -> JsonObject:
    return {
        "file_path": str(log_root),
        "line_number": 0,
        "severity": "failure",
        "category": "log-root",
        "pattern": "log_root_error",
        "matched_text": str(error),
    }


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
