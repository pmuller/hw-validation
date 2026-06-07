from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import cast

import pytest

import hw_validation.profile as profile_module
from hw_validation.json_types import JsonObject
from hw_validation.profile import (
    ProfileName,
    ProfileSettings,
    ProfileSpeed,
    ProfileStep,
    ProfileStepId,
    build_profile_steps,
    parse_parts,
    run_profile,
)


def test_smoke_plan_only_writes_manifest_and_reports() -> None:
    with tempfile.TemporaryDirectory() as directory_text:
        out_root = Path(directory_text)
        assert run_profile(smoke_settings(out_root, plan_only=True)) == 0
        assert json_object(out_root / "profile_manifest.json")["profile"] == "smoke"
        assert json_object(out_root / "result.json") == json_object(
            out_root / "report.json"
        )
        assert json_object(out_root / "result.json")["completed_reason"] == "plan-only"
        assert (out_root / "profile_plan.md").exists()
        assert (
            (out_root / "summary.txt")
            .read_text(encoding="utf-8")
            .startswith("RESULT=PASS\n")
        )


def test_standard_profile_requires_external_inputs() -> None:
    with (
        tempfile.TemporaryDirectory() as directory_text,
        pytest.raises(
            ValueError,
            match="--scratch-path is required when filesystem validation is selected",
        ),
    ):
        _ = run_profile(
            ProfileSettings(
                profile=ProfileName.standard,
                speed=ProfileSpeed.standard,
                out_root=Path(directory_text),
                parts=(),
                scratch_path=None,
                server=None,
                interface=None,
                devices=(),
                smartctl_type=None,
                erase_ok=False,
                cleanup=True,
                plan_only=True,
                resume=False,
            )
        )


def test_disk_burnin_profile_requires_device_even_for_plan_only() -> None:
    with (
        tempfile.TemporaryDirectory() as directory_text,
        pytest.raises(
            ValueError, match="requires exactly one --device or --all-devices"
        ),
    ):
        _ = run_profile(
            ProfileSettings(
                profile=ProfileName.disk_burnin,
                speed=ProfileSpeed.standard,
                out_root=Path(directory_text),
                parts=(),
                scratch_path=None,
                server=None,
                interface=None,
                devices=(),
                smartctl_type=None,
                erase_ok=False,
                cleanup=True,
                plan_only=True,
                resume=False,
            )
        )


def test_disk_burnin_all_devices_rejects_explicit_devices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def discover_profile_burnin_devices(out_root: Path) -> tuple[str, ...]:
        _ = out_root
        raise AssertionError("discovery should not run")

    monkeypatch.setattr(
        profile_module,
        "discover_profile_burnin_devices",
        discover_profile_burnin_devices,
    )
    with (
        tempfile.TemporaryDirectory() as directory_text,
        pytest.raises(
            ValueError, match="--all-devices cannot be combined with --device"
        ),
    ):
        _ = run_profile(
            ProfileSettings(
                profile=ProfileName.disk_burnin,
                speed=ProfileSpeed.standard,
                out_root=Path(directory_text),
                parts=(),
                scratch_path=None,
                server=None,
                interface=None,
                devices=("/dev/sda",),
                smartctl_type=None,
                erase_ok=False,
                cleanup=True,
                plan_only=True,
                resume=False,
                all_devices=True,
            )
        )


def test_disk_burnin_all_devices_rejects_resume() -> None:
    with (
        tempfile.TemporaryDirectory() as directory_text,
        pytest.raises(
            ValueError, match="--resume cannot be combined with --all-devices"
        ),
    ):
        _ = run_profile(
            ProfileSettings(
                profile=ProfileName.disk_burnin,
                speed=ProfileSpeed.standard,
                out_root=Path(directory_text),
                parts=(),
                scratch_path=None,
                server=None,
                interface=None,
                devices=(),
                smartctl_type=None,
                erase_ok=False,
                cleanup=True,
                plan_only=True,
                resume=True,
                all_devices=True,
            )
        )


