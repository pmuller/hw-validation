from __future__ import annotations

from typing import Annotated

import typer

from hw_validation import __version__
from hw_validation.console import console, failure, info, warning
from hw_validation.disk import run_disk_audit, run_disk_burnin, run_disk_monitor
from hw_validation.filesystem import run_filesystem_scratch
from hw_validation.network import run_network_burnin
from hw_validation.paths import absolute_path
from hw_validation.profile import (
    ProfileSettings,
    parse_parts,
    parse_profile_name,
    parse_profile_speed,
    run_profile,
)
from hw_validation.readiness import run_report
from hw_validation.root import require_root
from hw_validation.setup_host import run_setup
from hw_validation.status import ExitCode
from hw_validation.system_audit import run_system_audit
from hw_validation.system_stress import run_system_stress
from hw_validation.triage import run_triage

application = typer.Typer(
    name="hw-validation",
    help="Generic Linux hardware validation toolkit.",
    no_args_is_help=True,
)
system_application = typer.Typer(
    help="System audit and stress commands.", no_args_is_help=True
)
network_application = typer.Typer(
    help="Network validation commands.", no_args_is_help=True
)
filesystem_application = typer.Typer(
    help="Filesystem validation commands.", no_args_is_help=True
)
disk_application = typer.Typer(help="Disk validation commands.", no_args_is_help=True)
logs_application = typer.Typer(help="Log analysis commands.", no_args_is_help=True)
readiness_application = typer.Typer(
    help="Readiness report commands.", no_args_is_help=True
)

application.add_typer(system_application, name="system")
application.add_typer(network_application, name="network")
application.add_typer(filesystem_application, name="filesystem")
application.add_typer(disk_application, name="disk")
application.add_typer(logs_application, name="logs")
application.add_typer(readiness_application, name="readiness")


@application.callback()
def main(
    version: Annotated[
        bool, typer.Option("--version", help="Show version and exit.")
    ] = False,
) -> None:
    if version:
        console.print(__version__)
        raise typer.Exit(ExitCode.pass_status.code)


@application.command()
def setup(
    no_apt_update: Annotated[
        bool, typer.Option("--no-apt-update", help="Do not run apt-get update.")
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print commands without changing the host."),
    ] = False,
) -> None:
    if not dry_run:
        require_root()
    exit_with(run_setup(no_apt_update=no_apt_update, dry_run=dry_run))


@application.command("run")
def profile_run(
    profile: Annotated[
        str,
        typer.Argument(help="Profile: smoke, standard, acceptance, or disk-burnin."),
    ],
    out_root: Annotated[str, typer.Option("--out-root", help="Absolute output root.")],
    parts: Annotated[
        str | None,
        typer.Option(
            "--parts",
            help="Comma-separated parts: system, filesystem, network, disk, disk-burnin.",
        ),
    ] = None,
    speed: Annotated[
        str | None,
        typer.Option(
            "--speed", help="Bounded workload speed: smoke, standard, or long."
        ),
    ] = None,
    plan_only: Annotated[
        bool,
        typer.Option("--plan-only", help="Write and print the profile plan only."),
    ] = False,
    resume: Annotated[
        bool,
        typer.Option("--resume", help="Skip profile steps with existing PASS results."),
    ] = False,
    scratch_path: Annotated[
        str | None,
        typer.Option(
            "--scratch-path", help="Absolute scratch parent for filesystem validation."
        ),
    ] = None,
    server: Annotated[
        str | None,
        typer.Option("--server", help="iperf3 server for network validation."),
    ] = None,
    interface: Annotated[
        str | None, typer.Option("--interface", help="Network interface for burn-in.")
    ] = None,
    devices: Annotated[
        list[str] | None,
        typer.Option("--device", help="Disk device for disk parts. Repeatable."),
    ] = None,
    all_devices: Annotated[
        bool,
        typer.Option(
            "--all-devices",
            help="Use all discovered non-removable writable disks for disk-burnin.",
        ),
    ] = False,
    smartctl_type: Annotated[
        str | None,
        typer.Option("--smartctl-type", help="Pass smartctl -d TYPE for disk SMART."),
    ] = None,
    erase_ok: Annotated[
        bool,
        typer.Option(
            "--i-know-this-erases-data",
            help="Required for the destructive disk-burnin profile or part.",
        ),
    ] = False,
    cleanup: Annotated[
        bool,
        typer.Option("--cleanup/--no-cleanup", help="Clean profile scratch directory."),
    ] = True,
) -> None:
    profile_name = parse_profile_name(profile)
    if not plan_only:
        require_root()
    exit_with(
        run_profile(
            ProfileSettings(
                profile=profile_name,
                speed=parse_profile_speed(speed, profile_name),
                out_root=absolute_path(out_root, "--out-root"),
                parts=parse_parts(parts),
                scratch_path=absolute_path(scratch_path, "--scratch-path")
                if scratch_path is not None
                else None,
                server=server,
                interface=interface,
                devices=tuple(devices or ()),
                all_devices=all_devices,
                smartctl_type=smartctl_type,
                erase_ok=erase_ok,
                cleanup=cleanup,
                plan_only=plan_only,
                resume=resume,
            )
        )
    )


