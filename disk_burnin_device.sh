#!/usr/bin/env bash
# disk_burnin_device.sh
# Destructive per-device burn-in runner for HDDs and NVMe/SATA SSDs.
# Run one instance per disk, preferably in separate tmux windows.

set -Euo pipefail
shopt -s nullglob

VERSION="$(git -C "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)" describe --always --dirty)"
DEVICE=""
LOG_ROOT=""
BURNIN_ROOT=""
ERASE_OK=0
DRY_RUN=0
KIND="auto"            # auto|hdd|ssd|nvme
HDD_METHOD="badblocks" # badblocks|fio
SMARTCTL_TYPE=""       # e.g. sat, scsi, megaraid,N
FIO_ENGINE="auto"
FIO_BIN=""
FIO_BS="1M"
HDD_RANDREAD_MINUTES=30
SSD_RANDREAD_MINUTES=60
SSD_FULL_PASSES=1
HDD_FIO_PASSES=1
BADBLOCKS_BLOCK_SIZE=4096
BADBLOCKS_BLOCKS_AT_ONCE=65536
SKIP_SELFTESTS=0
ALLOW_HOLDERS=0
FAILURES=0
WARNINGS=0
LOG_DIR=""
LOG_FILE=""
LOCK_FD=9

usage() {
  cat <<'EOF'
Usage:
  sudo ./disk_burnin_device.sh --device /dev/disk/by-id/... --i-know-this-erases-data [options]

Required:
  --device PATH                 Whole block device or stable /dev/disk/by-id path.
  --i-know-this-erases-data     Required unless --dry-run is used.

Options:
  --log-root DIR                Required central log root. Burn-in logs go under DIR/burnin.
  --kind auto|hdd|ssd|nvme      Override device kind detection. Default: auto
  --hdd-method badblocks|fio    HDD destructive method. Default: badblocks
  --smartctl-type TYPE          Pass smartctl -d TYPE, e.g. sat, scsi, megaraid,N
  --fio-engine ENGINE           auto|io_uring|libaio|posixaio|sync|psync. Default: auto
  --fio-bs SIZE                 fio sequential block size. Default: 1M
  --ssd-full-passes N           Full-device write+verify passes for SSD/NVMe. Default: 1
  --ssd-randread-minutes N      SSD/NVMe random-read stress minutes. Default: 60
  --hdd-randread-minutes N      HDD random-read stress minutes after surface test. Default: 30
  --hdd-fio-passes N            Full-device write+verify passes when --hdd-method=fio. Default: 1
  --badblocks-block-size BYTES  badblocks -b. Default: 4096
  --badblocks-count N           badblocks -c blocks at a time. Default: 65536
  --skip-selftests              Skip SMART/NVMe short/long self-tests; still runs I/O workload.
  --allow-holders               Do not fail when sysfs holders exist. Dangerous; avoid unless you know why.
  --dry-run                     Print intended actions and capture non-destructive metadata only.
  -h, --help                    Show this help.

Exit codes:
  0 pass/no detected burn-in failure
  1 burn-in completed but one or more stages failed
  2 preflight/configuration failure
  130 interrupted
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
  --device)
    DEVICE="${2:-}"
    shift 2
    ;;
  --log-root)
    LOG_ROOT="${2:-}"
    shift 2
    ;;
  --kind)
    KIND="${2:-}"
    shift 2
    ;;
  --hdd-method)
    HDD_METHOD="${2:-}"
    shift 2
    ;;
  --smartctl-type)
    SMARTCTL_TYPE="${2:-}"
    shift 2
    ;;
  --fio-engine)
    FIO_ENGINE="${2:-}"
    shift 2
    ;;
  --fio-bs)
    FIO_BS="${2:-}"
    shift 2
    ;;
  --ssd-full-passes)
    SSD_FULL_PASSES="${2:-}"
    shift 2
    ;;
  --ssd-randread-minutes)
    SSD_RANDREAD_MINUTES="${2:-}"
    shift 2
    ;;
  --hdd-randread-minutes)
    HDD_RANDREAD_MINUTES="${2:-}"
    shift 2
    ;;
  --hdd-fio-passes)
    HDD_FIO_PASSES="${2:-}"
    shift 2
    ;;
  --badblocks-block-size)
    BADBLOCKS_BLOCK_SIZE="${2:-}"
    shift 2
    ;;
  --badblocks-count)
    BADBLOCKS_BLOCKS_AT_ONCE="${2:-}"
    shift 2
    ;;
  --skip-selftests)
    SKIP_SELFTESTS=1
    shift
    ;;
  --allow-holders)
    ALLOW_HOLDERS=1
    shift
    ;;
  --i-know-this-erases-data)
    ERASE_OK=1
    shift
    ;;
  --dry-run)
    DRY_RUN=1
    shift
    ;;
  -h | --help)
    usage
    exit 0
    ;;
  *)
    echo "Unknown argument: $1" >&2
    usage >&2
    exit 2
    ;;
  esac