def test_disk_burnin_all_devices_plan_expands_discovered_devices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def discover_profile_burnin_devices(out_root: Path) -> tuple[str, ...]:
        _ = out_root
        return ("/dev/sda", "/dev/nvme0n1")

    monkeypatch.setattr(
        profile_module,
        "discover_profile_burnin_devices",
        discover_profile_burnin_devices,
    )
    with tempfile.TemporaryDirectory() as directory_text:
        out_root = Path(directory_text)
        assert (
            run_profile(
                ProfileSettings(
                    profile=ProfileName.disk_burnin,
                    speed=ProfileSpeed.standard,
                    out_root=out_root,
                    parts=(),
                    scratch_path=None,
                    server=None,
                    interface=None,
                    devices=(),
                    smartctl_type=None,
                    erase_ok=False,
                    cleanup=True,
                    plan_only=True,
                    resume=False,
                    all_devices=True,
                )
            )
            == 0
        )
        steps = cast(
            list[JsonObject], json_object(out_root / "profile_manifest.json")["steps"]
        )
        assert [step["label"] for step in steps] == [
            "disk-burnin-disk-audit",
            "disk-burnin-disk-burnin-sda",
            "disk-burnin-disk-burnin-nvme0n1",
        ]
        assert [step.get("target_device", "") for step in steps] == [
            "",
            "/dev/sda",
            "/dev/nvme0n1",
        ]


def test_disk_burnin_all_devices_reports_empty_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def discover_profile_burnin_devices(out_root: Path) -> tuple[str, ...]:
        _ = out_root
        return ()

    monkeypatch.setattr(
        profile_module,
        "discover_profile_burnin_devices",
        discover_profile_burnin_devices,
    )
    with (
        tempfile.TemporaryDirectory() as directory_text,
        pytest.raises(ValueError, match="--all-devices discovered no eligible disks"),
    ):
        _ = run_profile(
            ProfileSettings(
                profile=ProfileName.disk_burnin,
                speed=ProfileSpeed.standard,
                out_root=Path(directory_text),
                parts=(),
                scratch_path=None,
                server=None,
                interface=None,
                devices=(),
                smartctl_type=None,
                erase_ok=False,
                cleanup=True,
                plan_only=True,
                resume=False,
                all_devices=True,
            )
        )


def test_disk_burnin_all_devices_runs_each_discovered_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as directory_text:
        out_root = Path(directory_text)
        calls: list[tuple[str, str]] = []

        def discover_profile_burnin_devices(out_root: Path) -> tuple[str, ...]:
            _ = out_root
            return ("/dev/sda", "/dev/sdb")

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
            _ = (
                erase_ok,
                dry_run,
                kind,
                hdd_method,
                smartctl_type,
                fio_block_size,
                ssd_full_passes,
                ssd_randread_duration,
                hdd_randread_duration,
                hdd_fio_passes,
                skip_randread,
                skip_selftests,
            )
            calls.append((label, device))
            write_component_result(out_root, label, directory_name=label)
            return 0

        monkeypatch.setattr(
            profile_module,
            "discover_profile_burnin_devices",
            discover_profile_burnin_devices,
        )
        monkeypatch.setattr(profile_module, "run_disk_burnin", run_disk_burnin)
        monkeypatch.setattr(
            profile_module, "preflight_profile_burnin_targets", pass_preflight
        )
        monkeypatch.setattr(profile_module, "run_triage", pass_report)
        monkeypatch.setattr(profile_module, "run_report", pass_report)
        assert (
            run_profile(
                ProfileSettings(
                    profile=ProfileName.disk_burnin,
                    speed=ProfileSpeed.standard,
                    out_root=out_root,
                    parts=("disk-burnin",),
                    scratch_path=None,
                    server=None,
                    interface=None,
                    devices=(),
                    smartctl_type=None,
                    erase_ok=True,
                    cleanup=True,
                    plan_only=False,
                    resume=False,
                    all_devices=True,
                )
            )
            == 0
        )
        assert set(calls) == {
            ("disk-burnin-disk-burnin-sda", "/dev/sda"),
            ("disk-burnin-disk-burnin-sdb", "/dev/sdb"),
        }


