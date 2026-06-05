#!/usr/bin/env bash
# Run destructive-to-scratch filesystem validation with fio write and verify jobs.

set -Eeuo pipefail

EXIT_PASS=0
EXIT_FAIL=1
EXIT_WARN=2
EXIT_USAGE=64
EXIT_TOOLING=70

TARGET_PATH=""
OUT_ROOT=""
LABEL="filesystem-scratch"
SIZE="10G"
RUNTIME="30m"
CLEANUP=0
RUN_DIRECTORY=""
SCRATCH_DIRECTORY=""
LOG_FILE=""
FAILURES=0
WARNINGS=0
STARTED_AT=""

usage() {
  cat <<'USAGE'
Usage:
  ./scripts/filesystem_scratch_test.sh --path /absolute/path --out-root /absolute/path [options]

Required:
  --path PATH                 Existing absolute directory used to create one scratch subdirectory.
  --out-root PATH             Absolute output root.

Options:
  --label LABEL               Run label. Default: filesystem-scratch
  --size SIZE                 fio file size per phase. Default: 10G
  --runtime DURATION          Random read/write phase runtime. Default: 30m
  --cleanup                   Delete only the script-created scratch directory at the end.
  -h, --help                  Show this help.

Exit codes:
  0 pass
  1 hard failure
  2 warnings/manual review
  64 usage/configuration error
  70 internal/tooling error
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
  --path)
    TARGET_PATH="${2:-}"
    shift 2
    ;;
  --out-root)
    OUT_ROOT="${2:-}"
    shift 2
    ;;
  --label)
    LABEL="${2:-}"
    shift 2
    ;;
  --size)
    SIZE="${2:-}"
    shift 2
    ;;
  --runtime)
    RUNTIME="${2:-}"
    shift 2
    ;;
  --cleanup)
    CLEANUP=1
    shift
    ;;
  -h | --help)
    usage
    exit 0
    ;;
  *)
    printf 'Unknown argument: %s\n' "$1" >&2
    usage >&2
    exit "$EXIT_USAGE"
    ;;
  esac
done

if [[ "$EUID" -ne 0 ]]; then
  printf 'This script must be run as root.\n' >&2
  exit "$EXIT_USAGE"
fi

fail_usage() {
  printf '%s\n' "$*" >&2
  exit "$EXIT_USAGE"
}

fail_tooling() {
  printf '%s\n' "$*" >&2
  exit "$EXIT_TOOLING"
}

