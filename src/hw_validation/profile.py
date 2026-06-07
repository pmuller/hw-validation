from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import cast
from uuid import uuid4

from hw_validation.console import info
from hw_validation.disk import (
    discover_devices,
    run_disk_audit,
    run_disk_burnin,
    run_disk_monitor,
    validate_burnin_target,
)
from hw_validation.files import write_json, write_text
from hw_validation.filesystem import run_filesystem_scratch
from hw_validation.json_types import JsonObject, JsonValue
from hw_validation.network import run_network_burnin
from hw_validation.paths import ensure_directory, slug
from hw_validation.readiness import run_report
from hw_validation.runner import CommandRunner
from hw_validation.status import ExitCode, ResultStatus, outcome_from_counts
from hw_validation.system_audit import run_system_audit
from hw_validation.system_stress import run_system_stress
from hw_validation.timeutil import elapsed_seconds, utc_now, utc_stamp
from hw_validation.tooling import require_commands
from hw_validation.triage import run_triage


class ProfileName(StrEnum):
    smoke = "smoke"
    standard = "standard"
    acceptance = "acceptance"
    disk_burnin = "disk-burnin"


class ProfileSpeed(StrEnum):
    smoke = "smoke"
    standard = "standard"
    long = "long"


class ProfileStepId(StrEnum):
    system_audit_pre = "system-audit-pre"
    system_stress = "system-stress"
    filesystem_scratch = "filesystem-scratch"
    network_burnin = "network-burnin"
    disk_audit = "disk-audit"
    disk_monitor = "disk-monitor"
    disk_burnin = "disk-burnin"
    system_audit_post = "system-audit-post"


@dataclass(frozen=True, slots=True)
class ProfileTiming:
    stress_phase_duration: str
    filesystem_size: str
    filesystem_runtime: str
    network_duration: str
    disk_monitor_duration: str


@dataclass(frozen=True, slots=True)
class ProfileSettings:
    profile: ProfileName
    speed: ProfileSpeed
    out_root: Path
    parts: tuple[str, ...]
    scratch_path: Path | None
    server: str | None
    interface: str | None
    devices: tuple[str, ...]
    smartctl_type: str | None
    erase_ok: bool
    cleanup: bool
    plan_only: bool
    resume: bool
    all_devices: bool = False


@dataclass(frozen=True, slots=True)
class ProfileStep:
    step_id: ProfileStepId
    part: str
    label: str
    title: str
    command: tuple[str, ...]
    required: bool = True
    destructive: bool = False
    estimated_duration: str = ""
    target_device: str = ""
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProfileStepRun:
    step: ProfileStep
    exit_code: int
    status: ResultStatus
    skipped: bool
    result_path: str
    error: str = ""


@dataclass(frozen=True, slots=True)
class ProfileStepFailure:
    step: ProfileStep
    error: str


class ProfilePreflightError(RuntimeError):
    def __init__(self, failures: tuple[ProfileStepFailure, ...]) -> None:
        self.failures: tuple[ProfileStepFailure, ...] = failures
        super().__init__("; ".join(failure.error for failure in failures))


SPEED_TIMINGS: dict[ProfileSpeed, ProfileTiming] = {
    ProfileSpeed.smoke: ProfileTiming("5m", "1G", "2m", "2m", "2m"),
    ProfileSpeed.standard: ProfileTiming("1h", "20G", "30m", "1h", "1h"),
    ProfileSpeed.long: ProfileTiming("8h", "100G", "2h", "8h", "8h"),
}

DEFAULT_PROFILE_STEPS: dict[ProfileName, tuple[ProfileStepId, ...]] = {
    ProfileName.smoke: (
        ProfileStepId.system_audit_pre,
        ProfileStepId.disk_audit,
        ProfileStepId.disk_monitor,
        ProfileStepId.system_audit_post,
    ),
    ProfileName.standard: (
        ProfileStepId.system_audit_pre,
        ProfileStepId.system_stress,
        ProfileStepId.filesystem_scratch,
        ProfileStepId.network_burnin,
        ProfileStepId.disk_audit,
        ProfileStepId.disk_monitor,
        ProfileStepId.system_audit_post,
    ),
    ProfileName.acceptance: (
        ProfileStepId.system_audit_pre,
        ProfileStepId.system_stress,
        ProfileStepId.filesystem_scratch,
        ProfileStepId.network_burnin,
        ProfileStepId.disk_audit,
        ProfileStepId.disk_monitor,
        ProfileStepId.system_audit_post,
    ),
    ProfileName.disk_burnin: (
        ProfileStepId.disk_audit,
        ProfileStepId.disk_burnin,
    ),
}

