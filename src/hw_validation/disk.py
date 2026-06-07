from __future__ import annotations

import fcntl
import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO, cast

from hw_validation.files import read_text, write_json, write_text
from hw_validation.json_types import JsonObject, JsonValue
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
from hw_validation.status import (
    ExitCode,
    ResultStatus,
    ValidationOutcome,
    outcome_from_counts,
)
from hw_validation.timeutil import elapsed_seconds, utc_now, utc_stamp
from hw_validation.timing import write_timing_summary
from hw_validation.tooling import check_fio, require_commands

DISK_AUDIT_COMMANDS = (
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
DISK_BURNIN_COMMANDS = (
    "lsblk",
    "readlink",
    "smartctl",
    "nvme",
    "badblocks",
    "blockdev",
    "sync",
)
LSBLK_COLUMNS = "NAME,KNAME,PATH,TYPE,SIZE,MODEL,SERIAL,WWN,VENDOR,TRAN,ROTA,RM,RO,MOUNTPOINT,FSTYPE,LABEL,UUID,PARTUUID,PKNAME"
DISK_AUDIT_DIRECTORY = "disk-audit"
DISK_BURNIN_DIRECTORY = "disk-burnin"
DISK_MONITOR_DIRECTORY = "disk-monitor"
SMART_SELFTEST_POLL_SECONDS = 60
SMART_SELFTEST_INITIAL_SLEEP_SECONDS = 10
SELFTEST_MAX_POLLS = 180
SMART_SELFTEST_IN_PROGRESS_PATTERNS = (
    "Self-test routine in progress",
    "Self test in progress",
    "% of test remaining",
    "remaining",
)
DISK_SECTOR_SIZE = 512
DISK_BURNIN_KINDS = ("auto", "hdd", "ssd", "nvme")
DISK_BURNIN_HDD_METHODS = ("badblocks", "fio")
MONITOR_EXCLUDED_DEVICE_PREFIXES = ("loop", "ram", "zram", "dm-")


@dataclass(frozen=True, slots=True)
class KernelFollower:
    process: subprocess.Popen[str] | None
    stdout_file: TextIO
    stderr_file: TextIO


def run_disk_audit(
    out_root: Path,
    label: str,
    devices: tuple[str, ...],
    audit_all: bool,
    include_removable: bool,
    include_readonly: bool,
    quiet: bool,
    smartctl_type: str | None,
) -> int:
    require_commands(DISK_AUDIT_COMMANDS)
    started_monotonic = time.monotonic()
    if not devices and not audit_all:
        raise ValueError("provide --devices or --all")
    run_directory = out_root / DISK_AUDIT_DIRECTORY / f"{utc_stamp()}_{slug(label)}"
    ensure_directory(run_directory)
    plan = RunPlan(
        command="disk audit",
        duration_mode=DurationMode.fast,
        phases=(RunPhase("inventory", "Capture disk inventory and SMART/NVMe state."),),
    )
    write_run_plan(run_directory, plan)
    print_run_plan(plan)
    runner = CommandRunner(run_directory, verbose=not quiet)
    started_at = utc_now()
    runner.record("lsblk_all_json", ["lsblk", "-J", "-b", "-o", LSBLK_COLUMNS])
    runner.record("lspci", ["lspci", "-nn"])
    runner.record("dmesg", ["dmesg", "-T"])
    selected_devices = discover_devices(
        runner, devices, audit_all, include_removable, include_readonly
    )
    if not selected_devices:
        outcome = ValidationOutcome(
            ResultStatus.warn, ExitCode.warning.code, warnings=1
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
                "message": "no devices discovered",
            },
        )
        write_timing_summary(run_directory)
        return outcome.exit_code
    manifest: list[JsonObject] = []
    for device_path in selected_devices:
        manifest.append(
            audit_device(runner, run_directory, label, device_path, smartctl_type)
        )
    write_json(run_directory / "manifest.json", [item for item in manifest])
    write_text(run_directory / "inventory.tsv", inventory_table(manifest))
    outcome = ValidationOutcome(ResultStatus.pass_status, ExitCode.pass_status.code)
    write_result(
        run_directory / "result.json",
        outcome,
        label,
        run_directory,
        elapsed_seconds(started_monotonic),
        {
            "started_at": started_at,
            "duration_mode": DurationMode.fast.value,
            "devices": len(selected_devices),
        },
    )
    write_timing_summary(run_directory)
    return outcome.exit_code


def discover_devices(
    runner: CommandRunner,
    devices: tuple[str, ...],
    audit_all: bool,
    include_removable: bool,
    include_readonly: bool,
) -> list[Path]:
    if devices:
        return [Path(device).expanduser().resolve(strict=True) for device in devices]
    if not audit_all:
        return []
    result = runner.capture(
        "lsblk_discover", ["lsblk", "-J", "-b", "-o", LSBLK_COLUMNS]
    )
    payload = cast(JsonObject, json.loads(result.stdout))
    block_devices = payload.get("blockdevices")
    selected: list[Path] = []
    if not isinstance(block_devices, list):
        return selected
    for item in block_devices:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "disk":
            continue
        if not include_removable and str(item.get("rm", "0")) == "1":
            continue
        if not include_readonly and str(item.get("ro", "0")) == "1":
            continue
        path_value = item.get("path")
        if isinstance(path_value, str) and not Path(path_value).name.startswith(
            ("loop", "ram", "zram", "dm-")
        ):
            selected.append(Path(path_value).resolve(strict=False))
    return selected


