#!/usr/bin/env bash
# Prepare a Debian validation host for generic Linux hardware validation.

set -Eeuo pipefail

SCRIPT_DIRECTORY="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
VERSION="$(git -C "$SCRIPT_DIRECTORY" describe --always --dirty 2>/dev/null || printf 'unknown')"
ASSUME_YES=0
APT_UPDATE=1
DRY_RUN=0

usage() {
  cat <<'USAGE'
Usage:
  ./scripts/setup.sh [options]

Options:
  -y, --yes          Pass -y to apt-get install.
  --no-apt-update    Do not run apt-get update before installing.
  --dry-run          Print commands without changing the system. Root is not required.
  -h, --help         Show this help.

Installs the complete validation tool package set, marks toolkit scripts
executable, syntax-checks shell scripts, byte-compiles Python scripts, and
verifies required commands.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
  -y | --yes)
    ASSUME_YES=1
    shift
    ;;
  --no-apt-update)
    APT_UPDATE=0
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
    printf 'Unknown argument: %s\n' "$1" >&2
    usage >&2
    exit 64
    ;;
  esac
done

if [[ "$DRY_RUN" -eq 0 && "$EUID" -ne 0 ]]; then
  printf 'This script must be run as root.\n' >&2
  exit 64
fi

quote_command() {
  local quoted_command=""
  local command_argument
  for command_argument in "$@"; do
    printf -v quoted_command '%s%q ' "$quoted_command" "$command_argument"
  done
  printf '%s' "${quoted_command% }"
}

log() {
  printf '%s [INFO] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

fail_usage() {
  printf '%s\n' "$*" >&2
  exit 64
}

fail_tooling() {
  printf '%s\n' "$*" >&2
  exit 70
}

run() {
  log "RUN $(quote_command "$@")"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    return 0
  fi
  "$@"
}

run_required() {
  if ! run "$@"; then
    fail_tooling "Command failed: $(quote_command "$@")"
  fi
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    fail_tooling "Required command is missing after setup: $1"
  fi
  printf 'OK\t%s\t%s\n' "$1" "$(command -v "$1")"
}

verify_fio() {
  require_command fio
  if ! fio --version 2>/dev/null | grep -Eq '^fio-[0-9]'; then
    fail_tooling "The fio command in PATH is not Flexible I/O Tester: $(command -v fio)"
  fi
  log "fio identity check passed: $(fio --version 2>/dev/null | sed -n '1p')"
}

if [[ "$DRY_RUN" -eq 0 ]]; then
  command -v apt-get >/dev/null 2>&1 || fail_usage "apt-get not found. This setup script is for Debian systems."
else
  log "Dry run: commands will be printed and not executed."
fi

log "Linux hardware validation setup version=$VERSION"
log "script_directory=$SCRIPT_DIRECTORY"

PACKAGE_SET=(
  bash
  coreutils
  findutils
  grep
  sed
  gawk
  python3
  smartmontools
  nvme-cli
  fio
  e2fsprogs
  util-linux
  udev
  tmux
  jq
  sysstat
  pciutils
  usbutils
  lsscsi
  lm-sensors
  hdparm
  sg3-utils
  dmidecode
  iproute2
  ethtool
  stress-ng
  stressapptest
  memtester
  iperf3
  rasdaemon
  edac-utils
  ipmitool
  numactl
  memtest86+
  procps
  kmod
)

APT_INSTALL_COMMAND=(apt-get install --no-install-recommends)
if [[ "$ASSUME_YES" -eq 1 ]]; then
  APT_INSTALL_COMMAND=(apt-get -y install --no-install-recommends)
fi

if [[ "$DRY_RUN" -eq 0 ]]; then
  export DEBIAN_FRONTEND=noninteractive
fi

if [[ "$APT_UPDATE" -eq 1 ]]; then
  run_required apt-get update
fi

log "Installing complete package set: ${PACKAGE_SET[*]}"
run_required "${APT_INSTALL_COMMAND[@]}" "${PACKAGE_SET[@]}"

if [[ "$DRY_RUN" -eq 0 ]]; then
  hash -r || true
fi

SHELL_SCRIPTS=(
  setup.sh
  system_stress.sh
  network_burnin.sh
  filesystem_scratch_test.sh
  disk_burnin_device.sh
)

PYTHON_SCRIPTS=(
  system_audit.py
  log_triage.py
  readiness_report.py
  disk_audit.py
  disk_burnin_monitor.py
)

log "Marking toolkit scripts executable"
for script_name in "${SHELL_SCRIPTS[@]}" "${PYTHON_SCRIPTS[@]}"; do
  if [[ -f "$SCRIPT_DIRECTORY/$script_name" ]]; then
    run_required chmod 0755 "$SCRIPT_DIRECTORY/$script_name"
  else
    fail_tooling "Toolkit script is missing: $script_name"
  fi
done

log "Syntax-checking shell scripts"
for script_name in "${SHELL_SCRIPTS[@]}"; do
  run_required bash -n "$SCRIPT_DIRECTORY/$script_name"
done

log "Byte-compiling Python scripts"
run_required python3 -m py_compile \
  "$SCRIPT_DIRECTORY/system_audit.py" \
  "$SCRIPT_DIRECTORY/log_triage.py" \
  "$SCRIPT_DIRECTORY/readiness_report.py" \
  "$SCRIPT_DIRECTORY/disk_audit.py" \
  "$SCRIPT_DIRECTORY/disk_burnin_monitor.py"

if [[ "$DRY_RUN" -eq 1 ]]; then
  log "Dry run complete. Command verification was not performed because packages were not installed."
  exit 0
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
  iostat
  lspci
  lsusb
  lsscsi
  sensors
  hdparm
  sg_vpd
  dmidecode
  ip
  ethtool
  stress-ng
  stressapptest
  memtester
  iperf3
  ras-mc-ctl
  edac-util
  ipmitool
  numactl
  dmesg
  journalctl
  free
  lscpu
  vmstat
  lsmod
  hostname
  date
  uname
  readlink
  cat
  rm
  mkdir
  sync
  wipefs
)

log "Verifying required commands"
for command_name in "${REQUIRED_COMMANDS[@]}"; do
  if [[ "$command_name" == "fio" ]]; then
    verify_fio
  else
    require_command "$command_name"
  fi
done

if ! dpkg-query -W -f='${Status}\n' memtest86+ 2>/dev/null | grep -q '^install ok installed$'; then
  fail_tooling "Required package is not installed: memtest86+"
fi
printf 'OK\tpackage\tmemtest86+\n'

log "Setup complete. Create an explicit run directory and pass it with --out-root to validation scripts."
