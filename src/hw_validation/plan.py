from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from hw_validation.console import info
from hw_validation.files import write_json
from hw_validation.json_types import JsonObject


class DurationMode(StrEnum):
    fast = "fast"
    bounded = "bounded"
    phase_bounded = "phase-bounded"
    size_bounded = "size-bounded"
    pass_bounded = "pass-bounded"
    mixed = "mixed"
    until_interrupted = "until-interrupted"


@dataclass(frozen=True, slots=True)
class RunPhase:
    name: str
    description: str
    duration_seconds: int | None = None
    duration_text: str = ""


@dataclass(frozen=True, slots=True)
class RunPlan:
    command: str
    duration_mode: DurationMode
    phases: tuple[RunPhase, ...]
    estimated_minimum_seconds: int | None = None
    requested_duration_seconds: int | None = None
    notes: tuple[str, ...] = ()


def write_run_plan(run_directory: Path, plan: RunPlan) -> None:
    write_json(run_directory / "plan.json", run_plan_to_json(plan))


def print_run_plan(plan: RunPlan) -> None:
    info(f"PLAN {plan.command}: mode={plan.duration_mode.value}")
    if plan.estimated_minimum_seconds is not None:
        info(f"PLAN minimum runtime: {format_seconds(plan.estimated_minimum_seconds)}")
    for phase_number, phase in enumerate(plan.phases, start=1):
        duration_text = (
            f" ({format_seconds(phase.duration_seconds)})"
            if phase.duration_seconds is not None
            else ""
        )
        info(f"PLAN phase {phase_number}: {phase.name}{duration_text}")
    for note in plan.notes:
        info(f"PLAN note: {note}")


def run_plan_to_json(plan: RunPlan) -> JsonObject:
    payload: JsonObject = {
        "command": plan.command,
        "duration_mode": plan.duration_mode.value,
        "phases": [run_phase_to_json(phase) for phase in plan.phases],
        "notes": [note for note in plan.notes],
    }
    if plan.estimated_minimum_seconds is not None:
        payload["estimated_minimum_seconds"] = plan.estimated_minimum_seconds
    if plan.requested_duration_seconds is not None:
        payload["requested_duration_seconds"] = plan.requested_duration_seconds
    return payload


def run_phase_to_json(phase: RunPhase) -> JsonObject:
    payload: JsonObject = {
        "name": phase.name,
        "description": phase.description,
    }
    if phase.duration_seconds is not None:
        payload["duration_seconds"] = phase.duration_seconds
    if phase.duration_text:
        payload["duration_text"] = phase.duration_text
    return payload


def format_seconds(seconds: int) -> str:
    if seconds % 86_400 == 0:
        return f"{seconds // 86_400}d"
    if seconds % 3_600 == 0:
        return f"{seconds // 3_600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"