@system_application.command("audit")
def system_audit(
    out_root: Annotated[str, typer.Option("--out-root", help="Absolute output root.")],
    label: Annotated[str, typer.Option("--label", help="Run label.")] = "system-audit",
) -> None:
    require_root()
    exit_with(run_system_audit(absolute_path(out_root, "--out-root"), label))


@system_application.command("stress")
def system_stress(
    out_root: Annotated[str, typer.Option("--out-root", help="Absolute output root.")],
    label: Annotated[str, typer.Option("--label", help="Run label.")] = "system-stress",
    phase_duration: Annotated[
        str,
        typer.Option(
            "--phase-duration",
            help="Duration for each stress phase. Format: 30s, 5m, 2h, or 1d.",
        ),
    ] = "8h",
    mem_percent: Annotated[
        int, typer.Option("--mem-percent", help="Memory percent for stress tools.")
    ] = 75,
    memtester_amount: Annotated[
        str | None,
        typer.Option("--memtester-amount", help="Run memtester with this amount."),
    ] = None,
    allow_corrected_ecc: Annotated[
        bool,
        typer.Option("--allow-corrected-ecc", help="Treat corrected ECC as warning."),
    ] = False,
    allow_thermal_throttle: Annotated[
        bool,
        typer.Option(
            "--allow-thermal-throttle", help="Treat thermal throttling as warning."
        ),
    ] = False,
) -> None:
    require_root()
    exit_with(
        run_system_stress(
            absolute_path(out_root, "--out-root"),
            label,
            phase_duration,
            mem_percent,
            memtester_amount,
            allow_corrected_ecc,
            allow_thermal_throttle,
        )
    )


@filesystem_application.command("scratch")
def filesystem_scratch(
    path: Annotated[
        str, typer.Option("--path", help="Existing absolute scratch parent path.")
    ],
    out_root: Annotated[str, typer.Option("--out-root", help="Absolute output root.")],
    label: Annotated[
        str, typer.Option("--label", help="Run label.")
    ] = "filesystem-scratch",
    size: Annotated[
        str, typer.Option("--size", help="fio file size per phase.")
    ] = "10G",
    runtime: Annotated[
        str,
        typer.Option(
            "--runtime",
            help="Random fio phase runtime. Format: 30s, 5m, 2h, or 1d.",
        ),
    ] = "30m",
    cleanup: Annotated[
        bool,
        typer.Option("--cleanup", help="Delete the script-created scratch directory."),
    ] = False,
) -> None:
    require_root()
    exit_with(
        run_filesystem_scratch(
            absolute_path(path, "--path"),
            absolute_path(out_root, "--out-root"),
            label,
            size,
            runtime,
            cleanup,
        )
    )


