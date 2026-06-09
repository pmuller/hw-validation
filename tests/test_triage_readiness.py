from __future__ import annotations

import json
import tempfile
from pathlib import Path

from hw_validation.readiness import run_report
from hw_validation.triage import run_triage, triage_candidate


def test_triage_status_matrix() -> None:
    with tempfile.TemporaryDirectory() as directory_text:
        root = Path(directory_text)
        assert (
            run_triage(write_log(root / "pass", "clean log\n"), root / "pass-out") == 0
        )
        assert (
            run_triage(
                write_log(root / "warn", "ECC corrected on DIMM A1\n"),
                root / "warn-out",
            )
            == 2
        )
        assert (
            run_triage(
                write_log(root / "fail", "NVMe timeout on controller\n"),
                root / "fail-out",
            )
            == 1
        )


def test_triage_missing_log_root_writes_failure_result() -> None:
    with tempfile.TemporaryDirectory() as directory_text:
        root = Path(directory_text)
        assert run_triage(root / "missing", root / "out") == 1
        assert json.loads((root / "out" / "result.json").read_text(encoding="utf-8"))[
            "counts_by_pattern"
        ] == {"log_root_error": 1}


def test_readiness_status_matrix() -> None:
    with tempfile.TemporaryDirectory() as directory_text:
        root = Path(directory_text)
        assert (
            run_report(write_result(root / "pass", "PASS", 0), root / "pass-out") == 0
        )
        assert (
            run_report(write_result(root / "warn", "WARN", 2), root / "warn-out") == 2
        )
        assert (
            run_report(write_result(root / "fail", "FAIL", 1), root / "fail-out") == 1
        )


def test_readiness_fails_when_profile_manifest_step_is_missing() -> None:
    with tempfile.TemporaryDirectory() as directory_text:
        root = Path(directory_text)
        write_profile_manifest(root, "missing-system-stress")
        assert run_report(write_result(root, "PASS", 0), root / "readiness") == 1
        assert "missing-system-stress" in (
            root / "readiness" / "readiness_report.md"
        ).read_text(encoding="utf-8")


def test_readiness_requires_matching_profile_step_fingerprint() -> None:
    with tempfile.TemporaryDirectory() as directory_text:
        root = Path(directory_text)
        write_profile_manifest(root, "system-stress", "expected-fingerprint")
        _ = write_result(root, "PASS", 0, label="system-stress")
        assert run_report(root, root / "readiness") == 1
        assert "system-stress" in (
            root / "readiness" / "readiness_report.md"
        ).read_text(encoding="utf-8")


def test_readiness_accepts_matching_profile_manifest_step() -> None:
    with tempfile.TemporaryDirectory() as directory_text:
        root = Path(directory_text)
        write_profile_manifest(root, "system-stress", "expected-fingerprint")
        _ = write_result(
            root,
            "PASS",
            0,
            label="system-stress",
            profile_step_id="system-stress",
            profile_step_fingerprint="expected-fingerprint",
            profile_run_id="current-run",
        )
        assert run_report(root, root / "readiness") == 0


def test_readiness_rejects_matching_step_from_old_profile_run() -> None:
    with tempfile.TemporaryDirectory() as directory_text:
        root = Path(directory_text)
        write_profile_manifest(root, "system-stress", "expected-fingerprint")
        _ = write_result(
            root,
            "PASS",
            0,
            label="system-stress",
            profile_step_id="system-stress",
            profile_step_fingerprint="expected-fingerprint",
            profile_run_id="old-run",
        )
        assert run_report(root, root / "readiness") == 1


def test_readiness_markdown_renders_read_errors() -> None:
    with tempfile.TemporaryDirectory() as directory_text:
        root = Path(directory_text)
        bad_result_directory = root / "bad"
        bad_result_directory.mkdir()
        _ = (bad_result_directory / "result.json").write_text(
            "not json", encoding="utf-8"
        )
        assert run_report(root, root / "readiness") == 1
        assert "Read Errors" in (root / "readiness" / "readiness_report.md").read_text(
            encoding="utf-8"
        )


def test_readiness_reports_corrupt_profile_manifest() -> None:
    with tempfile.TemporaryDirectory() as directory_text:
        root = Path(directory_text)
        _ = (root / "profile_manifest.json").write_text("not json", encoding="utf-8")
        _ = write_result(root, "PASS", 0)
        assert run_report(root, root / "readiness") == 1
        assert "profile_manifest.json" in (
            root / "readiness" / "readiness_report.md"
        ).read_text(encoding="utf-8")


