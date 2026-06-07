from __future__ import annotations

import json
import tempfile
from collections.abc import Sequence
from pathlib import Path

import pytest

from hw_validation.disk import (
    block_metrics,
    discover_monitor_devices,
    monitor_device_name,
    monitor_required_commands,
    nvme_current_operation,
    nvme_selftest_complete,
    nvme_selftest_passed,
    smart_health_check,
    smart_selftest_in_progress,
    smart_selftest_passed,
    smartctl_command,
    validate_disk_burnin_modes,
)
from hw_validation.filesystem import filesystem_kernel_scan_paths, fio_job_failures
from hw_validation.log_scan import matching_lines
from hw_validation.network import (
    error_stats,
    error_stats_table,
    network_kernel_scan_paths,
    validate_network_interface,
)
from hw_validation.runner import CommandResult, CommandRunner


def test_matching_lines() -> None:
    with tempfile.TemporaryDirectory() as directory_text:
        log_path = Path(directory_text) / "kernel.log"
        _ = log_path.write_text("clean\nNVMe timeout on controller\n", encoding="utf-8")
        assert matching_lines((log_path,), (r"NVMe timeout",)) == [
            f"{log_path}:2:NVMe timeout on controller"
        ]


def test_fio_job_failures() -> None:
    with tempfile.TemporaryDirectory() as directory_text:
        fio_path = Path(directory_text) / "fio.json"
        _ = fio_path.write_text(
            json.dumps({"jobs": [{"error": 0}, {"error": 5}]}) + "\n",
            encoding="utf-8",
        )
        assert fio_job_failures(fio_path) == 1


def test_network_error_stats() -> None:
    with tempfile.TemporaryDirectory() as directory_text:
        stats_path = Path(directory_text) / "ethtool.log"
        _ = stats_path.write_text(
            "rx_packets: 10\nrx_crc_errors: 2\ntx_timeout: 1\n",
            encoding="utf-8",
        )
        assert error_stats(stats_path) == {"rx_crc_errors": 2, "tx_timeout": 1}
        assert error_stats_table(stats_path) == "rx_crc_errors\t2\ntx_timeout\t1\n"


def test_selftest_parsers() -> None:
    assert (
        smart_selftest_in_progress("Self-test routine in progress 90% remaining"),
        smart_selftest_passed("# 1  Short offline Completed without error 00%"),
        nvme_current_operation("Current operation: 0x1"),
        nvme_selftest_complete("Current operation: 0"),
        nvme_selftest_complete("Current operation: 0x1"),
        nvme_selftest_passed("Current operation: 0\nSelf-test Result: 0"),
    ) == (True, True, "0x1", True, False, True)


def test_block_metrics_uses_sample_interval() -> None:
    assert block_metrics(
        (20, 0, 2000, 0, 30, 0, 3000, 0, 0, 200, 0),
        (10, 0, 1000, 0, 20, 0, 1000, 0, 0, 100, 0),
        10.0,
    ) == {
        "raw": [20, 0, 2000, 0, 30, 0, 3000, 0, 0, 200, 0],
        "read_iops": 1.0,
        "write_iops": 1.0,
        "read_mb_per_second": 0.051,
        "write_mb_per_second": 0.102,
        "discard_mb_per_second": 0.0,
        "io_utilization_percent": 1.0,
        "inflight_io": 0,
    }


@pytest.mark.parametrize(
    ("devices", "smart_snapshots", "sensors_snapshots", "expected_commands"),
    [
        ((), True, True, ("lsblk", "lspci", "journalctl", "sensors")),
        (
            (Path("/dev/sda"),),
            True,
            False,
            ("lsblk", "lspci", "journalctl", "smartctl", "nvme"),
        ),
        (
            (Path("/dev/sda"),),
            False,
            True,
            ("lsblk", "lspci", "journalctl", "sensors"),
        ),
        ((Path("/dev/sda"),), False, False, ("lsblk", "lspci", "journalctl")),
        (
            (Path("/dev/sda"),),
            True,
            True,
            ("lsblk", "lspci", "journalctl", "smartctl", "nvme", "sensors"),
        ),
    ],
)
def test_monitor_required_commands(
    devices: tuple[Path, ...],
    smart_snapshots: bool,
    sensors_snapshots: bool,
    expected_commands: tuple[str, ...],
) -> None:
    assert (
        monitor_required_commands(devices, smart_snapshots, sensors_snapshots)
        == expected_commands
    )