PART_EXPANSIONS: dict[str, tuple[ProfileStepId, ...]] = {
    "system": (
        ProfileStepId.system_audit_pre,
        ProfileStepId.system_stress,
        ProfileStepId.system_audit_post,
    ),
    "system-audit": (
        ProfileStepId.system_audit_pre,
        ProfileStepId.system_audit_post,
    ),
    "system-audit-pre": (ProfileStepId.system_audit_pre,),
    "system-audit-post": (ProfileStepId.system_audit_post,),
    "system-stress": (ProfileStepId.system_stress,),
    "filesystem": (ProfileStepId.filesystem_scratch,),
    "filesystem-scratch": (ProfileStepId.filesystem_scratch,),
    "network": (ProfileStepId.network_burnin,),
    "network-burnin": (ProfileStepId.network_burnin,),
    "disk": (ProfileStepId.disk_audit, ProfileStepId.disk_monitor),
    "disk-audit": (ProfileStepId.disk_audit,),
    "disk-monitor": (ProfileStepId.disk_monitor,),
    "disk-burnin": (ProfileStepId.disk_burnin,),
}


def parse_profile_name(profile: str) -> ProfileName:
    try:
        return ProfileName(profile)
    except ValueError:
        raise ValueError(
            "profile must be smoke, standard, acceptance, or disk-burnin"
        ) from None


def parse_profile_speed(speed: str | None, profile: ProfileName) -> ProfileSpeed:
    if speed is None:
        return default_speed(profile)
    try:
        return ProfileSpeed(speed)
    except ValueError:
        raise ValueError("--speed must be smoke, standard, or long") from None


def default_speed(profile: ProfileName) -> ProfileSpeed:
    if profile == ProfileName.smoke:
        return ProfileSpeed.smoke
    if profile == ProfileName.acceptance:
        return ProfileSpeed.long
    return ProfileSpeed.standard


def parse_parts(parts: str | None) -> tuple[str, ...]:
    if parts is None or not parts.strip():
        return ()
    return tuple(part.strip() for part in parts.split(",") if part.strip())


def profile_step_ids(settings: ProfileSettings) -> tuple[ProfileStepId, ...]:
    if not settings.parts:
        return DEFAULT_PROFILE_STEPS[settings.profile]
    selected_steps: list[ProfileStepId] = []
    for part in settings.parts:
        if part not in PART_EXPANSIONS:
            raise ValueError(f"unknown profile part: {part}")
        for step_id in PART_EXPANSIONS[part]:
            if step_id not in selected_steps:
                selected_steps.append(step_id)
    return tuple(selected_steps)


def build_profile_steps(settings: ProfileSettings) -> tuple[ProfileStep, ...]:
    timing = SPEED_TIMINGS[settings.speed]
    steps: list[ProfileStep] = []
    for step_id in profile_step_ids(settings):
        if step_id == ProfileStepId.disk_burnin and settings.all_devices:
            for device in settings.devices:
                steps.append(build_profile_step(settings, timing, step_id, device))
        else:
            steps.append(build_profile_step(settings, timing, step_id))
    return tuple(steps)