def test_disk_burnin_all_devices_preflight_failure_writes_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as directory_text:
        out_root = Path(directory_text)

        def discover_profile_burnin_devices(out_root: Path) -> tuple[str, ...]:
            _ = out_root
            return ("/dev/sda",)

        def preflight_profile_burnin_target(out_root: Path, step: ProfileStep) -> None:
            _ = out_root
            if step.target_device == "/dev/sda":
                raise ValueError("mounted descendants: /dev/sda1")

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
            _ = (
                out_root,
                label,
                device,
                erase_ok,
                dry_run,
                kind,
                hdd_method,
                smartctl_type,
                fio_block_size,
                ssd_full_passes,
                ssd_randread_duration,
                hdd_randread_duration,
                hdd_fio_passes,
                skip_randread,
                skip_selftests,
            )
            raise AssertionError("burn-in should not start after preflight failure")

        monkeypatch.setattr(
            profile_module,
            "discover_profile_burnin_devices",
            discover_profile_burnin_devices,
        )
        monkeypatch.setattr(
            profile_module,
            "preflight_profile_burnin_target",
            preflight_profile_burnin_target,
        )
        monkeypatch.setattr(profile_module, "run_disk_burnin", run_disk_burnin)
        monkeypatch.setattr(profile_module, "run_triage", pass_report)
        monkeypatch.setattr(profile_module, "run_report", pass_report)
        assert (
            run_profile(
                ProfileSettings(
                    profile=ProfileName.disk_burnin,
                    speed=ProfileSpeed.standard,
                    out_root=out_root,
                    parts=("disk-burnin",),
                    scratch_path=None,
                    server=None,
                    interface=None,
                    devices=(),
                    smartctl_type=None,
                    erase_ok=True,
                    cleanup=True,
                    plan_only=False,
                    resume=False,
                    all_devices=True,
                )
            )
            == 1
        )
        steps = cast(list[JsonObject], json_object(out_root / "report.json")["steps"])
        assert steps[0]["status"] == "FAIL"
        assert "preflight failed: mounted descendants" in str(steps[0]["error"])


def test_disk_burnin_all_devices_preflight_failure_blocks_unfailed_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as directory_text:
        out_root = Path(directory_text)

        def discover_profile_burnin_devices(out_root: Path) -> tuple[str, ...]:
            _ = out_root
            return ("/dev/sda", "/dev/sdb")

        def preflight_profile_burnin_target(out_root: Path, step: ProfileStep) -> None:
            _ = out_root
            if step.target_device == "/dev/sdb":
                raise ValueError("mounted descendants: /dev/sdb1")

        monkeypatch.setattr(
            profile_module,
            "discover_profile_burnin_devices",
            discover_profile_burnin_devices,
        )
        monkeypatch.setattr(
            profile_module,
            "preflight_profile_burnin_target",
            preflight_profile_burnin_target,
        )
        monkeypatch.setattr(profile_module, "run_triage", pass_report)
        monkeypatch.setattr(profile_module, "run_report", pass_report)
        assert (
            run_profile(
                ProfileSettings(
                    profile=ProfileName.disk_burnin,
                    speed=ProfileSpeed.standard,
                    out_root=out_root,
                    parts=(),
                    scratch_path=None,
                    server=None,
                    interface=None,
                    devices=(),
                    smartctl_type=None,
                    erase_ok=True,
                    cleanup=True,
                    plan_only=False,
                    resume=False,
                    all_devices=True,
                )
            )
            == 1
        )
        steps = cast(list[JsonObject], json_object(out_root / "report.json")["steps"])
        assert [(step["label"], step["status"], step["skipped"]) for step in steps] == [
            ("disk-burnin-disk-audit", "WARN", True),
            ("disk-burnin-disk-burnin-sda", "WARN", True),
            ("disk-burnin-disk-burnin-sdb", "FAIL", False),
        ]