@network_application.command("burnin")
def network_burnin(
    server: Annotated[str, typer.Option("--server", help="iperf3 server.")],
    out_root: Annotated[str, typer.Option("--out-root", help="Absolute output root.")],
    label: Annotated[
        str, typer.Option("--label", help="Run label.")
    ] = "network-burnin",
    interface: Annotated[
        str | None, typer.Option("--interface", help="Network interface.")
    ] = None,
    duration: Annotated[
        str,
        typer.Option(
            "--duration", help="Total iperf3 duration. Format: 30s, 5m, 2h, or 1d."
        ),
    ] = "1h",
    parallel: Annotated[
        int, typer.Option("--parallel", help="iperf3 parallel streams.")
    ] = 1,
    bidirectional: Annotated[
        bool, typer.Option("--bidir", help="Run bidirectional iperf3.")
    ] = False,
    expect_bandwidth: Annotated[
        str | None, typer.Option("--expect-bandwidth", help="Minimum bits per second.")
    ] = None,
) -> None:
    require_root()
    exit_with(
        run_network_burnin(
            server,
            absolute_path(out_root, "--out-root"),
            label,
            interface,
            duration,
            parallel,
            bidirectional,
            expect_bandwidth,
        )
    )


@disk_application.command("audit")
def disk_audit(
    out_root: Annotated[str, typer.Option("--out-root", help="Absolute output root.")],
    devices: Annotated[
        list[str] | None, typer.Option("--device", help="Block device to audit.")
    ] = None,
    audit_all: Annotated[
        bool, typer.Option("--all", help="Audit all non-removable disks.")
    ] = False,
    include_removable: Annotated[
        bool, typer.Option("--include-removable", help="Include removable disks.")
    ] = False,
    include_readonly: Annotated[
        bool, typer.Option("--include-readonly", help="Include read-only disks.")
    ] = False,
    smartctl_type: Annotated[
        str | None,
        typer.Option("--smartctl-type", help="Pass smartctl -d TYPE for all devices."),
    ] = None,
    label: Annotated[str, typer.Option("--label", help="Run label.")] = "disk-audit",
    quiet: Annotated[
        bool, typer.Option("--quiet", help="Reduce progress output.")
    ] = False,
) -> None:
    require_root()
    exit_with(
        run_disk_audit(
            absolute_path(out_root, "--out-root"),
            label,
            tuple(devices or ()),
            audit_all,
            include_removable,
            include_readonly,
            quiet,
            smartctl_type,
        )
    )


@disk_application.command("burnin")
def disk_burnin(
    device: Annotated[str, typer.Option("--device", help="Whole block device.")],
    out_root: Annotated[str, typer.Option("--out-root", help="Absolute output root.")],
    erase_ok: Annotated[
        bool,
        typer.Option(
            "--i-know-this-erases-data", help="Required destructive confirmation."
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", help="Capture metadata without destructive workloads."
        ),
    ] = False,
    label: Annotated[str, typer.Option("--label", help="Run label.")] = "disk-burnin",
    kind: Annotated[
        str, typer.Option("--kind", help="auto, hdd, ssd, or nvme.")
    ] = "auto",
    hdd_method: Annotated[
        str, typer.Option("--hdd-method", help="badblocks or fio.")
    ] = "badblocks",
    smartctl_type: Annotated[
        str | None,
        typer.Option("--smartctl-type", help="Pass smartctl -d TYPE for this device."),
    ] = None,
    fio_block_size: Annotated[
        str, typer.Option("--fio-bs", help="fio sequential block size.")
    ] = "1M",
    ssd_full_passes: Annotated[
        int, typer.Option("--ssd-full-passes", help="SSD/NVMe full passes.")
    ] = 1,
    ssd_randread_duration: Annotated[
        str,
        typer.Option(
            "--ssd-randread-duration",
            help="SSD/NVMe random read duration. Format: 30s, 5m, 2h, or 1d.",
        ),
    ] = "60m",
    hdd_randread_duration: Annotated[
        str,
        typer.Option(
            "--hdd-randread-duration",
            help="HDD random read duration. Format: 30s, 5m, 2h, or 1d.",
        ),
    ] = "30m",
    hdd_fio_passes: Annotated[
        int, typer.Option("--hdd-fio-passes", help="HDD fio full passes.")
    ] = 1,
    skip_randread: Annotated[
        bool, typer.Option("--skip-randread", help="Skip final random read phase.")
    ] = False,
    skip_selftests: Annotated[
        bool, typer.Option("--skip-selftests", help="Skip SMART/NVMe self-tests.")
    ] = False,
) -> None:
    require_root()
    exit_with(
        run_disk_burnin(
            absolute_path(out_root, "--out-root"),
            label,
            device,
            erase_ok,
            dry_run,
            kind,
            hdd_method,
            smartctl_type,
            fio_block_size,
            ssd_full_passes,
            ssd_randread_duration,
            hdd_randread_duration,
            hdd_fio_passes,
            skip_randread,
            skip_selftests,
        )
    )