def build_profile_step(
    settings: ProfileSettings,
    timing: ProfileTiming,
    step_id: ProfileStepId,
    target_device: str = "",
) -> ProfileStep:
    label = profile_step_label(settings, step_id, target_device)
    out_root_text = str(settings.out_root)
    match step_id:
        case ProfileStepId.system_audit_pre:
            return ProfileStep(
                step_id,
                "system",
                label,
                "Pre-validation system audit",
                (
                    "hw-validation",
                    "system",
                    "audit",
                    "--out-root",
                    out_root_text,
                    "--label",
                    label,
                ),
            )
        case ProfileStepId.system_stress:
            return ProfileStep(
                step_id,
                "system",
                label,
                "CPU, memory, EDAC/RAS/AER, and thermal stress",
                (
                    "hw-validation",
                    "system",
                    "stress",
                    "--out-root",
                    out_root_text,
                    "--label",
                    label,
                    "--phase-duration",
                    timing.stress_phase_duration,
                ),
                estimated_duration=timing.stress_phase_duration,
            )
        case ProfileStepId.filesystem_scratch:
            return ProfileStep(
                step_id,
                "filesystem",
                label,
                "Scratch filesystem write and verify workload",
                filesystem_command(settings, timing, label),
                estimated_duration=timing.filesystem_runtime,
                notes=("Writes inside the scratch directory created for this run.",),
            )
        case ProfileStepId.network_burnin:
            return ProfileStep(
                step_id,
                "network",
                label,
                "iperf3 network burn-in",
                network_command(settings, timing, label),
                estimated_duration=timing.network_duration,
            )
        case ProfileStepId.disk_audit:
            return ProfileStep(
                step_id,
                "disk",
                label,
                "Disk inventory and SMART/NVMe audit",
                disk_audit_command(settings, label),
            )
        case ProfileStepId.disk_monitor:
            return ProfileStep(
                step_id,
                "disk",
                label,
                "Disk telemetry monitor",
                disk_monitor_command(settings, timing, label),
                estimated_duration=timing.disk_monitor_duration,
            )
        case ProfileStepId.disk_burnin:
            disk_burnin_target = target_device or (
                settings.devices[0] if len(settings.devices) == 1 else ""
            )
            return ProfileStep(
                step_id,
                "disk",
                label,
                f"Destructive disk burn-in {disk_burnin_target}".strip(),
                disk_burnin_command(settings, label, disk_burnin_target),
                destructive=True,
                target_device=disk_burnin_target,
                notes=(
                    "True HDD burn-in is pass-bound and can take a week or more on large disks.",
                ),
            )
        case ProfileStepId.system_audit_post:
            return ProfileStep(
                step_id,
                "system",
                label,
                "Post-validation system audit",
                (
                    "hw-validation",
                    "system",
                    "audit",
                    "--out-root",
                    out_root_text,
                    "--label",
                    label,
                ),
            )


def profile_step_label(
    settings: ProfileSettings, step_id: ProfileStepId, target_device: str
) -> str:
    if step_id == ProfileStepId.disk_burnin and settings.all_devices and target_device:
        return (
            f"{settings.profile.value}-{step_id.value}-{slug(Path(target_device).name)}"
        )
    return f"{settings.profile.value}-{step_id.value}"


def filesystem_command(
    settings: ProfileSettings, timing: ProfileTiming, label: str
) -> tuple[str, ...]:
    scratch_path_text = (
        str(settings.scratch_path) if settings.scratch_path else "REQUIRED"
    )
    command = [
        "hw-validation",
        "filesystem",
        "scratch",
        "--path",
        scratch_path_text,
        "--out-root",
        str(settings.out_root),
        "--label",
        label,
        "--size",
        timing.filesystem_size,
        "--runtime",
        timing.filesystem_runtime,
    ]
    if settings.cleanup:
        command.append("--cleanup")
    return tuple(command)


def network_command(
    settings: ProfileSettings, timing: ProfileTiming, label: str
) -> tuple[str, ...]:
    command = [
        "hw-validation",
        "network",
        "burnin",
        "--server",
        settings.server or "REQUIRED",
        "--out-root",
        str(settings.out_root),
        "--label",
        label,
        "--duration",
        timing.network_duration,
    ]
    if settings.interface:
        command.extend(("--interface", settings.interface))
    return tuple(command)


def disk_audit_command(settings: ProfileSettings, label: str) -> tuple[str, ...]:
    command = [
        "hw-validation",
        "disk",
        "audit",
        "--out-root",
        str(settings.out_root),
        "--label",
        label,
    ]
    if settings.devices:
        for device in settings.devices:
            command.extend(("--device", device))
    else:
        command.append("--all")
    if settings.smartctl_type:
        command.extend(("--smartctl-type", settings.smartctl_type))
    return tuple(command)


def disk_monitor_command(
    settings: ProfileSettings, timing: ProfileTiming, label: str
) -> tuple[str, ...]:
    command = [
        "hw-validation",
        "disk",
        "monitor",
        "--out-root",
        str(settings.out_root),
        "--label",
        label,
        "--duration",
        timing.disk_monitor_duration,
    ]
    for device in settings.devices:
        command.extend(("--device", device))
    if settings.smartctl_type:
        command.extend(("--smartctl-type", settings.smartctl_type))
    return tuple(command)


def disk_burnin_command(
    settings: ProfileSettings, label: str, target_device: str
) -> tuple[str, ...]:
    command = [
        "hw-validation",
        "disk",
        "burnin",
        "--device",
        target_device or "REQUIRED",
        "--out-root",
        str(settings.out_root),
        "--label",
        label,
    ]
    if settings.smartctl_type:
        command.extend(("--smartctl-type", settings.smartctl_type))
    if settings.erase_ok:
        command.append("--i-know-this-erases-data")
    return tuple(command)