def test_disk_burnin_all_devices_records_parallel_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as directory_text:
        out_root = Path(directory_text)

        def discover_profile_burnin_devices(out_root: Path) -> tuple[str, ...]:
            _ = out_root
            return ("/dev/sda", "/dev/sdb")

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
            _ = (
                erase_ok,
                dry_run,
                kind,
                hdd_method,
                smartctl_type,
                fio_block_size,
                ssd_full_passes,
                ssd_randread_duration,
                hdd_randread_duration,
                hdd_fio_passes,
                skip_randread,
                skip_selftests,
            )
            if device == "/dev/sdb":
                raise ValueError("mounted descendants: /dev/sdb1")
            write_component_result(out_root, label, directory_name=label)
            return 0

        monkeypatch.setattr(
            profile_module,
            "discover_profile_burnin_devices",
            discover_profile_burnin_devices,
        )
        monkeypatch.setattr(profile_module, "run_disk_burnin", run_disk_burnin)
        monkeypatch.setattr(
            profile_module, "preflight_profile_burnin_targets", pass_preflight
        )
        monkeypatch.setattr(profile_module, "run_triage", pass_report)
        monkeypatch.setattr(profile_module, "run_report", pass_report)
        assert (
            run_profile(
                ProfileSettings(
                    profile=ProfileName.disk_burnin,
                    speed=ProfileSpeed.standard,
                    out_root=out_root,
                    parts=("disk-burnin",),
                    scratch_path=None,
                    server=None,
                    interface=None,
                    devices=(),
                    smartctl_type=None,
                    erase_ok=True,
                    cleanup=True,
                    plan_only=False,
                    resume=False,
                    all_devices=True,
                )
            )
            == 1
        )
        steps = cast(list[JsonObject], json_object(out_root / "report.json")["steps"])
        assert steps[1]["status"] == "FAIL"
        assert "mounted descendants" in str(steps[1]["error"])


def test_parts_expand_without_duplicates() -> None:
    with tempfile.TemporaryDirectory() as directory_text:
        assert [
            step.step_id
            for step in build_profile_steps(
                ProfileSettings(
                    profile=ProfileName.standard,
                    speed=ProfileSpeed.standard,
                    out_root=Path(directory_text),
                    parts=parse_parts("system,system-stress,disk-audit"),
                    scratch_path=None,
                    server=None,
                    interface=None,
                    devices=(),
                    smartctl_type=None,
                    erase_ok=False,
                    cleanup=True,
                    plan_only=True,
                    resume=False,
                )
            )
        ] == [
            ProfileStepId.system_audit_pre,
            ProfileStepId.system_stress,
            ProfileStepId.system_audit_post,
            ProfileStepId.disk_audit,
        ]


def test_profile_run_executes_selected_step_and_writes_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as directory_text:
        out_root = Path(directory_text)
        calls: list[tuple[Path, str]] = []

        def run_system_audit(out_root: Path, label: str) -> int:
            calls.append((out_root, label))
            write_component_result(out_root, label)
            return 0

        monkeypatch.setattr(profile_module, "run_system_audit", run_system_audit)
        monkeypatch.setattr(profile_module, "run_triage", pass_report)
        monkeypatch.setattr(profile_module, "run_report", pass_report)
        assert (
            run_profile(
                ProfileSettings(
                    profile=ProfileName.smoke,
                    speed=ProfileSpeed.smoke,
                    out_root=out_root,
                    parts=("system-audit-pre",),
                    scratch_path=None,
                    server=None,
                    interface=None,
                    devices=(),
                    smartctl_type=None,
                    erase_ok=False,
                    cleanup=True,
                    plan_only=False,
                    resume=False,
                )
            )
            == 0
        )
        assert calls == [(out_root, "smoke-system-audit-pre")]
        assert json_object(out_root / "report.json")["steps"] == [
            {
                "destructive": False,
                "exit_code": 0,
                "id": "system-audit-pre",
                "label": "smoke-system-audit-pre",
                "result_path": str(out_root / "component" / "result.json"),
                "skipped": False,
                "status": "PASS",
                "title": "Pre-validation system audit",
            }
        ]


def test_profile_resume_skips_matching_pass_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as directory_text:
        out_root = Path(directory_text)
        settings = ProfileSettings(
            profile=ProfileName.smoke,
            speed=ProfileSpeed.smoke,
            out_root=out_root,
            parts=("system-audit-pre",),
            scratch_path=None,
            server=None,
            interface=None,
            devices=(),
            smartctl_type=None,
            erase_ok=False,
            cleanup=True,
            plan_only=False,
            resume=True,
        )
        write_component_result(
            out_root, "smoke-system-audit-pre", build_profile_steps(settings)[0]
        )

        def run_system_audit(out_root: Path, label: str) -> int:
            raise AssertionError(f"unexpected rerun: {out_root} {label}")

        monkeypatch.setattr(profile_module, "run_system_audit", run_system_audit)
        monkeypatch.setattr(profile_module, "run_triage", pass_report)
        monkeypatch.setattr(profile_module, "run_report", pass_report)
        assert run_profile(settings) == 0
        assert (
            json_object(out_root / "component" / "result.json")["profile_run_id"]
            == json_object(out_root / "profile_manifest.json")["profile_run_id"]
        )
        assert (
            json_object(out_root / "component" / "result.json")["profile_step_resumed"]
            is True
        )
        assert (
            cast(list[JsonObject], json_object(out_root / "report.json")["steps"])[0][
                "skipped"
            ]
            is True
        )