@disk_application.command("monitor")
def disk_monitor(
    out_root: Annotated[str, typer.Option("--out-root", help="Absolute output root.")],
    devices: Annotated[
        list[str] | None, typer.Option("--device", help="Device to monitor.")
    ] = None,
    label: Annotated[str, typer.Option("--label", help="Run label.")] = "disk-monitor",
    interval: Annotated[
        str,
        typer.Option(
            "--interval", help="Telemetry sample interval. Format: 30s, 5m, 2h, or 1d."
        ),
    ] = "30s",
    smart_interval: Annotated[
        str,
        typer.Option(
            "--smart-interval",
            help="SMART snapshot interval. Format: 30s, 5m, 2h, or 1d.",
        ),
    ] = "5m",
    sensors_interval: Annotated[
        str,
        typer.Option(
            "--sensors-interval",
            help="Sensor snapshot interval. Format: 30s, 5m, 2h, or 1d.",
        ),
    ] = "5m",
    duration: Annotated[
        str | None,
        typer.Option(
            "--duration", help="Bounded monitor duration. Format: 30s, 5m, 2h, or 1d."
        ),
    ] = None,
    until_interrupted: Annotated[
        bool,
        typer.Option(
            "--until-interrupted",
            help="Run until interrupted instead of using --duration.",
        ),
    ] = False,
    smartctl_type: Annotated[
        str | None,
        typer.Option(
            "--smartctl-type", help="Pass smartctl -d TYPE for SMART snapshots."
        ),
    ] = None,
    no_smart_snapshots: Annotated[
        bool,
        typer.Option(
            "--no-smart-snapshots", help="Disable periodic SMART/NVMe snapshots."
        ),
    ] = False,
    no_sensors_snapshots: Annotated[
        bool,
        typer.Option(
            "--no-sensors-snapshots", help="Disable periodic sensor snapshots."
        ),
    ] = False,
) -> None:
    require_root()
    exit_with(
        run_disk_monitor(
            absolute_path(out_root, "--out-root"),
            label,
            tuple(devices or ()),
            interval,
            smart_interval,
            sensors_interval,
            duration,
            until_interrupted,
            smartctl_type,
            not no_smart_snapshots,
            not no_sensors_snapshots,
        )
    )


@logs_application.command("triage")
def logs_triage(
    log_root: Annotated[
        str, typer.Option("--log-root", help="Absolute log root to scan.")
    ],
    out_root: Annotated[str, typer.Option("--out-root", help="Absolute output root.")],
) -> None:
    exit_with(
        run_triage(
            absolute_path(log_root, "--log-root"), absolute_path(out_root, "--out-root")
        )
    )


@readiness_application.command("report")
def readiness_report(
    log_root: Annotated[
        str, typer.Option("--log-root", help="Absolute log root to summarize.")
    ],
    out_root: Annotated[str, typer.Option("--out-root", help="Absolute output root.")],
) -> None:
    exit_with(
        run_report(
            absolute_path(log_root, "--log-root"), absolute_path(out_root, "--out-root")
        )
    )


def exit_with(exit_code: int) -> None:
    if exit_code == 0:
        info("RESULT=PASS")
    elif exit_code == ExitCode.warning.code:
        warning("RESULT=WARN")
    elif exit_code == ExitCode.hard_failure.code:
        failure("RESULT=FAIL")
    else:
        failure(f"RESULT=ERROR exit_code={exit_code}")
    raise typer.Exit(exit_code)


def main_entry() -> None:
    try:
        application()
    except ValueError as error:
        failure(str(error))
        raise SystemExit(ExitCode.usage.code) from None
    except RuntimeError as error:
        failure(str(error))
        raise SystemExit(ExitCode.tooling.code) from None