def profile_manifest(
    settings: ProfileSettings, steps: tuple[ProfileStep, ...], profile_run_id: str
) -> JsonObject:
    return {
        "manifest_version": 1,
        "created_at": utc_now(),
        "profile_run_id": profile_run_id,
        "profile": settings.profile.value,
        "speed": settings.speed.value,
        "out_root": str(settings.out_root),
        "parts": [part for part in settings.parts],
        "devices": [device for device in settings.devices],
        "all_devices": settings.all_devices,
        "plan_only": settings.plan_only,
        "resume": settings.resume,
        "reports": {
            "triage_out_root": str(logs_triage_directory(settings.out_root)),
            "readiness_out_root": str(readiness_directory(settings.out_root)),
        },
        "steps": [profile_step_to_json(step) for step in steps],
    }


def profile_step_to_json(step: ProfileStep) -> JsonObject:
    payload: JsonObject = {
        "id": step.step_id.value,
        "part": step.part,
        "label": step.label,
        "profile_step_fingerprint": profile_step_fingerprint(step),
        "title": step.title,
        "command": [cast(JsonValue, command_part) for command_part in step.command],
        "required": step.required,
        "destructive": step.destructive,
        "estimated_duration": step.estimated_duration,
        "notes": [note for note in step.notes],
    }
    if step.target_device:
        payload["target_device"] = step.target_device
    return payload


def validate_profile_settings(
    settings: ProfileSettings, steps: tuple[ProfileStep, ...]
) -> None:
    step_ids = {step.step_id for step in steps}
    if settings.all_devices and ProfileStepId.disk_burnin not in step_ids:
        raise ValueError("--all-devices is only valid when disk-burnin is selected")
    if ProfileStepId.filesystem_scratch in step_ids and settings.scratch_path is None:
        raise ValueError(
            "--scratch-path is required when filesystem validation is selected"
        )
    if ProfileStepId.network_burnin in step_ids and not settings.server:
        raise ValueError("--server is required when network validation is selected")
    if ProfileStepId.disk_burnin in step_ids:
        if settings.all_devices:
            if not settings.devices:
                raise ValueError("--all-devices discovered no eligible disks")
        elif len(settings.devices) != 1:
            raise ValueError(
                "disk-burnin requires exactly one --device or --all-devices"
            )
        if not settings.plan_only and not settings.erase_ok:
            raise ValueError("disk-burnin requires --i-know-this-erases-data")


def resolve_profile_settings(settings: ProfileSettings) -> ProfileSettings:
    step_ids = set(profile_step_ids(settings))
    if not settings.all_devices:
        return settings
    if settings.resume:
        raise ValueError("--resume cannot be combined with --all-devices")
    if settings.devices:
        raise ValueError("--all-devices cannot be combined with --device")
    if ProfileStepId.disk_burnin not in step_ids:
        raise ValueError("--all-devices is only valid when disk-burnin is selected")
    discovered_devices = discover_profile_burnin_devices(settings.out_root)
    if not discovered_devices:
        raise ValueError("--all-devices discovered no eligible disks")
    return replace(
        settings,
        devices=discovered_devices,
    )


def discover_profile_burnin_devices(out_root: Path) -> tuple[str, ...]:
    require_commands(("lsblk",))
    discovery_directory = out_root / "profile-discovery" / f"{utc_stamp()}_all-devices"
    ensure_directory(discovery_directory)
    return tuple(
        str(device)
        for device in discover_devices(
            CommandRunner(discovery_directory), (), True, False, False
        )
    )


def run_profile(settings: ProfileSettings) -> int:
    settings.out_root.mkdir(parents=True, exist_ok=True)
    started_monotonic = time.monotonic()
    resolved_settings = resolve_profile_settings(settings)
    profile_run_id = f"{resolved_settings.profile.value}-{uuid4().hex}"
    steps = build_profile_steps(resolved_settings)
    validate_profile_settings(resolved_settings, steps)
    manifest = profile_manifest(resolved_settings, steps, profile_run_id)
    write_json(profile_manifest_path(resolved_settings.out_root), manifest)
    write_text(
        resolved_settings.out_root / "profile_plan.md",
        profile_plan_markdown(resolved_settings, steps),
    )
    print_profile_plan(resolved_settings, steps)
    if resolved_settings.plan_only:
        write_profile_plan_only_reports(
            resolved_settings, manifest, steps, started_monotonic
        )
        return ExitCode.pass_status.code
    try:
        preflight_profile_burnin_targets(resolved_settings, steps)
    except ProfilePreflightError as error:
        step_runs = preflight_failed_profile_step_runs(steps, error.failures)
    else:
        step_runs = run_profile_steps(resolved_settings, steps, profile_run_id)
    triage_exit_code = run_triage(
        resolved_settings.out_root, logs_triage_directory(resolved_settings.out_root)
    )
    readiness_exit_code = run_report(
        resolved_settings.out_root, readiness_directory(resolved_settings.out_root)
    )
    profile_exit_code = profile_exit_code_from_runs(
        step_runs, triage_exit_code, readiness_exit_code
    )
    write_profile_reports(
        resolved_settings,
        manifest,
        step_runs,
        triage_exit_code,
        readiness_exit_code,
        profile_exit_code,
        elapsed_seconds(started_monotonic),
    )
    return profile_exit_code


