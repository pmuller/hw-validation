#!/usr/bin/env bash
# Run generic CPU, memory, kernel, and platform stress validation.

set -Eeuo pipefail

EXIT_PASS=0
EXIT_FAIL=1
EXIT_WARN=2
EXIT_USAGE=64
EXIT_TOOLING=70

OUT_ROOT=""
LABEL="system-stress"
DURATION="8h"
MEM_PERCENT=75
MEMTESTER_AMOUNT=""
ALLOW_CORRECTED_ECC=0
ALLOW_THERMAL_THROTTLE=0
RUN_DIRECTORY=""
LOG_FILE=""
FAILURES=0
WARNINGS=0
STARTED_AT=""
STOP_FILE=""
MONITOR_PROCESS_IDS=()

usage() {
  cat <<'USAGE'
Usage:
  ./scripts/system_stress.sh --out-root /absolute/path [options]

Required:
  --out-root PATH              Absolute output root.

Options:
  --label LABEL                Run label. Default: system-stress
  --duration DURATION          Stress duration per phase. Default: 8h
  --mem-percent PERCENT        Memory percent for stress-ng and stressapptest. Default: 75
  --memtester-amount AMOUNT    Run memtester once with this amount, for example 8G.
  --allow-corrected-ecc        Corrected ECC during validation becomes a warning.
  --allow-thermal-throttle     Thermal throttling during validation becomes a warning.
  -h, --help                   Show this help.

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
  --out-root)
    OUT_ROOT="${2:-}"
    shift 2
    ;;
  --label)
    LABEL="${2:-}"
    shift 2
    ;;
  --duration)
    DURATION="${2:-}"
    shift 2
    ;;
  --mem-percent)
    MEM_PERCENT="${2:-}"
    shift 2
    ;;
  --memtester-amount)
    MEMTESTER_AMOUNT="${2:-}"
    shift 2
    ;;
  --allow-corrected-ecc)
    ALLOW_CORRECTED_ECC=1
    shift
    ;;
  --allow-thermal-throttle)
    ALLOW_THERMAL_THROTTLE=1
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

