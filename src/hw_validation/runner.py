from __future__ import annotations

import shlex
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from os import environ
from pathlib import Path
from typing import cast

from hw_validation.console import info, warning
from hw_validation.files import write_json, write_text
from hw_validation.json_types import JsonObject, JsonValue
from hw_validation.timeutil import elapsed_seconds, utc_now


@dataclass(frozen=True, slots=True)
class CommandResult:
    command: tuple[str, ...]
    return_code: int
    stdout: str
    stderr: str
    started_at: str
    ended_at: str
    elapsed_seconds: float

    @property
    def ok(self) -> bool:
        return self.return_code == 0


@dataclass(slots=True)
class CommandRunner:
    root: Path | None = None
    verbose: bool = True
    dry_run: bool = False
    command_count: int = 0

    def capture(
        self,
        name: str,
        command: Sequence[str],
        timeout_seconds: float | None = None,
        check: bool = False,
        environment: Mapping[str, str] | None = None,
    ) -> CommandResult:
        command_tuple = tuple(command)
        command_text = shlex.join(command_tuple)
        if self.verbose:
            info(f"RUN {name}: {command_text}")
        if self.dry_run:
            started_at = utc_now()
            return CommandResult(command_tuple, 0, "", "", started_at, started_at, 0.0)
        started_at = utc_now()
        started_monotonic = time.monotonic()
        try:
            completed_process = subprocess.run(
                command_tuple,
                stdin=subprocess.DEVNULL,
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                errors="replace",
                env=None if environment is None else environ | dict(environment),
                check=False,
            )
        except OSError as error:
            result = CommandResult(
                command_tuple,
                127,
                "",
                str(error),
                started_at,
                utc_now(),
                elapsed_seconds(started_monotonic),
            )
        else:
            result = CommandResult(
                command_tuple,
                completed_process.returncode,
                completed_process.stdout or "",
                completed_process.stderr or "",
                started_at,
                utc_now(),
                elapsed_seconds(started_monotonic),
            )
        self._record(name, result)
        if self.verbose:
            if result.ok:
                info(f"DONE {name}: rc=0 elapsed={result.elapsed_seconds}s")
            else:
                warning(
                    f"DONE {name}: rc={result.return_code} elapsed={result.elapsed_seconds}s"
                )
        if check and not result.ok:
            raise subprocess.CalledProcessError(
                result.return_code,
                command_tuple,
                result.stdout,
                result.stderr,
            )
        return result

    def stream(
        self,
        name: str,
        command: Sequence[str],
        stdout_path: Path | None = None,
        stderr_path: Path | None = None,
    ) -> CommandResult:
        command_tuple = tuple(command)
        command_text = shlex.join(command_tuple)
        if self.verbose:
            info(f"RUN {name}: {command_text}")
        if self.dry_run:
            started_at = utc_now()
            return CommandResult(command_tuple, 0, "", "", started_at, started_at, 0.0)
        started_at = utc_now()
        started_monotonic = time.monotonic()
        try:
            completed_process = subprocess.run(
                command_tuple,
                stdin=subprocess.DEVNULL,
                text=True,
                capture_output=True,
                errors="replace",
                check=False,
            )
        except OSError as error:
            result = CommandResult(
                command_tuple,
                127,
                "",
                str(error),
                started_at,
                utc_now(),
                elapsed_seconds(started_monotonic),
            )
        else:
            result = CommandResult(
                command_tuple,
                completed_process.returncode,
                completed_process.stdout or "",
                completed_process.stderr or "",
                started_at,
                utc_now(),
                elapsed_seconds(started_monotonic),
            )
        if stdout_path is not None:
            write_text(stdout_path, result.stdout)
        if stderr_path is not None:
            write_text(stderr_path, result.stderr)
        self._record(name, result)
        return result

    def record(self, name: str, command: Sequence[str]) -> None:
        result = self.capture(name, command)
        del result

    def _record(self, name: str, result: CommandResult) -> None:
        if self.root is None:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        self.command_count += 1
        prefix = self.root / f"{self.command_count:04d}_{safe_name(name)}"
        stdout_path = prefix.with_suffix(".stdout")
        stderr_path = prefix.with_suffix(".stderr")
        metadata_path = prefix.with_suffix(".meta.json")
        write_text(stdout_path, result.stdout)
        write_text(stderr_path, result.stderr)
        write_json(
            metadata_path,
            {
                "name": name,
                "command": shlex.join(result.command),
                "return_code": result.return_code,
                "started_at": result.started_at,
                "ended_at": result.ended_at,
                "elapsed_seconds": result.elapsed_seconds,
                "stdout_path": stdout_path.name,
                "stderr_path": stderr_path.name,
            },
        )


def safe_name(value: str) -> str:
    return "".join(character if character.isalnum() else "_" for character in value)[
        :100
    ]


def result_to_json(result: CommandResult) -> JsonObject:
    return {
        "command": [cast(JsonValue, command_part) for command_part in result.command],
        "return_code": result.return_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "started_at": result.started_at,
        "ended_at": result.ended_at,
        "elapsed_seconds": result.elapsed_seconds,
    }
