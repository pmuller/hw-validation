from __future__ import annotations

import re
import time
from pathlib import Path

from hw_validation.parsing import duration_seconds
from hw_validation.paths import ensure_directory, slug
from hw_validation.plan import (
    DurationMode,
    RunPhase,
    RunPlan,
    print_run_plan,
    write_run_plan,
)
from hw_validation.results import write_result
from hw_validation.runner import CommandRunner
from hw_validation.status import ResultStatus, outcome_from_counts
from hw_validation.timeutil import elapsed_seconds, utc_now, utc_stamp
from hw_validation.timing import write_timing_summary
from hw_validation.tooling import require_commands

SYSTEM_STRESS_COMMANDS = (
    "stress-ng",
    "stressapptest",
    "sensors",
    "vmstat",
    "iostat",
    "journalctl",
    "dmesg",
    "edac-util",
    "ras-mc-ctl",
    "awk",
    "free",
)

HARD_PATTERNS = (
    "Machine check exception",
    r"\bMCE\b",
    "Hardware Error",
    "EDAC uncorrected",
    "uncorrected EDAC",
    "PCIe AER fatal",
    "AER fatal",
    "PCIe AER nonfatal",
    "kernel oops",
    "Oops:",
    "panic",
    "BUG:",
    "soft lockup",
    "hard lockup",
    "hung task",
    "critical temperature",
    "watchdog",
    "segfault",
)

CORRECTED_ECC_PATTERNS = (
    "ECC corrected",
    "corrected ECC",
    "EDAC corrected",
    "corrected EDAC",
)
THERMAL_PATTERNS = ("thermal throttling", "throttled")


def run_system_stress(
    out_root: Path,
    label: str,
    phase_duration: str,
    mem_percent: int,
    memtester_amount: str | None,
    allow_corrected_ecc: bool,
    allow_thermal_throttle: bool,
) -> int:
    require_commands(SYSTEM_STRESS_COMMANDS)
    started_monotonic = time.monotonic()
    seconds = duration_seconds(phase_duration)
    if mem_percent < 1 or mem_percent > 95:
        raise ValueError("--mem-percent must be between 1 and 95")
    run_directory = out_root / "system-stress" / f"{utc_stamp()}_{slug(label)}"
    ensure_directory(run_directory)
    plan = RunPlan(
        command="system stress",
        duration_mode=DurationMode.phase_bounded,
        estimated_minimum_seconds=seconds * 2,
        requested_duration_seconds=seconds,
        phases=(
            RunPhase("snapshot-before", "Capture kernel, EDAC, RAS, and sensor state."),
            RunPhase(
                "stress-ng",
                "Run CPU and memory stress workload.",
                seconds,
                phase_duration,
            ),
            RunPhase(
                "stressapptest",
                "Run stressapptest memory workload.",
                seconds,
                phase_duration,
            ),
            RunPhase(
                "snapshot-after", "Capture post-stress kernel and hardware state."
            ),
        ),
        notes=("Minimum runtime excludes snapshots and optional memtester.",),
    )
    write_run_plan(run_directory, plan)
    print_run_plan(plan)
    runner = CommandRunner(run_directory)
    started_at = utc_now()
    collect_snapshot(runner, run_directory / "before")
    failures = 0
    warnings = 0
    if not runner.stream(
        "stress_ng",
        stress_ng_command(phase_duration, mem_percent),
        run_directory / "stress-ng.stdout",
        run_directory / "stress-ng.stderr",
    ).ok:
        failures += 1
    stressapptest_memory = memory_megabytes(mem_percent)
    if not runner.stream(
        "stressapptest",
        ["stressapptest", "-W", "-s", str(seconds), "-M", str(stressapptest_memory)],
        run_directory / "stressapptest.stdout",
        run_directory / "stressapptest.stderr",
    ).ok:
        failures += 1
    if (
        memtester_amount
        and not runner.stream(
            "memtester",
            ["memtester", memtester_amount, "1"],
            run_directory / "memtester.stdout",
            run_directory / "memtester.stderr",
        ).ok
    ):
        failures += 1
    collect_snapshot(runner, run_directory / "after")
    kernel_failures, kernel_warnings = scan_kernel_logs(
        run_directory / "after", allow_corrected_ecc, allow_thermal_throttle
    )
    outcome = outcome_from_counts(
        failures + kernel_failures, warnings + kernel_warnings
    )
    write_result(
        run_directory / "result.json",
        outcome,
        label,
        run_directory,
        elapsed_seconds(started_monotonic),
        {
            "started_at": started_at,
            "duration_mode": DurationMode.phase_bounded.value,
            "phase_duration": phase_duration,
            "phase_duration_seconds": seconds,
            "estimated_minimum_seconds": seconds * 2,
            "mem_percent": mem_percent,
            "memtester_amount": memtester_amount or "",
        },
    )
    write_timing_summary(run_directory)
    return outcome.exit_code


