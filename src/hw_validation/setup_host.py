from __future__ import annotations

from hw_validation.console import info
from hw_validation.status import ExitCode
from hw_validation.tooling import check_tools, install_debian_tools


def run_setup(no_apt_update: bool, dry_run: bool) -> int:
    install_debian_tools(no_update=no_apt_update, dry_run=dry_run)
    if dry_run:
        info(
            "Dry run complete. Tool verification skipped because packages were not installed."
        )
        return ExitCode.pass_status.code
    tool_check = check_tools()
    if not tool_check.ok:
        if tool_check.missing_commands:
            raise RuntimeError(
                "Required commands are missing after setup: "
                + ", ".join(tool_check.missing_commands)
            )
        raise RuntimeError("The fio command in PATH is not Flexible I/O Tester")
    info(
        "Setup complete. Create an explicit run directory and pass it with --out-root."
    )
    return ExitCode.pass_status.code