done

if [[ -z "$LOG_ROOT" ]]; then
  echo "--log-root is required" >&2
  exit 2
fi
BURNIN_ROOT="$LOG_ROOT/burnin"

ts() { date -Is; }
ts_file() { date -u +%Y%m%dT%H%M%SZ; }

log_raw() {
  local level="$1"
  shift
  local msg="$*"
  if [[ -n "${LOG_FILE:-}" ]]; then
    printf '[%s] [%s] %s\n' "$(ts)" "$level" "$msg" | tee -a "$LOG_FILE"
  else
    printf '[%s] [%s] %s\n' "$(ts)" "$level" "$msg"
  fi
}
log() { log_raw INFO "$@"; }
warn() {
  WARNINGS=$((WARNINGS + 1))
  log_raw WARN "$@"
}
die() {
  log_raw ERROR "$@" >&2
  exit 2
}
mark_fail() {
  FAILURES=$((FAILURES + 1))
  log_raw FAIL "$@"
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

select_fio_binary() {
  local candidate=""
  if command -v fio >/dev/null 2>&1; then
    candidate="$(command -v fio)"
    if "$candidate" --version 2>/dev/null | grep -Eq '^fio-[0-9]'; then
      FIO_BIN="$candidate"
      return 0
    fi
    warn "fio in PATH is not the Flexible I/O Tester: $candidate"
  fi
  if [[ -x /usr/bin/fio ]] && /usr/bin/fio --version 2>/dev/null | grep -Eq '^fio-[0-9]'; then
    FIO_BIN="/usr/bin/fio"
    warn "Using /usr/bin/fio because PATH fio is missing or shadowed."
    return 0
  fi
  die "Required Flexible I/O Tester not found. Install Debian package 'fio' and ensure 'fio --version' starts with 'fio-'."
}

quote_cmd() {
  local out="" arg
  for arg in "$@"; do
    printf -v out '%s%q ' "$out" "$arg"
  done
  printf '%s' "${out% }"
}

prefix_stream() {
  local stream="$1" logfile="$2" line
  while IFS= read -r line; do
    printf '[%s] [%s] %s\n' "$(ts)" "$stream" "$line" | tee -a "$logfile"
  done
}

run_cmd() {
  local cmdstr rc
  cmdstr="$(quote_cmd "$@")"
  log "RUN $cmdstr"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "DRY-RUN skip: $cmdstr"
    return 0
  fi
  set +e
  "$@" > >(prefix_stream stdout "$LOG_FILE") 2> >(prefix_stream stderr "$LOG_FILE" >&2)
  rc=$?
  set -e
  if [[ "$rc" -eq 0 ]]; then
    log "OK rc=0 $cmdstr"
  else
    log_raw ERROR "RC=$rc $cmdstr"
  fi
  return "$rc"
}

capture_cmd() {
  local outfile="$1"
  shift
  local cmdstr rc
  cmdstr="$(quote_cmd "$@")"
  log "CAPTURE $cmdstr -> $outfile"
  mkdir -p "$(dirname "$outfile")"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    {
      echo "# dry_run=true"
      echo "# timestamp=$(ts)"
      echo "# command=$cmdstr"
    } >"$outfile"
    return 0
  fi
  set +e
  (
    echo "# timestamp=$(ts)"
    echo "# command=$cmdstr"
    echo "# cwd=$(pwd)"
    echo "# --- output ---"
    "$@"
    rc=$?
    echo "# --- rc=$rc ---"
    exit "$rc"
  ) >"$outfile" 2>&1
  rc=$?
  set -e
  while IFS= read -r line; do
    printf '[%s] [capture] %s\n' "$(ts)" "$line" | tee -a "$LOG_FILE" >/dev/null
  done <"$outfile"
  return "$rc"
}

smartctl_args() {
  if [[ -n "$SMARTCTL_TYPE" && "$SMARTCTL_TYPE" != "auto" ]]; then
    printf '%s\0' smartctl -d "$SMARTCTL_TYPE"
  else
    printf '%s\0' smartctl
  fi
}

run_smartctl() {
  local -a cmd=()
  while IFS= read -r -d '' x; do cmd+=("$x"); done < <(smartctl_args)
  cmd+=("$@" "$REAL_DEVICE")
  run_cmd "${cmd[@]}"
}

capture_smartctl() {
  local outfile="$1"
  shift
  local -a cmd=()
  while IFS= read -r -d '' x; do cmd+=("$x"); done < <(smartctl_args)
  cmd+=("$@" "$REAL_DEVICE")
  capture_cmd "$outfile" "${cmd[@]}"
}

require_integer() {
  local name="$1" value="$2"
  [[ "$value" =~ ^[0-9]+$ ]] || die "$name must be an integer; got '$value'"
}

resolve_by_id() {
  local real="$1" p
  for p in /dev/disk/by-id/*; do
    [[ -e "$p" ]] || continue
    if [[ "$(readlink -f "$p" 2>/dev/null || true)" == "$real" ]]; then
      printf '%s\n' "$p"
      return 0
    fi
  done
  return 1
}

human_bytes() {
  if command -v numfmt >/dev/null 2>&1; then
    numfmt --to=iec-i --suffix=B "$1" 2>/dev/null || printf '%s bytes' "$1"
  else
    printf '%s bytes' "$1"
  fi
}

select_fio_engine() {
  if [[ "$FIO_ENGINE" != "auto" ]]; then
    printf '%s\n' "$FIO_ENGINE"
    return 0
  fi
  if "$FIO_BIN" --enghelp=io_uring >/dev/null 2>&1; then
    printf 'io_uring\n'
  elif "$FIO_BIN" --enghelp=libaio >/dev/null 2>&1; then
    printf 'libaio\n'
  else
    printf 'psync\n'
  fi
}

is_nvme_path() {
  [[ "$REAL_DEVICE" =~ ^/dev/nvme[0-9]+n[0-9]+$ || "$TRAN" == "nvme" ]]
}

snapshot_device() {
  local stage="$1" stamp snapdir
  stamp="$(ts_file)"
  snapdir="$LOG_DIR/snapshots/$stage-$stamp"
  mkdir -p "$snapdir"
  log "SNAPSHOT stage=$stage dir=$snapdir"
  capture_cmd "$snapdir/lsblk.txt" lsblk --bytes --output-all "$REAL_DEVICE" || true
  capture_cmd "$snapdir/lsblk.json" lsblk --json --bytes --output-all "$REAL_DEVICE" || true
  capture_cmd "$snapdir/udevadm-info.txt" udevadm info --query=property --name "$REAL_DEVICE" || true
  capture_smartctl "$snapdir/smartctl-x.txt" -x || true
  capture_smartctl "$snapdir/smartctl-x.json" -x -j || true
  capture_smartctl "$snapdir/smartctl-health.txt" -H || true
  capture_smartctl "$snapdir/smartctl-selftest-log.txt" -l selftest || true
  capture_smartctl "$snapdir/smartctl-error-log.txt" -l error || true
  if is_nvme_path && command -v nvme >/dev/null 2>&1; then
    capture_cmd "$snapdir/nvme-smart-log.json" nvme smart-log -o json "$REAL_DEVICE" || true
    capture_cmd "$snapdir/nvme-error-log.json" nvme error-log -o json "$REAL_DEVICE" || true
    capture_cmd "$snapdir/nvme-self-test-log.json" nvme self-test-log -o json "$REAL_DEVICE" || true
    capture_cmd "$snapdir/nvme-list.json" nvme list -o json || true
  fi
  if [[ -e "/sys/class/block/$KNAME/stat" ]]; then
    cp "/sys/class/block/$KNAME/stat" "$snapdir/sysfs-stat.txt" 2>/dev/null || true
  fi
}

cleanup() {
  local rc=$?
  if [[ -n "${LOG_FILE:-}" ]]; then
    log "cleanup rc=$rc failures=$FAILURES warnings=$WARNINGS"
  fi
}
trap cleanup EXIT
trap 'log_raw ERROR "Interrupted"; exit 130' INT TERM

preflight_mounts() {
  local mounted swaps holders
  mounted="$(lsblk -nrpo NAME,MOUNTPOINT "$REAL_DEVICE" | awk 'NF >= 2 && $2 != "" {print}' || true)"
  if [[ -n "$mounted" ]]; then
    die "Refusing destructive test: device or child partition is mounted: $mounted"
  fi

  swaps=""
  if [[ -r /proc/swaps ]]; then
    while read -r swapdev _rest; do
      [[ "$swapdev" == Filename ]] && continue
      [[ -z "$swapdev" ]] && continue
      local real_swap
      real_swap="$(readlink -f "$swapdev" 2>/dev/null || true)"
      if [[ "$real_swap" == "$REAL_DEVICE" || "$real_swap" == "$REAL_DEVICE"* ]]; then
        swaps+="$swapdev "
      fi
    done </proc/swaps
  fi
  if [[ -n "$swaps" ]]; then
    die "Refusing destructive test: device or child partition is active swap: $swaps"
  fi

  holders=""
  if [[ -d "/sys/class/block/$KNAME/holders" ]]; then
    holders="$(find "/sys/class/block/$KNAME/holders" -mindepth 1 -maxdepth 1 -printf '%f ' 2>/dev/null || true)"
  fi
  if [[ -n "$holders" && "$ALLOW_HOLDERS" -ne 1 ]]; then
    die "Refusing destructive test: $REAL_DEVICE has sysfs holders: $holders (use --allow-holders only if you understand the stack)"
  fi
}

smartctl_wait_until_done() {
  local label="$1" max_seconds="$2" poll=60 elapsed=0 pollfile
  log "Waiting for SMART self-test '$label' to finish; max_seconds=$max_seconds"
  sleep 10 || true
  while ((elapsed < max_seconds)); do
    pollfile="$LOG_DIR/selftests/smartctl-${label}-poll-$(ts_file).txt"
    mkdir -p "$LOG_DIR/selftests"
    capture_smartctl "$pollfile" -c || true
    if grep -Eiq 'Self-test routine in progress|Self test in progress|[0-9]+% of test remaining|remaining' "$pollfile"; then
      log "SMART self-test '$label' still in progress; elapsed=${elapsed}s"
      sleep "$poll" || true
      elapsed=$((elapsed + poll))
    else
      log "SMART self-test '$label' no longer reports in-progress"
      break
    fi
  done
  capture_smartctl "$LOG_DIR/selftests/smartctl-${label}-selftest-log-$(ts_file).txt" -l selftest || true
  if ((elapsed >= max_seconds)); then
    warn "SMART self-test '$label' exceeded wait limit; continuing"
  fi
}

run_smartctl_selftest() {
  local test="$1" max_seconds="$2"
  if [[ "$SKIP_SELFTESTS" -eq 1 ]]; then
    warn "Skipping SMART self-test $test by request"
    return 0
  fi
  mkdir -p "$LOG_DIR/selftests"
  log "Starting SMART self-test: $test"
  if run_smartctl -t "$test"; then
    smartctl_wait_until_done "$test" "$max_seconds"
  else
    mark_fail "SMART self-test command failed to start: $test"
  fi
}

maybe_run_conveyance() {
  local capfile="$LOG_DIR/selftests/smartctl-capabilities-$(ts_file).txt"
  mkdir -p "$LOG_DIR/selftests"
  capture_smartctl "$capfile" -c || true
  if grep -Eiq 'Conveyance Self-test supported' "$capfile"; then
    run_smartctl_selftest conveyance 7200
  else
    warn "Conveyance self-test not reported as supported; skipped"
  fi
}

nvme_wait_until_done() {
  local label="$1" max_seconds="$2" poll=60 elapsed=0 pollfile
  log "Waiting for NVMe self-test '$label' to finish; max_seconds=$max_seconds"
  sleep 10 || true
  while ((elapsed < max_seconds)); do
    pollfile="$LOG_DIR/selftests/nvme-${label}-poll-$(ts_file).txt"
    mkdir -p "$LOG_DIR/selftests"
    capture_cmd "$pollfile" nvme self-test-log "$REAL_DEVICE" -v || true
    if grep -Eiq 'Current operation[[:space:]]*:?[[:space:]]*0([^0-9]|$)|Current operation[[:space:]]*:?[[:space:]]*0x0([^0-9A-Fa-f]|$)' "$pollfile"; then
      log "NVMe self-test '$label' reports no current operation"
      break
    fi
    log "NVMe self-test '$label' still in progress; elapsed=${elapsed}s"
    sleep "$poll" || true
    elapsed=$((elapsed + poll))
  done
  capture_cmd "$LOG_DIR/selftests/nvme-${label}-self-test-log-$(ts_file).txt" nvme self-test-log "$REAL_DEVICE" -v || true
  if ((elapsed >= max_seconds)); then
    warn "NVMe self-test '$label' exceeded wait limit; continuing"
  fi
}

run_nvme_selftest() {
  local code="$1" label="$2" max_seconds="$3"
  if [[ "$SKIP_SELFTESTS" -eq 1 ]]; then
    warn "Skipping NVMe self-test $label by request"
    return 0
  fi
  if ! command -v nvme >/dev/null 2>&1; then
    warn "nvme command not found; skipping NVMe self-test $label"
    return 0
  fi
  mkdir -p "$LOG_DIR/selftests"
  log "Starting NVMe self-test: $label code=$code"
  if run_cmd nvme device-self-test "$REAL_DEVICE" -s "$code"; then
    nvme_wait_until_done "$label" "$max_seconds"
  else
    warn "NVMe self-test command failed to start; device may not support it: $label"
  fi
}

run_hdd_badblocks() {
  local listfile="$LOG_DIR/badblocks.list"
  log "Starting HDD destructive surface test with badblocks. This writes/reads multiple patterns across the whole device."
  if ! run_cmd badblocks -wsv -b "$BADBLOCKS_BLOCK_SIZE" -c "$BADBLOCKS_BLOCKS_AT_ONCE" -o "$listfile" "$REAL_DEVICE"; then
    mark_fail "badblocks command failed for $REAL_DEVICE"
  fi
  if [[ -s "$listfile" ]]; then
    mark_fail "badblocks reported bad blocks; see $listfile"
  else
    log "badblocks list is empty: no bad blocks reported"
  fi
}

run_fio_full_write_verify() {
  local label="$1" passes="$2" qd="$3" p engine
  engine="$(select_fio_engine)"
  for ((p = 1; p <= passes; p++)); do
    log "Starting fio full-device write+verify: label=$label pass=$p/$passes engine=$engine bs=$FIO_BS qd=$qd"
    if ! run_cmd "$FIO_BIN" \
      --name="${label}_pass${p}" \
      --filename="$REAL_DEVICE" \
      --rw=write \
      --bs="$FIO_BS" \
      --ioengine="$engine" \
      --iodepth="$qd" \
      --direct=1 \
      --verify=crc32c \
      --do_verify=1 \
      --verify_fatal=1 \
      --continue_on_error=none \
      --size=100% \
      --group_reporting \
      --eta=always \
      --log_avg_msec=10000 \
      --write_bw_log="$LOG_DIR/${label}_pass${p}_bw" \
      --write_iops_log="$LOG_DIR/${label}_pass${p}_iops" \
      --write_lat_log="$LOG_DIR/${label}_pass${p}_lat"; then
      mark_fail "fio full write+verify failed: $label pass=$p"
    fi
  done
}

run_fio_randread() {
  local label="$1" minutes="$2" bs="$3" qd="$4" engine
  if [[ "$minutes" -le 0 ]]; then
    warn "Skipping $label random-read stress because minutes=$minutes"
    return 0
  fi
  engine="$(select_fio_engine)"
  log "Starting fio random-read stress: label=$label minutes=$minutes engine=$engine bs=$bs qd=$qd"
  if ! run_cmd "$FIO_BIN" \
    --name="$label" \
    --filename="$REAL_DEVICE" \
    --rw=randread \
    --runtime="${minutes}m" \
    --time_based=1 \
    --bs="$bs" \
    --ioengine="$engine" \
    --iodepth="$qd" \
    --direct=1 \
    --readonly=1 \
    --group_reporting \
    --eta=always \
    --log_avg_msec=10000 \
    --write_bw_log="$LOG_DIR/${label}_bw" \
    --write_iops_log="$LOG_DIR/${label}_iops" \
    --write_lat_log="$LOG_DIR/${label}_lat"; then
    mark_fail "fio random-read stress failed: $label"
  fi
}

final_health_check() {
  log "Running final health checks"
  if command -v smartctl >/dev/null 2>&1; then
    if ! run_smartctl -H; then
      mark_fail "smartctl health check returned non-zero for $REAL_DEVICE"
    fi
  fi
  snapshot_device final
}

[[ -n "$DEVICE" ]] || {
  usage >&2
  exit 2
}
case "$KIND" in auto | hdd | ssd | nvme) ;; *)
  echo "Invalid --kind: $KIND" >&2
  exit 2
  ;;
esac
case "$HDD_METHOD" in badblocks | fio) ;; *)
  echo "Invalid --hdd-method: $HDD_METHOD" >&2
  exit 2
  ;;
esac
require_integer --ssd-full-passes "$SSD_FULL_PASSES"
require_integer --ssd-randread-minutes "$SSD_RANDREAD_MINUTES"
require_integer --hdd-randread-minutes "$HDD_RANDREAD_MINUTES"
require_integer --hdd-fio-passes "$HDD_FIO_PASSES"
require_integer --badblocks-block-size "$BADBLOCKS_BLOCK_SIZE"
require_integer --badblocks-count "$BADBLOCKS_BLOCKS_AT_ONCE"

need_cmd lsblk
need_cmd readlink
need_cmd awk
need_cmd tee
need_cmd date
need_cmd flock
need_cmd jq
need_cmd smartctl
select_fio_binary
if [[ "$HDD_METHOD" == "badblocks" ]]; then need_cmd badblocks; fi
if [[ "$DRY_RUN" -ne 1 && "$ERASE_OK" -ne 1 ]]; then
  echo "Refusing to run destructive burn-in without --i-know-this-erases-data" >&2
  exit 2
fi
if [[ "$EUID" -ne 0 ]]; then
  echo "Run as root. This script needs raw block-device and SMART/NVMe access." >&2
  exit 2
fi

REAL_DEVICE="$(readlink -f "$DEVICE")"
[[ -b "$REAL_DEVICE" ]] || {
  echo "Not a block device: $DEVICE -> $REAL_DEVICE" >&2
  exit 2
}
KNAME="$(basename "$REAL_DEVICE")"
TYPE="$(lsblk -dnro TYPE "$REAL_DEVICE" 2>/dev/null || true)"
[[ "$TYPE" == "disk" ]] || {
  echo "Refusing: target must be a whole disk/namespace, not type '$TYPE': $REAL_DEVICE" >&2
  exit 2
}
TRAN="$(lsblk -dnro TRAN "$REAL_DEVICE" 2>/dev/null | tr -d ' ' || true)"
ROTA="$(lsblk -dnro ROTA "$REAL_DEVICE" 2>/dev/null | tr -d ' ' || true)"
MODEL="$(lsblk -dnro MODEL "$REAL_DEVICE" 2>/dev/null | sed 's/^ *//;s/ *$//' || true)"
SERIAL="$(lsblk -dnro SERIAL "$REAL_DEVICE" 2>/dev/null | sed 's/^ *//;s/ *$//' || true)"
SIZE_BYTES="$(blockdev --getsize64 "$REAL_DEVICE" 2>/dev/null || echo 0)"
PREFERRED_BY_ID="$(resolve_by_id "$REAL_DEVICE" || true)"
SAFE_ID="$(printf '%s_%s_%s' "$KNAME" "${SERIAL:-noserial}" "${MODEL:-nomodel}" | tr -c 'A-Za-z0-9_.+-' '_' | cut -c1-140)"
RUN_ID="burnin-$(date -u +%Y%m%dT%H%M%SZ)-$(hostname -s)-$SAFE_ID"
LOG_DIR="$BURNIN_ROOT/$RUN_ID"
mkdir -p "$LOG_DIR" "$LOG_DIR/snapshots" "$LOG_DIR/selftests"
LOG_FILE="$LOG_DIR/burnin.log"
touch "$LOG_FILE"

LOCK_DIR="/run/lock"
[[ -d "$LOCK_DIR" ]] || LOCK_DIR="$LOG_DIR"
LOCK_FILE="$LOCK_DIR/disk-burnin-$KNAME.lock"
exec {LOCK_FD}>"$LOCK_FILE"
flock -n "$LOCK_FD" || die "Another burn-in process appears to hold lock $LOCK_FILE"

log "disk_burnin_device.sh version=$VERSION"
log "burnin_root=$BURNIN_ROOT"
log "device_input=$DEVICE real_device=$REAL_DEVICE preferred_by_id=${PREFERRED_BY_ID:-none}"
log "model=$MODEL serial=$SERIAL size_bytes=$SIZE_BYTES size=$(human_bytes "$SIZE_BYTES") tran=$TRAN rota=$ROTA"
log "log_dir=$LOG_DIR"
log "fio_binary=$FIO_BIN"

if [[ -z "$PREFERRED_BY_ID" ]]; then
  warn "No /dev/disk/by-id alias found for $REAL_DEVICE; use stable paths whenever possible."
fi

preflight_mounts

if [[ "$KIND" == "auto" ]]; then
  if [[ "$REAL_DEVICE" =~ ^/dev/nvme[0-9]+n[0-9]+$ || "$TRAN" == "nvme" ]]; then
    DETECTED_KIND="nvme"
  elif [[ "$ROTA" == "1" ]]; then
    DETECTED_KIND="hdd"
  else
    DETECTED_KIND="ssd"
  fi
else
  DETECTED_KIND="$KIND"
fi
log "detected_kind=$DETECTED_KIND"

if [[ "$DETECTED_KIND" == "nvme" ]] && ! command -v nvme >/dev/null 2>&1; then
  warn "nvme-cli is not installed; NVMe-specific logs and self-tests will be skipped. Install nvme-cli."
fi

{
  echo "RUN_ID=$RUN_ID"
  echo "VERSION=$VERSION"
  echo "BURNIN_ROOT=$BURNIN_ROOT"
  echo "START_TIME=$(ts)"
  echo "DEVICE_INPUT=$DEVICE"
  echo "REAL_DEVICE=$REAL_DEVICE"
  echo "PREFERRED_BY_ID=${PREFERRED_BY_ID:-}"
  echo "MODEL=$MODEL"
  echo "SERIAL=$SERIAL"
  echo "SIZE_BYTES=$SIZE_BYTES"
  echo "TRAN=$TRAN"
  echo "ROTA=$ROTA"
  echo "DETECTED_KIND=$DETECTED_KIND"
  echo "HDD_METHOD=$HDD_METHOD"
  echo "SSD_FULL_PASSES=$SSD_FULL_PASSES"
  echo "SSD_RANDREAD_MINUTES=$SSD_RANDREAD_MINUTES"
  echo "HDD_RANDREAD_MINUTES=$HDD_RANDREAD_MINUTES"
} >"$LOG_DIR/run_metadata.env"

snapshot_device pre

# Enable SMART autosave/offline data collection where supported. Do not fail if unsupported.
run_smartctl -s on -S on -o on || warn "SMART enable/autosave/offline command failed or unsupported"

case "$DETECTED_KIND" in
hdd)
  log "Burn-in plan: HDD => optional conveyance + short SMART, destructive full-surface write/read, randread stress, final long SMART"
  if [[ "$SKIP_SELFTESTS" -ne 1 ]]; then
    maybe_run_conveyance
    run_smartctl_selftest short 7200
  fi
  snapshot_device after-pre-selftests
  if [[ "$HDD_METHOD" == "badblocks" ]]; then
    run_hdd_badblocks
  else
    run_fio_full_write_verify hdd_full_write_verify "$HDD_FIO_PASSES" 4
  fi
  snapshot_device after-hdd-surface-test
  run_fio_randread hdd_randread "$HDD_RANDREAD_MINUTES" 128k 16
  snapshot_device after-hdd-randread
  if [[ "$SKIP_SELFTESTS" -ne 1 ]]; then
    run_smartctl_selftest long 172800
  fi
  ;;
ssd | nvme)
  log "Burn-in plan: SSD/NVMe => SMART/NVMe short, fio full write+verify, randread stress, final extended self-test"
  local_est_writes=$((SIZE_BYTES * SSD_FULL_PASSES))
  log "Estimated minimum host writes from full-pass phase: $local_est_writes bytes ($(human_bytes "$local_est_writes"))"
  if [[ "$SKIP_SELFTESTS" -ne 1 ]]; then
    if [[ "$DETECTED_KIND" == "nvme" ]]; then
      run_nvme_selftest 1 short 7200
    else
      run_smartctl_selftest short 7200
    fi
  fi
  snapshot_device after-pre-selftests
  run_fio_full_write_verify "${DETECTED_KIND}_full_write_verify" "$SSD_FULL_PASSES" 32
  snapshot_device after-ssd-full-verify
  run_fio_randread "${DETECTED_KIND}_randread" "$SSD_RANDREAD_MINUTES" 4k 64
  snapshot_device after-ssd-randread
  if [[ "$SKIP_SELFTESTS" -ne 1 ]]; then
    if [[ "$DETECTED_KIND" == "nvme" ]]; then
      run_nvme_selftest 2 extended 172800
    else
      run_smartctl_selftest long 172800
    fi
  fi
  ;;