def test_profile_resume_reruns_stale_matching_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as directory_text:
        out_root = Path(directory_text)
        calls: list[tuple[Path, str]] = []
        write_component_result(out_root, "smoke-system-audit-pre")

        def run_system_audit(out_root: Path, label: str) -> int:
            calls.append((out_root, label))
            write_component_result(out_root, label)
            return 0

        monkeypatch.setattr(profile_module, "run_system_audit", run_system_audit)
        monkeypatch.setattr(profile_module, "run_triage", pass_report)
        monkeypatch.setattr(profile_module, "run_report", pass_report)
        assert (
            run_profile(
                ProfileSettings(
                    profile=ProfileName.smoke,
                    speed=ProfileSpeed.smoke,
                    out_root=out_root,
                    parts=("system-audit-pre",),
                    scratch_path=None,
                    server=None,
                    interface=None,
                    devices=(),
                    smartctl_type=None,
                    erase_ok=False,
                    cleanup=True,
                    plan_only=False,
                    resume=True,
                )
            )
            == 0
        )
        assert calls == [(out_root, "smoke-system-audit-pre")]


def test_profile_resume_reruns_contradictory_pass_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as directory_text:
        out_root = Path(directory_text)
        calls: list[tuple[Path, str]] = []
        settings = ProfileSettings(
            profile=ProfileName.smoke,
            speed=ProfileSpeed.smoke,
            out_root=out_root,
            parts=("system-audit-pre",),
            scratch_path=None,
            server=None,
            interface=None,
            devices=(),
            smartctl_type=None,
            erase_ok=False,
            cleanup=True,
            plan_only=False,
            resume=True,
        )
        write_component_result(
            out_root,
            "smoke-system-audit-pre",
            build_profile_steps(settings)[0],
            exit_code=1,
        )

        def run_system_audit(out_root: Path, label: str) -> int:
            calls.append((out_root, label))
            write_component_result(out_root, label)
            return 0

        monkeypatch.setattr(profile_module, "run_system_audit", run_system_audit)
        monkeypatch.setattr(profile_module, "run_triage", pass_report)
        monkeypatch.setattr(profile_module, "run_report", pass_report)
        assert run_profile(settings) == 0
        assert calls == [(out_root, "smoke-system-audit-pre")]


def smoke_settings(out_root: Path, plan_only: bool) -> ProfileSettings:
    return ProfileSettings(
        profile=ProfileName.smoke,
        speed=ProfileSpeed.smoke,
        out_root=out_root,
        parts=(),
        scratch_path=None,
        server=None,
        interface=None,
        devices=(),
        smartctl_type=None,
        erase_ok=False,
        cleanup=True,
        plan_only=plan_only,
        resume=False,
    )


def json_object(path: Path) -> JsonObject:
    return cast(JsonObject, json.loads(path.read_text(encoding="utf-8")))


def write_component_result(
    out_root: Path,
    label: str,
    step: ProfileStep | None = None,
    exit_code: int = 0,
    directory_name: str = "component",
) -> None:
    result_directory = out_root / directory_name
    result_directory.mkdir(parents=True, exist_ok=True)
    payload: JsonObject = {
        "status": "PASS",
        "result": "PASS",
        "exit_code": exit_code,
        "failures": 0,
        "warnings": 0,
        "label": label,
    }
    if step is not None:
        payload["profile_step_id"] = step.step_id.value
        payload["profile_step_fingerprint"] = profile_module.profile_step_fingerprint(
            step
        )
    _ = (result_directory / "result.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def pass_report(log_root: Path, out_root: Path) -> int:
    _ = (log_root, out_root)
    return 0


def pass_preflight(settings: ProfileSettings, steps: tuple[ProfileStep, ...]) -> None:
    _ = (settings, steps)
