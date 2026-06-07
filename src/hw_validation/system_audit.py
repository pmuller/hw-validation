from __future__ import annotations

import socket
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast

from hw_validation.files import read_text, write_json, write_text
from hw_validation.json_types import JsonObject, JsonValue
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
from hw_validation.status import ExitCode, ResultStatus, ValidationOutcome
from hw_validation.timeutil import elapsed_seconds, utc_now, utc_stamp
from hw_validation.timing import write_timing_summary
from hw_validation.tooling import command_path

type AuditCommand = tuple[str, tuple[str, ...]]
type CommandResolver = Callable[[str], str | None]

LSBLK_COLUMNS = "NAME,KNAME,PATH,TYPE,SIZE,MODEL,SERIAL,WWN,VENDOR,TRAN,ROTA,RM,RO,MOUNTPOINTS,FSTYPE,LABEL,UUID,PARTUUID,PKNAME"


def run_system_audit(out_root: Path, label: str) -> int:
    started_monotonic = time.monotonic()
    run_directory = out_root / "system-audit" / f"{utc_stamp()}_{slug(label)}"
    ensure_directory(run_directory)
    plan = RunPlan(
        command="system audit",
        duration_mode=DurationMode.fast,
        phases=(RunPhase("inventory", "Capture host and hardware inventory."),),
    )
    write_run_plan(run_directory, plan)
    print_run_plan(plan)
    runner = CommandRunner(run_directory)
    started_at = utc_now()
    hostname = (
        runner.capture("hostname", ["hostname"]).stdout.strip() or socket.gethostname()
    )
    audit_commands = system_audit_commands()
    missing_commands = missing_audit_commands(audit_commands)
    skipped_commands = skipped_audit_commands(audit_commands)
    for name, command in available_audit_commands(audit_commands):
        runner.record(name, command)
    write_json(
        run_directory / "missing_commands.json",
        {
            "missing_commands": [
                cast(JsonValue, command) for command in missing_commands
            ],
            "skipped_commands": [cast(JsonValue, item) for item in skipped_commands],
        },
    )
    write_json(run_directory / "hwmon_readings.json", collect_hwmon_readings())
    write_json(
        run_directory / "block_queue_settings.json", collect_block_queue_settings()
    )
    write_json(run_directory / "absent_features.json", collect_absent_features())
    record_file(run_directory, "os_release", Path("/etc/os-release"))
    record_file(run_directory, "kernel_cmdline", Path("/proc/cmdline"))
    record_file(run_directory, "cpuinfo", Path("/proc/cpuinfo"))
    record_file(run_directory, "meminfo", Path("/proc/meminfo"))
    outcome = ValidationOutcome(
        ResultStatus.warn if missing_commands else ResultStatus.pass_status,
        ExitCode.warning.code if missing_commands else ExitCode.pass_status.code,
        warnings=len(missing_commands),
    )
    write_result(
        run_directory / "result.json",
        outcome,
        label,
        run_directory,
        elapsed_seconds(started_monotonic),
        {
            "started_at": started_at,
            "duration_mode": DurationMode.fast.value,
            "hostname": hostname,
            "missing_commands": [
                cast(JsonValue, command) for command in missing_commands
            ],
        },
    )
    write_timing_summary(run_directory)
    return outcome.exit_code


