#!/usr/bin/env python3
"""
Global system monitor for disk validation.

Logs host state, kernel storage warnings, per-block-device I/O counters,
thermal data, memory/load/pressure, and periodic SMART/NVMe health snapshots.
Run once while per-device validation jobs run in parallel.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType
from typing import NoReturn, cast, override

type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)
type JsonObject = dict[str, JsonValue]

EXIT_PASS = 0
EXIT_USAGE = 64
EXIT_TOOLING = 70

LSBLK_COLUMNS = "NAME,KNAME,PATH,TYPE,SIZE,MODEL,SERIAL,WWN,VENDOR,TRAN,ROTA,RM,RO,MOUNTPOINT,FSTYPE,LABEL,UUID,PARTUUID,PKNAME"
CPU_STAT_FIELDS = (
    "user",
    "nice",
    "system",
    "idle",
    "iowait",
    "irq",
    "softirq",
    "steal",
    "guest",
    "guest_nice",
)
BLOCK_STAT_FIELDS = (
    "read_ios",
    "read_merges",
    "read_sectors",
    "read_ticks_ms",
    "write_ios",
    "write_merges",
    "write_sectors",
    "write_ticks_ms",
    "in_flight",
    "io_ticks_ms",
    "time_in_queue_ms",
    "discard_ios",
    "discard_merges",
    "discard_sectors",
    "discard_ticks_ms",
    "flush_ios",
    "flush_ticks_ms",
)
MEMORY_SAMPLE_FIELDS = {
    "MemTotal",
    "MemFree",
    "MemAvailable",
    "Buffers",
    "Cached",
    "SwapTotal",
    "SwapFree",
    "Dirty",
    "Writeback",
}
SLUG_INVALID_CHARACTER_PATTERN = re.compile(r"[^A-Za-z0-9._=-]+")
SLUG_UNDERSCORE_PATTERN = re.compile(r"_+")
TEMPERATURE_INPUT_PATTERN = re.compile(r"temp(?P<sensor_number>\d+)_input")
REQUIRED_COMMANDS = (
    "lsblk",
    "lspci",
    "journalctl",
    "smartctl",
    "nvme",
    "sensors",
    "date",
    "uname",
)


@dataclass(frozen=True, slots=True)
class MonitorSettings:
    devices: tuple[Path, ...]
    out_root: Path
    label: str
    interval: float
    smart_interval: float
    duration_minutes: float
    no_kernel_follow: bool
    sensors_interval: float

    def json_arguments(self) -> JsonObject:
        return {
            "devices": json_strings(str(device_path) for device_path in self.devices),
            "out_root": str(self.out_root),
            "label": self.label,
            "interval": self.interval,
            "smart_interval": self.smart_interval,
            "duration_minutes": self.duration_minutes,
            "no_kernel_follow": self.no_kernel_follow,
            "sensors_interval": self.sensors_interval,
        }


class MaximumLevelFilter(logging.Filter):
    maximum_level: int

    def __init__(self, maximum_level: int) -> None:
        super().__init__()
        self.maximum_level = maximum_level

    @override
    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno <= self.maximum_level


class ValidationArgumentParser(argparse.ArgumentParser):
    @override
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


class UtcFormatter(logging.Formatter):
    @override
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802
        del datefmt
        return datetime.fromtimestamp(record.created, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )


@dataclass(slots=True)
class CommandCapture:
    root: Path
    logger: logging.Logger
    sequence: int = 0
    commands_jsonl: Path = field(init=False)

    def __post_init__(self) -> None:
        self.commands_jsonl = self.root / "commands.jsonl"

    def capture(
        self,
        name: str,
        command: list[str],
        timeout: float | None = 60,
        quiet: bool = False,
    ) -> None:
        self.sequence += 1
        prefix = self.root / f"{self.sequence:05d}_{slug(name)}"
        stdout_path = prefix.with_suffix(".stdout")
        stderr_path = prefix.with_suffix(".stderr")
        metadata_path = prefix.with_suffix(".meta.json")
        started_at = utc_now()
        command_text = shlex.join(command)
        if not quiet:
            self.logger.info("RUN %s: %s", name, command_text)

        try:
            completed_process = subprocess.run(
                command,
                text=True,
                capture_output=True,
                timeout=timeout,
                errors="replace",
            )
        except (OSError, subprocess.SubprocessError) as error:
            completed_process = subprocess.CompletedProcess(
                command, 127, "", repr(error)
            )

        write_text(stdout_path, completed_process.stdout or "")
        write_text(stderr_path, completed_process.stderr or "")
        metadata: JsonObject = {
            "name": name,
            "cmd": json_strings(command),
            "cmd_text": command_text,
            "start_utc": started_at,
            "end_utc": utc_now(),
            "returncode": completed_process.returncode,
            "stdout_path": stdout_path.name,
            "stderr_path": stderr_path.name,
        }
        write_json(metadata_path, metadata)
        append_json_line(self.commands_jsonl, metadata)
        if completed_process.returncode != 0 and not quiet:
            self.logger.warning("%s returned rc=%s", name, completed_process.returncode)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def slug(value: str, maximum_length: int = 120) -> str:
    normalized = SLUG_INVALID_CHARACTER_PATTERN.sub("_", value.strip())
    normalized = SLUG_UNDERSCORE_PATTERN.sub("_", normalized).strip("_")
    return (normalized or "unknown")[:maximum_length]


def write_text(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", errors="replace") as text_file:
        print(text, file=text_file, end="")


def write_json(path: Path, payload: JsonObject) -> None:
    write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def append_json_line(path: Path, payload: JsonObject) -> None:
    with path.open("a", encoding="utf-8") as jsonl_file:
        print(json.dumps(payload, sort_keys=True), file=jsonl_file)


def require_root() -> None:
    if os.geteuid() != 0:
        print("This script must be run as root.", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def verify_required_commands() -> None:
    missing_commands = [
        command_name
        for command_name in REQUIRED_COMMANDS
        if shutil.which(command_name) is None
    ]
    if missing_commands:
        raise RuntimeError(
            "Required commands are missing after setup: " + ", ".join(missing_commands)
        )


def json_strings(values: Iterable[str]) -> list[JsonValue]:
    return [value for value in values]


def json_int_mapping(values: dict[str, int] | None) -> JsonObject | None:
    if values is None:
        return None
    return {field_name: value for field_name, value in values.items()}


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def parse_key_values(text: str) -> dict[str, int]:
    output: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            output[parts[0].removesuffix(":")] = int(parts[1])
        except ValueError:
            continue
    return output


def read_meminfo() -> dict[str, int]:
    return parse_key_values(read_text(Path("/proc/meminfo")) or "")


def read_loadavg() -> JsonObject:
    parts = (read_text(Path("/proc/loadavg")) or "").split()
    if len(parts) < 5:
        return {}
    try:
        return {
            "load1": float(parts[0]),
            "load5": float(parts[1]),
            "load15": float(parts[2]),
            "runnable_total": parts[3],
            "last_pid": int(parts[4]),
        }
    except ValueError:
        return {}


def read_uptime() -> JsonObject:
    parts = (read_text(Path("/proc/uptime")) or "").split()
    if len(parts) < 2:
        return {}
    try:
        return {"uptime_seconds": float(parts[0]), "idle_seconds": float(parts[1])}
    except ValueError:
        return {}


def read_cpu_stat() -> dict[str, int]:
    for line in (read_text(Path("/proc/stat")) or "").splitlines():
        if not line.startswith("cpu "):
            continue
        try:
            values = [int(value) for value in line.split()[1:]]
        except ValueError:
            return {}
        return {
            field_name: values[position] if position < len(values) else 0
            for position, field_name in enumerate(CPU_STAT_FIELDS)
        }
    return {}


def cpu_pct(previous: dict[str, int] | None, current: dict[str, int]) -> float | None:
    if not previous or not current:
        return None
    previous_total = sum(previous.values())
    current_total = sum(current.values())
    total_delta = current_total - previous_total
    idle_delta = (current.get("idle", 0) + current.get("iowait", 0)) - (
        previous.get("idle", 0) + previous.get("iowait", 0)
    )
    if total_delta <= 0:
        return None
    return round(100.0 * (1.0 - idle_delta / total_delta), 2)


def read_pressure() -> JsonObject:
    output: JsonObject = {}
    for pressure_name in ("cpu", "io", "memory"):
        text = read_text(Path("/proc/pressure") / pressure_name)
        if text:
            output[pressure_name] = text
    return output


def list_hwmon_temps() -> JsonObject:
    output: JsonObject = {}
    for hardware_monitor_path in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
        chip_name = (
            read_text(hardware_monitor_path / "name") or hardware_monitor_path.name
        )
        temperatures: JsonObject = {}
        for temperature_input_path in sorted(hardware_monitor_path.glob("temp*_input")):
            match = TEMPERATURE_INPUT_PATTERN.match(temperature_input_path.name)
            sensor_number = (
                match.group("sensor_number") if match else temperature_input_path.stem
            )
            raw_temperature = read_text(temperature_input_path)
            if raw_temperature is None:
                continue
            try:
                temperature_celsius = int(raw_temperature) / 1000.0
            except ValueError:
                continue
            label = (
                read_text(hardware_monitor_path / f"temp{sensor_number}_label")
                or f"temp{sensor_number}"
            )
            temperatures[label] = temperature_celsius
        if temperatures:
            output[f"{hardware_monitor_path.name}:{chip_name}"] = temperatures
    return output


def resolved_device_path(device_path: str) -> Path:
    return Path(device_path).expanduser().resolve(strict=False)


def block_name_from_device(device_path: Path) -> str:
    return device_path.name


def discover_block_names(devices: tuple[Path, ...]) -> list[str]:
    if devices:
        return sorted(
            {
                block_name_from_device(device_path)
                for device_path in devices
                if device_path.is_block_device()
            }
        )

    block_names: list[str] = []
    for block_device_path in sorted(Path("/sys/block").iterdir()):
        block_device_name = block_device_path.name
        if block_device_name.startswith(("loop", "ram", "zram", "dm-")):
            continue
        if (block_device_path / "stat").exists():
            block_names.append(block_device_name)
    return block_names


def read_block_stat(block_name: str) -> dict[str, int] | None:
    text = read_text(Path("/sys/block") / block_name / "stat")
    if not text:
        return None
    try:
        values = [int(value) for value in text.split()]
    except ValueError:
        return None
    return {
        field_name: values[position] if position < len(values) else 0
        for position, field_name in enumerate(BLOCK_STAT_FIELDS)
    }


def read_block_static(block_name: str) -> JsonObject:
    root = Path("/sys/block") / block_name
    output: JsonObject = {"name": block_name}
    try:
        output["size_bytes"] = int(read_text(root / "size") or "0") * 512
    except ValueError:
        pass

    for relative_path in (
        "queue/logical_block_size",
        "queue/physical_block_size",
        "queue/rotational",
        "device/model",
        "device/vendor",
    ):
        text = read_text(root / relative_path)
        if text is not None:
            output[relative_path.replace("/", "_")] = text
    return output


def block_delta(
    previous: dict[str, int] | None,
    current: dict[str, int] | None,
    seconds_delta: float,
) -> JsonObject:
    if not previous or not current or seconds_delta <= 0:
        return {}

    def delta(field_name: str) -> int:
        return current.get(field_name, 0) - previous.get(field_name, 0)

    return {
        "read_iops": round(delta("read_ios") / seconds_delta, 2),
        "write_iops": round(delta("write_ios") / seconds_delta, 2),
        "read_MBps": round(delta("read_sectors") * 512 / seconds_delta / 1_000_000, 3),
        "write_MBps": round(
            delta("write_sectors") * 512 / seconds_delta / 1_000_000, 3
        ),
        "discard_MBps": round(
            delta("discard_sectors") * 512 / seconds_delta / 1_000_000, 3
        ),
        "io_util_pct_approx": round(delta("io_ticks_ms") / (seconds_delta * 10.0), 2),
        "in_flight": current.get("in_flight", 0),
        "read_ticks_ms_delta": delta("read_ticks_ms"),
        "write_ticks_ms_delta": delta("write_ticks_ms"),
        "io_ticks_ms_delta": delta("io_ticks_ms"),
    }


def smart_device_snapshot(
    capture: CommandCapture,
    devices: tuple[Path, ...],
    nvme_devices: set[Path],
    label: str,
) -> None:
    snapshot_directory = capture.root / f"smart_{slug(label)}_{run_id()}"
    snapshot_directory.mkdir(parents=True, exist_ok=True)
    snapshot_capture = CommandCapture(snapshot_directory, capture.logger)
    for device_path in devices:
        device = str(device_path)
        block_name = block_name_from_device(device_path)
        snapshot_capture.capture(
            f"smartctl_{block_name}",
            ["smartctl", "-H", "-A", "-l", "error", "-l", "selftest", device],
            timeout=120,
            quiet=True,
        )
        snapshot_capture.capture(
            f"smartctl_x_json_{block_name}",
            ["smartctl", "-x", "-j", device],
            timeout=120,
            quiet=True,
        )
        if device_path in nvme_devices:
            snapshot_capture.capture(
                f"nvme_smart_log_{block_name}",
                ["nvme", "smart-log", device, "-o", "json"],
                timeout=120,
                quiet=True,
            )
            snapshot_capture.capture(
                f"nvme_error_log_{block_name}",
                ["nvme", "error-log", device, "-e", "64", "-o", "json"],
                timeout=120,
                quiet=True,
            )


def start_kernel_follower(
    root: Path,
    logger: logging.Logger,
    stop_event: threading.Event,
) -> subprocess.Popen[str] | None:
    output_path = root / "kernel-follow.log"
    command = ["journalctl", "-k", "-f", "-p", "warning", "-o", "short-iso-precise"]

    logger.info("kernel warning follower: %s", shlex.join(command))
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        errors="replace",
    )

    def read_kernel_output() -> None:
        with output_path.open(
            "a", encoding="utf-8", errors="replace"
        ) as kernel_log_file:
            process_stdout = process.stdout
            if process_stdout is None:
                return
            for line in process_stdout:
                if stop_event.is_set():
                    break
                kernel_line = line.rstrip("\n")
                print(f"{utc_now()} [kernel] {kernel_line}", file=kernel_log_file)
                kernel_log_file.flush()
                logger.warning("kernel: %s", kernel_line)

    threading.Thread(target=read_kernel_output, daemon=True).start()
    return process


def is_nvme_path(device_path: Path) -> bool:
    return device_path.name.startswith("nvme")


def require_argument_action(action: argparse.Action) -> None:
    if not action.dest:
        raise RuntimeError("argparse returned an action without a destination")


def parse_arguments(arguments: Sequence[str]) -> MonitorSettings:
    parser = ValidationArgumentParser(
        description="Global system monitor for disk validation runs"
    )
    require_argument_action(
        parser.add_argument(
            "--devices",
            nargs="+",
            help="Devices under test. Prefer /dev/disk/by-id/... . If omitted, monitor all physical /sys/block devices.",
        )
    )
    require_argument_action(
        parser.add_argument(
            "--out-root", required=True, help="Required absolute output root."
        )
    )
    require_argument_action(
        parser.add_argument(
            "--label", default="monitor", help="Label for this monitor run."
        )
    )
    require_argument_action(
        parser.add_argument(
            "--interval", type=float, default=30.0, help="Sampling interval in seconds."
        )
    )
    require_argument_action(
        parser.add_argument(
            "--smart-interval",
            type=float,
            default=300.0,
            help="SMART/NVMe snapshot interval in seconds. Set 0 to disable.",
        )
    )
    require_argument_action(
        parser.add_argument(
            "--duration-minutes",
            type=float,
            default=0.0,
            help="Stop automatically after this many minutes. 0 means until Ctrl-C/SIGTERM.",
        )
    )
    require_argument_action(
        parser.add_argument(
            "--no-kernel-follow",
            action="store_true",
            help="Do not follow kernel warnings/errors.",
        )
    )
    require_argument_action(
        parser.add_argument(
            "--sensors-interval",
            type=float,
            default=300.0,
            help="Run sensors -j every N seconds. 0 disables.",
        )
    )
    namespace = parser.parse_args(arguments)
    out_root_path = Path(cast(str, namespace.out_root)).expanduser()
    if not out_root_path.is_absolute():
        parser.error("--out-root must be an absolute path")
    devices = tuple(
        resolved_device_path(device)
        for device in cast(list[str] | None, namespace.devices) or ()
    )
    return MonitorSettings(
        devices=devices,
        out_root=out_root_path.resolve(),
        label=cast(str, namespace.label),
        interval=cast(float, namespace.interval),
        smart_interval=cast(float, namespace.smart_interval),
        duration_minutes=cast(float, namespace.duration_minutes),
        no_kernel_follow=cast(bool, namespace.no_kernel_follow),
        sensors_interval=cast(float, namespace.sensors_interval),
    )


def configure_logger(log_file: Path) -> logging.Logger:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("disk_burnin_monitor")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in tuple(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = UtcFormatter("%(asctime)s [%(levelname)-5s] %(message)s")
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.INFO)
    stdout_handler.addFilter(MaximumLevelFilter(logging.INFO))
    stdout_handler.setFormatter(formatter)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(formatter)

    file_handler = logging.FileHandler(log_file, encoding="utf-8", errors="replace")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    logger.addHandler(stdout_handler)
    logger.addHandler(stderr_handler)
    logger.addHandler(file_handler)
    return logger


def close_logger(logger: logging.Logger) -> None:
    for handler in tuple(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def terminate_process(process: subprocess.Popen[str] | None) -> None:
    if process is None:
        return
    try:
        process.terminate()
        process_return_code = process.wait(timeout=5)
        if process_return_code < -255:
            return
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process_return_code = process.wait(timeout=5)
            if process_return_code < -255:
                return
        except subprocess.TimeoutExpired:
            return
    except OSError:
        return


def sample_block_devices(
    block_names: list[str],
    static_block: dict[str, JsonObject],
    previous_block: dict[str, dict[str, int]],
    seconds_delta: float,
) -> tuple[JsonObject, dict[str, dict[str, int]]]:
    current_block = {
        block_name: read_block_stat(block_name) for block_name in block_names
    }
    block_samples: JsonObject = {}
    for block_name, current_stat in current_block.items():
        static_value = static_block.get(block_name)
        block_samples[block_name] = {
            "static": static_value if static_value is not None else {},
            "raw": json_int_mapping(current_stat),
            "delta": block_delta(
                previous_block.get(block_name), current_stat, seconds_delta
            ),
        }
    next_previous_block = {
        block_name: current_stat
        for block_name, current_stat in current_block.items()
        if current_stat is not None
    }
    return block_samples, next_previous_block


def summarize_blocks(block_samples: JsonObject) -> str:
    summaries: list[str] = []
    for block_name, block_sample in block_samples.items():
        if not isinstance(block_sample, dict):
            continue
        delta_value = block_sample.get("delta")
        if not isinstance(delta_value, dict) or not delta_value:
            continue
        summaries.append(
            f"{block_name}:r{delta_value.get('read_MBps', 0)}MB/s w{delta_value.get('write_MBps', 0)}MB/s util~{delta_value.get('io_util_pct_approx', 0)}%"
        )
    return "; ".join(summaries[:12])


def run_monitor(settings: MonitorSettings) -> int:
    try:
        verify_required_commands()
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return EXIT_TOOLING

    run_directory = settings.out_root / "monitor" / f"{run_id()}_{slug(settings.label)}"
    run_directory.mkdir(parents=True, exist_ok=True)
    logger = configure_logger(run_directory / "monitor.log")
    capture = CommandCapture(run_directory, logger)
    stop_event = threading.Event()
    block_names = discover_block_names(settings.devices)
    static_block = {
        block_name: read_block_static(block_name) for block_name in block_names
    }
    nvme_devices = {
        device_path for device_path in settings.devices if is_nvme_path(device_path)
    }
    static_block_json: JsonObject = {
        block_name: block_static for block_name, block_static in static_block.items()
    }

    invocation: JsonObject = {
        "timestamp_utc": utc_now(),
        "argv": json_strings(sys.argv),
        "devices": json_strings(str(device_path) for device_path in settings.devices),
        "block_names": json_strings(block_names),
        "static_block": static_block_json,
        "args": settings.json_arguments(),
    }
    write_json(run_directory / "invocation.json", invocation)
    logger.info("monitor output: %s", run_directory)
    logger.info("block devices monitored: %s", ", ".join(block_names))

    capture.capture("uname", ["uname", "-a"], quiet=True)
    capture.capture("date_utc", ["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"], quiet=True)
    capture.capture(
        "lsblk_all_json", ["lsblk", "-J", "-b", "-o", LSBLK_COLUMNS], quiet=True
    )
    capture.capture("lsblk_all_text", ["lsblk", "-o", LSBLK_COLUMNS], quiet=True)
    capture.capture("lspci_all", ["lspci", "-nn"], quiet=True)

    kernel_process = (
        None
        if settings.no_kernel_follow
        else start_kernel_follower(run_directory, logger, stop_event)
    )

    def handle_signal(signal_number: int, current_frame: FrameType | None) -> None:
        del current_frame
        logger.warning("received signal %s; stopping monitor", signal_number)
        stop_event.set()

    previous_interrupt_handler = signal.signal(signal.SIGINT, handle_signal)
    previous_termination_handler = signal.signal(signal.SIGTERM, handle_signal)

    samples_path = run_directory / "samples.jsonl"
    previous_cpu: dict[str, int] | None = None
    previous_block: dict[str, dict[str, int]] = {}
    previous_monotonic = time.monotonic()
    next_smart_capture = time.monotonic()
    next_sensors_capture = time.monotonic()
    deadline = (
        time.monotonic() + settings.duration_minutes * 60
        if settings.duration_minutes > 0
        else None
    )
    sample_count = 0

    try:
        while not stop_event.is_set():
            current_monotonic = time.monotonic()
            if deadline is not None and current_monotonic >= deadline:
                logger.info("duration reached; stopping monitor")
                break
            seconds_delta = max(0.001, current_monotonic - previous_monotonic)
            current_cpu = read_cpu_stat()
            block_samples, previous_block = sample_block_devices(
                block_names,
                static_block,
                previous_block,
                seconds_delta,
            )
            memory: JsonObject = {
                field_name: value
                for field_name, value in read_meminfo().items()
                if field_name in MEMORY_SAMPLE_FIELDS
            }
            sample: JsonObject = {
                "timestamp_utc": utc_now(),
                "monotonic_seconds": current_monotonic,
                "sample_index": sample_count,
                "loadavg": read_loadavg(),
                "uptime": read_uptime(),
                "cpu": {
                    "raw": json_int_mapping(current_cpu),
                    "busy_pct": cpu_pct(previous_cpu, current_cpu),
                },
                "meminfo_kb": memory,
                "pressure": read_pressure(),
                "hwmon_temps_c": list_hwmon_temps(),
                "block": block_samples,
            }
            append_json_line(samples_path, sample)
            cpu_value = sample["cpu"]
            load_value = sample["loadavg"]
            cpu_busy = (
                cpu_value.get("busy_pct") if isinstance(cpu_value, dict) else None
            )
            load_one_minute = (
                load_value.get("load1") if isinstance(load_value, dict) else None
            )
            logger.info(
                "sample=%s cpu=%s%% load1=%s %s",
                sample_count,
                cpu_busy,
                load_one_minute,
                summarize_blocks(block_samples),
            )

            if (
                settings.smart_interval > 0
                and settings.devices
                and current_monotonic >= next_smart_capture
            ):
                logger.info("collecting periodic SMART/NVMe snapshots")
                smart_device_snapshot(
                    capture, settings.devices, nvme_devices, f"sample{sample_count}"
                )
                next_smart_capture = current_monotonic + settings.smart_interval

            if (
                settings.sensors_interval > 0
                and current_monotonic >= next_sensors_capture
            ):
                capture.capture(
                    f"sensors_json_sample{sample_count}",
                    ["sensors", "-j"],
                    timeout=60,
                    quiet=True,
                )
                next_sensors_capture = current_monotonic + settings.sensors_interval

            previous_cpu = current_cpu
            previous_monotonic = current_monotonic
            sample_count += 1
            if stop_event.wait(settings.interval):
                break
    finally:
        stop_event.set()
        terminate_process(kernel_process)
        restored_interrupt_handler = signal.signal(
            signal.SIGINT, previous_interrupt_handler
        )
        restored_termination_handler = signal.signal(
            signal.SIGTERM, previous_termination_handler
        )
        del restored_interrupt_handler, restored_termination_handler
        logger.info(
            "monitor finished; samples=%s; output=%s", sample_count, run_directory
        )
        write_json(
            run_directory / "result.json",
            {
                "status": "PASS",
                "result": "PASS",
                "exit_code": EXIT_PASS,
                "failures": 0,
                "warnings": 0,
                "label": settings.label,
                "run_directory": str(run_directory),
                "samples": sample_count,
            },
        )
        close_logger(logger)
    return EXIT_PASS


def main(arguments: Sequence[str] | None = None) -> int:
    settings = parse_arguments(sys.argv[1:] if arguments is None else arguments)
    require_root()
    return run_monitor(settings)


if __name__ == "__main__":
    raise SystemExit(main())
