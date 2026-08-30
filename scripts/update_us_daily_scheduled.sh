#!/usr/bin/env bash
set -euo pipefail

kolmo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="$kolmo_root/.env"
data_root="${KOLMO_DATA_ROOT:-$HOME/dat/all}"

if [[ -f "$env_file" ]]; then
    # shellcheck disable=SC1090
    source "$env_file"
    data_root="${KOLMO_DATA_ROOT:-$data_root}"
fi

export KOLMO_DATA_ROOT="$data_root"
python_bin="$kolmo_root/.venv/bin/python"
if [[ ! -x "$python_bin" ]]; then
    python_bin="$(command -v python3)"
fi
mkdir -p "$KOLMO_DATA_ROOT/logs/kolmo" "$KOLMO_DATA_ROOT/work/US"

log_file="$KOLMO_DATA_ROOT/logs/kolmo/update_us_daily.log"
lock_dir="$KOLMO_DATA_ROOT/work/US/update_us_daily.lock"

if [[ -z "${TIINGO_API_TOKEN:-}" ]]; then
    printf '{"event":"us_daily_update_failed","reason":"missing_tiingo_token","time":"%s"}\n' \
        "$(date '+%Y-%m-%dT%H:%M:%S%z')" >>"$log_file"
    exit 2
fi

if ! mkdir "$lock_dir" 2>/dev/null; then
    lock_pid="$(cat "$lock_dir/pid" 2>/dev/null || true)"
    lock_command="$(ps -p "$lock_pid" -o command= 2>/dev/null || true)"
    if [[ "$lock_pid" =~ ^[0-9]+$ && "$lock_command" == *"update_us_daily_scheduled.sh"* ]]; then
        printf '{"event":"us_daily_update_skipped","reason":"active_lock","pid":%s,"time":"%s"}\n' \
            "$lock_pid" "$(date '+%Y-%m-%dT%H:%M:%S%z')" >>"$log_file"
        exit 75
    fi
    rm -f "$lock_dir/pid" "$lock_dir/started_at"
    if ! rmdir "$lock_dir" 2>/dev/null || ! mkdir "$lock_dir" 2>/dev/null; then
        printf '{"event":"us_daily_update_failed","reason":"lock_recovery_failed","time":"%s"}\n' \
            "$(date '+%Y-%m-%dT%H:%M:%S%z')" >>"$log_file"
        exit 1
    fi
fi
printf '%s\n' "$$" >"$lock_dir/pid"
date '+%Y-%m-%dT%H:%M:%S%z' >"$lock_dir/started_at"
trap 'rm -f "$lock_dir/pid" "$lock_dir/started_at"; rmdir "$lock_dir"' EXIT

{
    printf '{"event":"us_daily_update_started","time":"%s"}\n' \
        "$(date '+%Y-%m-%dT%H:%M:%S%z')"
    cd "$kolmo_root"

    fetch_status=0
    "$python_bin" -m kolmo.us_market.fetch_daily || fetch_status=$?

    validation_status=0
    "$python_bin" -m kolmo.us_market.validate_daily \
        --json-output "$KOLMO_DATA_ROOT/validation/US/daily_latest.json" || validation_status=$?

    drawdown_status=0
    "$python_bin" -m kolmo.us_market.drawdown || drawdown_status=$?

    printf '{"event":"us_daily_update_finished","time":"%s","fetch_status":%s,"validation_status":%s,"drawdown_status":%s}\n' \
        "$(date '+%Y-%m-%dT%H:%M:%S%z')" \
        "$fetch_status" "$validation_status" "$drawdown_status"

    if [[ "$fetch_status" -ne 0 || "$validation_status" -ne 0 || "$drawdown_status" -ne 0 ]]; then
        exit 1
    fi
} >>"$log_file" 2>&1
