from __future__ import annotations

import pytest
from typer.testing import CliRunner

import hw_validation.cli as cli
from hw_validation.cli import application


@pytest.mark.parametrize(
    ("arguments", "exit_code"),
    [
        (("--help",), 0),
        (("setup", "--help"), 0),
        (("system", "--help"), 0),
        (("disk", "--help"), 0),
        (("logs", "--help"), 0),
        (("readiness", "--help"), 0),
    ],
)
def test_cli_smoke(arguments: tuple[str, ...], exit_code: int) -> None:
    assert CliRunner().invoke(application, list(arguments)).exit_code == exit_code


def test_commands_subcommand_is_not_registered() -> None:
    assert CliRunner().invoke(application, ["commands"]).exit_code == 2


def test_main_entry_converts_runtime_error_to_clean_system_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_application() -> None:
        raise RuntimeError("broken tooling")

    monkeypatch.setattr(cli, "application", fail_application)
    with pytest.raises(SystemExit) as caught_exit:
        cli.main_entry()
    assert (caught_exit.value.code, caught_exit.value.__cause__) == (70, None)


def test_setup_runs_noninteractive_package_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[bool, bool]] = []

    def run_setup(no_apt_update: bool, dry_run: bool) -> int:
        calls.append((no_apt_update, dry_run))
        return 0

    monkeypatch.setattr(cli, "run_setup", run_setup)
    assert CliRunner().invoke(application, ["setup", "--dry-run"]).exit_code == 0
    assert calls == [(False, True)]


def test_logs_triage_prints_fail_result(monkeypatch: pytest.MonkeyPatch) -> None:
    def run_triage(log_root: object, out_root: object) -> int:
        _ = (log_root, out_root)
        return 1

    monkeypatch.setattr(cli, "run_triage", run_triage)
    result = CliRunner().invoke(
        application,
        ["logs", "triage", "--log-root", "/tmp", "--out-root", "/tmp/out"],
    )
    assert (result.exit_code, "RESULT=FAIL" in result.output) == (1, True)