def audit_device(
    runner: CommandRunner,
    run_directory: Path,
    label: str,
    device_path: Path,
    smartctl_type: str | None,
) -> JsonObject:
    device_directory = run_directory / slug(device_path.name)
    ensure_directory(device_directory)
    device_runner = CommandRunner(device_directory, verbose=runner.verbose)
    device_text = str(device_path)
    lsblk = device_runner.capture(
        "lsblk", ["lsblk", "-J", "-b", "-o", LSBLK_COLUMNS, device_text]
    )
    udev = device_runner.capture(
        "udevadm", ["udevadm", "info", "--query=property", "--name", device_text]
    )
    smart = device_runner.capture(
        "smartctl_json", smartctl_command(device_path, smartctl_type, "-x", "-j")
    )
    device_runner.record(
        "smartctl_text", smartctl_command(device_path, smartctl_type, "-x")
    )
    device_runner.record("blockdev_size", ["blockdev", "--getsize64", device_text])
    device_runner.record("wipefs", ["wipefs", "--no-act", device_text])
    if device_path.name.startswith("nvme"):
        device_runner.record(
            "nvme_smart", ["nvme", "smart-log", device_text, "-o", "json"]
        )
        device_runner.record(
            "nvme_error", ["nvme", "error-log", device_text, "-e", "64", "-o", "json"]
        )
    else:
        device_runner.record("hdparm", ["hdparm", "-I", device_text])
        device_runner.record("sg_vpd", ["sg_vpd", "-a", device_text])
    return {
        "timestamp_utc": utc_now(),
        "label": label,
        "device": device_text,
        "device_dir": str(device_directory),
        "lsblk": parse_json_object(lsblk.stdout),
        "udev": parse_udev(udev.stdout),
        "smart": parse_json_object(smart.stdout),
    }


def parse_json_object(text: str) -> JsonObject:
    try:
        value = cast(object, json.loads(text))
    except json.JSONDecodeError:
        return {}
    return cast(JsonObject, value) if isinstance(value, dict) else {}


def parse_udev(text: str) -> JsonObject:
    output: JsonObject = {}
    for line in text.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            output[key] = value
    return output


def inventory_table(manifest: list[JsonObject]) -> str:
    lines = ["device\tdevice_dir"]
    for item in manifest:
        lines.append(f"{item.get('device', '')}\t{item.get('device_dir', '')}")
    return "\n".join(lines) + "\n"


def run_disk_burnin(
    out_root: Path,
    label: str,
    device: str,
    erase_ok: bool,
    dry_run: bool,
    kind: str,
    hdd_method: str,
    smartctl_type: str | None,
    fio_block_size: str,
    ssd_full_passes: int,
    ssd_randread_duration: str,
    hdd_randread_duration: str,
    hdd_fio_passes: int,
    skip_randread: bool,
    skip_selftests: bool,
) -> int:
    validate_disk_burnin_modes(kind, hdd_method)
    require_commands(DISK_BURNIN_COMMANDS)
    started_monotonic = time.monotonic()
    if not dry_run and not erase_ok:
        raise ValueError(
            "refusing destructive disk validation without --i-know-this-erases-data"
        )
    fio_path, fio_version = check_fio()
    if fio_path is None or fio_version is None:
        raise RuntimeError("The fio command in PATH is not Flexible I/O Tester")
    real_device = Path(device).expanduser().resolve(strict=True)
    run_directory = (
        out_root
        / DISK_BURNIN_DIRECTORY
        / f"{utc_stamp()}_{slug(label)}_{slug(real_device.name)}"
    )
    ensure_directory(run_directory)
    runner = CommandRunner(run_directory)
    validate_burnin_target(runner, real_device)
    lock_file = acquire_device_lock(real_device)
    try:
        return run_locked_disk_burnin(
            runner,
            run_directory,
            label,
            real_device,
            dry_run,
            kind,
            hdd_method,
            smartctl_type,
            fio_path,
            fio_block_size,
            ssd_full_passes,
            ssd_randread_duration,
            hdd_randread_duration,
            hdd_fio_passes,
            skip_randread,
            skip_selftests,
            started_monotonic,
        )
    finally:
        lock_file.close()


def validate_disk_burnin_modes(kind: str, hdd_method: str) -> None:
    if kind not in DISK_BURNIN_KINDS:
        raise ValueError("--kind must be one of auto, hdd, ssd, or nvme")
    if hdd_method not in DISK_BURNIN_HDD_METHODS:
        raise ValueError("--hdd-method must be one of badblocks or fio")


