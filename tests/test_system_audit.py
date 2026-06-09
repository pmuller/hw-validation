from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest

import hw_validation.tooling as tooling_module
from hw_validation.runner import CommandResult, CommandRunner
from hw_validation.system_audit import (
    AuditCommand,
    available_audit_commands,
    missing_audit_commands,
    skipped_audit_commands,
)
from hw_validation.tooling import (
    APT_INSTALL_ENVIRONMENT,
    apt_install_command,
    install_debian_tools,
)


def test_audit_command_filtering() -> None:
    commands: tuple[AuditCommand, ...] = (
        ("present_info", ("present", "--info")),
        ("missing_info", ("missing", "--info")),
        ("missing_more", ("missing", "--more")),
    )

    def resolve_command(command_name: str) -> str | None:
        return f"/usr/bin/{command_name}" if command_name == "present" else None

    assert available_audit_commands(commands, resolve_command) == (
        ("present_info", ("present", "--info")),
    )
    assert missing_audit_commands(commands, resolve_command) == ("missing",)
    assert skipped_audit_commands(commands, resolve_command) == [
        {
            "name": "missing_info",
            "executable": "missing",
            "command": ["missing", "--info"],
            "reason": "missing command",
        },
        {
            "name": "missing_more",
            "executable": "missing",
            "command": ["missing", "--more"],
            "reason": "missing command",
        },
    ]


def test_apt_install_command_assumes_yes() -> None:
    assert apt_install_command(packages=("example-package",)) == (
        "apt-get",
        "-y",
        "install",
        "--no-install-recommends",
        "example-package",
    )


def test_install_debian_tools_only_install_is_noninteractive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, tuple[str, ...], dict[str, str]]] = []

    def command_path(command_name: str) -> str | None:
        return "/usr/bin/apt-get" if command_name == "apt-get" else None

    def capture(
        runner: CommandRunner,
        name: str,
        command: Sequence[str],
        timeout_seconds: float | None = None,
        check: bool = False,
        environment: Mapping[str, str] | None = None,
    ) -> CommandResult:
        _ = (runner, timeout_seconds, check)
        calls.append((name, tuple(command), dict(environment or {})))
        return CommandResult(tuple(command), 0, "", "", "start", "end", 0.0)

    monkeypatch.setattr(tooling_module, "command_path", command_path)
    monkeypatch.setattr(CommandRunner, "capture", capture)
    install_debian_tools(no_update=False, dry_run=False)
    assert calls == [
        ("apt_update", ("apt-get", "update"), {}),
        ("apt_install", apt_install_command(), APT_INSTALL_ENVIRONMENT),
    ]
