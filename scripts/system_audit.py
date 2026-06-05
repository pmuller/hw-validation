#!/usr/bin/env python3
"""Collect a non-destructive Linux hardware validation audit."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn, cast, override

type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)
type JsonObject = dict[str, JsonValue]

EXIT_PASS = 0
EXIT_USAGE = 64
EXIT_TOOLING = 70
LSBLK_COLUMNS = "NAME,KNAME,PATH,TYPE,SIZE,MODEL,SERIAL,WWN,VENDOR,TRAN,ROTA,RM,RO,MOUNTPOINTS,FSTYPE,LABEL,UUID,PARTUUID,PKNAME"
SLUG_INVALID_CHARACTER_PATTERN = re.compile(r"[^A-Za-z0-9._=-]+")
SLUG_UNDERSCORE_PATTERN = re.compile(r"_+")
PCIE_LINK_PATTERN = re.compile(r"\b(LnkCap|LnkSta|LnkCtl2|LnkSta2):")
STORAGE_PCI_PATTERN = re.compile(
    r"\b(storage|sata|sas|scsi|raid|nvme|non-volatile memory)\b", re.IGNORECASE
)
REQUIRED_COMMANDS = (
    "hostname",
    "date",
    "uname",
    "lscpu",
    "free",
    "numactl",
    "dmidecode",
    "edac-util",
    "ras-mc-ctl",
    "journalctl",
    "dmesg",
    "lspci",
    "ip",
    "ethtool",
    "sensors",
    "lsblk",
    "lsscsi",
    "nvme",
    "lsusb",
    "lsmod",
    "findmnt",
)


class ToolingError(RuntimeError):
    """Raised when setup did not provide a required command."""


class ValidationArgumentParser(argparse.ArgumentParser):
    @override
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


@dataclass(frozen=True, slots=True)
class AuditSettings:
    out_root: Path
    label: str


@dataclass(slots=True)
class CommandRecorder:
    run_directory: Path
    command_sequence: int = 0
    commands_path: Path = field(init=False)
    log_path: Path = field(init=False)

    def __post_init__(self) -> None:
        self.commands_path = self.run_directory / "commands.jsonl"
        self.log_path = self.run_directory / "system_audit.log"

    def log(self, message: str) -> None:
        line = f"{utc_now()} [INFO] {message}"
        print(line, flush=True)
        with self.log_path.open("a", encoding="utf-8") as log_file:
            print(line, file=log_file)

    def capture(
        self, name: str, command: list[str]
    ) -> subprocess.CompletedProcess[str]:
        self.command_sequence += 1
        output_prefix = self.run_directory / f"{self.command_sequence:03d}_{slug(name)}"
        stdout_path = output_prefix.with_suffix(".stdout")
        stderr_path = output_prefix.with_suffix(".stderr")
        metadata_path = output_prefix.with_suffix(".meta.json")
        started_at = utc_now()
        self.log(f"RUN {name}: {shlex_join(command)}")
        try:
            completed_process = subprocess.run(
                command,
                text=True,
                capture_output=True,
                errors="replace",
                check=False,
            )
        except FileNotFoundError as error:
            raise ToolingError(f"Required command is missing: {command[0]}") from error
        except OSError as error:
            raise ToolingError(
                f"Could not run command {shlex_join(command)}: {error}"
            ) from error

        write_text(stdout_path, completed_process.stdout)
        write_text(stderr_path, completed_process.stderr)
        metadata: JsonObject = {
            "name": name,
            "command": json_strings(command),
            "start_utc": started_at,
            "end_utc": utc_now(),
            "returncode": completed_process.returncode,
            "stdout_path": stdout_path.name,
            "stderr_path": stderr_path.name,
        }
        write_json(metadata_path, metadata)
        append_json_line(self.commands_path, metadata)
        self.log(f"DONE {name}: returncode={completed_process.returncode}")
        return completed_process

    def record(self, name: str, command: list[str]) -> None:
        completed_process = self.capture(name, command)
        del completed_process

    def record_file(self, name: str, source_path: Path) -> JsonObject:
        destination_path = self.run_directory / f"file_{slug(name)}.txt"
        if not source_path.exists():
            write_text(destination_path, "ABSENT\n")
            return {"name": name, "path": str(source_path), "state": "absent"}
        try:
            write_text(
                destination_path,
                source_path.read_text(encoding="utf-8", errors="replace"),
            )
        except OSError as error:
            write_text(destination_path, f"ERROR: {error}\n")
            return {
                "name": name,
                "path": str(source_path),
                "state": "error",
                "error": str(error),
            }
        return {
            "name": name,
            "path": str(source_path),
            "state": "captured",
            "output": destination_path.name,
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def slug(value: str, maximum_length: int = 120) -> str:
    normalized = SLUG_INVALID_CHARACTER_PATTERN.sub("_", value.strip())
    normalized = SLUG_UNDERSCORE_PATTERN.sub("_", normalized).strip("_")
    return (normalized or "unknown")[:maximum_length]


def shlex_join(command: Sequence[str]) -> str:
    return " ".join(shlex_quote(command_argument) for command_argument in command)


def shlex_quote(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", errors="replace") as text_file:
        print(text, file=text_file, end="")


def write_json(path: Path, payload: JsonValue) -> None:
    write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def append_json_line(path: Path, payload: JsonObject) -> None:
    with path.open("a", encoding="utf-8") as jsonl_file:
        print(json.dumps(payload, sort_keys=True), file=jsonl_file)


def json_strings(values: Iterable[str]) -> list[JsonValue]:
    return [value for value in values]


def require_argument_action(action: argparse.Action) -> None:
    if not action.dest:
        raise RuntimeError("argparse returned an action without a destination")


def require_absolute_path(path_text: str, argument_name: str) -> Path:
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{argument_name} must be an absolute path")
    return path.resolve(strict=False)


def require_root() -> None:
    if os.geteuid() != 0:
        print("This script must be run as root.", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def verify_required_commands() -> JsonObject:
    command_paths: JsonObject = {}
    missing_commands: list[str] = []
    for command_name in REQUIRED_COMMANDS:
        command_path = shutil.which(command_name)
        if command_path is None:
            missing_commands.append(command_name)
        else:
            command_paths[command_name] = command_path
    if missing_commands:
        raise ToolingError(
            "Required commands are missing after setup: " + ", ".join(missing_commands)
        )
    return command_paths


def physical_network_interfaces() -> list[str]:
    interfaces: list[str] = []
    for interface_path in sorted(Path("/sys/class/net").glob("*")):
        if interface_path.name == "lo":
            continue
        if (interface_path / "device").exists():
            interfaces.append(interface_path.name)
    return interfaces


def read_sysfs_text(path: Path) -> str | None:
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    return None


def collect_hwmon_readings() -> JsonObject:
    hwmon_root = Path("/sys/class/hwmon")
    readings: JsonObject = {}
    if not hwmon_root.exists():
        return {"state": "absent", "reason": "no sensors exposed"}
    for hwmon_path in sorted(hwmon_root.glob("hwmon*")):
        hwmon_data: JsonObject = {}
        for reading_path in sorted(hwmon_path.glob("*")):
            if reading_path.is_dir():
                continue
            reading = read_sysfs_text(reading_path)
            if reading is not None:
                hwmon_data[reading_path.name] = reading
        readings[hwmon_path.name] = hwmon_data
    if not readings:
        return {"state": "absent", "reason": "no sensors exposed"}
    return readings


def collect_block_queue_settings() -> JsonObject:
    block_root = Path("/sys/block")
    queue_settings: JsonObject = {}
    if not block_root.exists():
        return {"state": "absent", "reason": "no block queue sysfs tree"}
    for block_device_path in sorted(block_root.glob("*")):
        queue_path = block_device_path / "queue"
        if not queue_path.exists():
            continue
        device_settings: JsonObject = {}
        for setting_path in sorted(queue_path.glob("*")):
            reading = read_sysfs_text(setting_path)
            if reading is not None:
                device_settings[setting_path.name] = reading
        queue_settings[block_device_path.name] = device_settings
    return queue_settings


def collect_edac_state() -> JsonObject:
    edac_root = Path("/sys/devices/system/edac")
    if not edac_root.exists() or not any(edac_root.glob("mc/mc*")):
        return {"state": "absent", "reason": "no EDAC driver exposed"}
    edac_state: JsonObject = {}
    for counter_path in sorted(edac_root.glob("**/*count")):
        reading = read_sysfs_text(counter_path)
        if reading is not None:
            edac_state[str(counter_path)] = reading
    return {"state": "present", "counters": edac_state}


def collect_ipmi_state() -> JsonObject:
    for device_path in (
        Path("/dev/ipmi0"),
        Path("/dev/ipmi/0"),
        Path("/dev/ipmidev/0"),
    ):
        if device_path.exists():
            return {"state": "present", "device": str(device_path)}
    return {"state": "absent", "reason": "no IPMI device"}


def collect_nvme_state() -> JsonObject:
    if Path("/sys/class/nvme").exists() and any(Path("/sys/class/nvme").glob("nvme*")):
        return {"state": "present"}
    return {"state": "absent", "reason": "no NVMe controller"}


def collect_pcie_link_summary(lspci_verbose_output: str) -> list[JsonValue]:
    summary: list[JsonValue] = []
    current_device = ""
    for line in lspci_verbose_output.splitlines():
        if line and not line.startswith((" ", "\t")):
            current_device = line
            continue
        if PCIE_LINK_PATTERN.search(line):
            summary.append({"device": current_device, "link": line.strip()})
    return summary


def collect_storage_lspci(lspci_output: str) -> str:
    return (
        "\n".join(
            line
            for line in lspci_output.splitlines()
            if STORAGE_PCI_PATTERN.search(line)
        )
        + "\n"
    )


def capture_network_details(recorder: CommandRecorder, interfaces: list[str]) -> None:
    for interface_name in interfaces:
        recorder.record(f"ethtool_{interface_name}", ["ethtool", interface_name])
        recorder.record(
            f"ethtool_driver_{interface_name}", ["ethtool", "-i", interface_name]
        )
        recorder.record(
            f"ethtool_stats_{interface_name}", ["ethtool", "-S", interface_name]
        )


def capture_audit(settings: AuditSettings) -> int:
    audit_directory = (
        settings.out_root / "system-audit" / f"{run_id()}_{slug(settings.label)}"
    )
    audit_directory.mkdir(parents=True, exist_ok=False)
    recorder = CommandRecorder(audit_directory)
    failures = 0
    warnings = 0
    started_at = utc_now()
    result_path = audit_directory / "result.json"

    try:
        command_paths = verify_required_commands()
        recorder.log(f"audit_directory={audit_directory}")
        recorder.log(f"label={settings.label}")
        hostname_process = recorder.capture("hostname", ["hostname"])
        recorder.record("date_utc", ["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"])
        recorder.record("uname", ["uname", "-a"])
        recorder.record("lscpu_text", ["lscpu"])
        recorder.record("lscpu_json", ["lscpu", "-J"])
        recorder.record("free_bytes", ["free", "-b"])
        recorder.record("free_human", ["free", "-h"])
        recorder.record("numactl_hardware", ["numactl", "--hardware"])
        recorder.record("dmidecode_all", ["dmidecode"])
        recorder.record("dmidecode_bios", ["dmidecode", "-t", "bios"])
        recorder.record("dmidecode_baseboard", ["dmidecode", "-t", "baseboard"])
        recorder.record("dmidecode_chassis", ["dmidecode", "-t", "chassis"])
        recorder.record("dmidecode_memory", ["dmidecode", "-t", "memory"])
        recorder.record("edac_util_status", ["edac-util", "--status"])
        recorder.record("edac_util_verbose", ["edac-util", "--verbose"])
        recorder.record("ras_mc_ctl_summary", ["ras-mc-ctl", "--summary"])
        recorder.record("ras_mc_ctl_errors", ["ras-mc-ctl", "--errors"])
        recorder.record(
            "journalctl_kernel_warnings",
            [
                "journalctl",
                "-k",
                "-p",
                "warning..alert",
                "--no-pager",
                "-o",
                "short-iso-precise",
            ],
        )
        recorder.record(
            "dmesg_warnings", ["dmesg", "--level=err,warn,crit,alert,emerg"]
        )
        lspci_process = recorder.capture("lspci_nn", ["lspci", "-nn"])
        lspci_verbose_process = recorder.capture("lspci_verbose", ["lspci", "-vv"])
        recorder.record("ip_link", ["ip", "-details", "link"])
        recorder.record("ip_addr", ["ip", "addr"])
        recorder.record("sensors_text", ["sensors"])
        recorder.record("sensors_json", ["sensors", "-j"])
        recorder.record("lsblk_json", ["lsblk", "-J", "-b", "-o", LSBLK_COLUMNS])
        recorder.record("lsblk_text", ["lsblk", "-o", LSBLK_COLUMNS])
        recorder.record("lsscsi", ["lsscsi", "-g"])
        recorder.record("nvme_list_json", ["nvme", "list", "-o", "json"])
        recorder.record("nvme_list_text", ["nvme", "list"])
        recorder.record("lsusb", ["lsusb"])
        recorder.record("lsmod", ["lsmod"])
        recorder.record("findmnt", ["findmnt"])

        interfaces = physical_network_interfaces()
        capture_network_details(recorder, interfaces)
        write_text(
            audit_directory / "storage_lspci.txt",
            collect_storage_lspci(lspci_process.stdout),
        )
        write_json(
            audit_directory / "pcie_link_summary.json",
            collect_pcie_link_summary(lspci_verbose_process.stdout),
        )
        write_json(audit_directory / "hwmon_readings.json", collect_hwmon_readings())
        write_json(
            audit_directory / "block_queue_settings.json",
            collect_block_queue_settings(),
        )
        captured_files = [
            recorder.record_file("os_release", Path("/etc/os-release")),
            recorder.record_file("kernel_cmdline", Path("/proc/cmdline")),
            recorder.record_file("cpuinfo", Path("/proc/cpuinfo")),
            recorder.record_file("meminfo", Path("/proc/meminfo")),
        ]
        absent_features = [
            feature
            for feature in (
                collect_edac_state(),
                collect_ipmi_state(),
                collect_nvme_state(),
                {"state": "absent", "reason": "no physical NIC found"}
                if not interfaces
                else {
                    "state": "present",
                    "feature": "physical network interfaces",
                    "interfaces": json_strings(interfaces),
                },
            )
            if feature.get("state") == "absent"
        ]
        summary: JsonObject = {
            "status": "PASS",
            "result": "PASS",
            "exit_code": EXIT_PASS,
            "failures": failures,
            "warnings": warnings,
            "started_at": started_at,
            "ended_at": utc_now(),
            "label": settings.label,
            "audit_directory": str(audit_directory),
            "hostname": hostname_process.stdout.strip() or socket.gethostname(),
            "command_paths": command_paths,
            "captured_files": cast(JsonValue, captured_files),
            "absent_features": cast(JsonValue, absent_features),
        }
        write_json(result_path, summary)
        recorder.log(f"RESULT=PASS result_path={result_path}")
        return EXIT_PASS
    except ToolingError as error:
        failures = 1
        write_json(
            result_path,
            {
                "status": "FAIL",
                "result": "FAIL",
                "exit_code": EXIT_TOOLING,
                "failures": failures,
                "warnings": warnings,
                "started_at": started_at,
                "ended_at": utc_now(),
                "label": settings.label,
                "audit_directory": str(audit_directory),
                "error": str(error),
            },
        )
        print(str(error), file=sys.stderr)
        return EXIT_TOOLING


def parse_arguments(arguments: Sequence[str]) -> AuditSettings:
    parser = ValidationArgumentParser(
        description="Collect a Linux hardware validation audit."
    )
    require_argument_action(
        parser.add_argument(
            "--out-root", required=True, help="Required absolute output root."
        )
    )
    require_argument_action(
        parser.add_argument("--label", default="system-audit", help="Run label.")
    )
    namespace = parser.parse_args(arguments)
    try:
        out_root = require_absolute_path(cast(str, namespace.out_root), "--out-root")
    except ValueError as error:
        parser.error(str(error))
    return AuditSettings(out_root=out_root, label=cast(str, namespace.label))


def main(arguments: Sequence[str] | None = None) -> int:
    settings = parse_arguments(sys.argv[1:] if arguments is None else arguments)
    require_root()
    return capture_audit(settings)


if __name__ == "__main__":
    raise SystemExit(main())