def smartctl_command(
    device_path: Path, smartctl_type: str | None, *arguments: str
) -> list[str]:
    command = ["smartctl"]
    if smartctl_type:
        command.extend(("-d", smartctl_type))
    command.extend(arguments)
    command.append(str(device_path))
    return command


def run_locked_disk_burnin(
    runner: CommandRunner,
    run_directory: Path,
    label: str,
    real_device: Path,
    dry_run: bool,
    kind: str,
    hdd_method: str,
    smartctl_type: str | None,
    fio_path: str,
    fio_block_size: str,
    ssd_full_passes: int,
    ssd_randread_duration: str,
    hdd_randread_duration: str,
    hdd_fio_passes: int,
    skip_randread: bool,
    skip_selftests: bool,
    started_monotonic: float,
) -> int:
    started_at = utc_now()
    failures = 0
    snapshot_device(runner, run_directory / "pre", real_device, smartctl_type)
    detected_kind = kind if kind != "auto" else detect_kind(real_device)
    randread_duration = (
        hdd_randread_duration if detected_kind == "hdd" else ssd_randread_duration
    )
    randread_seconds = duration_seconds(randread_duration)
    plan = disk_burnin_plan(
        detected_kind,
        hdd_method,
        fio_block_size,
        ssd_full_passes,
        hdd_fio_passes,
        randread_duration,
        randread_seconds,
        skip_randread,
        skip_selftests,
    )
    write_run_plan(run_directory, plan)
    print_run_plan(plan)
    if not dry_run and not skip_selftests:
        failures += run_selftests(
            runner, run_directory, real_device, detected_kind, smartctl_type, False
        )
    if not dry_run:
        if detected_kind == "hdd" and hdd_method == "badblocks":
            if not runner.stream(
                "badblocks",
                [
                    "badblocks",
                    "-wsv",
                    "-b",
                    "4096",
                    "-c",
                    "65536",
                    "-o",
                    str(run_directory / "badblocks.list"),
                    str(real_device),
                ],
                run_directory / "badblocks.stdout",
                run_directory / "badblocks.stderr",
            ).ok:
                failures += 1
        else:
            passes = hdd_fio_passes if detected_kind == "hdd" else ssd_full_passes
            failures += run_full_write_verify(
                runner, fio_path, real_device, run_directory, passes, fio_block_size
            )
        if not skip_randread:
            failures += run_randread(
                runner, fio_path, real_device, run_directory, randread_duration
            )
    if not dry_run and not skip_selftests:
        failures += run_selftests(
            runner, run_directory, real_device, detected_kind, smartctl_type, True
        )
    final_snapshot_directory = run_directory / "final"
    snapshot_device(runner, final_snapshot_directory, real_device, smartctl_type)
    if not dry_run:
        failures += smart_health_check(
            runner, final_snapshot_directory, real_device, smartctl_type
        )
    outcome = outcome_from_counts(failures, 0)
    write_result(
        run_directory / "result.json",
        outcome,
        label,
        run_directory,
        elapsed_seconds(started_monotonic),
        {
            "started_at": started_at,
            "duration_mode": plan.duration_mode.value,
            "device": str(real_device),
            "detected_kind": detected_kind,
            "dry_run": dry_run,
            "smartctl_type": smartctl_type or "",
            "fio_block_size": fio_block_size,
            "ssd_full_passes": ssd_full_passes,
            "hdd_fio_passes": hdd_fio_passes,
            "randread_duration": randread_duration,
            "randread_duration_seconds": randread_seconds,
            "skip_randread": skip_randread,
            "skip_selftests": skip_selftests,
        },
    )
    write_timing_summary(run_directory)
    return outcome.exit_code


def validate_burnin_target(runner: CommandRunner, real_device: Path) -> None:
    if not real_device.is_block_device():
        raise ValueError(f"--device is not a block device: {real_device}")
    device_type = runner.capture(
        "preflight_lsblk_type", ["lsblk", "-dnro", "TYPE", str(real_device)]
    )
    if device_type.stdout.strip() != "disk":
        raise ValueError(
            f"--device must be a whole disk, not a partition: {real_device}"
        )
    descendants = device_descendants(runner, real_device)
    mounted = mounted_descendants(runner, real_device)
    if mounted:
        raise ValueError(f"refusing mounted device tree: {', '.join(mounted)}")
    swaps = active_swap_devices(descendants)
    if swaps:
        raise ValueError(f"refusing active swap device tree: {', '.join(swaps)}")
    holders = device_tree_holders(descendants)
    if holders:
        raise ValueError(f"refusing device with active holders: {', '.join(holders)}")


def device_descendants(runner: CommandRunner, real_device: Path) -> tuple[Path, ...]:
    result = runner.capture(
        "preflight_lsblk_descendants", ["lsblk", "-nrpo", "NAME", str(real_device)]
    )
    paths: list[Path] = []
    for line in result.stdout.splitlines():
        line_text = line.strip()
        if line_text:
            paths.append(Path(line_text).resolve(strict=False))
    return tuple(paths)


