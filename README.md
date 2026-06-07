# Hardware Validation Toolkit

One Python CLI for generic Linux hardware validation: host audit, stress, scratch filesystem I/O, network burn-in, disk validation, log triage, and readiness reporting.

The `hw-validation` command can run from any directory. Become root first for commands that require privileged hardware access.

Repository-root context is only required for development commands such as `uv run ...`, `make ...`, tests, and builds.

## Development

Run these commands from the repository root:

```bash
make sync
make check
```

Common tasks:

| Command | Purpose |
|---|---|
| `make help` | Show Make targets. |
| `make format` | Format and auto-fix Python code. |
| `make check` | Run lint, format check, typecheck, and tests. |
| `make smoke` | Run CLI help smoke checks. |
| `make build` | Build `dist/hw-validation.pyz` with shiv. |

## Setup

From a development checkout, run this from the repository root because `uv run` reads `pyproject.toml` and `uv.lock`:

```bash
uv run hw-validation setup
```

Dry-run without changing the host:

```bash
uv run hw-validation setup --dry-run
```

The setup command uses one Debian package set. Package installation failure is a setup failure.

`setup` always passes `-y` to `apt-get install` to avoid interactive package prompts.

## Commands

| Command | Purpose | Destructive |
|---|---|---:|
| `hw-validation setup` | Install and verify host tooling. | No |
| `hw-validation system audit` | Capture host, firmware, CPU, memory, PCIe, network, sensor, and storage inventory. | No |
| `hw-validation system stress` | Run CPU, memory, EDAC/RAS/AER, and thermal stress validation. | No |
| `hw-validation filesystem scratch` | Run fio write and verify jobs inside one created scratch directory. | Yes, inside scratch only |
| `hw-validation network burnin` | Run iperf3 network burn-in and compare NIC health counters. | No |
| `hw-validation disk audit` | Capture disk inventory and SMART/NVMe state. | No |
| `hw-validation disk burnin` | Run destructive per-device disk validation. | Yes |
| `hw-validation disk monitor` | Capture disk validation telemetry. | No |
| `hw-validation logs triage` | Scan logs for hardware, kernel, storage, thermal, and network findings. | No |
| `hw-validation readiness report` | Aggregate final PASS, WARN, or FAIL readiness status. | No |

Every command has `--help`.

Commands that write artifacts require `--out-root /absolute/path`. `logs triage` and `readiness report` also require `--log-root /absolute/path` as input.

## Duration And Timing

Duration values are positive integers with an optional suffix: `s`, `m`, `h`, or `d`. A bare integer means seconds. Examples: `30s`, `5m`, `2h`, `1d`.

Commands that run workloads write `plan.json`, per-command `*.meta.json` files, `timing_summary.json`, and `result.json` with `duration_seconds` and `completed_reason`.

| Mode | Commands | Runtime behavior |
|---|---|---|
| Fast | `system audit`, `disk audit`, `logs triage`, `readiness report` | Runs collectors or scanners once. |
| Bounded | `network burnin`, `disk monitor --duration` | Runs for the requested duration plus setup and teardown. |
| Phase-bounded | `system stress --phase-duration` | Applies the duration to each stress phase, so total runtime is longer than one phase. |
| Size-bound | `filesystem scratch --size` | Full-file phases depend on size and device speed; random phases use `--runtime`. |
| Pass-bound | `disk burnin` | Full-device passes depend on disk size and speed; random read phases use `--ssd-randread-duration` or `--hdd-randread-duration` unless `--skip-randread` is set. |
| Until interrupted | `disk monitor --until-interrupted` | Runs until interrupted and records `completed_reason` as `interrupted`. |

## Profiles

Use `run` when you want the toolkit to compose the sequence, write a manifest, run reports, and enforce expected coverage:

```bash
uv run hw-validation run smoke \
  --out-root /var/log/hw-validation/2026-06-05-run01
```

Profiles:

| Profile | Purpose |
|---|---|
| `smoke` | Short non-destructive validation: system audits, disk audit, disk monitor, triage, readiness. |
| `standard` | Normal non-destructive validation: audits, stress, filesystem scratch, network burn-in, disk audit, disk monitor, reports. |
| `acceptance` | Longer bounded validation using `long` durations. It still does not wipe disks by default. |
| `disk-burnin` | Explicit destructive disk burn-in workflow. Requires exactly one `--device` or `--all-devices`, plus `--i-know-this-erases-data`. |

Speeds apply only to bounded workloads such as stress, filesystem, network, and monitor:

| Speed | Stress phase | Filesystem runtime | Network | Monitor |
|---|---:|---:|---:|---:|
| `smoke` | `5m` | `2m` | `2m` | `2m` |
| `standard` | `1h` | `30m` | `1h` | `1h` |
| `long` | `8h` | `2h` | `8h` | `8h` |

Disk burn-in is not a speed. It is pass-bound and device-bound. A true burn-in for a large HDD can take a week or more.

`--all-devices` is intentionally explicit. It discovers non-removable writable whole disks, writes each selected device into `profile_manifest.json`, runs the pre burn-in disk audit for those devices, preflights every selected disk for whole-disk, mounted descendant, active swap, and holder safety, then runs the destructive burn-in steps for the selected disks in parallel. Each disk is validated again by the normal per-device burn-in safety checks and per-device locking.

`--all-devices` cannot be combined with `--resume`. Kernel device names are not stable enough to safely resume destructive all-device selection. If you need to resume after a partial all-device run, rerun the remaining disks explicitly with `--device /dev/disk/by-id/...`.

Common profile examples:

