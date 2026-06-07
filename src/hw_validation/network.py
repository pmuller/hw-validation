from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import cast

from hw_validation.files import read_text, write_text
from hw_validation.json_types import JsonObject
from hw_validation.log_scan import matching_lines
from hw_validation.parsing import bandwidth_bits, duration_seconds
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
from hw_validation.status import outcome_from_counts
from hw_validation.timeutil import elapsed_seconds, utc_now, utc_stamp
from hw_validation.timing import write_timing_summary
from hw_validation.tooling import require_commands

NETWORK_COMMANDS = ("iperf3", "ip", "ethtool", "journalctl", "dmesg")
NETWORK_FAILURE_PATTERNS = (
    r"link down",
    r"link up",
    r"link flap",
    r"NIC.*reset",
    r"adapter.*reset",
    r"network.*driver.*reset",
    r"transmit timeout",
    r"tx timeout",
    r"watchdog timeout",
)


def run_network_burnin(
    server: str,
    out_root: Path,
    label: str,
    interface: str | None,
    duration: str,
    parallel: int,
    bidirectional: bool,
    expect_bandwidth: str | None,
) -> int:
    require_commands(NETWORK_COMMANDS)
    started_monotonic = time.monotonic()
    seconds = duration_seconds(duration)
    if parallel < 1:
        raise ValueError("--parallel must be at least 1")
    selected_interface = interface or infer_interface(server)
    if not selected_interface:
        raise ValueError("could not infer interface; provide --interface")
    validate_network_interface(selected_interface)
    run_directory = out_root / "network-burnin" / f"{utc_stamp()}_{slug(label)}"
    ensure_directory(run_directory)
    plan = RunPlan(
        command="network burnin",
        duration_mode=DurationMode.bounded,
        estimated_minimum_seconds=seconds,
        requested_duration_seconds=seconds,
        phases=(
            RunPhase("snapshot-before", "Capture interface state and counters."),
            RunPhase("iperf3", "Run iperf3 traffic workload.", seconds, duration),
            RunPhase(
                "snapshot-after", "Capture interface state, counters, and kernel logs."
            ),
        ),
    )
    write_run_plan(run_directory, plan)
    print_run_plan(plan)
    runner = CommandRunner(run_directory)
    started_at = utc_now()
    capture_interface(runner, selected_interface, run_directory / "before", started_at)
    failures = 0
    if read_text(Path("/sys/class/net") / selected_interface / "operstate") != "up":
        failures += 1
    iperf_command = [
        "iperf3",
        "-J",
        "-c",
        server,
        "-t",
        str(seconds),
        "-P",
        str(parallel),
    ]
    if bidirectional:
        iperf_command.append("--bidir")
    iperf_result = runner.stream(
        "iperf3",
        iperf_command,
        run_directory / "iperf3.json",
        run_directory / "iperf3.stderr",
    )
    if not iperf_result.ok:
        failures += 1
    capture_interface(runner, selected_interface, run_directory / "after", started_at)
    failures += compare_error_stats(
        run_directory / "before" / "ethtool_stats.log",
        run_directory / "after" / "ethtool_stats.log",
    )
    kernel_findings = matching_lines(
        network_kernel_scan_paths(run_directory),
        NETWORK_FAILURE_PATTERNS,
    )
    if kernel_findings:
        write_text(
            run_directory / "kernel_findings.log", "\n".join(kernel_findings) + "\n"
        )
        failures += len(kernel_findings)
    if read_text(Path("/sys/class/net") / selected_interface / "operstate") != "up":
        failures += 1
    if (
        expect_bandwidth
        and iperf_result.ok
        and measured_bandwidth(run_directory / "iperf3.json")
        < bandwidth_bits(expect_bandwidth)
    ):
        failures += 1
    outcome = outcome_from_counts(failures, 0)
    write_result(
        run_directory / "result.json",
        outcome,
        label,
        run_directory,
        elapsed_seconds(started_monotonic),
        {
            "started_at": started_at,
            "duration_mode": DurationMode.bounded.value,
            "requested_duration_seconds": seconds,
            "server": server,
            "interface": selected_interface,
            "duration": duration,
            "parallel": parallel,
            "bidirectional": bidirectional,
        },
    )
    write_timing_summary(run_directory)
    return outcome.exit_code


