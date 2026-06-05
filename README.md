# Hardware Validation Toolkit

Linux hardware validation scripts for host audit, stress, scratch filesystem I/O, network burn-in, disk validation, log triage, and readiness reporting.

Run from the repository root. Become root first for scripts that require privileged hardware access.

## Setup

```bash
./scripts/setup.sh --yes
```

Dry-run without changing the host:

```bash
./scripts/setup.sh --dry-run --yes
```

The installer uses one package set. Package installation failure is a setup failure.

## Scripts

| Script | Purpose | Destructive |
|---|---|---:|
| `scripts/setup.sh` | Install the Debian validation toolchain and check scripts. | No |
| `scripts/system_audit.py` | Capture host, firmware, CPU, memory, PCIe, network, sensor, and storage inventory. | No |
| `scripts/system_stress.sh` | Run CPU, memory, kernel, EDAC/RAS/AER, and thermal stress validation. | No |
| `scripts/filesystem_scratch_test.sh` | Run fio write and verify jobs inside one script-created scratch directory. | Yes, inside scratch only |
| `scripts/network_burnin.sh` | Run iperf3 network burn-in and compare NIC health counters. | No |
| `scripts/log_triage.py` | Scan logs for hardware, kernel, storage, thermal, and network findings. | No |
| `scripts/readiness_report.py` | Aggregate result files into final PASS, WARN, or FAIL readiness status. | No |
| `scripts/disk_audit.py` | Existing disk inventory and SMART/NVMe audit. | No |
| `scripts/disk_burnin_device.sh` | Existing destructive per-device disk validation runner. | Yes |
| `scripts/disk_burnin_monitor.py` | Existing disk validation monitor for kernel warnings and device telemetry. | No |

Every script has `--help`.

Scripts that write artifacts require `--out-root /absolute/path`. `log_triage.py` and `readiness_report.py` also require `--log-root /absolute/path` as input.

## Run Order

1. Setup tooling.
2. Boot-level RAM test.
3. Pre-system audit.
4. System stress.
5. Filesystem scratch test.
6. Network burn-in.
7. Existing disk validation, when storage validation is in scope.
8. Post-system audit.
9. Log triage.
10. Readiness report.

## Example Run

```bash
mkdir -p /var/log/hw-validation/2026-06-05-run01

./scripts/system_audit.py \
  --out-root /var/log/hw-validation/2026-06-05-run01 \
  --label system-pre

./scripts/system_stress.sh \
  --out-root /var/log/hw-validation/2026-06-05-run01 \
  --label system-24h \
  --duration 24h

mkdir -p /var/tmp/hw-validation-scratch

./scripts/filesystem_scratch_test.sh \
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
./scripts/network_burnin.sh \
  --server 192.0.2.10 \
  --out-root /var/log/hw-validation/2026-06-05-run01 \
  --label primary-network \
  --duration 2h \
  --parallel 8 \
  --bidir
```

Disk validation, when in scope:

```bash
./scripts/disk_audit.py \
  --out-root /var/log/hw-validation/2026-06-05-run01 \
  --label disk-pre \
  --all

./scripts/disk_burnin_monitor.py \
  --out-root /var/log/hw-validation/2026-06-05-run01 \
  --label disk-monitor \
  --interval 30 \
  --smart-interval 300

./scripts/disk_burnin_device.sh \
  --device /dev/disk/by-id/ata-EXAMPLE_DISK_SERIAL \
  --out-root /var/log/hw-validation/2026-06-05-run01 \
  --i-know-this-erases-data
```

Finish:

```bash
./scripts/system_audit.py \
  --out-root /var/log/hw-validation/2026-06-05-run01 \
  --label system-post

./scripts/log_triage.py \
  --log-root /var/log/hw-validation/2026-06-05-run01 \
  --out-root /var/log/hw-validation/2026-06-05-run01/log-triage

./scripts/readiness_report.py \
  --log-root /var/log/hw-validation/2026-06-05-run01 \
  --out-root /var/log/hw-validation/2026-06-05-run01/readiness
```

## Results

Every major script writes `result.json` under the explicit output root.

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