def print_profile_plan(
    settings: ProfileSettings, steps: tuple[ProfileStep, ...]
) -> None:
    info(f"PROFILE {settings.profile.value}: speed={settings.speed.value}")
    for step_number, step in enumerate(steps, start=1):
        destructive_text = " destructive" if step.destructive else ""
        duration_text = (
            f" duration={step.estimated_duration}" if step.estimated_duration else ""
        )
        info(
            f"PROFILE step {step_number}: {step.label}{duration_text}{destructive_text}"
        )


def run_profile_steps(
    settings: ProfileSettings, steps: tuple[ProfileStep, ...], profile_run_id: str
) -> list[ProfileStepRun]:
    step_runs: list[ProfileStepRun] = []
    step_index = 0
    while step_index < len(steps):
        step = steps[step_index]
        if settings.all_devices and step.step_id == ProfileStepId.disk_burnin:
            disk_burnin_steps = consecutive_disk_burnin_steps(steps, step_index)
            step_runs.extend(
                run_parallel_profile_steps(settings, disk_burnin_steps, profile_run_id)
            )
            step_index += len(disk_burnin_steps)
            continue
        step_runs.append(run_profile_step(settings, step, profile_run_id))
        step_index += 1
    return step_runs


def consecutive_disk_burnin_steps(
    steps: tuple[ProfileStep, ...], start_index: int
) -> tuple[ProfileStep, ...]:
    disk_burnin_steps: list[ProfileStep] = []
    for step in steps[start_index:]:
        if step.step_id != ProfileStepId.disk_burnin:
            break
        disk_burnin_steps.append(step)
    return tuple(disk_burnin_steps)


def run_parallel_profile_steps(
    settings: ProfileSettings, steps: tuple[ProfileStep, ...], profile_run_id: str
) -> list[ProfileStepRun]:
    if not steps:
        return []
    info(f"PROFILE RUN disk-burnin: {len(steps)} device(s) in parallel")
    with ThreadPoolExecutor(max_workers=len(steps)) as executor:
        submitted_steps = [
            (step, executor.submit(run_profile_step, settings, step, profile_run_id))
            for step in steps
        ]
        step_runs: list[ProfileStepRun] = []
        for step, future in submitted_steps:
            try:
                step_runs.append(future.result())
            except Exception as error:
                step_runs.append(failed_profile_step_run(step, str(error)))
        return step_runs


def run_profile_step(
    settings: ProfileSettings, step: ProfileStep, profile_run_id: str
) -> ProfileStepRun:
    if settings.resume and step_has_passed(settings.out_root, step):
        annotate_profile_step_result(settings.out_root, step, profile_run_id, True)
        info(f"SKIP {step.label}: existing PASS result")
        return skipped_profile_step_run(settings.out_root, step, profile_run_id)
    info(f"PROFILE RUN {step.label}: {step.title}")
    exit_code = execute_profile_step(settings, step)
    annotate_profile_step_result(settings.out_root, step, profile_run_id, False)
    return profile_step_run(settings.out_root, step, exit_code, False, profile_run_id)


def preflight_profile_burnin_targets(
    settings: ProfileSettings, steps: tuple[ProfileStep, ...]
) -> None:
    if not settings.all_devices:
        return
    failures: list[ProfileStepFailure] = []
    for step in steps:
        if step.step_id == ProfileStepId.disk_burnin:
            try:
                preflight_profile_burnin_target(settings.out_root, step)
            except (OSError, RuntimeError, ValueError) as error:
                failures.append(ProfileStepFailure(step, str(error)))
    if failures:
        raise ProfilePreflightError(tuple(failures))


