from __future__ import annotations

import re
from pathlib import Path


def matching_lines(paths: tuple[Path, ...], patterns: tuple[str, ...]) -> list[str]:
    expression = re.compile(
        "|".join(f"(?:{pattern})" for pattern in patterns), re.IGNORECASE
    )
    findings: list[str] = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line_number, line in enumerate(lines, start=1):
            if expression.search(line):
                findings.append(f"{path}:{line_number}:{line}")
    return findings
