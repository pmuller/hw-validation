# HDD + NVMe Burn-in Guide

This toolkit validates new empty disks before you use them in a server or storage pool. The per-device burn-in script is intentionally destructive and will erase the selected target device.

## Toolkit contents

| Script | Purpose | Destructive |
|---|---|---:|
| `setup.sh` | Prepare a Debian/Debian-like host: install dependencies, create log directories, chmod scripts, syntax-check scripts, verify tool availability. | No |
| `disk_audit.py` | Capture non-destructive inventory and health data: `lsblk`, udev, SMART, NVMe logs, by-id paths, host context. | No |
| `disk_burnin_device.sh` | Run one burn-in instance against one disk/namespace. Designed for one tmux window per target device. | Yes |
| `disk_burnin_monitor.py` | Monitor global host state during concurrent burn-in: kernel warnings, I/O counters, pressure, load, memory, temperatures, periodic SMART/NVMe snapshots. | No |

## Safety model

The destructive runner refuses to proceed unless these checks pass:

- You pass `--i-know-this-erases-data`, except for `--dry-run`.
- The target resolves to a block device.
- The target is a whole disk or NVMe namespace, not a partition.
- The target and its child partitions are not mounted.
- The target is not active swap.
- No sysfs holders exist unless you explicitly pass `--allow-holders`.
- A per-device lock can be acquired.

You still own final path verification. Prefer stable `/dev/disk/by-id/...` paths. Do not use `/dev/sdX` names for destructive commands unless you re-check them immediately before launch.

## 1. Prepare the Debian host

Choose the central log root before setup so setup, audit, monitor, and burn-in logs stay together. Use storage that is not on the disks under test.

```bash
LOG_ROOT=/var/log/disk-burnin-$(date -u +%Y%m%dT%H%M%SZ)
sudo mkdir -p "$LOG_ROOT"
sudo chown root:root "$LOG_ROOT"
```

Run the setup script from the toolkit directory:

```bash
sudo ./setup.sh -y --log-root "$LOG_ROOT"
```

For extra temperature sensor probing:

```bash
sudo ./setup.sh -y --log-root "$LOG_ROOT" --sensors-detect
```

Useful setup options:

```bash
# Show what would be run without changing the system
./setup.sh --dry-run --log-root "$LOG_ROOT"

# Skip optional diagnostics packages
sudo ./setup.sh -y --log-root "$LOG_ROOT" --minimal

# Avoid apt update if you already did it
sudo ./setup.sh -y --log-root "$LOG_ROOT" --no-apt-update
```

The setup script installs the required runtime packages:

```text
python3 jq smartmontools nvme-cli fio e2fsprogs util-linux udev tmux
```

It also installs recommended diagnostics packages unless `--minimal` is used:

```text
sysstat pciutils usbutils lsscsi lm-sensors hdparm sg3-utils dmidecode iproute2
```

The setup script checks for a common failure mode: a Python package binary named `fio` shadowing the real Flexible I/O Tester. If it reports that `fio` is shadowed, fix your root `PATH` before burn-in. A safe invocation pattern is:

```bash
sudo env PATH=/usr/sbin:/usr/bin:/sbin:/bin ./disk_burnin_device.sh --log-root "$LOG_ROOT" ...
```

## 2. Identify exact devices

Use stable names:

```bash
ls -l /dev/disk/by-id/
lsblk -o NAME,PATH,TYPE,SIZE,MODEL,SERIAL,WWN,ROTA,TRAN,MOUNTPOINTS
```

Create a target list. Example only:

```bash
cat > devices.txt <<'EOF_DEVICES'
/dev/disk/by-id/ata-HDD_SERIAL_01
/dev/disk/by-id/ata-HDD_SERIAL_02
/dev/disk/by-id/ata-HDD_SERIAL_03
/dev/disk/by-id/ata-HDD_SERIAL_04
/dev/disk/by-id/ata-HDD_SERIAL_05
/dev/disk/by-id/ata-HDD_SERIAL_06
/dev/disk/by-id/ata-HDD_SERIAL_07
/dev/disk/by-id/nvme-NVME_SERIAL_01
/dev/disk/by-id/nvme-NVME_SERIAL_02
/dev/disk/by-id/nvme-NVME_SERIAL_03
/dev/disk/by-id/nvme-NVME_SERIAL_04
EOF_DEVICES

mapfile -t DEVICES < devices.txt
printf '%s\n' "${DEVICES[@]}"
```

