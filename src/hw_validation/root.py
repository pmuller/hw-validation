from __future__ import annotations

import os

import typer

from hw_validation.console import failure
from hw_validation.status import ExitCode


def require_root() -> None:
    if os.geteuid() != 0:
        failure("This script must be run as root.")
        raise typer.Exit(ExitCode.usage.code)