def preflight_profile_burnin_target(out_root: Path, step: ProfileStep) -> None:
    if not step.target_device:
        raise ValueError("disk-burnin requires exactly one --device or --all-devices")
    require_commands(("lsblk",))
    preflight_directory = (
        out_root / "profile-preflight" / f"{utc_stamp()}_{slug(step.label)}"
    )
    ensure_directory(preflight_directory)
    validate_burnin_target(
        CommandRunner(preflight_directory),
        Path(step.target_device).expanduser().resolve(strict=True),
    )


def execute_profile_step(settings: ProfileSettings, step: ProfileStep) -> int:
    timing = SPEED_TIMINGS[settings.speed]
    match step.step_id:
        case ProfileStepId.system_audit_pre | ProfileStepId.system_audit_post:
            return run_system_audit(settings.out_root, step.label)
        case ProfileStepId.system_stress:
            return run_system_stress(
                settings.out_root,
                step.label,
                timing.stress_phase_duration,
                75,
                None,
                False,
                False,
            )
        case ProfileStepId.filesystem_scratch:
            if settings.scratch_path is None:
                raise ValueError(
                    "--scratch-path is required when filesystem validation is selected"
                )
            return run_filesystem_scratch(
                settings.scratch_path,
                settings.out_root,
                step.label,
                timing.filesystem_size,
                timing.filesystem_runtime,
                settings.cleanup,
            )
        case ProfileStepId.network_burnin:
            if settings.server is None:
                raise ValueError(
                    "--server is required when network validation is selected"
                )
            return run_network_burnin(
                settings.server,
                settings.out_root,
                step.label,
                settings.interface,
                timing.network_duration,
                1,
                False,
                None,
            )
        case ProfileStepId.disk_audit:
            return run_disk_audit(
                settings.out_root,
                step.label,
                settings.devices,
                not settings.devices,
                False,
                False,
                False,
                settings.smartctl_type,
            )
        case ProfileStepId.disk_monitor:
            return run_disk_monitor(
                settings.out_root,
                step.label,
                settings.devices,
                "30s",
                "5m",
                "5m",
                timing.disk_monitor_duration,
                False,
                settings.smartctl_type,
                True,
                True,
            )
        case ProfileStepId.disk_burnin:
            if not step.target_device:
                raise ValueError(
                    "disk-burnin requires exactly one --device or --all-devices"
                )
            return run_disk_burnin(
                settings.out_root,
                step.label,
                step.target_device,
                settings.erase_ok,
                False,
                "auto",
                "badblocks",
                settings.smartctl_type,
                "1M",
                1,
                "60m",
                "30m",
                1,
                False,
                False,
            )


def profile_step_run(
    out_root: Path,
    step: ProfileStep,
    exit_code: int,
    skipped: bool,
    profile_run_id: str,
) -> ProfileStepRun:
    result_path = result_path_for_profile_run_step(out_root, step, profile_run_id)
    return ProfileStepRun(
        step,
        exit_code,
        result_status_for_exit_code(exit_code),
        skipped,
        str(result_path) if result_path else "",
    )


def skipped_profile_step_run(
    out_root: Path, step: ProfileStep, profile_run_id: str
) -> ProfileStepRun:
    result_path = result_path_for_profile_run_step(out_root, step, profile_run_id)
    return ProfileStepRun(
        step,
        ExitCode.pass_status.code,
        ResultStatus.pass_status,
        True,
        str(result_path) if result_path else "",
    )


def failed_profile_step_run(step: ProfileStep, error: str) -> ProfileStepRun:
    return ProfileStepRun(
        step,
        ExitCode.hard_failure.code,
        ResultStatus.fail,
        False,
        "",
        error,
    )


def blocked_profile_step_run(step: ProfileStep, error: str) -> ProfileStepRun:
    return ProfileStepRun(
        step,
        ExitCode.warning.code,
        ResultStatus.warn,
        True,
        "",
        error,
    )


def preflight_failed_profile_step_runs(
    steps: tuple[ProfileStep, ...], failures: tuple[ProfileStepFailure, ...]
) -> list[ProfileStepRun]:
    failures_by_label = {failure.step.label: failure.error for failure in failures}
    return [
        failed_profile_step_run(
            step, f"preflight failed: {failures_by_label[step.label]}"
        )
        if step.label in failures_by_label
        else blocked_profile_step_run(
            step, "not run because disk burn-in preflight failed"
        )
        for step in steps
    ]


def result_status_for_exit_code(exit_code: int) -> ResultStatus:
    if exit_code == ExitCode.pass_status.code:
        return ResultStatus.pass_status
    if exit_code == ExitCode.warning.code:
        return ResultStatus.warn
    return ResultStatus.fail