def mounted_descendants(runner: CommandRunner, real_device: Path) -> tuple[str, ...]:
    result = runner.capture(
        "preflight_lsblk_mounts",
        ["lsblk", "-nrpo", "NAME,MOUNTPOINT", str(real_device)],
    )
    mounted: list[str] = []
    for line in result.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[1].strip():
            mounted.append(line)
    return tuple(mounted)


def active_swap_devices(descendants: tuple[Path, ...]) -> tuple[str, ...]:
    swaps_text = read_text(Path("/proc/swaps")) or ""
    descendant_texts = {str(path) for path in descendants}
    active: list[str] = []
    for line in swaps_text.splitlines()[1:]:
        parts = line.split()
        if not parts:
            continue
        swap_path = Path(parts[0]).resolve(strict=False)
        if str(swap_path) in descendant_texts:
            active.append(str(swap_path))
    return tuple(active)


def device_tree_holders(descendants: tuple[Path, ...]) -> tuple[str, ...]:
    holders: list[str] = []
    for device_path in descendants:
        holders.extend(
            f"{device_path.name}:{holder}" for holder in device_holders(device_path)
        )
    return tuple(holders)


def device_holders(device_path: Path) -> tuple[str, ...]:
    holders_directory = Path("/sys/class/block") / device_path.name / "holders"
    try:
        return tuple(sorted(path.name for path in holders_directory.iterdir()))
    except OSError:
        return ()


def acquire_device_lock(real_device: Path) -> TextIO:
    lock_path = Path("/run/lock") / f"hw-validation-disk-burnin-{real_device.name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        lock_file.close()
        raise RuntimeError(
            f"another burn-in is already running for {real_device}"
        ) from error
    return lock_file


def detect_kind(device_path: Path) -> str:
    if device_path.name.startswith("nvme"):
        return "nvme"
    rotational = read_text(
        Path("/sys/class/block") / device_path.name / "queue/rotational"
    )
    return "hdd" if rotational == "1" else "ssd"


def snapshot_device(
    runner: CommandRunner,
    snapshot_directory: Path,
    device_path: Path,
    smartctl_type: str | None,
) -> None:
    ensure_directory(snapshot_directory)
    device_text = str(device_path)
    _ = runner.stream(
        "lsblk",
        ["lsblk", "--bytes", "--output-all", device_text],
        snapshot_directory / "lsblk.txt",
        snapshot_directory / "lsblk.stderr",
    )
    _ = runner.stream(
        "smartctl",
        smartctl_command(device_path, smartctl_type, "-x"),
        snapshot_directory / "smartctl-x.txt",
        snapshot_directory / "smartctl.stderr",
    )
    if device_path.name.startswith("nvme"):
        _ = runner.stream(
            "nvme_smart",
            ["nvme", "smart-log", "-o", "json", device_text],
            snapshot_directory / "nvme-smart-log.json",
            snapshot_directory / "nvme-smart.stderr",
        )


def run_selftests(
    runner: CommandRunner,
    run_directory: Path,
    device_path: Path,
    detected_kind: str,
    smartctl_type: str | None,
    final: bool,
) -> int:
    if detected_kind == "nvme":
        return run_nvme_selftest(
            runner, run_directory, device_path, "extended" if final else "short"
        )
    return run_smart_selftest(
        runner, run_directory, device_path, smartctl_type, "long" if final else "short"
    )


def run_smart_selftest(
    runner: CommandRunner,
    run_directory: Path,
    device_path: Path,
    smartctl_type: str | None,
    test_name: str,
) -> int:
    selftest_directory = run_directory / "selftests"
    ensure_directory(selftest_directory)
    start = runner.capture(
        f"smart_{test_name}_selftest_start",
        smartctl_command(device_path, smartctl_type, "-t", test_name),
    )
    if not start.ok:
        return 1
    time.sleep(SMART_SELFTEST_INITIAL_SLEEP_SECONDS)
    completed = False
    for poll_number in range(1, SELFTEST_MAX_POLLS + 1):
        poll = runner.stream(
            f"smart_{test_name}_selftest_poll_{poll_number}",
            smartctl_command(device_path, smartctl_type, "-c"),
            selftest_directory / f"smartctl-{test_name}-poll-{poll_number}.txt",
            selftest_directory / f"smartctl-{test_name}-poll-{poll_number}.stderr",
        )
        if not smart_selftest_in_progress(poll.stdout):
            completed = True
            break
        time.sleep(SMART_SELFTEST_POLL_SECONDS)
    final_log = runner.stream(
        f"smart_{test_name}_selftest_log",
        smartctl_command(device_path, smartctl_type, "-l", "selftest"),
        selftest_directory / f"smartctl-{test_name}-selftest-log.txt",
        selftest_directory / f"smartctl-{test_name}-selftest-log.stderr",
    )
    return (
        0
        if completed and final_log.ok and smart_selftest_passed(final_log.stdout)
        else 1
    )