[[ -n "$OUT_ROOT" ]] || fail_usage "--out-root is required"
[[ "$OUT_ROOT" = /* ]] || fail_usage "--out-root must be an absolute path"
[[ "$MEM_PERCENT" =~ ^[0-9]+$ ]] || fail_usage "--mem-percent must be an integer"
((MEM_PERCENT >= 1 && MEM_PERCENT <= 95)) || fail_usage "--mem-percent must be between 1 and 95"

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

collect_snapshot() {
  local snapshot_directory="$1"
  mkdir -p "$snapshot_directory"
  journalctl -k --no-pager -o short-iso-precise >"$snapshot_directory/kernel_journal.log" 2>&1 || true
  dmesg -T >"$snapshot_directory/dmesg.log" 2>&1 || true
  edac-util --verbose >"$snapshot_directory/edac-util.log" 2>&1 || true
  ras-mc-ctl --summary >"$snapshot_directory/ras-summary.log" 2>&1 || true
  ras-mc-ctl --errors >"$snapshot_directory/ras-errors.log" 2>&1 || true
  sensors >"$snapshot_directory/sensors.log" 2>&1 || true
}

start_monitors() {
  vmstat 10 >"$RUN_DIRECTORY/vmstat.log" 2>&1 &
  MONITOR_PROCESS_IDS+=("$!")
  iostat -xz 10 >"$RUN_DIRECTORY/iostat.log" 2>&1 &
  MONITOR_PROCESS_IDS+=("$!")
  (
    while [[ ! -f "$STOP_FILE" ]]; do
      printf '%s\n' "$(timestamp)"
      sensors || true
      sleep 60
    done
  ) >"$RUN_DIRECTORY/sensors-loop.log" 2>&1 &
  MONITOR_PROCESS_IDS+=("$!")
}

stop_monitors() {
  touch "$STOP_FILE"
  local process_id
  for process_id in "${MONITOR_PROCESS_IDS[@]}"; do
    kill "$process_id" >/dev/null 2>&1 || true
  done
  wait >/dev/null 2>&1 || true
}

scan_kernel_failures() {
  local source_file
  local hard_pattern='Machine check exception|\bMCE\b|Hardware Error|EDAC.*uncorrected|uncorrected.*EDAC|PCIe AER fatal|AER.*fatal|PCIe AER nonfatal|AER.*non.?fatal|kernel oops|Oops:|panic|BUG:|soft lockup|hard lockup|hung task|critical temperature|watchdog|segfault'
  local corrected_ecc_pattern='ECC corrected|corrected ECC|EDAC.*corrected|corrected.*EDAC'
  local thermal_pattern='thermal throttling|throttled'
  for source_file in "$RUN_DIRECTORY/after/kernel_journal.log" "$RUN_DIRECTORY/after/dmesg.log"; do
    [[ -f "$source_file" ]] || continue
    if grep -Eiq "$hard_pattern" "$source_file"; then
      mark_failure "Kernel/platform hard failure pattern found in $source_file"
    fi
    if grep -Eiq "$corrected_ecc_pattern" "$source_file"; then
      if [[ "$ALLOW_CORRECTED_ECC" -eq 1 ]]; then
        warn "Corrected ECC pattern found in $source_file"
      else
        mark_failure "Corrected ECC pattern found in $source_file"
      fi
    fi
    if grep -Eiq "$thermal_pattern" "$source_file"; then
      if [[ "$ALLOW_THERMAL_THROTTLE" -eq 1 ]]; then
        warn "Thermal throttling pattern found in $source_file"
      else
        mark_failure "Thermal throttling pattern found in $source_file"
      fi
    fi
  done
}

write_result() {
  local status="$1"
  local exit_code="$2"
  jq -n \
    --arg status "$status" \
    --arg result "$status" \
    --arg label "$LABEL" \
    --arg started_at "$STARTED_AT" \
    --arg ended_at "$(timestamp)" \
    --arg out_root "$OUT_ROOT" \
    --arg run_directory "$RUN_DIRECTORY" \
    --arg duration "$DURATION" \
    --arg memtester_amount "$MEMTESTER_AMOUNT" \
    --argjson mem_percent "$MEM_PERCENT" \
    --argjson failures "$FAILURES" \
    --argjson warnings "$WARNINGS" \
    --argjson exit_code "$exit_code" \
    '{status: $status, result: $result, label: $label, started_at: $started_at, ended_at: $ended_at, out_root: $out_root, run_directory: $run_directory, duration: $duration, mem_percent: $mem_percent, memtester_amount: $memtester_amount, failures: $failures, warnings: $warnings, exit_code: $exit_code}' \
    >"$RUN_DIRECTORY/result.json" || fail_tooling "Could not write result.json"
}

for command_name in stress-ng stressapptest sensors vmstat iostat journalctl dmesg edac-util ras-mc-ctl jq awk date tee grep sleep free; do
  require_command "$command_name"
done

DURATION_SECONDS="$(duration_seconds "$DURATION")" || fail_usage "--duration must be an integer with s, m, h, or d suffix"
((DURATION_SECONDS > 0)) || fail_usage "--duration must be greater than zero"
TOTAL_MEMORY_MB="$(awk '/MemTotal/ {print int($2 / 1024)}' /proc/meminfo)"
STRESSAPPTEST_MB=$((TOTAL_MEMORY_MB * MEM_PERCENT / 100))
((STRESSAPPTEST_MB > 0)) || fail_usage "Computed stressapptest memory is zero"

RUN_DIRECTORY="$OUT_ROOT/system-stress/$(timestamp_file)_$(slug "$LABEL")"
mkdir -p "$RUN_DIRECTORY"
LOG_FILE="$RUN_DIRECTORY/system_stress.log"
STOP_FILE="$RUN_DIRECTORY/stop-monitors"
STARTED_AT="$(timestamp)"

trap stop_monitors EXIT

log "run_directory=$RUN_DIRECTORY"
log "duration=$DURATION duration_seconds=$DURATION_SECONDS mem_percent=$MEM_PERCENT stressapptest_mb=$STRESSAPPTEST_MB"
collect_snapshot "$RUN_DIRECTORY/before"
start_monitors

if ! run_logged stress-ng stress-ng --cpu 0 --cpu-method all --matrix 0 --vm 2 --vm-bytes "${MEM_PERCENT}%" --verify --metrics-brief --timeout "$DURATION"; then
  mark_failure "stress-ng failed"
fi

if ! run_logged stressapptest stressapptest -W -s "$DURATION_SECONDS" -M "$STRESSAPPTEST_MB"; then
  mark_failure "stressapptest failed"
fi

if [[ -n "$MEMTESTER_AMOUNT" ]]; then
  if ! run_logged memtester memtester "$MEMTESTER_AMOUNT" 1; then
    mark_failure "memtester failed"
  fi
fi

collect_snapshot "$RUN_DIRECTORY/after"
scan_kernel_failures

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
