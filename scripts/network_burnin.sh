#!/usr/bin/env bash
# Run generic network validation with iperf3 and NIC health checks.

set -Eeuo pipefail

EXIT_PASS=0
EXIT_FAIL=1
EXIT_WARN=2
EXIT_USAGE=64
EXIT_TOOLING=70

SERVER=""
OUT_ROOT=""
LABEL="network-burnin"
INTERFACE=""
DURATION="1h"
PARALLEL=1
BIDIR=0
EXPECT_BANDWIDTH=""
RUN_DIRECTORY=""
LOG_FILE=""
FAILURES=0
WARNINGS=0
STARTED_AT=""

usage() {
  cat <<'USAGE'
Usage:
  ./scripts/network_burnin.sh --server HOST --out-root /absolute/path [options]

Required:
  --server HOST               iperf3 server address or name.
  --out-root PATH             Absolute output root.

Options:
  --label LABEL               Run label. Default: network-burnin
  --interface NAME            Network interface. Inferred with ip route get when omitted.
  --duration DURATION         iperf3 duration. Default: 1h
  --parallel COUNT            iperf3 parallel streams. Default: 1
  --bidir                     Run iperf3 bidirectional mode.
  --expect-bandwidth RATE     Minimum bits per second, for example 1G or 900M.
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
  --server)
    SERVER="${2:-}"
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
  --interface)
    INTERFACE="${2:-}"
    shift 2
    ;;
  --duration)
    DURATION="${2:-}"
    shift 2
    ;;
  --parallel)
    PARALLEL="${2:-}"
    shift 2
    ;;
  --bidir)
    BIDIR=1
    shift
    ;;
  --expect-bandwidth)
    EXPECT_BANDWIDTH="${2:-}"
    shift 2
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