def smart_health_check(
    runner: CommandRunner,
    snapshot_directory: Path,
    device_path: Path,
    smartctl_type: str | None,
) -> int:
    return (
        0
        if runner.stream(
            "smartctl_health",
            smartctl_command(device_path, smartctl_type, "-H"),
            snapshot_directory / "smartctl-health.txt",
            snapshot_directory / "smartctl-health.stderr",
        ).ok
        else 1
    )


def smart_selftest_in_progress(text: str) -> bool:
    return any(pattern in text for pattern in SMART_SELFTEST_IN_PROGRESS_PATTERNS)


def smart_selftest_passed(text: str) -> bool:
    for line in text.splitlines():
        if line.strip().startswith("#"):
            return "Completed without error" in line
    return False


def run_nvme_selftest(
    runner: CommandRunner, run_directory: Path, device_path: Path, test_name: str
) -> int:
    selftest_directory = run_directory / "selftests"
    ensure_directory(selftest_directory)
    test_code = "2" if test_name == "extended" else "1"
    start = runner.capture(
        f"nvme_{test_name}_selftest_start",
        ["nvme", "device-self-test", str(device_path), "-s", test_code],
    )
    if not start.ok:
        return 1
    time.sleep(SMART_SELFTEST_INITIAL_SLEEP_SECONDS)
    completed = False
    for poll_number in range(1, SELFTEST_MAX_POLLS + 1):
        poll = runner.stream(
            f"nvme_{test_name}_selftest_poll_{poll_number}",
            ["nvme", "self-test-log", str(device_path), "-v"],
            selftest_directory / f"nvme-{test_name}-poll-{poll_number}.txt",
            selftest_directory / f"nvme-{test_name}-poll-{poll_number}.stderr",
        )
        if nvme_selftest_complete(poll.stdout):
            completed = True
            break
        time.sleep(SMART_SELFTEST_POLL_SECONDS)
    final_log = runner.stream(
        f"nvme_{test_name}_selftest_log",
        ["nvme", "self-test-log", str(device_path), "-v"],
        selftest_directory / f"nvme-{test_name}-selftest-log.txt",
        selftest_directory / f"nvme-{test_name}-selftest-log.stderr",
    )
    return (
        0
        if completed and final_log.ok and nvme_selftest_passed(final_log.stdout)
        else 1
    )


def nvme_selftest_complete(text: str) -> bool:
    return nvme_current_operation(text) in {"0", "0x0"}


def nvme_current_operation(text: str) -> str:
    for line in text.splitlines():
        if "Current operation" not in line or ":" not in line:
            continue
        return line.split(":", 1)[1].strip().split(maxsplit=1)[0].lower()
    return ""


def nvme_selftest_passed(text: str) -> bool:
    lowered = text.lower()
    return (
        "failed" not in lowered
        and "aborted" not in lowered
        and nvme_selftest_complete(text)
    )


def run_full_write_verify(
    runner: CommandRunner,
    fio_path: str,
    device_path: Path,
    run_directory: Path,
    passes: int,
    block_size: str,
) -> int:
    failures = 0
    for pass_number in range(1, passes + 1):
        if not runner.stream(
            f"fio_full_pass_{pass_number}",
            [
                fio_path,
                f"--name=full_write_verify_pass{pass_number}",
                f"--filename={device_path}",
                "--rw=write",
                f"--bs={block_size}",
                "--ioengine=psync",
                "--direct=1",
                "--verify=crc32c",
                "--do_verify=1",
                "--verify_fatal=1",
                "--size=100%",
                "--group_reporting",
            ],
            run_directory / f"fio_full_pass_{pass_number}.stdout",
            run_directory / f"fio_full_pass_{pass_number}.stderr",
        ).ok:
            failures += 1
    return failures


def disk_burnin_plan(
    detected_kind: str,
    hdd_method: str,
    fio_block_size: str,
    ssd_full_passes: int,
    hdd_fio_passes: int,
    randread_duration: str,
    randread_seconds: int,
    skip_randread: bool,
    skip_selftests: bool,
) -> RunPlan:
    phases: list[RunPhase] = [
        RunPhase("preflight", "Validate target is a whole idle disk and acquire lock."),
        RunPhase("snapshot-before", "Capture disk state before destructive work."),
    ]
    if not skip_selftests:
        phases.append(
            RunPhase("short-self-test", "Run and verify short SMART/NVMe self-test.")
        )
    if detected_kind == "hdd" and hdd_method == "badblocks":
        phases.append(
            RunPhase("badblocks", "Run destructive badblocks full surface test.")
        )
    else:
        passes = hdd_fio_passes if detected_kind == "hdd" else ssd_full_passes
        phases.append(
            RunPhase(
                "full-write-verify",
                f"Run destructive fio write/verify with {passes} pass(es) at {fio_block_size}.",
            )
        )
    if not skip_randread:
        phases.append(
            RunPhase(
                "random-read",
                "Run fio random read verification.",
                randread_seconds,
                randread_duration,
            )
        )
    if not skip_selftests:
        phases.append(
            RunPhase("final-self-test", "Run and verify final SMART/NVMe self-test.")
        )
    phases.append(RunPhase("snapshot-after", "Capture final disk state."))
    return RunPlan(
        command="disk burnin",
        duration_mode=DurationMode.pass_bounded,
        estimated_minimum_seconds=None if skip_randread else randread_seconds,
        requested_duration_seconds=None if skip_randread else randread_seconds,
        phases=tuple(phases),
        notes=(
            "Total runtime also depends on disk size, media speed, pass counts, and self-test duration.",
        ),
    )


