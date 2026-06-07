from __future__ import annotations

import re
from pathlib import Path

SLUG_INVALID_CHARACTER_PATTERN = re.compile(r"[^A-Za-z0-9._=-]+")
SLUG_UNDERSCORE_PATTERN = re.compile(r"_+")


def slug(value: str, maximum_length: int = 120) -> str:
    normalized = SLUG_INVALID_CHARACTER_PATTERN.sub("_", value.strip())
    normalized = SLUG_UNDERSCORE_PATTERN.sub("_", normalized).strip("_")
    return (normalized or "unknown")[:maximum_length]


def absolute_path(path_text: str, name: str) -> Path:
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return path.resolve(strict=False)


def ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def path_is_within(path: Path, possible_parent: Path) -> bool:
    return path.resolve(strict=False).is_relative_to(
        possible_parent.resolve(strict=False)
    )
