from __future__ import annotations

import json
import time
from pathlib import Path
from typing import cast

from hw_validation.files import write_text
from hw_validation.log_scan import matching_lines
from hw_validation.parsing import duration_seconds
from hw_validation.paths import ensure_directory, path_is_within, slug
from hw_validation.plan import (
    DurationMode,
    RunPhase,
    RunPlan,
    print_run_plan,
    write_run_plan,
)
from hw_validation.results import write_result
from hw_validation.runner import CommandRunner
from hw_validation.status import outcome_from_counts
from hw_validation.timeutil import elapsed_seconds, utc_now, utc_stamp
from hw_validation.timing import write_timing_summary
from hw_validation.tooling import check_fio, require_commands

FILESYSTEM_COMMANDS = ("fio", "findmnt", "journalctl", "dmesg", "sync")
STORAGE_FAILURE_PATTERNS = (
    r"\bI/O error\b",
    r"buffer I/O error",
    r"blk_update_request",
    r"NVMe reset",
    r"NVMe timeout",
    r"NVMe controller down",
    r"SATA link reset",
    r"ATA exception",
    r"filesystem.*remount.*read-only",
    r"remounted read-only",
)


def run_filesystem_scratch(
    path: Path,
    out_root: Path,
    label: str,
    size: str,
    runtime: str,
    cleanup: bool,
) -> int:
    require_commands(FILESYSTEM_COMMANDS)
    started_monotonic = time.monotonic()
    fio_path, fio_version = check_fio()
    if fio_path is None or fio_version is None:
        raise RuntimeError("The fio command in PATH is not Flexible I/O Tester")
    runtime_seconds = duration_seconds(runtime)
    target_path = path.resolve(strict=True)
    if target_path == Path("/"):
        raise ValueError("refusing to operate directly on /")
    if not target_path.is_dir():
        raise ValueError("--path must be an existing directory")
    run_directory = out_root / "filesystem-scratch" / f"{utc_stamp()}_{slug(label)}"
    ensure_directory(run_directory)
    plan = RunPlan(
        command="filesystem scratch",
        duration_mode=DurationMode.mixed,
        estimated_minimum_seconds=runtime_seconds,
        requested_duration_seconds=runtime_seconds,
        phases=(
            RunPhase("snapshot-before", "Capture mount and kernel state."),
            RunPhase(
                "sequential-write-verify", "Run size-bound fio write/verify phase."
            ),
            RunPhase(
                "random-read-write",
                "Run time-bound random fio phase.",
                runtime_seconds,
                runtime,
            ),
            RunPhase(
                "snapshot-after", "Capture mount and kernel state, then scan logs."
            ),
        ),
        notes=(
            "Sequential write/verify runtime depends on storage throughput and --size.",
        ),
    )
    write_run_plan(run_directory, plan)
    print_run_plan(plan)
    scratch_directory = (
        target_path / f"hw-validation-scratch-{utc_stamp()}-{slug(label)}"
    )
    scratch_directory.mkdir()
    runner = CommandRunner(run_directory)
    started_at = utc_now()
    failures = 0
    capture_filesystem_snapshot(
        runner, run_directory / "before", target_path, started_at
    )
    if not runner.stream(
        "fio_sequential",
        fio_sequential_command(
            fio_path, scratch_directory, size, run_directory / "fio_sequential.json"
        ),
        run_directory / "fio_sequential.stdout",
        run_directory / "fio_sequential.stderr",
    ).ok:
        failures += 1
    if not runner.stream(
        "fio_random",
        fio_random_command(
            fio_path,
            scratch_directory,
            size,
            runtime,
            run_directory / "fio_random.json",
        ),
        run_directory / "fio_random.stdout",
        run_directory / "fio_random.stderr",
    ).ok:
        failures += 1
    failures += fio_job_failures(run_directory / "fio_sequential.json")
    failures += fio_job_failures(run_directory / "fio_random.json")
    runner.record("sync", ["sync"])
    capture_filesystem_snapshot(
        runner, run_directory / "after", target_path, started_at
    )
    kernel_findings = matching_lines(
        filesystem_kernel_scan_paths(run_directory),
        STORAGE_FAILURE_PATTERNS,
    )
    if kernel_findings:
        write_text(
            run_directory / "kernel_findings.log", "\n".join(kernel_findings) + "\n"
        )
        failures += len(kernel_findings)
    if filesystem_readonly(target_path):
        failures += 1
    if cleanup:
        cleanup_scratch(target_path, scratch_directory)
    outcome = outcome_from_counts(failures, 0)
    write_result(
        run_directory / "result.json",
        outcome,
        label,
        run_directory,
        elapsed_seconds(started_monotonic),
        {
            "started_at": started_at,
            "duration_mode": DurationMode.mixed.value,
            "requested_duration_seconds": runtime_seconds,
            "path": str(target_path),
            "scratch_directory": str(scratch_directory),
            "size": size,
            "runtime": runtime,
        },
    )
    write_timing_summary(run_directory)
    return outcome.exit_code