```bash
uv run hw-validation run standard \
  --out-root /var/log/hw-validation/2026-06-05-run01 \
  --scratch-path /var/tmp/hw-validation-scratch \
  --server 192.0.2.10

uv run hw-validation run acceptance \
  --out-root /var/log/hw-validation/2026-06-05-run01 \
  --scratch-path /var/tmp/hw-validation-scratch \
  --server 192.0.2.10 \
  --speed long

uv run hw-validation run disk-burnin \
  --out-root /var/log/hw-validation/2026-06-05-run01 \
  --device /dev/disk/by-id/ata-EXAMPLE_DISK_SERIAL \
  --i-know-this-erases-data

uv run hw-validation run disk-burnin \
  --out-root /var/log/hw-validation/2026-06-05-run01 \
  --all-devices \
  --i-know-this-erases-data
```

Use `--parts` for targeted runs:

```bash
uv run hw-validation run standard \
  --out-root /var/log/hw-validation/2026-06-05-run01 \
  --parts system,disk-audit
```

Profile artifacts at the run root:

| Artifact | Purpose |
|---|---|
| `profile_manifest.json` | Expected steps, labels, commands, selected profile, and speed. Written before execution. |
| `profile_plan.md` | Human-readable plan. |
| `report.json` | Profile-level execution summary. |
| `report.md` | Profile-level human-readable report. |
| `summary.txt` | One-screen status summary. |
| `result.json` | Profile aggregate status. |

Useful profile controls:

| Option | Purpose |
|---|---|
| `--plan-only` | Write and print the plan without root or workloads. |
| `--resume` | Skip profile steps that already have a matching `PASS` result for the expected step fingerprint. Not allowed with `--all-devices`. |
| `--parts` | Select only some parts. |
| `--speed` | Override bounded workload durations. |
| `--all-devices` | For `disk-burnin`, discover all eligible disks and burn them in concurrently. |

Readiness automatically checks `profile_manifest.json` when it exists. Missing required profile steps are reported as missing coverage instead of being silently ignored.

## Run Order

1. Setup tooling.
2. Boot-level RAM test.
3. Pre-system audit.
4. System stress.
5. Filesystem scratch test.
6. Network burn-in.
7. Disk validation, when storage validation is in scope.
8. Post-system audit.
9. Log triage.
10. Readiness report.

## Example Run

```bash
mkdir -p /var/log/hw-validation/2026-06-05-run01

uv run hw-validation system audit \
  --out-root /var/log/hw-validation/2026-06-05-run01 \
  --label system-pre

uv run hw-validation system stress \
  --out-root /var/log/hw-validation/2026-06-05-run01 \
  --label system-24h \
  --phase-duration 24h

mkdir -p /var/tmp/hw-validation-scratch

uv run hw-validation filesystem scratch \
  --path /var/tmp/hw-validation-scratch \
  --out-root /var/log/hw-validation/2026-06-05-run01 \
  --label filesystem-scratch \
  --size 20G \
  --runtime 1h \
  --cleanup
```

On the peer machine:

```bash
iperf3 -s
```

Back on this machine:

```bash
uv run hw-validation network burnin \
  --server 192.0.2.10 \
  --out-root /var/log/hw-validation/2026-06-05-run01 \
  --label primary-network \
  --duration 2h \
  --parallel 8 \
  --bidir
```

Disk validation, when in scope:

```bash
uv run hw-validation disk audit \
  --out-root /var/log/hw-validation/2026-06-05-run01 \
  --label disk-pre \
  --all

uv run hw-validation disk monitor \
  --out-root /var/log/hw-validation/2026-06-05-run01 \
  --label disk-monitor \
  --duration 24h \
  --interval 30s \
  --smart-interval 5m

uv run hw-validation disk burnin \
  --device /dev/disk/by-id/ata-EXAMPLE_DISK_SERIAL \
  --out-root /var/log/hw-validation/2026-06-05-run01 \
  --ssd-randread-duration 60m \
  --hdd-randread-duration 30m \
  --i-know-this-erases-data
```

Use `--smartctl-type TYPE` on disk audit, burn-in, or monitor commands when a device requires `smartctl -d TYPE`, for example USB SAT bridges or RAID-backed devices.

Finish:

```bash
uv run hw-validation system audit \
  --out-root /var/log/hw-validation/2026-06-05-run01 \
  --label system-post

uv run hw-validation logs triage \
  --log-root /var/log/hw-validation/2026-06-05-run01 \
  --out-root /var/log/hw-validation/2026-06-05-run01/logs-triage

uv run hw-validation readiness report \
  --log-root /var/log/hw-validation/2026-06-05-run01 \
  --out-root /var/log/hw-validation/2026-06-05-run01/readiness-report
```

## Deployment

Build from the repository root:

```bash
make build
python3 dist/hw-validation.pyz --help
```

Copy `dist/hw-validation.pyz` to the validation host after building.

## Results

Every major command writes `result.json` under the explicit output root.

| Code | Meaning |
|---:|---|
| 0 | Pass |
| 1 | Hard failure |
| 2 | Warnings or manual review required |
| 64 | Usage or configuration error |
| 70 | Internal or tooling error |

| Status | Meaning |
|---|---|
| PASS | No failures and no unresolved serious warnings. |
| WARN | No hard failures, but warnings require human review. |
| FAIL | One or more hard failures. |

## Blind Spots

OS memory stress does not replace boot-level memory testing.

ECC must be verified as enabled.

Corrected ECC errors during validation matter.

PCIe AER errors are not normal during a clean validation run.

Storage resets are not normal during a clean validation run.

Passing one subsystem test does not validate all other subsystems.

Keep the full run directory.