[[ -n "$SERVER" ]] || fail_usage "--server is required"
[[ -n "$OUT_ROOT" ]] || fail_usage "--out-root is required"
[[ "$OUT_ROOT" = /* ]] || fail_usage "--out-root must be an absolute path"
[[ "$PARALLEL" =~ ^[0-9]+$ ]] || fail_usage "--parallel must be an integer"
((PARALLEL >= 1)) || fail_usage "--parallel must be at least 1"

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

bandwidth_bits() {
  local bandwidth_text="$1"
  if [[ "$bandwidth_text" =~ ^([0-9]+)([KkMmGgTt]?)$ ]]; then
    local bandwidth_value="${BASH_REMATCH[1]}"
    local bandwidth_unit="${BASH_REMATCH[2]}"
    case "$bandwidth_unit" in
    "") printf '%s\n' "$bandwidth_value" ;;
    K | k) printf '%s\n' $((bandwidth_value * 1000)) ;;
    M | m) printf '%s\n' $((bandwidth_value * 1000 * 1000)) ;;
    G | g) printf '%s\n' $((bandwidth_value * 1000 * 1000 * 1000)) ;;
    T | t) printf '%s\n' $((bandwidth_value * 1000 * 1000 * 1000 * 1000)) ;;
    esac
    return 0
  fi
  return 1
}

infer_interface() {
  ip route get "$SERVER" | awk '{for (field_index = 1; field_index <= NF; field_index++) if ($field_index == "dev") {print $(field_index + 1); exit}}'
}

capture_interface_snapshot() {
  local snapshot_name="$1"
  local snapshot_directory="$RUN_DIRECTORY/$snapshot_name"
  mkdir -p "$snapshot_directory"
  ip link >"$snapshot_directory/ip_link.log" 2>&1 || true
  ip addr >"$snapshot_directory/ip_addr.log" 2>&1 || true
  ethtool "$INTERFACE" >"$snapshot_directory/ethtool.log" 2>&1 || true
  ethtool -i "$INTERFACE" >"$snapshot_directory/ethtool_driver.log" 2>&1 || true
  ethtool -S "$INTERFACE" >"$snapshot_directory/ethtool_stats.log" 2>&1 || true
  cat "/sys/class/net/$INTERFACE/operstate" >"$snapshot_directory/operstate" 2>&1 || true
  journalctl -k --since "$STARTED_AT" --no-pager -o short-iso-precise >"$snapshot_directory/kernel_journal_since_start.log" 2>&1 || true
  dmesg -T >"$snapshot_directory/dmesg.log" 2>&1 || true
}

extract_error_stats() {
  awk -F: '
    BEGIN {IGNORECASE = 1}
    $1 ~ /(rx|tx).*error|crc|frame|reset|timeout/ {
      stat_name = $1
      stat_value = $2
      gsub(/^[ \t]+|[ \t]+$/, "", stat_name)
      gsub(/^[ \t]+|[ \t]+$/, "", stat_value)
      if (stat_value ~ /^[0-9]+$/) print stat_name "\t" stat_value
    }
  ' "$1"
}

compare_error_stats() {
  local before_stats="$RUN_DIRECTORY/before/error_stats.tsv"
  local after_stats="$RUN_DIRECTORY/after/error_stats.tsv"
  extract_error_stats "$RUN_DIRECTORY/before/ethtool_stats.log" >"$before_stats"
  extract_error_stats "$RUN_DIRECTORY/after/ethtool_stats.log" >"$after_stats"
  local stat_name
  local before_value
  local after_value
  while IFS=$'\t' read -r stat_name before_value; do
    [[ -n "$stat_name" ]] || continue
    after_value="$(awk -F '\t' -v searched_name="$stat_name" '$1 == searched_name {print $2; exit}' "$after_stats")"
    [[ -n "$after_value" ]] || continue
    if ((after_value > before_value)); then
      mark_failure "Interface counter increased: $stat_name before=$before_value after=$after_value"
    fi
  done <"$before_stats"
}

scan_kernel_network_failures() {
  local source_file="$RUN_DIRECTORY/after/kernel_journal_since_start.log"
  local network_pattern='link down|link up|link flap|NIC.*reset|adapter.*reset|network.*driver.*reset|transmit timeout|tx timeout|watchdog timeout'
  [[ -f "$source_file" ]] || return 0
  if grep -Eiq "$network_pattern" "$source_file"; then
    mark_failure "Kernel network reset, timeout, or link event found in $source_file"
  fi
}

run_iperf() {
  local iperf_command=(iperf3 -J -c "$SERVER" -t "$DURATION_SECONDS" -P "$PARALLEL")
  if [[ "$BIDIR" -eq 1 ]]; then
    iperf_command+=(--bidir)
  fi
  log "RUN iperf3: $(quote_command "${iperf_command[@]}")"
  set +e
  "${iperf_command[@]}" > >(tee "$RUN_DIRECTORY/iperf3.json") 2> >(tee -a "$RUN_DIRECTORY/iperf3.stderr" >&2)
  local iperf_status=$?
  set -e
  log "DONE iperf3 exit=$iperf_status"
  return "$iperf_status"
}

check_bandwidth() {
  [[ -n "$EXPECT_BANDWIDTH" ]] || return 0
  local expected_bits
  expected_bits="$(bandwidth_bits "$EXPECT_BANDWIDTH")" || fail_usage "--expect-bandwidth must be an integer with K, M, G, or T suffix"
  local measured_bits
  measured_bits="$(jq '[.end.sum.bits_per_second?, .end.sum_received.bits_per_second?, .end.sum_sent.bits_per_second?] | map(select(. != null)) | min // 0' "$RUN_DIRECTORY/iperf3.json")"
  log "bandwidth_bits measured=$measured_bits expected=$expected_bits"
  if ! awk -v measured_value="$measured_bits" -v expected_value="$expected_bits" 'BEGIN {exit(measured_value + 0 >= expected_value + 0 ? 0 : 1)}'; then
    mark_failure "Throughput below expectation: measured=$measured_bits expected=$expected_bits"
  fi
}

write_result() {
  local status="$1"
  local exit_code="$2"
  jq -n \
    --arg status "$status" \
    --arg result "$status" \
    --arg server "$SERVER" \
    --arg interface "$INTERFACE" \
    --arg label "$LABEL" \
    --arg started_at "$STARTED_AT" \
    --arg ended_at "$(timestamp)" \
    --arg out_root "$OUT_ROOT" \
    --arg run_directory "$RUN_DIRECTORY" \
    --arg duration "$DURATION" \
    --arg expect_bandwidth "$EXPECT_BANDWIDTH" \
    --argjson parallel "$PARALLEL" \
    --argjson bidir "$BIDIR" \
    --argjson failures "$FAILURES" \
    --argjson warnings "$WARNINGS" \
    --argjson exit_code "$exit_code" \
    '{status: $status, result: $result, server: $server, interface: $interface, label: $label, started_at: $started_at, ended_at: $ended_at, out_root: $out_root, run_directory: $run_directory, duration: $duration, parallel: $parallel, bidir: ($bidir == 1), expect_bandwidth: $expect_bandwidth, failures: $failures, warnings: $warnings, exit_code: $exit_code}' \
    >"$RUN_DIRECTORY/result.json" || fail_tooling "Could not write result.json"
}

for command_name in iperf3 ip ethtool journalctl dmesg jq awk date tee grep cat; do
  require_command "$command_name"
done

DURATION_SECONDS="$(duration_seconds "$DURATION")" || fail_usage "--duration must be an integer with s, m, h, or d suffix"
((DURATION_SECONDS > 0)) || fail_usage "--duration must be greater than zero"

if [[ -z "$INTERFACE" ]]; then
  INTERFACE="$(infer_interface)"
fi
[[ -n "$INTERFACE" ]] || fail_usage "Could not infer interface. Provide --interface."
[[ -d "/sys/class/net/$INTERFACE" ]] || fail_usage "Interface does not exist: $INTERFACE"

RUN_DIRECTORY="$OUT_ROOT/network-burnin/$(timestamp_file)_$(slug "$LABEL")"
mkdir -p "$RUN_DIRECTORY"
LOG_FILE="$RUN_DIRECTORY/network_burnin.log"
STARTED_AT="$(timestamp)"

log "run_directory=$RUN_DIRECTORY"
log "server=$SERVER interface=$INTERFACE duration=$DURATION parallel=$PARALLEL bidir=$BIDIR"
capture_interface_snapshot before

if [[ "$(cat "/sys/class/net/$INTERFACE/operstate" 2>/dev/null || true)" != "up" ]]; then
  mark_failure "Interface is not up before test: $INTERFACE"
fi

if ! run_iperf; then
  mark_failure "iperf3 failed"
fi

capture_interface_snapshot after

if [[ "$(cat "/sys/class/net/$INTERFACE/operstate" 2>/dev/null || true)" != "up" ]]; then
  mark_failure "Interface is not up after test: $INTERFACE"
fi

compare_error_stats
scan_kernel_network_failures
if [[ -f "$RUN_DIRECTORY/iperf3.json" ]]; then
  check_bandwidth
fi

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