*)
  die "Unhandled detected kind: $DETECTED_KIND"
  ;;
esac

sync || true
blockdev --flushbufs "$REAL_DEVICE" 2>/dev/null || true
final_health_check

if ((FAILURES == 0)); then
  RESULT="PASS"
  EXIT_CODE=0
else
  RESULT="FAIL"
  EXIT_CODE=1
fi
END_TIME="$(ts)"
if ! jq -n \
  --arg result "$RESULT" \
  --argjson failures "$FAILURES" \
  --argjson warnings "$WARNINGS" \
  --argjson exit_code "$EXIT_CODE" \
  --arg end_time "$END_TIME" \
  --arg log_dir "$LOG_DIR" \
  --arg run_id "$RUN_ID" \
  --arg device_input "$DEVICE" \
  --arg real_device "$REAL_DEVICE" \
  --arg preferred_by_id "${PREFERRED_BY_ID:-}" \
  --arg detected_kind "$DETECTED_KIND" \
  --arg model "$MODEL" \
  --arg serial "$SERIAL" \
  --argjson size_bytes "$SIZE_BYTES" \
  --arg fio_binary "$FIO_BIN" \
  '{
    result: $result,
    failures: $failures,
    warnings: $warnings,
    exit_code: $exit_code,
    end_time: $end_time,
    log_dir: $log_dir,
    run_id: $run_id,
    device_input: $device_input,
    real_device: $real_device,
    preferred_by_id: $preferred_by_id,
    detected_kind: $detected_kind,
    model: $model,
    serial: $serial,
    size_bytes: $size_bytes,
    fio_binary: $fio_binary
  }' >"$LOG_DIR/result.json"; then
  warn "Failed to write result JSON with jq: $LOG_DIR/result.json"
fi
{
  echo "RESULT=$RESULT"
  echo "FAILURES=$FAILURES"
  echo "WARNINGS=$WARNINGS"
  echo "END_TIME=$END_TIME"
  echo "LOG_DIR=$LOG_DIR"
} >"$LOG_DIR/result.env"
log "RESULT=$RESULT failures=$FAILURES warnings=$WARNINGS log_dir=$LOG_DIR"
exit "$EXIT_CODE"