Before destructive burn-in, confirm every listed disk is new/empty and not part of the boot device, an existing array, LVM, ZFS, mdraid, swap, or mounted filesystem.

## 3. Confirm log layout

All scripts use one log-path option: `--log-root DIR`. It is required.

Expected structure:

```text
$LOG_ROOT/
  setup/setup-<timestamp>.log
  audit/<timestamp_label>/
  monitor/<timestamp_label>/
  burnin/burnin-<timestamp>-<host>-<device>/
```

## 4. Run a pre-burn audit

```bash
sudo ./disk_audit.py \
  --log-root "$LOG_ROOT" \
  --label pre \
  --devices "${DEVICES[@]}"
```

Review the inventory:

```bash
find "$LOG_ROOT/audit" -name inventory.tsv -print -exec column -t -s $'\t' {} \;
```

Stop here if any device is missing, misidentified, mounted, the wrong size/model/serial, or unexpectedly already contains data.

## 5. Start the global monitor

Run this in its own tmux window and leave it running until all burn-ins finish:

```bash
sudo ./disk_burnin_monitor.py \
  --log-root "$LOG_ROOT" \
  --label global \
  --interval 30 \
  --smart-interval 300 \
  --devices "${DEVICES[@]}"
```

Important monitor outputs:

| File | Use |
|---|---|
| `monitor.log` | Human-readable timestamped summary. |
| `samples.jsonl` | Machine-readable CPU/memory/pressure/diskstats samples. |
| `kernel-follow.log` | Live kernel storage errors, resets, PCIe/NVMe warnings, I/O errors. |
| `smart_*` directories | Periodic SMART/NVMe snapshots for correlation. |

The burn-in device runner records stage snapshots only. The global monitor owns periodic telemetry.

## 6. Run one burn-in instance per device

Open a tmux session:

```bash
tmux new -s diskburn
```

Create one tmux window per target device. Manual launch is safer than auto-launching destructive commands.

### HDD example

```bash
sudo ./disk_burnin_device.sh \
  --device /dev/disk/by-id/ata-HDD_SERIAL_01 \
  --log-root "$LOG_ROOT" \
  --kind hdd \
  --i-know-this-erases-data
```

HDD default path:

1. SMART enable attempt.
2. SMART conveyance self-test if supported.
3. SMART short self-test.
4. Destructive `badblocks -wsv` full-surface write/read verification.
5. `fio` random-read stress.
6. SMART long self-test.
7. Final SMART/NVMe snapshot and health check.

### NVMe SSD example

```bash
sudo ./disk_burnin_device.sh \
  --device /dev/disk/by-id/nvme-NVME_SERIAL_01 \
  --log-root "$LOG_ROOT" \
  --kind nvme \
  --i-know-this-erases-data
```

NVMe/SSD default path:

1. NVMe short self-test or SMART short self-test.
2. One full-device `fio` write+verify pass.
3. `fio` random-read stress.
4. NVMe extended self-test or SMART long self-test.
5. Final SMART/NVMe snapshot and health check.

### Dry-run example

```bash
sudo ./disk_burnin_device.sh \
  --device /dev/disk/by-id/ata-HDD_SERIAL_01 \
  --log-root "$LOG_ROOT" \
  --kind hdd \
  --dry-run
```

## 7. Concurrency guidance

Running on multiple devices concurrently is valid for a multi-disk system burn-in,
but it tests more than the drives:

- PSU stability
- HBA/backplane stability
- SATA/SAS cabling
- PCIe/NVMe lane stability
- NVMe thermal behavior
- chassis airflow
- controller firmware behavior under sustained queue depth

Practical sequence:

1. Start the global monitor.
2. Start one HDD and one NVMe burn-in.
3. Watch temperatures and kernel logs for 30-60 minutes.
4. Start the remaining NVMe jobs.
5. Start the remaining HDD jobs, optionally staggered by 5-10 minutes.

If several devices throw errors at the same timestamp, suspect infrastructure first rather than assuming every drive is bad.

## 8. Runtime expectations

HDD `badblocks -w` is slow because it writes and reads multiple full-device patterns. Rough lower-bound estimate:

```text
runtime_hours ~= disk_size_bytes * 8 / sustained_bytes_per_second / 3600
```

Example: a 20 TB HDD at an ideal 250 MB/s average is roughly 178 hours for the `badblocks` phase alone. Real-world concurrent runs can be slower due to HBA/backplane limits.