def profile_step_fingerprint(step: ProfileStep) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "id": step.step_id.value,
                "part": step.part,
                "label": step.label,
                "command": [command_part for command_part in step.command],
                "required": step.required,
                "destructive": step.destructive,
                "target_device": step.target_device,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def annotate_profile_step_result(
    out_root: Path, step: ProfileStep, profile_run_id: str, resumed: bool
) -> None:
    result_path = (
        result_path_for_step(out_root, step)
        if resumed
        else result_path_for_label(out_root, step.label)
    )
    if result_path is None:
        return
    payload = load_result_payload(result_path)
    if payload is None:
        return
    payload_json = cast(JsonObject, payload)
    payload_json["profile_step_id"] = step.step_id.value
    payload_json["profile_step_fingerprint"] = profile_step_fingerprint(step)
    payload_json["profile_run_id"] = profile_run_id
    payload_json["profile_step_resumed"] = resumed
    payload_json["profile_step_command"] = [
        cast(JsonValue, command_part) for command_part in step.command
    ]
    write_json(result_path, payload_json)


def result_path_for_step(out_root: Path, step: ProfileStep) -> Path | None:
    result_paths: list[Path] = []
    for result_path in sorted(out_root.rglob("result.json")):
        payload = load_result_payload(result_path)
        if payload is not None and payload_matches_step(payload, step):
            result_paths.append(result_path)
    return result_paths[-1] if result_paths else None


def result_path_for_profile_run_step(
    out_root: Path, step: ProfileStep, profile_run_id: str
) -> Path | None:
    result_paths: list[Path] = []
    for result_path in sorted(out_root.rglob("result.json")):
        payload = load_result_payload(result_path)
        if (
            payload is not None
            and payload_matches_step(payload, step)
            and payload.get("profile_run_id") == profile_run_id
        ):
            result_paths.append(result_path)
    return result_paths[-1] if result_paths else None


def result_path_for_label(out_root: Path, label: str) -> Path | None:
    result_paths: list[Path] = []
    for result_path in sorted(out_root.rglob("result.json")):
        payload_object = load_result_payload(result_path)
        if payload_object is None:
            continue
        if payload_object.get("label") == label:
            result_paths.append(result_path)
    return result_paths[-1] if result_paths else None


def step_has_passed(out_root: Path, step: ProfileStep) -> bool:
    result_path = result_path_for_step(out_root, step)
    if result_path is None:
        return False
    payload = load_result_payload(result_path)
    if payload is None:
        return False
    return (
        payload.get("status") == ResultStatus.pass_status.value
        and payload.get("result") == ResultStatus.pass_status.value
        and payload.get("exit_code") == ExitCode.pass_status.code
        and payload.get("failures") == 0
        and payload.get("warnings") == 0
    )


def payload_matches_step(payload: dict[str, object], step: ProfileStep) -> bool:
    return (
        payload.get("label") == step.label
        and payload.get("profile_step_id") == step.step_id.value
        and payload.get("profile_step_fingerprint") == profile_step_fingerprint(step)
    )