def run_randread(
    runner: CommandRunner,
    fio_path: str,
    device_path: Path,
    run_directory: Path,
    duration: str,
) -> int:
    _ = duration_seconds(duration)
    return (
        0
        if runner.stream(
            "fio_randread",
            [
                fio_path,
                "--name=randread",
                f"--filename={device_path}",
                "--rw=randread",
                f"--runtime={duration}",
                "--time_based=1",
                "--bs=4k",
                "--ioengine=psync",
                "--direct=1",
                "--readonly=1",
                "--group_reporting",
            ],
            run_directory / "fio_randread.stdout",
            run_directory / "fio_randread.stderr",
        ).ok
        else 1
    )


def run_disk_monitor(
    out_root: Path,
    label: str,
    devices: tuple[str, ...],
    interval: str,
    smart_interval: str,
    sensors_interval: str,
    duration: str | None,
    until_interrupted: bool,
    smartctl_type: str | None,
    smart_snapshots: bool,
    sensors_snapshots: bool,
) -> int:
    resolved_devices = discover_monitor_devices(devices)
    require_commands(
        monitor_required_commands(resolved_devices, smart_snapshots, sensors_snapshots)
    )
    started_monotonic = time.monotonic()
    interval_seconds = duration_seconds(interval)
    smart_interval_seconds = duration_seconds(smart_interval)
    sensors_interval_seconds = duration_seconds(sensors_interval)
    if until_interrupted == (duration is not None):
        raise ValueError("provide exactly one of --duration or --until-interrupted")
    duration_seconds_limit = (
        duration_seconds(duration) if duration is not None else None
    )
    run_directory = out_root / DISK_MONITOR_DIRECTORY / f"{utc_stamp()}_{slug(label)}"
    ensure_directory(run_directory)
    plan = RunPlan(
        command="disk monitor",
        duration_mode=DurationMode.until_interrupted
        if until_interrupted
        else DurationMode.bounded,
        estimated_minimum_seconds=duration_seconds_limit,
        requested_duration_seconds=duration_seconds_limit,
        phases=(
            RunPhase("startup", "Capture baseline disk and PCI inventory."),
            RunPhase(
                "sampling",
                "Write periodic disk telemetry samples.",
                duration_seconds_limit,
                duration or "until interrupted",
            ),
        ),
        notes=(
            f"Telemetry interval: {interval}.",
            "SMART snapshots disabled."
            if not smart_snapshots
            else f"SMART interval: {smart_interval}.",
            "Sensor snapshots disabled."
            if not sensors_snapshots
            else f"Sensor interval: {sensors_interval}.",
        ),
    )
    write_run_plan(run_directory, plan)
    print_run_plan(plan)
    runner = CommandRunner(run_directory)
    started_at = utc_now()
    runner.record("uname", ["uname", "-a"])
    runner.record("lsblk_json", ["lsblk", "-J", "-b", "-o", LSBLK_COLUMNS])
    runner.record("lsblk", ["lsblk", "-o", LSBLK_COLUMNS])
    runner.record("lspci", ["lspci", "-nn"])
    device_names = tuple(device.name for device in resolved_devices)
    write_json(
        run_directory / "invocation.json",
        {
            "started_at": started_at,
            "interval": interval,
            "interval_seconds": interval_seconds,
            "smart_interval": smart_interval,
            "smart_interval_seconds": smart_interval_seconds,
            "sensors_interval": sensors_interval,
            "sensors_interval_seconds": sensors_interval_seconds,
            "duration": duration or "",
            "requested_duration_seconds": duration_seconds_limit,
            "until_interrupted": until_interrupted,
            "smartctl_type": smartctl_type or "",
            "smart_snapshots": smart_snapshots,
            "sensors_snapshots": sensors_snapshots,
            "devices": [cast(JsonValue, str(device)) for device in resolved_devices],
            "device_names": [cast(JsonValue, name) for name in device_names],
        },
    )
    kernel_follower = start_kernel_follower(run_directory)
    sample_path = run_directory / "samples.jsonl"
    previous_stats: dict[str, tuple[int, ...]] = {}
    previous_sample_monotonic = started_monotonic
    sample_index = 0
    next_smart_snapshot = started_monotonic
    next_sensors_snapshot = started_monotonic
    completed_reason = "completed"
    try:
        with sample_path.open("a", encoding="utf-8") as sample_file:
            while True:
                sample_index += 1
                sample, previous_stats, previous_sample_monotonic = monitor_sample(
                    sample_index,
                    started_monotonic,
                    previous_sample_monotonic,
                    previous_stats,
                    device_names,
                )
                _ = sample_file.write(json.dumps(sample, sort_keys=True) + "\n")
                sample_file.flush()
                now_monotonic = time.monotonic()
                if (
                    resolved_devices
                    and smart_snapshots
                    and now_monotonic >= next_smart_snapshot
                ):
                    capture_monitor_smart(
                        runner, run_directory, label, resolved_devices, smartctl_type
                    )
                    next_smart_snapshot = now_monotonic + smart_interval_seconds
                if sensors_snapshots and now_monotonic >= next_sensors_snapshot:
                    capture_monitor_sensors(runner, run_directory)
                    next_sensors_snapshot = now_monotonic + sensors_interval_seconds
                if duration_seconds_limit is not None:
                    remaining_seconds = duration_seconds_limit - (
                        time.monotonic() - started_monotonic
                    )
                    if remaining_seconds <= 0:
                        break
                    time.sleep(min(interval_seconds, remaining_seconds))
                else:
                    time.sleep(interval_seconds)
    except KeyboardInterrupt:
        completed_reason = "interrupted"
        write_text(run_directory / "monitor.log", "Interrupted by user\n")
    finally:
        stop_kernel_follower(kernel_follower)
    write_result(
        run_directory / "result.json",
        ValidationOutcome(ResultStatus.pass_status, ExitCode.pass_status.code),
        label,
        run_directory,
        elapsed_seconds(started_monotonic),
        {
            "started_at": started_at,
            "duration_mode": plan.duration_mode.value,
            "interval": interval,
            "interval_seconds": interval_seconds,
            "duration": duration or "",
            "requested_duration_seconds": duration_seconds_limit,
            "until_interrupted": until_interrupted,
            "samples": sample_index,
            "devices": [cast(JsonValue, str(device)) for device in resolved_devices],
        },
        completed_reason,
    )
    write_timing_summary(run_directory)
    return ExitCode.pass_status.code


