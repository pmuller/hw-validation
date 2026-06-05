#!/usr/bin/env python3
"""
Disk audit collector.

Non-destructive inventory/snapshot tool for HDD/NVMe validation workflows.
Collects lsblk, udev, SMART, and NVMe output into a timestamped
log directory and emits a manifest for later comparison.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn, cast, override

type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)
type JsonObject = dict[str, JsonValue]

EXIT_PASS = 0
EXIT_WARN = 2
EXIT_USAGE = 64
EXIT_TOOLING = 70

LSBLK_COLUMNS = "NAME,KNAME,PATH,TYPE,SIZE,MODEL,SERIAL,WWN,VENDOR,TRAN,ROTA,RM,RO,MOUNTPOINT,FSTYPE,LABEL,UUID,PARTUUID,PKNAME"
SLUG_INVALID_CHARACTER_PATTERN = re.compile(r"[^A-Za-z0-9._=-]+")
SLUG_UNDERSCORE_PATTERN = re.compile(r"_+")
NVME_NAMESPACE_PATTERN = re.compile(r"^(?P<controller>nvme\d+)n\d+")
NVME_CONTROLLER_PATTERN = re.compile(r"^(?P<controller>nvme\d+)$")
DISK_SYMLINK_ROOTS = (
    Path("/dev/disk/by-id"),
    Path("/dev/disk/by-path"),
    Path("/dev/disk/by-uuid"),
    Path("/dev/disk/by-partuuid"),
)
SMART_IDENTITY_KEYS = ("model_name", "serial_number", "firmware_version", "wwn")
SMART_DEVICE_KEYS = ("name", "type", "protocol", "info_name")
NVME_HEALTH_KEYS = (
    "critical_warning",
    "temperature",
    "percentage_used",
    "media_errors",
    "num_err_log_entries",
)
UDEV_SLUG_KEYS = ("ID_SERIAL_SHORT", "ID_SERIAL", "ID_WWN", "ID_MODEL")
REQUIRED_COMMANDS = (
    "lsblk",
    "lspci",
    "dmesg",
    "journalctl",
    "smartctl",
    "nvme",
    "blockdev",
    "wipefs",
    "hdparm",
    "sg_vpd",
    "udevadm",
)


@dataclass(frozen=True, slots=True)
class AuditSettings:
    devices: tuple[str, ...]
    audit_all: bool
    include_removable: bool
    include_readonly: bool
    out_root: Path
    label: str
    quiet: bool


class ValidationArgumentParser(argparse.ArgumentParser):
    @override
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


@dataclass(slots=True)
class CommandRunner:
    root: Path
    verbose: bool = True
    sequence: int = 0
    commands_jsonl: Path = field(init=False)

    def __post_init__(self) -> None:
        self.commands_jsonl = self.root / "commands.jsonl"

    def have(self, command_name: str) -> bool:
        return shutil.which(command_name) is not None

    def capture(
        self,
        name: str,
        command: list[str],
        cwd: Path | None = None,
        check: bool = False,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.sequence += 1
        prefix = self.root / f"{self.sequence:03d}_{slug(name)}"
        metadata_path = prefix.with_suffix(".meta.json")
        stdout_path = prefix.with_suffix(".stdout")
        stderr_path = prefix.with_suffix(".stderr")
        started_at = utc_now()
        command_text = shlex.join(command)
        if self.verbose:
            audit_log(f"RUN {name}: {command_text}")

        try:
            completed_process = subprocess.run(
                command,
                cwd=cwd,
                text=True,
                capture_output=True,
                timeout=timeout,
                errors="replace",
            )
        except (OSError, subprocess.SubprocessError) as error:
            metadata: JsonObject = {
                "name": name,
                "cmd": json_strings(command),
                "cmd_text": command_text,
                "start_utc": started_at,
                "end_utc": utc_now(),
                "exception": repr(error),
            }
            write_json(metadata_path, metadata)
            append_json_line(self.commands_jsonl, metadata)
            if check:
                raise
            return subprocess.CompletedProcess(command, 127, "", repr(error))

        write_text(stdout_path, completed_process.stdout or "")
        write_text(stderr_path, completed_process.stderr or "")
        metadata = {
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
        if self.verbose:
            audit_log(f"DONE {name}: rc={completed_process.returncode}")
            for stderr_line in completed_process.stderr.strip().splitlines()[-5:]:
                audit_log(f"STDERR {name}: {stderr_line}")
        if check and completed_process.returncode != 0:
            raise subprocess.CalledProcessError(
                completed_process.returncode,
                command,
                completed_process.stdout,
                completed_process.stderr,
            )
        return completed_process

    def record(
        self,
        name: str,
        command: list[str],
        cwd: Path | None = None,
        timeout: float | None = None,
    ) -> None:
        completed_process = self.capture(name, command, cwd=cwd, timeout=timeout)
        del completed_process


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def slug(value: str, maximum_length: int = 120) -> str:
    normalized = SLUG_INVALID_CHARACTER_PATTERN.sub("_", value.strip())
    normalized = SLUG_UNDERSCORE_PATTERN.sub("_", normalized).strip("_")
    return (normalized or "unknown")[:maximum_length]


def audit_log(message: str) -> None:
    print(f"{utc_now()} [audit] {message}", file=sys.stderr, flush=True)


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


def write_text(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", errors="replace") as text_file:
        print(text, file=text_file, end="")


def write_json(path: Path, payload: JsonValue) -> None:
    write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def append_json_line(path: Path, payload: JsonObject) -> None:
    with path.open("a", encoding="utf-8") as jsonl_file:
        print(json.dumps(payload, sort_keys=True), file=jsonl_file)


def json_strings(values: Iterable[str]) -> list[JsonValue]:
    return [value for value in values]


def json_objects(values: Iterable[JsonObject]) -> list[JsonValue]:
    return [value for value in values]


def parse_json(text: str) -> JsonValue | None:
    try:
        return normalize_json(cast(object, json.loads(text)))
    except json.JSONDecodeError:
        return None


def normalize_json(value: object) -> JsonValue | None:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list):
        list_output: list[JsonValue] = []
        for item in cast(list[object], value):
            normalized = normalize_json(item)
            if normalized is not None or item is None:
                list_output.append(normalized)
        return list_output
    if isinstance(value, dict):
        object_output: JsonObject = {}
        for key, item in cast(dict[object, object], value).items():
            if not isinstance(key, str):
                continue
            normalized = normalize_json(item)
            if normalized is not None or item is None:
                object_output[key] = normalized
        return object_output
    return None


def json_object(value: JsonValue | None) -> JsonObject | None:
    return value if isinstance(value, dict) else None


def json_object_or_empty(value: JsonValue | None) -> JsonObject:
    return value if isinstance(value, dict) else {}


def json_object_list(value: JsonValue | None) -> list[JsonObject]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def json_string(value: JsonValue | None) -> str | None:
    return value if isinstance(value, str) else None


def json_text(value: JsonValue | None) -> str:
    return "" if value is None else str(value)


def json_string_list(value: JsonValue | None) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def flatten_lsblk(nodes: Iterable[JsonObject]) -> Iterator[JsonObject]:
    for node in nodes:
        yield node
        yield from flatten_lsblk(json_object_list(node.get("children")))


def lsblk_all(runner: CommandRunner) -> JsonObject:
    completed_process = runner.capture(
        "lsblk_all_json", ["lsblk", "-J", "-b", "-o", LSBLK_COLUMNS]
    )
    data = json_object(parse_json(completed_process.stdout))
    if data is None:
        raise RuntimeError("lsblk did not return parseable JSON")
    return data


def resolve_block_device(device_path: str) -> Path:
    resolved_path = Path(device_path).expanduser().resolve(strict=True)
    if not resolved_path.is_block_device():
        raise ValueError(f"not a block device: {device_path} -> {resolved_path}")
    return resolved_path


def is_lsblk_truthy(value: JsonValue | None) -> bool:
    return value is True or value == 1 or value == "1"


def discover_devices(runner: CommandRunner, settings: AuditSettings) -> list[Path]:
    if settings.devices:
        return [resolve_block_device(device_path) for device_path in settings.devices]

    devices: list[Path] = []
    for node in json_object_list(lsblk_all(runner).get("blockdevices")):
        if json_text(node.get("type")) != "disk":
            continue
        device_path = (
            json_string(node.get("path")) or f"/dev/{json_text(node.get('name'))}"
        )
        if device_path == "/dev/":
            continue
        if not settings.include_removable and is_lsblk_truthy(node.get("rm")):
            continue
        if not settings.include_readonly and is_lsblk_truthy(node.get("ro")):
            continue
        if Path(device_path).name.startswith(("loop", "ram", "zram", "dm-")):
            continue
        try:
            devices.append(resolve_block_device(device_path))
        except (OSError, ValueError) as error:
            audit_log(f"skipping {device_path}: {error}")
    return sorted(set(devices), key=str)


def parse_udev_properties(text: str) -> dict[str, str]:
    properties: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        properties[key] = value
    return properties


def symlinks_to_device(real_device: Path) -> list[str]:
    links: list[str] = []
    for root in DISK_SYMLINK_ROOTS:
        if not root.exists():
            continue
        for item in root.iterdir():
            try:
                if item.resolve(strict=False) == real_device:
                    links.append(str(item))
            except OSError:
                continue
    return sorted(links)


def lsblk_tree_for_device(
    runner: CommandRunner, device: Path
) -> tuple[JsonObject, list[JsonObject]]:
    completed_process = runner.capture(
        "lsblk_device_json",
        ["lsblk", "-J", "-b", "-o", LSBLK_COLUMNS, str(device)],
    )
    data = json_object(parse_json(completed_process.stdout)) or {}
    return data, list(flatten_lsblk(json_object_list(data.get("blockdevices"))))


def is_nvme_device(
    device: Path, lsblk_nodes: list[JsonObject], udev_properties: dict[str, str]
) -> bool:
    if device.name.startswith("nvme"):
        return True
    if udev_properties.get("ID_BUS", "").lower() == "nvme":
        return True
    return any(json_text(node.get("tran")).lower() == "nvme" for node in lsblk_nodes)


def nvme_controller_for(device: Path) -> str:
    namespace_match = NVME_NAMESPACE_PATTERN.match(device.name)
    if namespace_match:
        return f"/dev/{namespace_match.group('controller')}"
    controller_match = NVME_CONTROLLER_PATTERN.match(device.name)
    if controller_match:
        return str(device)
    return str(device)


def smart_identity(smart: JsonObject | None) -> JsonObject:
    if smart is None:
        return {}
    output: JsonObject = {}
    for key in SMART_IDENTITY_KEYS:
        if key in smart:
            output[key] = smart[key]

    device = json_object(smart.get("device"))
    if device is not None:
        for key in SMART_DEVICE_KEYS:
            if key in device:
                output[f"device_{key}"] = device[key]

    nvme = json_object(smart.get("nvme_smart_health_information_log"))
    if nvme is not None:
        for key in NVME_HEALTH_KEYS:
            if key in nvme:
                output[f"nvme_{key}"] = nvme[key]
    return output


def summarize_mounts(nodes: list[JsonObject]) -> list[JsonValue]:
    mounts: list[JsonValue] = []
    for node in nodes:
        mountpoint = json_string(node.get("mountpoint"))
        if mountpoint:
            mounts.append(
                {
                    "name": node.get("name"),
                    "path": node.get("path"),
                    "type": node.get("type"),
                    "mountpoint": mountpoint,
                    "fstype": node.get("fstype"),
                }
            )
    return mounts


def udev_identity(properties: dict[str, str]) -> JsonObject:
    return {
        key: properties[key]
        for key in sorted(properties)
        if key.startswith(("ID_", "DEV", "SUBSYSTEM"))
    }


def device_directory_name(device: Path, udev_properties: dict[str, str]) -> str:
    parts = [device.name]
    for key in UDEV_SLUG_KEYS:
        value = udev_properties.get(key)
        if value:
            parts.append(value)
            break
    return slug("_".join(parts))


def capture_global_context(runner: CommandRunner) -> None:
    runner.record("uname", ["uname", "-a"])
    runner.record("date_utc", ["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"])
    runner.record("lsblk_all_json", ["lsblk", "-J", "-b", "-o", LSBLK_COLUMNS])
    runner.record("lsblk_all_text", ["lsblk", "-o", LSBLK_COLUMNS])
    runner.record("lspci_storage", ["lspci", "-nn"])
    runner.record("dmesg_recent", ["dmesg", "-T"])
    runner.record(
        "journalctl_kernel_recent",
        [
            "journalctl",
            "-k",
            "--no-pager",
            "-o",
            "short-iso-precise",
            "--since",
            "24 hours ago",
        ],
    )


def capture_device_details(
    runner: CommandRunner,
    device: Path,
    nodes: list[JsonObject],
    udev_properties: dict[str, str],
) -> JsonObject:
    smart_json: JsonObject | None = None
    device_text = str(device)
    completed_process = runner.capture(
        "smartctl_x_json", ["smartctl", "-x", "-j", device_text]
    )
    smart_json = json_object(parse_json(completed_process.stdout))
    runner.record("smartctl_x_text", ["smartctl", "-x", device_text])
    runner.record(
        "smartctl_health_attrs_logs",
        ["smartctl", "-H", "-A", "-l", "error", "-l", "selftest", device_text],
    )

    if is_nvme_device(device, nodes, udev_properties):
        capture_nvme_details(runner, device)
    else:
        capture_non_nvme_details(runner, device)

    for extra_command in (
        ["blockdev", "--getsize64", device_text],
        ["wipefs", "--no-act", device_text],
    ):
        runner.record("_".join(extra_command[:2]).replace("-", ""), extra_command)
    return smart_identity(smart_json)


def capture_nvme_details(runner: CommandRunner, device: Path) -> None:
    device_text = str(device)
    controller = nvme_controller_for(device)
    runner.record("nvme_list_json", ["nvme", "list", "-o", "json"])
    for target_name, target in (("namespace", device_text), ("controller", controller)):
        runner.record(
            f"nvme_smart_log_json_{target_name}",
            ["nvme", "smart-log", target, "-o", "json"],
        )
        runner.record(
            f"nvme_smart_log_text_{target_name}", ["nvme", "smart-log", target]
        )
        runner.record(
            f"nvme_error_log_json_{target_name}",
            ["nvme", "error-log", target, "-e", "64", "-o", "json"],
        )
        runner.record(
            f"nvme_self_test_log_json_{target_name}",
            ["nvme", "self-test-log", target, "-o", "json"],
        )
    runner.record("nvme_id_ctrl_json", ["nvme", "id-ctrl", controller, "-o", "json"])
    runner.record("nvme_id_ns_json", ["nvme", "id-ns", device_text, "-o", "json"])


def capture_non_nvme_details(runner: CommandRunner, device: Path) -> None:
    device_text = str(device)
    runner.record("hdparm_identify", ["hdparm", "-I", device_text])
    runner.record("sg_vpd_all", ["sg_vpd", "-a", device_text])


def audit_device(
    runner: CommandRunner, device: Path, run_directory: Path, label: str
) -> JsonObject:
    lsblk_tree, nodes = lsblk_tree_for_device(runner, device)
    top_level_nodes = json_object_list(lsblk_tree.get("blockdevices"))
    top_level = top_level_nodes[0] if top_level_nodes else {}

    udev_properties: dict[str, str] = {}
    completed_process = runner.capture(
        "udevadm_info",
        ["udevadm", "info", "--query=property", "--name", str(device)],
    )
    udev_properties = parse_udev_properties(completed_process.stdout)

    device_directory = run_directory / device_directory_name(device, udev_properties)
    device_directory.mkdir(parents=True, exist_ok=True)
    device_runner = CommandRunner(device_directory, verbose=runner.verbose)
    device_runner.record(
        "lsblk_device_json", ["lsblk", "-J", "-b", "-o", LSBLK_COLUMNS, str(device)]
    )
    device_runner.record(
        "udevadm_info",
        ["udevadm", "info", "--query=property", "--name", str(device)],
    )

    identity = capture_device_details(device_runner, device, nodes, udev_properties)
    detected_nvme = is_nvme_device(device, nodes, udev_properties)
    return {
        "timestamp_utc": utc_now(),
        "label": label,
        "device": str(device),
        "realpath": str(device.resolve(strict=False)),
        "by_id_and_path_symlinks": json_strings(symlinks_to_device(device)),
        "device_dir": str(device_directory),
        "lsblk_top": top_level,
        "mounted_filesystems": summarize_mounts(nodes),
        "udev": udev_identity(udev_properties),
        "smart_identity": identity,
        "is_nvme": detected_nvme,
    }


def require_argument_action(action: argparse.Action) -> None:
    if not action.dest:
        raise RuntimeError("argparse returned an action without a destination")


def parse_arguments(arguments: Sequence[str]) -> AuditSettings:
    parser = ValidationArgumentParser(
        description="Non-destructive disk audit collector"
    )
    require_argument_action(
        parser.add_argument(
            "--devices",
            nargs="+",
            help="Block devices to audit. Prefer /dev/disk/by-id/... paths.",
        )
    )
    require_argument_action(
        parser.add_argument(
            "--all",
            action="store_true",
            help="Audit all non-removable non-readonly block devices of TYPE=disk.",
        )
    )
    require_argument_action(
        parser.add_argument(
            "--include-removable",
            action="store_true",
            help="Include removable disks when using --all.",
        )
    )
    require_argument_action(
        parser.add_argument(
            "--include-readonly",
            action="store_true",
            help="Include read-only disks when using --all.",
        )
    )
    require_argument_action(
        parser.add_argument(
            "--out-root", required=True, help="Required absolute output root."
        )
    )
    require_argument_action(
        parser.add_argument(
            "--label",
            default="audit",
            help="Label for this audit run, e.g. pre, post, slot3_pre.",
        )
    )
    require_argument_action(
        parser.add_argument(
            "--quiet", action="store_true", help="Reduce stderr progress output."
        )
    )
    namespace = parser.parse_args(arguments)
    devices = tuple(cast(list[str] | None, namespace.devices) or ())
    audit_all = cast(bool, namespace.all)
    if not devices and not audit_all:
        parser.error("provide --devices ... or --all")
    out_root_path = Path(cast(str, namespace.out_root)).expanduser()
    if not out_root_path.is_absolute():
        parser.error("--out-root must be an absolute path")
    return AuditSettings(
        devices=devices,
        audit_all=audit_all,
        include_removable=cast(bool, namespace.include_removable),
        include_readonly=cast(bool, namespace.include_readonly),
        out_root=out_root_path.resolve(),
        label=cast(str, namespace.label),
        quiet=cast(bool, namespace.quiet),
    )


def manifest_error_row(
    label: str, device: Path, error: OSError | RuntimeError | ValueError
) -> JsonObject:
    return {
        "timestamp_utc": utc_now(),
        "label": label,
        "device": str(device),
        "error": repr(error),
    }


def inventory_value(row: JsonObject, key: str) -> str:
    return json_text(row.get(key))


def inventory_table(manifest: list[JsonObject]) -> str:
    table_lines = [
        "device\tmodel\tserial\twwn\tsize_bytes\tnvme\tmounted\tbest_symlink"
    ]
    for row in manifest:
        top_level = json_object_or_empty(row.get("lsblk_top"))
        identity = json_object_or_empty(row.get("smart_identity"))
        links = json_string_list(row.get("by_id_and_path_symlinks"))
        table_lines.append(
            "\t".join(
                [
                    inventory_value(row, "device"),
                    json_text(identity.get("model_name") or top_level.get("model")),
                    json_text(identity.get("serial_number") or top_level.get("serial")),
                    json_text(identity.get("wwn") or top_level.get("wwn")),
                    json_text(top_level.get("size")),
                    json_text(row.get("is_nvme")),
                    "yes" if row.get("mounted_filesystems") else "no",
                    links[0] if links else "",
                ]
            )
        )
    return "\n".join(table_lines) + "\n"


def run_audit(settings: AuditSettings) -> int:
    if not settings.out_root.is_absolute():
        audit_log("--out-root must be an absolute path")
        return EXIT_USAGE
    try:
        verify_required_commands()
    except RuntimeError as error:
        audit_log(str(error))
        return EXIT_TOOLING

    run_directory = settings.out_root / "audit" / f"{run_id()}_{slug(settings.label)}"
    run_directory.mkdir(parents=True, exist_ok=True)
    runner = CommandRunner(run_directory, verbose=not settings.quiet)

    audit_log(f"audit output: {run_directory}")
    capture_global_context(runner)

    devices = discover_devices(runner, settings)
    if not devices:
        audit_log("no devices discovered")
        write_json(
            run_directory / "result.json",
            {
                "status": "WARN",
                "result": "WARN",
                "exit_code": EXIT_WARN,
                "failures": 0,
                "warnings": 1,
                "label": settings.label,
                "run_directory": str(run_directory),
                "message": "no devices discovered",
            },
        )
        return EXIT_WARN
    audit_log(
        f"auditing {len(devices)} device(s): {' '.join(str(device) for device in devices)}"
    )

    manifest: list[JsonObject] = []
    for device in devices:
        try:
            audit_log(f"DEVICE {device}")
            manifest.append(audit_device(runner, device, run_directory, settings.label))
        except (OSError, RuntimeError, ValueError) as error:
            audit_log(f"ERROR auditing {device}: {error!r}")
            manifest.append(manifest_error_row(settings.label, device, error))

    write_json(run_directory / "manifest.json", json_objects(manifest))
    with (run_directory / "manifest.jsonl").open("w", encoding="utf-8") as jsonl_file:
        for row in manifest:
            print(json.dumps(row, sort_keys=True), file=jsonl_file)

    write_text(run_directory / "inventory.tsv", inventory_table(manifest))
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
            "devices": len(devices),
        },
    )
    audit_log(f"wrote manifest: {run_directory / 'manifest.json'}")
    audit_log(f"wrote inventory: {run_directory / 'inventory.tsv'}")
    return 0


def main(arguments: Sequence[str] | None = None) -> int:
    settings = parse_arguments(sys.argv[1:] if arguments is None else arguments)
    require_root()
    return run_audit(settings)


if __name__ == "__main__":
    raise SystemExit(main())
