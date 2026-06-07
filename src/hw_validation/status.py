from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ExitCode(StrEnum):
    pass_status = "0"
    hard_failure = "1"
    warning = "2"
    usage = "64"
    tooling = "70"

    @property
    def code(self) -> int:
        return int(self.value)


class ResultStatus(StrEnum):
    pass_status = "PASS"
    warn = "WARN"
    fail = "FAIL"


@dataclass(frozen=True, slots=True)
class ValidationOutcome:
    status: ResultStatus
    exit_code: int
    failures: int = 0
    warnings: int = 0


def outcome_from_counts(failures: int, warnings: int) -> ValidationOutcome:
    if failures > 0:
        return ValidationOutcome(
            status=ResultStatus.fail,
            exit_code=ExitCode.hard_failure.code,
            failures=failures,
            warnings=warnings,
        )
    if warnings > 0:
        return ValidationOutcome(
            status=ResultStatus.warn,
            exit_code=ExitCode.warning.code,
            failures=failures,
            warnings=warnings,
        )
    return ValidationOutcome(
        status=ResultStatus.pass_status,
        exit_code=ExitCode.pass_status.code,
        failures=failures,
        warnings=warnings,
    )