def network_kernel_scan_paths(run_directory: Path) -> tuple[Path, ...]:
    return (run_directory / "after" / "kernel_journal_since_start.log",)


def validate_network_interface(
    interface: str, system_network_path: Path | None = None
) -> None:
    network_path = (
        Path("/sys/class/net") if system_network_path is None else system_network_path
    )
    if not (network_path / interface).exists():
        raise ValueError(f"network interface does not exist: {interface}")


def infer_interface(server: str) -> str:
    completed_process = subprocess.run(
        ["ip", "route", "get", server],
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        errors="replace",
        check=False,
    )
    parts = completed_process.stdout.split()
    for position, field in enumerate(parts):
        if field == "dev" and position + 1 < len(parts):
            return parts[position + 1]
    return ""


def capture_interface(
    runner: CommandRunner, interface: str, snapshot_directory: Path, started_at: str
) -> None:
    ensure_directory(snapshot_directory)
    write_text(
        snapshot_directory / "operstate",
        read_text(Path("/sys/class/net") / interface / "operstate") or "",
    )
    _ = runner.stream(
        "ip_link",
        ["ip", "link"],
        snapshot_directory / "ip_link.log",
        snapshot_directory / "ip_link.stderr",
    )
    _ = runner.stream(
        "ip_addr",
        ["ip", "addr"],
        snapshot_directory / "ip_addr.log",
        snapshot_directory / "ip_addr.stderr",
    )
    _ = runner.stream(
        "ethtool",
        ["ethtool", interface],
        snapshot_directory / "ethtool.log",
        snapshot_directory / "ethtool.stderr",
    )
    _ = runner.stream(
        "ethtool_driver",
        ["ethtool", "-i", interface],
        snapshot_directory / "ethtool_driver.log",
        snapshot_directory / "ethtool_driver.stderr",
    )
    _ = runner.stream(
        "ethtool_stats",
        ["ethtool", "-S", interface],
        snapshot_directory / "ethtool_stats.log",
        snapshot_directory / "ethtool_stats.stderr",
    )
    write_text(
        snapshot_directory / "error_stats.tsv",
        error_stats_table(snapshot_directory / "ethtool_stats.log"),
    )
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


def compare_error_stats(before_path: Path, after_path: Path) -> int:
    before = error_stats(before_path)
    after = error_stats(after_path)
    return sum(
        1
        for stat_name, before_value in before.items()
        if after.get(stat_name, before_value) > before_value
    )


def error_stats(path: Path) -> dict[str, int]:
    stats: dict[str, int] = {}
    for line in (read_text(path) or "").splitlines():
        if ":" not in line:
            continue
        stat_name, stat_value = [part.strip() for part in line.split(":", 1)]
        if not any(
            token in stat_name.lower()
            for token in ("error", "crc", "frame", "reset", "timeout")
        ):
            continue
        if stat_value.isdigit():
            stats[stat_name] = int(stat_value)
    return stats


def error_stats_table(path: Path) -> str:
    return "".join(
        f"{stat_name}\t{stat_value}\n"
        for stat_name, stat_value in sorted(error_stats(path).items())
    )


def measured_bandwidth(path: Path) -> float:
    payload = cast(JsonObject, json.loads(path.read_text(encoding="utf-8")))
    end_value = payload.get("end")
    if not isinstance(end_value, dict):
        return 0.0
    values: list[float] = []
    for key in ("sum", "sum_received", "sum_sent"):
        item = end_value.get(key)
        if isinstance(item, dict):
            bits = item.get("bits_per_second")
            if isinstance(bits, int | float):
                values.append(float(bits))
    return min(values) if values else 0.0