def stress_ng_command(duration: str, mem_percent: int) -> list[str]:
    return [
        "stress-ng",
        "--cpu",
        "0",
        "--cpu-method",
        "all",
        "--matrix",
        "0",
        "--vm",
        "2",
        "--vm-bytes",
        f"{mem_percent}%",
        "--verify",
        "--metrics-brief",
        "--timeout",
        duration,
    ]


def memory_megabytes(mem_percent: int) -> int:
    meminfo = Path("/proc/meminfo").read_text(encoding="utf-8", errors="replace")
    for line in meminfo.splitlines():
        if line.startswith("MemTotal:"):
            return max(1, int(line.split()[1]) // 1024 * mem_percent // 100)
    return 1


def collect_snapshot(runner: CommandRunner, snapshot_directory: Path) -> None:
    ensure_directory(snapshot_directory)
    _ = runner.stream(
        "journal_kernel",
        ["journalctl", "-k", "--no-pager", "-o", "short-iso-precise"],
        snapshot_directory / "kernel_journal.log",
        snapshot_directory / "kernel_journal.stderr",
    )
    _ = runner.stream(
        "dmesg",
        ["dmesg", "-T"],
        snapshot_directory / "dmesg.log",
        snapshot_directory / "dmesg.stderr",
    )
    _ = runner.stream(
        "edac",
        ["edac-util", "--verbose"],
        snapshot_directory / "edac-util.log",
        snapshot_directory / "edac-util.stderr",
    )
    _ = runner.stream(
        "ras",
        ["ras-mc-ctl", "--errors"],
        snapshot_directory / "ras-errors.log",
        snapshot_directory / "ras-errors.stderr",
    )
    _ = runner.stream(
        "sensors",
        ["sensors"],
        snapshot_directory / "sensors.log",
        snapshot_directory / "sensors.stderr",
    )


def scan_kernel_logs(
    snapshot_directory: Path, allow_corrected_ecc: bool, allow_thermal_throttle: bool
) -> tuple[int, int]:
    failures = 0
    warnings = 0
    for log_path in (
        snapshot_directory / "kernel_journal.log",
        snapshot_directory / "dmesg.log",
    ):
        text = (
            log_path.read_text(encoding="utf-8", errors="replace")
            if log_path.exists()
            else ""
        )
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in HARD_PATTERNS):
            failures += 1
        if any(
            re.search(pattern, text, re.IGNORECASE)
            for pattern in CORRECTED_ECC_PATTERNS
        ):
            if allow_corrected_ecc:
                warnings += 1
            else:
                failures += 1
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in THERMAL_PATTERNS):
            if allow_thermal_throttle:
                warnings += 1
            else:
                failures += 1
    return failures, warnings


def result_status_for_code(exit_code: int) -> ResultStatus:
    if exit_code == 0:
        return ResultStatus.pass_status
    if exit_code == 2:
        return ResultStatus.warn
    return ResultStatus.fail
