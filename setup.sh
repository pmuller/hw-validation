#!/usr/bin/env bash
# setup.sh
# Prepare a Debian or Debian-like host to run the disk burn-in toolkit.

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
VERSION="$(git -C "$SCRIPT_DIR" describe --always --dirty)"
APT_UPDATE=1
ASSUME_YES=0
MINIMAL=0
DRY_RUN=0
RUN_SENSORS_DETECT=0
DO_CHMOD=1
LOG_ROOT=""

usage() {
  cat <<'USAGE'
Usage:
  sudo ./setup.sh [options]

Options:
  -y, --yes                 Pass -y to apt-get install.
  --minimal                 Install only required runtime packages; skip diagnostics niceties.
  --no-apt-update           Do not run apt-get update before installing.
  --sensors-detect          Run sensors-detect --auto after installing lm-sensors. Optional.
  --no-chmod                Do not chmod toolkit scripts in the current directory.
  --log-root DIR            Required central log root.
  --dry-run                 Print intended actions without modifying the system.
  -h, --help                Show this help.

This script installs the tools used by:
  - disk_audit.py
  - disk_burnin_device.sh
  - disk_burnin_monitor.py
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
  -y | --yes)
    ASSUME_YES=1
    shift
    ;;
  --minimal)
    MINIMAL=1
    shift
    ;;
  --no-apt-update)
    APT_UPDATE=0
    shift
    ;;
  --sensors-detect)
    RUN_SENSORS_DETECT=1
    shift
    ;;
  --no-chmod)
    DO_CHMOD=0
    shift
    ;;
  --log-root)
    LOG_ROOT="${2:-}"
    shift 2
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
SETUP_LOG_ROOT="$LOG_ROOT/setup"

ts() { date -Is; }
ts_file() { date -u +%Y%m%dT%H%M%SZ; }

