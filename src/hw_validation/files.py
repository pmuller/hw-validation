from __future__ import annotations

import json
from pathlib import Path

from hw_validation.json_types import JsonValue


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", errors="replace") as text_file:
        print(text, file=text_file, end="")


def write_json(path: Path, payload: JsonValue) -> None:
    write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