NVMe runs are usually much shorter but can be dominated by thermal throttling. Use monitor logs to correlate bandwidth drops with temperature and kernel events.

## 9. Post-burn audit

After all burn-in windows finish, stop the global monitor with `Ctrl-C`, then run:

```bash
sudo ./disk_audit.py \
  --log-root "$LOG_ROOT" \
  --label post \
  --devices "${DEVICES[@]}"
```

## 10. Fast result review

Show result summaries:

```bash
find "$LOG_ROOT/burnin" -name result.json -print0 \
  | xargs -0 -r -I{} sh -c 'echo === {}; jq -r ".result, \"failures=\" + (.failures|tostring), \"warnings=\" + (.warnings|tostring), .real_device" {}'
```

Fallback if `result.json` was not written:

```bash
find "$LOG_ROOT/burnin" -name result.env -print -exec cat {} \;
```

Search all logs for storage-level errors:

```bash
grep -RInE 'I/O error|medium error|UNC|uncorrect|reset|timeout|failed command|frozen|link.*down|CRC|SError|nvme.*error|AER|controller is down|abort' \
  "$LOG_ROOT/monitor" "$LOG_ROOT/burnin" "$LOG_ROOT/audit" || true
```

Check for non-empty badblocks reports:

```bash
find "$LOG_ROOT/burnin" -name badblocks.list -size +0 -print -exec cat {} \;
```

## 11. Pass/fail criteria

### HDD pass

A new HDD should be accepted only if all are true:

- `disk_burnin_device.sh` exits `0`.
- `result.json` says `PASS` or `result.env` says `RESULT=PASS`.
- `badblocks.list` is empty.
- Final SMART long self-test completes without error.
- No increase in critical SMART attributes:
  - reallocated sectors
  - current pending sectors
  - offline uncorrectable sectors
  - reported uncorrectable errors
  - command timeouts
  - UDMA CRC errors
- Kernel logs show no I/O errors, SATA/SAS link resets, HBA resets, medium errors, or command timeouts for the device.

Treat any non-zero pending/offline-uncorrectable/reallocated count on a new HDD as a rejection unless you have a specific, defensible reason not to.

### NVMe pass

A new NVMe SSD should be accepted only if all are true:

- `disk_burnin_device.sh` exits `0`.
- `result.json` says `PASS` or `result.env` says `RESULT=PASS`.
- `fio` reports no verify failures.
- NVMe `critical_warning` remains `0`.
- `media_errors` does not increase.
- Error-log entries do not increase during burn-in, unless clearly vendor-benign.
- Unsafe shutdown count does not increase.
- Warning/critical temperature time does not increase.
- Kernel logs show no PCIe AER errors, NVMe resets, controller timeouts, or namespace I/O errors.

## 12. Tuning knobs

### HDD

```bash
# Use fio instead of badblocks for a lighter destructive HDD pass
--hdd-method fio --hdd-fio-passes 1

# Longer random-read stress after surface verification
--hdd-randread-minutes 120

# Override badblocks chunking
--badblocks-block-size 8192
--badblocks-count 65536

# Skip drive self-tests only when you have a reason
--skip-selftests
```

### NVMe/SSD

```bash
# More full-device write+verify passes; increases SSD wear
--ssd-full-passes 2

# Longer random-read stress
--ssd-randread-minutes 120

# Select fio engine explicitly
--fio-engine io_uring
```

## 13. What to preserve

Keep the full `$LOG_ROOT` directory until the server or storage system has been in service for a while. At minimum, preserve:

- all `result.json` and `result.env` files
- all per-device `burnin.log` files
- all pre/final SMART/NVMe snapshots
- pre/post audit `manifest.json` and `inventory.tsv`
- monitor `kernel-follow.log`
- monitor `samples.jsonl`
- every `badblocks.list`, even when empty

Compress logs for archival or debugging:

```bash
sudo tar --zstd -cf disk-burnin-logs.tar.zst -C "$(dirname "$LOG_ROOT")" "$(basename "$LOG_ROOT")"
```

## 14. Do not do these

- Do not burn in disks after adding them to a storage pool.
- Do not run destructive tests on mounted disks.
- Do not use `/dev/sdX` names without immediate re-verification.
- Do not ignore UDMA CRC increments; they usually indicate cabling/backplane/link problems.
- Do not dismiss NVMe thermal warnings; fix cooling and rerun.
- Do not accept a new disk that shows verified data corruption during burn-in.