def filesystem_kernel_scan_paths(run_directory: Path) -> tuple[Path, ...]:
    return (run_directory / "after" / "kernel_journal_since_start.log",)


def fio_sequential_command(
    fio_path: str, scratch_directory: Path, size: str, output_path: Path
) -> list[str]:
    return [
        fio_path,
        "--name=sequential_write_verify",
        f"--directory={scratch_directory}",
        "--filename=sequential.dat",
        "--rw=write",
        "--bs=1M",
        f"--size={size}",
        "--direct=1",
        "--ioengine=psync",
        "--verify=crc32c",
        "--do_verify=1",
        "--verify_fatal=1",
        "--output-format=json",
        f"--output={output_path}",
    ]


def fio_random_command(
    fio_path: str, scratch_directory: Path, size: str, runtime: str, output_path: Path
) -> list[str]:
    return [
        fio_path,
        "--name=random_rw_verify",
        f"--directory={scratch_directory}",
        "--filename=random.dat",
        "--rw=randrw",
        "--rwmixread=70",
        "--bs=4k",
        f"--size={size}",
        f"--runtime={runtime}",
        "--time_based=1",
        "--direct=1",
        "--ioengine=psync",
        "--verify=crc32c",
        "--verify_fatal=1",
        "--output-format=json",
        f"--output={output_path}",
    ]


def capture_filesystem_snapshot(
    runner: CommandRunner,
    snapshot_directory: Path,
    target_path: Path,
    started_at: str,
) -> None:
    ensure_directory(snapshot_directory)
    _ = runner.stream(
        "journal_kernel",
        [
            "journalctl",
            "-k",
            "--since",
            started_at,
            "--no-pager",
            "-o",
            "short-iso-precise",
        ],
        snapshot_directory / "kernel_journal_since_start.log",
        snapshot_directory / "kernel_journal.stderr",
    )
    _ = runner.stream(
        "dmesg",
        ["dmesg", "-T"],
        snapshot_directory / "dmesg.log",
        snapshot_directory / "dmesg.stderr",
    )
    _ = runner.stream(
        "findmnt",
        ["findmnt", "-T", str(target_path)],
        snapshot_directory / "findmnt.log",
        snapshot_directory / "findmnt.stderr",
    )


def fio_job_failures(path: Path) -> int:
    try:
        payload_object = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return 1
    if not isinstance(payload_object, dict):
        return 1
    payload = cast(dict[str, object], payload_object)
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        return 1
    job_objects = cast(list[object], jobs)
    failures = 0
    for job in job_objects:
        if not isinstance(job, dict):
            failures += 1
            continue
        job_payload = cast(dict[str, object], job)
        error_value = job_payload.get("error")
        if isinstance(error_value, int) and error_value != 0:
            failures += 1
    return failures


def filesystem_readonly(path: Path) -> bool:
    runner = CommandRunner(verbose=False)
    result = runner.capture("findmnt", ["findmnt", "-T", str(path), "-no", "OPTIONS"])
    return any(option == "ro" for option in result.stdout.strip().split(","))


def cleanup_scratch(target_path: Path, scratch_directory: Path) -> None:
    if not path_is_within(scratch_directory, target_path):
        raise RuntimeError(f"refusing cleanup outside target path: {scratch_directory}")
    for item in sorted(scratch_directory.rglob("*"), reverse=True):
        if item.is_file() or item.is_symlink():
            item.unlink()
        elif item.is_dir():
            item.rmdir()
    scratch_directory.rmdir()