[[ -n "$TARGET_PATH" ]] || fail_usage "--path is required"
[[ -n "$OUT_ROOT" ]] || fail_usage "--out-root is required"
[[ "$TARGET_PATH" = /* ]] || fail_usage "--path must be an absolute path"
[[ "$OUT_ROOT" = /* ]] || fail_usage "--out-root must be an absolute path"

timestamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }
timestamp_file() { date -u +%Y%m%dT%H%M%SZ; }

slug() {
  printf '%s' "$1" | tr -c 'A-Za-z0-9._=-' '_' | tr -s '_' | cut -c1-120
}

quote_command() {
  local quoted_command=""
  local command_argument
  for command_argument in "$@"; do
    printf -v quoted_command '%s%q ' "$quoted_command" "$command_argument"
  done
  printf '%s' "${quoted_command% }"
}

log() {
  local message="$*"
  if [[ -n "$LOG_FILE" ]]; then
    printf '%s [INFO] %s\n' "$(timestamp)" "$message" | tee -a "$LOG_FILE"
  else
    printf '%s [INFO] %s\n' "$(timestamp)" "$message"
  fi
}

warn() {
  WARNINGS=$((WARNINGS + 1))
  printf '%s [WARN] %s\n' "$(timestamp)" "$*" | tee -a "$LOG_FILE" >&2
}

mark_failure() {
  FAILURES=$((FAILURES + 1))
  printf '%s [FAIL] %s\n' "$(timestamp)" "$*" | tee -a "$LOG_FILE" >&2
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail_tooling "Required command is missing: $1"
}

verify_fio() {
  require_command fio
  if ! fio --version 2>/dev/null | grep -Eq '^fio-[0-9]'; then
    fail_tooling "The fio command in PATH is not Flexible I/O Tester: $(command -v fio)"
  fi
}

duration_seconds() {
  local duration_text="$1"
  if [[ "$duration_text" =~ ^([0-9]+)([smhd]?)$ ]]; then
    local duration_value="${BASH_REMATCH[1]}"
    local duration_unit="${BASH_REMATCH[2]}"
    case "$duration_unit" in
    "" | s) printf '%s\n' "$duration_value" ;;
    m) printf '%s\n' $((duration_value * 60)) ;;
    h) printf '%s\n' $((duration_value * 3600)) ;;
    d) printf '%s\n' $((duration_value * 86400)) ;;
    esac
    return 0
  fi
  return 1
}

run_logged() {
  local command_name="$1"
  shift
  log "RUN $command_name: $(quote_command "$@")"
  set +e
  "$@" > >(tee -a "$RUN_DIRECTORY/$command_name.stdout") 2> >(tee -a "$RUN_DIRECTORY/$command_name.stderr" >&2)
  local command_status=$?
  set -e
  log "DONE $command_name exit=$command_status"
  return "$command_status"
}

capture_kernel_logs() {
  local snapshot_name="$1"
  local snapshot_directory="$RUN_DIRECTORY/$snapshot_name"
  mkdir -p "$snapshot_directory"
  journalctl -k --since "$STARTED_AT" --no-pager -o short-iso-precise >"$snapshot_directory/kernel_journal_since_start.log" 2>&1 || true
  dmesg -T >"$snapshot_directory/dmesg.log" 2>&1 || true
  findmnt -T "$TARGET_PATH" >"$snapshot_directory/findmnt.log" 2>&1 || true
}

mount_is_readonly() {
  findmnt -T "$TARGET_PATH" -no OPTIONS 2>/dev/null | grep -Eq '(^|,)ro(,|$)'
}

scan_storage_failures() {
  local source_file
  local storage_pattern='\bI/O error\b|buffer I/O error|blk_update_request|NVMe reset|NVMe timeout|NVMe controller down|SATA link reset|ATA exception|filesystem.*remount.*read-only|remounted read-only'
  for source_file in "$RUN_DIRECTORY/after/kernel_journal_since_start.log" "$RUN_DIRECTORY/after/dmesg.log"; do
    [[ -f "$source_file" ]] || continue
    if grep -Eiq "$storage_pattern" "$source_file"; then
      mark_failure "Kernel storage failure pattern found in $source_file"
    fi
  done
}

check_fio_json() {
  local fio_json="$1"
  if ! jq -e '[.jobs[]?.error // 0] | all(. == 0)' "$fio_json" >/dev/null 2>&1; then
    mark_failure "fio reported job errors in $fio_json"
  fi
}

cleanup_scratch() {
  if [[ "$CLEANUP" -eq 1 && -n "$SCRATCH_DIRECTORY" && -d "$SCRATCH_DIRECTORY" ]]; then
    case "$SCRATCH_DIRECTORY" in
    "$TARGET_PATH"/*) rm -rf --one-file-system "$SCRATCH_DIRECTORY" ;;
    *) mark_failure "Refusing cleanup outside target path: $SCRATCH_DIRECTORY" ;;
    esac
  fi
}

write_result() {
  local status="$1"
  local exit_code="$2"
  jq -n \
    --arg status "$status" \
    --arg result "$status" \
    --arg label "$LABEL" \
    --arg path "$TARGET_PATH" \
    --arg scratch_directory "$SCRATCH_DIRECTORY" \
    --arg started_at "$STARTED_AT" \
    --arg ended_at "$(timestamp)" \
    --arg out_root "$OUT_ROOT" \
    --arg run_directory "$RUN_DIRECTORY" \
    --arg size "$SIZE" \
    --arg runtime "$RUNTIME" \
    --argjson cleanup "$CLEANUP" \
    --argjson failures "$FAILURES" \
    --argjson warnings "$WARNINGS" \
    --argjson exit_code "$exit_code" \
    '{status: $status, result: $result, label: $label, path: $path, scratch_directory: $scratch_directory, started_at: $started_at, ended_at: $ended_at, out_root: $out_root, run_directory: $run_directory, size: $size, runtime: $runtime, cleanup: ($cleanup == 1), failures: $failures, warnings: $warnings, exit_code: $exit_code}' \
    >"$RUN_DIRECTORY/result.json" || fail_tooling "Could not write result.json"
}

for command_name in jq awk date tee grep rm mkdir sync findmnt journalctl dmesg readlink; do
  require_command "$command_name"
done
verify_fio

duration_seconds "$RUNTIME" >/dev/null || fail_usage "--runtime must be an integer with s, m, h, or d suffix"
TARGET_PATH="$(readlink -f "$TARGET_PATH")"
[[ -d "$TARGET_PATH" ]] || fail_usage "--path must be an existing directory"
[[ "$TARGET_PATH" != "/" ]] || fail_usage "Refusing to operate directly on /"

RUN_DIRECTORY="$OUT_ROOT/filesystem-scratch/$(timestamp_file)_$(slug "$LABEL")"
mkdir -p "$RUN_DIRECTORY"
LOG_FILE="$RUN_DIRECTORY/filesystem_scratch_test.log"
STARTED_AT="$(timestamp)"
SCRATCH_DIRECTORY="$TARGET_PATH/hw-validation-scratch-$(timestamp_file)-$(slug "$LABEL")"

log "run_directory=$RUN_DIRECTORY"
log "target_path=$TARGET_PATH scratch_directory=$SCRATCH_DIRECTORY size=$SIZE runtime=$RUNTIME cleanup=$CLEANUP"

if mount_is_readonly; then
  fail_usage "Target filesystem is mounted read-only: $TARGET_PATH"
fi

mkdir "$SCRATCH_DIRECTORY"
capture_kernel_logs before

if ! run_logged fio-sequential fio \
  --name=sequential_write_verify \
  --directory="$SCRATCH_DIRECTORY" \
  --filename=sequential.dat \
  --rw=write \
  --bs=1M \
  --size="$SIZE" \
  --direct=1 \
  --ioengine=psync \
  --verify=crc32c \
  --do_verify=1 \
  --verify_fatal=1 \
  --output-format=json \
  --output="$RUN_DIRECTORY/fio_sequential.json"; then
  mark_failure "fio sequential write+verify failed"
fi
check_fio_json "$RUN_DIRECTORY/fio_sequential.json"

if ! run_logged fio-random fio \
  --name=random_rw_verify \
  --directory="$SCRATCH_DIRECTORY" \
  --filename=random.dat \
  --rw=randrw \
  --rwmixread=70 \
  --bs=4k \
  --size="$SIZE" \
  --runtime="$RUNTIME" \
  --time_based=1 \
  --direct=1 \
  --ioengine=psync \
  --verify=crc32c \
  --verify_fatal=1 \
  --output-format=json \
  --output="$RUN_DIRECTORY/fio_random.json"; then
  mark_failure "fio random read/write verify failed"
fi
check_fio_json "$RUN_DIRECTORY/fio_random.json"

sync
capture_kernel_logs after
scan_storage_failures
if mount_is_readonly; then
  mark_failure "Filesystem remounted read-only during test: $TARGET_PATH"
fi
cleanup_scratch

if ((FAILURES > 0)); then
  RESULT_STATUS="FAIL"
  RESULT_EXIT="$EXIT_FAIL"
elif ((WARNINGS > 0)); then
  RESULT_STATUS="WARN"
  RESULT_EXIT="$EXIT_WARN"
else
  RESULT_STATUS="PASS"
  RESULT_EXIT="$EXIT_PASS"
fi

write_result "$RESULT_STATUS" "$RESULT_EXIT"
log "RESULT=$RESULT_STATUS failures=$FAILURES warnings=$WARNINGS result=$RUN_DIRECTORY/result.json"
exit "$RESULT_EXIT"
