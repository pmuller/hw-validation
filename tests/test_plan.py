from __future__ import annotations

from hw_validation.plan import DurationMode, RunPhase, RunPlan, run_plan_to_json


def test_run_plan_to_json() -> None:
    assert run_plan_to_json(
        RunPlan(
            command="network burnin",
            duration_mode=DurationMode.bounded,
            estimated_minimum_seconds=60,
            requested_duration_seconds=60,
            phases=(
                RunPhase("snapshot-before", "Capture baseline."),
                RunPhase("iperf3", "Run traffic.", 60, "1m"),
            ),
            notes=("extra setup time not included",),
        )
    ) == {
        "command": "network burnin",
        "duration_mode": "bounded",
        "estimated_minimum_seconds": 60,
        "requested_duration_seconds": 60,
        "phases": [
            {"description": "Capture baseline.", "name": "snapshot-before"},
            {
                "description": "Run traffic.",
                "duration_seconds": 60,
                "duration_text": "1m",
                "name": "iperf3",
            },
        ],
        "notes": ["extra setup time not included"],
    }
