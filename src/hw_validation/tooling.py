from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

from hw_validation.runner import CommandRunner

PACKAGE_SET = (
    "bash",
    "coreutils",
    "findutils",
    "grep",
    "sed",
    "gawk",
    "python3",
    "smartmontools",
    "nvme-cli",
    "fio",
    "e2fsprogs",
    "util-linux",
    "udev",
    "tmux",
    "jq",
    "sysstat",
    "pciutils",
    "usbutils",
    "lsscsi",
    "lm-sensors",
    "hdparm",
    "sg3-utils",
    "dmidecode",
    "iproute2",
    "ethtool",
    "stress-ng",
    "stressapptest",
    "memtester",
    "iperf3",
    "rasdaemon",
    "edac-utils",
    "ipmitool",
    "numactl",
    "memtest86+",
    "procps",
    "kmod",
)

REQUIRED_COMMANDS = (
    "bash",
    "python3",
    "smartctl",
    "nvme",
    "fio",
    "badblocks",
    "lsblk",
    "blockdev",
    "findmnt",
    "flock",
    "jq",
    "udevadm",
    "tmux",
    "awk",
    "sed",
    "grep",
    "iostat",
    "lspci",
    "lsusb",
    "lsscsi",
    "sensors",
    "hdparm",
    "sg_vpd",
    "dmidecode",
    "ip",
    "ethtool",
    "stress-ng",
    "stressapptest",
    "memtester",
    "iperf3",
    "ras-mc-ctl",
    "edac-util",
    "ipmitool",
    "numactl",
    "dmesg",
    "journalctl",
    "free",
    "lscpu",
    "vmstat",
    "lsmod",
    "hostname",
    "date",
    "uname",
    "readlink",
    "cat",
    "rm",
    "mkdir",
    "sync",
    "wipefs",
)


@dataclass(frozen=True, slots=True)
class ToolCheck:
    missing_commands: tuple[str, ...]
    fio_path: str | None
    fio_version: str | None

    @property
    def ok(self) -> bool:
        return not self.missing_commands and self.fio_version is not None


def command_path(command_name: str) -> str | None:
    return shutil.which(command_name)


def missing_command_names(command_names: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        command_name
        for command_name in command_names
        if command_path(command_name) is None
    )


def require_commands(command_names: tuple[str, ...]) -> None:
    missing_commands = missing_command_names(command_names)
    if missing_commands:
        raise RuntimeError(
            "Required commands are missing after setup: " + ", ".join(missing_commands)
        )


def check_fio() -> tuple[str | None, str | None]:
    fio_path = command_path("fio")
    if fio_path is None:
        return None, None
    try:
        completed_process = subprocess.run(
            [fio_path, "--version"],
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            errors="replace",
            check=False,
        )
    except OSError:
        return fio_path, None
    fio_version = (completed_process.stdout or completed_process.stderr).splitlines()[0]
    if not fio_version.startswith("fio-"):
        return fio_path, None
    return fio_path, fio_version


def check_tools(command_names: tuple[str, ...] = REQUIRED_COMMANDS) -> ToolCheck:
    missing_commands = tuple(
        command_name
        for command_name in command_names
        if command_path(command_name) is None
    )
    fio_path, fio_version = check_fio()
    return ToolCheck(missing_commands, fio_path, fio_version)


def install_debian_tools(no_update: bool, dry_run: bool) -> None:
    runner = CommandRunner(dry_run=dry_run)
    if command_path("apt-get") is None:
        raise RuntimeError(
            "apt-get not found. This setup command is for Debian systems."
        )
    if not no_update:
        _ = runner.capture("apt_update", ["apt-get", "update"], check=True)
    _ = runner.capture("apt_install", apt_install_command(), check=True)


def apt_install_command(packages: tuple[str, ...] = PACKAGE_SET) -> tuple[str, ...]:
    return (
        "apt-get",
        "-y",
        "install",
        "--no-install-recommends",
        *packages,
    )