def system_audit_commands() -> tuple[AuditCommand, ...]:
    return (
        ("date_utc", ("date", "-u", "+%Y-%m-%dT%H:%M:%SZ")),
        ("uname", ("uname", "-a")),
        ("lscpu_text", ("lscpu",)),
        ("lscpu_json", ("lscpu", "-J")),
        ("free_bytes", ("free", "-b")),
        ("free_human", ("free", "-h")),
        ("numactl_hardware", ("numactl", "--hardware")),
        ("dmidecode_all", ("dmidecode",)),
        ("dmidecode_bios", ("dmidecode", "-t", "bios")),
        ("dmidecode_baseboard", ("dmidecode", "-t", "baseboard")),
        ("dmidecode_chassis", ("dmidecode", "-t", "chassis")),
        ("dmidecode_memory", ("dmidecode", "-t", "memory")),
        ("edac_status", ("edac-util", "--status")),
        ("edac_verbose", ("edac-util", "--verbose")),
        ("ras_summary", ("ras-mc-ctl", "--summary")),
        ("ras_errors", ("ras-mc-ctl", "--errors")),
        (
            "journal_warnings",
            (
                "journalctl",
                "-k",
                "-p",
                "warning..alert",
                "--no-pager",
                "-o",
                "short-iso-precise",
            ),
        ),
        ("dmesg_warnings", ("dmesg", "--level=err,warn,crit,alert,emerg")),
        ("lspci_nn", ("lspci", "-nn")),
        ("lspci_verbose", ("lspci", "-vv")),
        ("ip_link", ("ip", "-details", "link")),
        ("ip_addr", ("ip", "addr")),
        ("sensors_text", ("sensors",)),
        ("sensors_json", ("sensors", "-j")),
        ("lsblk_json", ("lsblk", "-J", "-b", "-o", LSBLK_COLUMNS)),
        ("lsblk_text", ("lsblk", "-o", LSBLK_COLUMNS)),
        ("lsscsi", ("lsscsi", "-g")),
        ("nvme_list_json", ("nvme", "list", "-o", "json")),
        ("nvme_list_text", ("nvme", "list")),
        ("lsusb", ("lsusb",)),
        ("lsmod", ("lsmod",)),
        ("findmnt", ("findmnt",)),
    )


def available_audit_commands(
    commands: tuple[AuditCommand, ...], resolver: CommandResolver = command_path
) -> tuple[AuditCommand, ...]:
    return tuple(command for command in commands if resolver(command[1][0]) is not None)


def missing_audit_commands(
    commands: tuple[AuditCommand, ...], resolver: CommandResolver = command_path
) -> tuple[str, ...]:
    missing: list[str] = []
    for _name, command in commands:
        executable = command[0]
        if resolver(executable) is None and executable not in missing:
            missing.append(executable)
    return tuple(missing)


def skipped_audit_commands(
    commands: tuple[AuditCommand, ...], resolver: CommandResolver = command_path
) -> list[JsonObject]:
    return [
        {
            "name": name,
            "executable": command[0],
            "command": [cast(JsonValue, command_part) for command_part in command],
            "reason": "missing command",
        }
        for name, command in commands
        if resolver(command[0]) is None
    ]


def record_file(run_directory: Path, name: str, source_path: Path) -> None:
    if source_path.exists():
        write_text(run_directory / f"file_{name}.txt", read_text(source_path) or "")
    else:
        write_text(run_directory / f"file_{name}.txt", "ABSENT\n")


def collect_hwmon_readings() -> JsonObject:
    readings: JsonObject = {}
    for hardware_monitor_path in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
        monitor_data: JsonObject = {}
        for reading_path in sorted(hardware_monitor_path.glob("*")):
            if reading_path.is_file():
                reading = read_text(reading_path)
                if reading is not None:
                    monitor_data[reading_path.name] = reading
        readings[hardware_monitor_path.name] = monitor_data
    return readings or {"state": "absent", "reason": "no sensors exposed"}


def collect_block_queue_settings() -> JsonObject:
    settings: JsonObject = {}
    for block_device_path in sorted(Path("/sys/block").glob("*")):
        queue_path = block_device_path / "queue"
        if not queue_path.exists():
            continue
        device_settings: JsonObject = {}
        for setting_path in sorted(queue_path.glob("*")):
            reading = read_text(setting_path)
            if reading is not None:
                device_settings[setting_path.name] = reading
        settings[block_device_path.name] = device_settings
    return settings


def collect_absent_features() -> list[JsonValue]:
    absent_features: list[JsonValue] = []
    if not any(Path("/sys/devices/system/edac").glob("mc/mc*")):
        absent_features.append({"feature": "edac", "reason": "no EDAC driver exposed"})
    if not any(Path("/sys/class/nvme").glob("nvme*")):
        absent_features.append({"feature": "nvme", "reason": "no NVMe controller"})
    if not any(
        (network_path / "device").exists()
        for network_path in Path("/sys/class/net").glob("*")
        if network_path.name != "lo"
    ):
        absent_features.append(
            {"feature": "network", "reason": "no physical NIC found"}
        )
    if not any(
        Path(device_path).exists()
        for device_path in ("/dev/ipmi0", "/dev/ipmi/0", "/dev/ipmidev/0")
    ):
        absent_features.append({"feature": "ipmi", "reason": "no IPMI device"})
    return absent_features
