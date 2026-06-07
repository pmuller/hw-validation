from __future__ import annotations

from hw_validation.system_audit import (
    AuditCommand,
    available_audit_commands,
    missing_audit_commands,
    skipped_audit_commands,
)
from hw_validation.tooling import apt_install_command


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