def load_result_payload(result_path: Path) -> dict[str, object] | None:
    try:
        payload = cast(object, json.loads(result_path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return cast(dict[str, object], payload)


def profile_exit_code_from_runs(
    step_runs: Sequence[ProfileStepRun], triage_exit_code: int, readiness_exit_code: int
) -> int:
    exit_codes = [step_run.exit_code for step_run in step_runs]
    exit_codes.extend((triage_exit_code, readiness_exit_code))
    if any(exit_code == ExitCode.hard_failure.code for exit_code in exit_codes):
        return ExitCode.hard_failure.code
    if any(exit_code == ExitCode.warning.code for exit_code in exit_codes):
        return ExitCode.warning.code
    return ExitCode.pass_status.code


def write_profile_plan_only_reports(
    settings: ProfileSettings,
    manifest: JsonObject,
    steps: tuple[ProfileStep, ...],
    started_monotonic: float,
) -> None:
    profile_exit_code = ExitCode.pass_status.code
    write_profile_reports(
        settings,
        manifest,
        [
            ProfileStepRun(step, profile_exit_code, ResultStatus.pass_status, True, "")
            for step in steps
        ],
        profile_exit_code,
        profile_exit_code,
        profile_exit_code,
        elapsed_seconds(started_monotonic),
    )


def write_profile_reports(
    settings: ProfileSettings,
    manifest: JsonObject,
    step_runs: Sequence[ProfileStepRun],
    triage_exit_code: int,
    readiness_exit_code: int,
    profile_exit_code: int,
    duration_seconds: float,
) -> None:
    failures = sum(
        1
        for exit_code in [step_run.exit_code for step_run in step_runs]
        + [triage_exit_code, readiness_exit_code]
        if exit_code == ExitCode.hard_failure.code
    )
    warnings = sum(
        1
        for exit_code in [step_run.exit_code for step_run in step_runs]
        + [triage_exit_code, readiness_exit_code]
        if exit_code == ExitCode.warning.code
    )
    outcome = outcome_from_counts(failures, warnings)
    if profile_exit_code == ExitCode.pass_status.code:
        outcome = outcome_from_counts(0, 0)
    payload: JsonObject = {
        "result_type": "profile",
        "status": outcome.status.value,
        "result": outcome.status.value,
        "exit_code": profile_exit_code,
        "failures": failures,
        "warnings": warnings,
        "label": f"profile-{settings.profile.value}",
        "profile_run_id": manifest.get("profile_run_id", ""),
        "profile": settings.profile.value,
        "speed": settings.speed.value,
        "started_at": manifest.get("created_at", ""),
        "ended_at": utc_now(),
        "duration_seconds": duration_seconds,
        "completed_reason": "plan-only" if settings.plan_only else "completed",
        "manifest_path": str(profile_manifest_path(settings.out_root)),
        "triage_exit_code": triage_exit_code,
        "readiness_exit_code": readiness_exit_code,
        "steps": [profile_step_run_to_json(step_run) for step_run in step_runs],
    }
    write_json(settings.out_root / "report.json", payload)
    write_json(settings.out_root / "result.json", payload)
    write_text(settings.out_root / "report.md", profile_report_markdown(payload))
    write_text(settings.out_root / "summary.txt", profile_summary_text(payload))


def profile_step_run_to_json(step_run: ProfileStepRun) -> JsonObject:
    payload: JsonObject = {
        "id": step_run.step.step_id.value,
        "label": step_run.step.label,
        "title": step_run.step.title,
        "status": step_run.status.value,
        "exit_code": step_run.exit_code,
        "skipped": step_run.skipped,
        "result_path": step_run.result_path,
        "destructive": step_run.step.destructive,
    }
    if step_run.step.target_device:
        payload["target_device"] = step_run.step.target_device
    if step_run.error:
        payload["error"] = step_run.error
    return payload


def profile_plan_markdown(
    settings: ProfileSettings, steps: tuple[ProfileStep, ...]
) -> str:
    lines = [
        "# Hardware Validation Profile Plan",
        "",
        f"Profile: {settings.profile.value}",
        f"Speed: {settings.speed.value}",
        f"Output root: {settings.out_root}",
        "",
        "| Step | Part | Label | Required | Destructive | Duration | Command |",
        "|---:|---|---|---|---|---|---|",
    ]
    for step_number, step in enumerate(steps, start=1):
        lines.append(
            "| "
            + " | ".join(
                [
                    str(step_number),
                    markdown_cell(step.part),
                    markdown_cell(step.label),
                    str(step.required).lower(),
                    str(step.destructive).lower(),
                    markdown_cell(step.estimated_duration),
                    markdown_cell(" ".join(step.command)),
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def profile_report_markdown(payload: JsonObject) -> str:
    step_runs = cast(list[JsonObject], payload["steps"])
    lines = [
        "# Hardware Validation Profile Report",
        "",
        f"Status: {payload['status']}",
        f"Profile: {payload['profile']}",
        f"Speed: {payload['speed']}",
        f"Duration seconds: {payload['duration_seconds']}",
        "",
        "| Status | Step | Exit Code | Skipped | Result |",
        "|---|---|---:|---|---|",
    ]
    for step_run in step_runs:
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_cell(str(step_run.get("status", ""))),
                    markdown_cell(str(step_run.get("label", ""))),
                    str(step_run.get("exit_code", "")),
                    str(step_run.get("skipped", "")),
                    markdown_cell(str(step_run.get("result_path", ""))),
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def profile_summary_text(payload: JsonObject) -> str:
    return (
        f"RESULT={payload['status']}\n"
        f"profile={payload['profile']}\n"
        f"speed={payload['speed']}\n"
        f"exit_code={payload['exit_code']}\n"
        f"manifest={payload.get('manifest_path', '')}\n"
    )


def markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")[:500]


def profile_manifest_path(out_root: Path) -> Path:
    return out_root / "profile_manifest.json"


def logs_triage_directory(out_root: Path) -> Path:
    return out_root / "logs-triage"


def readiness_directory(out_root: Path) -> Path:
    return out_root / "readiness-report"