def test_readiness_fails_on_malformed_profile_manifest_steps() -> None:
    with tempfile.TemporaryDirectory() as directory_text:
        root = Path(directory_text)
        _ = (root / "profile_manifest.json").write_text(
            json.dumps({"profile_run_id": "current-run", "steps": "not-a-list"}),
            encoding="utf-8",
        )
        _ = write_result(root, "PASS", 0)
        assert run_report(root, root / "readiness") == 1
        assert "profile_manifest.json has no valid steps list" in (
            root / "readiness" / "readiness_report.md"
        ).read_text(encoding="utf-8")


def test_readiness_fails_on_malformed_profile_manifest_step_entries() -> None:
    with tempfile.TemporaryDirectory() as directory_text:
        root = Path(directory_text)
        _ = (root / "profile_manifest.json").write_text(
            json.dumps(
                {
                    "profile_run_id": "current-run",
                    "steps": ["not-an-object", {"label": "missing-id"}],
                }
            ),
            encoding="utf-8",
        )
        _ = write_result(root, "PASS", 0)
        assert run_report(root, root / "readiness") == 1
        assert "profile_manifest.json contains malformed step entry" in (
            root / "readiness" / "readiness_report.md"
        ).read_text(encoding="utf-8")


def test_triage_ignores_inventory_metadata_and_benign_negative_lines() -> None:
    with tempfile.TemporaryDirectory() as directory_text:
        root = Path(directory_text)
        write_named_log(root, "system-audit/0004_lscpu_text.stdout", "Flags: fpu mce\n")
        write_named_log(
            root,
            "system-audit/0009_dmidecode_all.stdout",
            "MCE (Machine check exception)\n",
        )
        write_named_log(
            root,
            "system-audit/0014_edac_status.meta.json",
            '{"command": "edac-util --status"}\n',
        )
        write_named_log(
            root, "system-audit/0016_ras_summary.stdout", "No MCE errors.\n"
        )
        write_named_log(
            root,
            "system-audit/0015_edac_verbose.stdout",
            "edac-util: No errors to report.\n",
        )
        write_named_log(
            root,
            "disk-audit/0003_dmesg.stdout",
            "NMI watchdog: Enabled. Permanently consumes one hw-PMU counter.\n",
        )
        assert run_triage(root, root / "out") == 0


def test_triage_detects_runtime_kernel_mce_error() -> None:
    with tempfile.TemporaryDirectory() as directory_text:
        root = Path(directory_text)
        write_named_log(
            root,
            "system-stress/after/kernel_journal.log",
            "kernel: MCE: [Hardware Error]: Machine check events logged\n",
        )
        assert run_triage(root, root / "out") == 1


def test_triage_candidate_rejects_metadata_and_inventory() -> None:
    assert (
        triage_candidate(Path("/tmp/run/system-audit/0014_edac_status.meta.json")),
        triage_candidate(Path("/tmp/run/system-audit/0004_lscpu_text.stdout")),
        triage_candidate(Path("/tmp/run/system-stress/after/kernel_journal.log")),
    ) == (False, False, True)


def write_log(root: Path, text: str) -> Path:
    root.mkdir(parents=True)
    log_path = root / "kernel.log"
    _ = log_path.write_text(text, encoding="utf-8")
    assert log_path.read_text(encoding="utf-8") == text
    return root


def write_named_log(root: Path, relative_path: str, text: str) -> None:
    log_path = root / relative_path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _ = log_path.write_text(text, encoding="utf-8")
    assert log_path.read_text(encoding="utf-8") == text


def write_profile_manifest(
    root: Path, label: str, profile_step_fingerprint: str = "expected-fingerprint"
) -> None:
    _ = (root / "profile_manifest.json").write_text(
        json.dumps(
            {
                "manifest_version": 1,
                "profile_run_id": "current-run",
                "steps": [
                    {
                        "id": "system-stress",
                        "label": label,
                        "profile_step_fingerprint": profile_step_fingerprint,
                        "title": "System stress",
                        "required": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def write_result(
    root: Path,
    status: str,
    exit_code: int,
    label: str | None = None,
    profile_step_id: str | None = None,
    profile_step_fingerprint: str | None = None,
    profile_run_id: str | None = None,
) -> Path:
    result_directory = root / "component"
    result_directory.mkdir(parents=True)
    result_payload = {
        "status": status,
        "result": status,
        "exit_code": exit_code,
        "failures": 1 if status == "FAIL" else 0,
        "warnings": 1 if status == "WARN" else 0,
    }
    if label is not None:
        result_payload["label"] = label
    if profile_step_id is not None:
        result_payload["profile_step_id"] = profile_step_id
    if profile_step_fingerprint is not None:
        result_payload["profile_step_fingerprint"] = profile_step_fingerprint
    if profile_run_id is not None:
        result_payload["profile_run_id"] = profile_run_id
    payload = json.dumps(result_payload) + "\n"
    result_path = result_directory / "result.json"
    _ = result_path.write_text(payload, encoding="utf-8")
    assert result_path.read_text(encoding="utf-8") == payload
    return root
