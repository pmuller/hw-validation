from __future__ import annotations

from hw_validation.disk import (
    DISK_AUDIT_DIRECTORY,
    DISK_BURNIN_DIRECTORY,
    DISK_MONITOR_DIRECTORY,
)


def test_disk_output_directories_are_descriptive() -> None:
    assert (
        DISK_AUDIT_DIRECTORY,
        DISK_BURNIN_DIRECTORY,
        DISK_MONITOR_DIRECTORY,
    ) == ("disk-audit", "disk-burnin", "disk-monitor")