def discover_monitor_devices(
    devices: tuple[str, ...], system_block_path: Path | None = None
) -> tuple[Path, ...]:
    if devices:
        return tuple(
            Path(device).expanduser().resolve(strict=True) for device in devices
        )
    block_path = Path("/sys/block") if system_block_path is None else system_block_path
    try:
        return tuple(
            Path("/dev") / block_device_path.name
            for block_device_path in sorted(block_path.iterdir())
            if monitor_device_name(block_device_path.name)
        )
    except OSError:
        return ()


def monitor_device_name(device_name: str) -> bool:
    return not device_name.startswith(MONITOR_EXCLUDED_DEVICE_PREFIXES)


def monitor_required_commands(
    devices: tuple[Path, ...], smart_snapshots: bool, sensors_snapshots: bool
) -> tuple[str, ...]:
    required_commands = ["lsblk", "lspci", "journalctl"]
    if smart_snapshots and devices:
        required_commands.extend(("smartctl", "nvme"))
    if sensors_snapshots:
        required_commands.append("sensors")
    return tuple(required_commands)


def start_kernel_follower(run_directory: Path) -> KernelFollower:
    stdout_file = (run_directory / "kernel-follow.log").open("w", encoding="utf-8")
    stderr_file = (run_directory / "kernel-follow.stderr").open("w", encoding="utf-8")
    try:
        process = subprocess.Popen(
            ["journalctl", "-k", "-f", "-p", "warning", "-o", "short-iso-precise"],
            stdin=subprocess.DEVNULL,
            text=True,
            stdout=stdout_file,
            stderr=stderr_file,
        )
    except OSError:
        process = None
    return KernelFollower(process, stdout_file, stderr_file)


def stop_kernel_follower(kernel_follower: KernelFollower) -> None:
    if kernel_follower.process is not None and kernel_follower.process.poll() is None:
        kernel_follower.process.terminate()
        try:
            _ = kernel_follower.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            kernel_follower.process.kill()
            _ = kernel_follower.process.wait(timeout=5)
    kernel_follower.stdout_file.close()
    kernel_follower.stderr_file.close()