def test_discover_monitor_devices_uses_physical_sys_block_devices_by_default() -> None:
    with tempfile.TemporaryDirectory() as directory_text:
        system_block_path = Path(directory_text)
        for device_name in ("sda", "nvme0n1", "loop0", "ram0", "zram0", "dm-0"):
            (system_block_path / device_name).mkdir()
        assert discover_monitor_devices((), system_block_path) == (
            Path("/dev/nvme0n1"),
            Path("/dev/sda"),
        )


@pytest.mark.parametrize(
    ("device_name", "expected"),
    [("sda", True), ("nvme0n1", True), ("loop0", False), ("dm-0", False)],
)
def test_monitor_device_name_filters_virtual_devices(
    device_name: str, expected: bool
) -> None:
    assert monitor_device_name(device_name) is expected


def test_smartctl_command_inserts_device_type_before_arguments() -> None:
    assert smartctl_command(Path("/dev/sda"), "sat", "-H") == [
        "smartctl",
        "-d",
        "sat",
        "-H",
        "/dev/sda",
    ]


def test_smartctl_command_without_device_type() -> None:
    assert smartctl_command(Path("/dev/sda"), None, "-x", "-j") == [
        "smartctl",
        "-x",
        "-j",
        "/dev/sda",
    ]


@pytest.mark.parametrize(("return_code", "expected_failure_count"), [(0, 0), (8, 1)])
def test_smart_health_check_counts_smartctl_failure(
    monkeypatch: pytest.MonkeyPatch, return_code: int, expected_failure_count: int
) -> None:
    commands: list[tuple[str, ...]] = []

    def stream(
        runner: CommandRunner,
        name: str,
        command: Sequence[str],
        stdout_path: Path | None = None,
        stderr_path: Path | None = None,
    ) -> CommandResult:
        _ = (runner, name, stdout_path, stderr_path)
        commands.append(tuple(command))
        return CommandResult(tuple(command), return_code, "", "", "start", "end", 0.0)

    monkeypatch.setattr(CommandRunner, "stream", stream)
    assert (
        smart_health_check(
            CommandRunner(verbose=False), Path("/tmp"), Path("/dev/sda"), "sat"
        )
        == expected_failure_count
    )
    assert commands == [("smartctl", "-d", "sat", "-H", "/dev/sda")]


@pytest.mark.parametrize(
    ("kind", "hdd_method"),
    [("auto", "badblocks"), ("hdd", "fio"), ("ssd", "badblocks"), ("nvme", "fio")],
)
def test_validate_disk_burnin_modes_accepts_valid_modes(
    kind: str, hdd_method: str
) -> None:
    validate_disk_burnin_modes(kind, hdd_method)


@pytest.mark.parametrize(
    ("kind", "hdd_method", "message"),
    [
        ("hddx", "badblocks", "--kind must be one of auto, hdd, ssd, or nvme"),
        ("hdd", "badblock", "--hdd-method must be one of badblocks or fio"),
    ],
)
def test_validate_disk_burnin_modes_rejects_invalid_modes(
    kind: str, hdd_method: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_disk_burnin_modes(kind, hdd_method)


def test_network_kernel_scan_paths_exclude_historical_dmesg() -> None:
    assert network_kernel_scan_paths(Path("/tmp/run")) == (
        Path("/tmp/run") / "after" / "kernel_journal_since_start.log",
    )


def test_filesystem_kernel_scan_paths_exclude_historical_dmesg() -> None:
    assert filesystem_kernel_scan_paths(Path("/tmp/run")) == (
        Path("/tmp/run") / "after" / "kernel_journal_since_start.log",
    )


def test_validate_network_interface_accepts_existing_interface() -> None:
    with tempfile.TemporaryDirectory() as directory_text:
        (Path(directory_text) / "enp1s0").mkdir()
        validate_network_interface("enp1s0", Path(directory_text))


def test_validate_network_interface_rejects_missing_interface() -> None:
    with (
        tempfile.TemporaryDirectory() as directory_text,
        pytest.raises(ValueError, match="network interface does not exist: enp1s0"),
    ):
        validate_network_interface("enp1s0", Path(directory_text))