if [[ "$DRY_RUN" -eq 0 && "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Run as root, for example: sudo $0 -y" >&2
  exit 2
fi

SETUP_LOG=""
if [[ "$DRY_RUN" -eq 0 ]]; then
  mkdir -p "$SETUP_LOG_ROOT"
  chmod 0750 "$LOG_ROOT" "$SETUP_LOG_ROOT" || true
  SETUP_LOG="$SETUP_LOG_ROOT/setup-$(ts_file).log"
  touch "$SETUP_LOG"
else
  SETUP_LOG="/tmp/disk-burnin-setup-dry-run-$(ts_file).log"
  : >"$SETUP_LOG"
fi

timestamp_pipe() {
  local stream="$1" line
  while IFS= read -r line; do
    printf '[%s] [%s] %s\n' "$(ts)" "$stream" "$line" | tee -a "$SETUP_LOG"
  done
}

exec > >(timestamp_pipe stdout) 2> >(timestamp_pipe stderr >&2)

log() { printf '[INFO] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*" >&2; }
fatal() {
  printf '[ERROR] %s\n' "$*" >&2
  exit 2
}

quote_cmd() {
  local out="" arg
  for arg in "$@"; do
    printf -v out '%s%q ' "$out" "$arg"
  done
  printf '%s' "${out% }"
}

run() {
  local cmdstr
  cmdstr="$(quote_cmd "$@")"
  log "RUN $cmdstr"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "DRY-RUN skip: $cmdstr"
    return 0
  fi
  "$@"
}

have_cmd() { command -v "$1" >/dev/null 2>&1; }

log "Disk burn-in Debian setup version=$VERSION"
log "script_dir=$SCRIPT_DIR"
log "log_root=$LOG_ROOT"
log "setup_log=$SETUP_LOG"

if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  log "os=${PRETTY_NAME:-unknown} id=${ID:-unknown} id_like=${ID_LIKE:-} version=${VERSION_ID:-unknown}"
  if [[ "${ID:-}" != "debian" && " ${ID_LIKE:-} " != *" debian "* ]]; then
    warn "This host does not identify as Debian/Debian-like. Continuing because apt-get availability is the real gate."
  fi
else
  warn "/etc/os-release not found; cannot identify OS."
fi

have_cmd apt-get || fatal "apt-get not found. This setup script is for Debian/Debian-like systems."

REQUIRED_PACKAGES=(
  bash
  coreutils
  findutils
  grep
  sed
  gawk
  procps
  python3
  jq
  smartmontools
  nvme-cli
  fio
  e2fsprogs
  util-linux
  udev
  tmux
)

RECOMMENDED_PACKAGES=(
  sysstat
  pciutils
  usbutils
  lsscsi
  lm-sensors
  hdparm
  sg3-utils
  dmidecode
  iproute2
)

APT_INSTALL_FLAGS=(install --no-install-recommends)
if [[ "$ASSUME_YES" -eq 1 ]]; then
  APT_INSTALL_FLAGS=(-y "${APT_INSTALL_FLAGS[@]}")
fi

export DEBIAN_FRONTEND=noninteractive

if [[ "$APT_UPDATE" -eq 1 ]]; then
  run apt-get update
else
  warn "Skipping apt-get update because --no-apt-update was supplied."
fi

log "Installing required packages: ${REQUIRED_PACKAGES[*]}"
run apt-get "${APT_INSTALL_FLAGS[@]}" "${REQUIRED_PACKAGES[@]}"

hash -r || true

if [[ "$MINIMAL" -eq 0 ]]; then
  log "Installing recommended diagnostics packages: ${RECOMMENDED_PACKAGES[*]}"
  if ! run apt-get "${APT_INSTALL_FLAGS[@]}" "${RECOMMENDED_PACKAGES[@]}"; then
    warn "Bulk recommended-package install failed. Retrying one package at a time so optional failures are visible but non-fatal."
    for pkg in "${RECOMMENDED_PACKAGES[@]}"; do
      if ! run apt-get "${APT_INSTALL_FLAGS[@]}" "$pkg"; then
        warn "Optional package install failed: $pkg"
      fi
    done
  fi
else
  warn "Minimal mode: recommended diagnostics packages skipped. The monitor will have less correlation data."
fi

run mkdir -p "$LOG_ROOT" "$LOG_ROOT/burnin" /var/lib/disk-burnin
run chmod 0750 "$LOG_ROOT" "$LOG_ROOT/burnin" /var/lib/disk-burnin

if [[ "$DO_CHMOD" -eq 1 ]]; then
  log "Marking toolkit scripts executable when present in $SCRIPT_DIR"
  for script in \
    setup.sh \
    disk_audit.py \
    disk_burnin_device.sh \
    disk_burnin_monitor.py; do
    if [[ -f "$SCRIPT_DIR/$script" ]]; then
      run chmod 0755 "$SCRIPT_DIR/$script"
    fi
  done
else
  warn "Skipping chmod because --no-chmod was supplied."
fi

log "Syntax-checking toolkit scripts when present"
if [[ -f "$SCRIPT_DIR/disk_burnin_device.sh" ]]; then
  run bash -n "$SCRIPT_DIR/disk_burnin_device.sh"
fi
if have_cmd python3; then
  PY_FILES=()
  for script in disk_audit.py disk_burnin_monitor.py; do
    [[ -f "$SCRIPT_DIR/$script" ]] && PY_FILES+=("$SCRIPT_DIR/$script")
  done
  if [[ "${#PY_FILES[@]}" -gt 0 ]]; then
    run python3 -m py_compile "${PY_FILES[@]}"
  fi
fi

if [[ "$RUN_SENSORS_DETECT" -eq 1 ]]; then
  if have_cmd sensors-detect; then
    warn "Running sensors-detect --auto. This may load hardware-monitoring kernel modules."
    run sensors-detect --auto
  else
    warn "sensors-detect not found; lm-sensors may not be installed."
  fi
else
  log "Skipping sensors-detect. Run this script with --sensors-detect if you want automatic hwmon probing."
fi

REQUIRED_COMMANDS=(
  bash
  python3
  smartctl
  nvme
  fio
  badblocks
  lsblk
  blockdev
  findmnt
  flock
  jq
  udevadm
  tmux
  awk
  sed
  grep
)

OPTIONAL_COMMANDS=(
  iostat
  lspci
  lsusb
  lsscsi
  sensors
  hdparm
  sg_vpd
  dmidecode
  journalctl
)

missing_required=0
log "Verifying required commands"
for cmd in "${REQUIRED_COMMANDS[@]}"; do
  if have_cmd "$cmd"; then
    printf 'OK\t%s\t%s\n' "$cmd" "$(command -v "$cmd")"
  else
    printf 'MISSING\t%s\n' "$cmd"
    missing_required=1
  fi
done

if have_cmd fio; then
  if fio --version 2>/dev/null | grep -Eq '^fio-[0-9]'; then
    log "fio identity check passed: $(fio --version 2>/dev/null | head -n 1) at $(command -v fio)"
  else
    warn "The 'fio' found in PATH does not look like the Flexible I/O Tester required for disk verification: $(command -v fio)"
    if [[ -x /usr/bin/fio ]] && /usr/bin/fio --version 2>/dev/null | grep -Eq '^fio-[0-9]'; then
      warn "/usr/bin/fio appears correct. Run burn-in with a clean root PATH, for example: sudo env PATH=/usr/sbin:/usr/bin:/sbin:/bin ./disk_burnin_device.sh ..."
    fi
    missing_required=1
  fi
fi

log "Checking optional diagnostics commands"
for cmd in "${OPTIONAL_COMMANDS[@]}"; do
  if have_cmd "$cmd"; then
    printf 'OK\t%s\t%s\n' "$cmd" "$(command -v "$cmd")"
  else
    printf 'MISSING-OPTIONAL\t%s\n' "$cmd"
  fi
done

log "Tool versions"
for spec in \
  "python3 --version" \
  "smartctl --version" \
  "nvme version" \
  "badblocks -V" \
  "lsblk --version" \
  "tmux -V" \
  "iostat -V" \
  "sensors -v" \
  "jq --version"; do
  read -r cmd _ <<<"$spec"
  if have_cmd "$cmd"; then
    # shellcheck disable=SC2086
    sh -c "$spec" 2>&1 | head -n 3 || true
  fi
done
if have_cmd fio; then
  fio --version 2>/dev/null | grep -E '^fio-[0-9]' | head -n 1 || true
  if [[ "$(command -v fio)" != "/usr/bin/fio" && -x /usr/bin/fio ]]; then
    /usr/bin/fio --version 2>/dev/null | grep -E '^fio-[0-9]' | sed 's/^/\/usr\/bin\//g' | head -n 1 || true
  fi
fi

log "Current block-device summary"
lsblk -o NAME,PATH,TYPE,SIZE,MODEL,SERIAL,WWN,ROTA,TRAN,MOUNTPOINTS 2>&1 || true

if [[ -d /dev/disk/by-id ]]; then
  log "Stable /dev/disk/by-id entries"
  find /dev/disk/by-id -maxdepth 1 -mindepth 1 -printf '%f -> %l\n' 2>/dev/null | sort || true
else
  warn "/dev/disk/by-id does not exist. udev may not be active or the environment may be a container."
fi

if [[ "$missing_required" -ne 0 ]]; then
  if [[ "$DRY_RUN" -eq 1 ]]; then
    warn "Dry-run mode: required commands may be missing because packages were not installed."
  else
    fatal "One or more required commands are missing. Review $SETUP_LOG."
  fi
fi

cat <<EOF2

Setup complete.

Recommended next commands:
  sudo ./disk_audit.py --log-root "$LOG_ROOT" --label pre --all
  tmux new -s diskburn
  sudo ./disk_burnin_monitor.py --log-root "$LOG_ROOT" --label global --interval 30 --smart-interval 300

Use /dev/disk/by-id/... paths for destructive burn-in targets. Do not use /dev/sdX unless you re-verify immediately before launch.

Setup log:
  $SETUP_LOG
EOF2