def monitor_sample(
    sample_index: int,
    started_monotonic: float,
    previous_sample_monotonic: float,
    previous_stats: dict[str, tuple[int, ...]],
    device_names: tuple[str, ...],
) -> tuple[JsonObject, dict[str, tuple[int, ...]], float]:
    now_monotonic = time.monotonic()
    interval_elapsed = max(now_monotonic - previous_sample_monotonic, 0.001)
    block_devices: JsonObject = {}
    next_stats: dict[str, tuple[int, ...]] = {}
    for device_name in device_names:
        current_stats = block_stat(device_name)
        if current_stats is None:
            continue
        next_stats[device_name] = current_stats
        block_devices[device_name] = block_metrics(
            current_stats,
            previous_stats.get(device_name),
            interval_elapsed,
        )
    return (
        {
            "timestamp_utc": utc_now(),
            "sample_index": sample_index,
            "monotonic_seconds": round(now_monotonic - started_monotonic, 3),
            "loadavg": read_text(Path("/proc/loadavg")) or "",
            "uptime": read_text(Path("/proc/uptime")) or "",
            "memory": meminfo_sample(),
            "pressure": pressure_sample(),
            "block_devices": block_devices,
        },
        next_stats,
        now_monotonic,
    )


def block_stat(device_name: str) -> tuple[int, ...] | None:
    stat_text = read_text(Path("/sys/class/block") / device_name / "stat")
    if stat_text is None:
        return None
    values: list[int] = []
    for field in stat_text.split():
        try:
            values.append(int(field))
        except ValueError:
            return None
    return tuple(values)


def block_metrics(
    current_stats: tuple[int, ...],
    previous_stats: tuple[int, ...] | None,
    elapsed: float,
) -> JsonObject:
    metrics: JsonObject = {"raw": [cast(JsonValue, value) for value in current_stats]}
    if previous_stats is None or len(previous_stats) < 11 or len(current_stats) < 11:
        return metrics
    read_ios = current_stats[0] - previous_stats[0]
    read_sectors = current_stats[2] - previous_stats[2]
    write_ios = current_stats[4] - previous_stats[4]
    write_sectors = current_stats[6] - previous_stats[6]
    discard_sectors = (
        current_stats[14] - previous_stats[14] if len(current_stats) > 14 else 0
    )
    io_ticks = current_stats[9] - previous_stats[9]
    metrics.update(
        {
            "read_iops": round(read_ios / elapsed, 3),
            "write_iops": round(write_ios / elapsed, 3),
            "read_mb_per_second": round(
                read_sectors * DISK_SECTOR_SIZE / elapsed / 1_000_000, 3
            ),
            "write_mb_per_second": round(
                write_sectors * DISK_SECTOR_SIZE / elapsed / 1_000_000, 3
            ),
            "discard_mb_per_second": round(
                discard_sectors * DISK_SECTOR_SIZE / elapsed / 1_000_000, 3
            ),
            "io_utilization_percent": round(io_ticks / (elapsed * 10), 3),
            "inflight_io": current_stats[8],
        }
    )
    return metrics


def meminfo_sample() -> JsonObject:
    selected_keys = {
        "MemTotal",
        "MemFree",
        "MemAvailable",
        "Buffers",
        "Cached",
        "SwapTotal",
        "SwapFree",
    }
    output: JsonObject = {}
    for line in (read_text(Path("/proc/meminfo")) or "").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key in selected_keys:
            output[key] = value.strip()
    return output


def pressure_sample() -> JsonObject:
    output: JsonObject = {}
    for pressure_name in ("cpu", "io", "memory"):
        output[pressure_name] = read_text(Path("/proc/pressure") / pressure_name) or ""
    return output


def capture_monitor_smart(
    runner: CommandRunner,
    run_directory: Path,
    label: str,
    devices: tuple[Path, ...],
    smartctl_type: str | None,
) -> None:
    snapshot_directory = run_directory / f"smart_{slug(label)}_{utc_stamp()}"
    ensure_directory(snapshot_directory)
    for device in devices:
        device_label = slug(device.name)
        _ = runner.stream(
            f"smart_health_{device_label}",
            smartctl_command(
                device, smartctl_type, "-H", "-A", "-l", "error", "-l", "selftest"
            ),
            snapshot_directory / f"smartctl-{device_label}.txt",
            snapshot_directory / f"smartctl-{device_label}.stderr",
        )
        _ = runner.stream(
            f"smart_json_{device_label}",
            smartctl_command(device, smartctl_type, "-x", "-j"),
            snapshot_directory / f"smartctl-{device_label}.json",
            snapshot_directory / f"smartctl-{device_label}-json.stderr",
        )
        if device.name.startswith("nvme"):
            _ = runner.stream(
                f"nvme_smart_{device_label}",
                ["nvme", "smart-log", str(device), "-o", "json"],
                snapshot_directory / f"nvme-smart-{device_label}.json",
                snapshot_directory / f"nvme-smart-{device_label}.stderr",
            )
            _ = runner.stream(
                f"nvme_error_{device_label}",
                ["nvme", "error-log", str(device), "-e", "64", "-o", "json"],
                snapshot_directory / f"nvme-error-{device_label}.json",
                snapshot_directory / f"nvme-error-{device_label}.stderr",
            )


def capture_monitor_sensors(runner: CommandRunner, run_directory: Path) -> None:
    _ = runner.stream(
        "sensors_json",
        ["sensors", "-j"],
        run_directory / f"sensors_{utc_stamp()}.json",
        run_directory / f"sensors_{utc_stamp()}.stderr",
    )
