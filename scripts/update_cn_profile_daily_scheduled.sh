#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
kolmo_root="$repo_root/kolmo"
env_file="$repo_root/.quant-lab.env"
data_root="${KOLMO_DATA_ROOT:-$HOME/dat/all}"

if [[ -f "$env_file" ]]; then
    # User-local configuration for KOLMO_DATA_ROOT and optional PATH overrides.
    # shellcheck disable=SC1090
    source "$env_file"
    data_root="${KOLMO_DATA_ROOT:-$data_root}"
fi

export KOLMO_DATA_ROOT="$data_root"
mkdir -p "$KOLMO_DATA_ROOT/logs/kolmo" "$KOLMO_DATA_ROOT/work/ashare"

lock_dir="$KOLMO_DATA_ROOT/work/ashare/update_cn_profile_daily.lock"
log_file="$KOLMO_DATA_ROOT/logs/kolmo/update_cn_profile_daily.log"

if ! mkdir "$lock_dir" 2>/dev/null; then
    lock_pid="$(cat "$lock_dir/pid" 2>/dev/null || true)"
    lock_command="$(ps -p "$lock_pid" -o command= 2>/dev/null || true)"
    if [[ "$lock_pid" =~ ^[0-9]+$ && "$lock_command" == *"update_cn_profile_daily_scheduled.sh"* ]]; then
        printf '{"event":"scheduled_update_skipped","reason":"active_lock","pid":%s,"time":"%s"}\n' \
            "$lock_pid" "$(date '+%Y-%m-%dT%H:%M:%S%z')" >>"$log_file"
        exit 75
    fi
    rm -f "$lock_dir/pid" "$lock_dir/started_at"
    if ! rmdir "$lock_dir" 2>/dev/null || ! mkdir "$lock_dir" 2>/dev/null; then
        printf '{"event":"scheduled_update_failed","reason":"lock_recovery_failed","time":"%s"}\n' \
            "$(date '+%Y-%m-%dT%H:%M:%S%z')" >>"$log_file"
        exit 1
    fi
fi
printf '%s\n' "$$" >"$lock_dir/pid"
date '+%Y-%m-%dT%H:%M:%S%z' >"$lock_dir/started_at"
trap 'rm -f "$lock_dir/pid" "$lock_dir/started_at"; rmdir "$lock_dir"' EXIT

{
    printf '{"event":"scheduled_update_started","time":"%s"}\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')"
    cd "$kolmo_root"
    update_status=0
    python3 -m kolmo.scheduler.update_cn_profile_if_trading_day --calendar baostock -- --target all --workers 4 || update_status=$?
    health_status=0
    python3 -m kolmo.ashare.profile_health_check \
        --days 20 \
        --json-output "$KOLMO_DATA_ROOT/logs/kolmo/profile_health_latest.json" \
        --csv-output "$KOLMO_DATA_ROOT/logs/kolmo/profile_health_latest.csv" || health_status=$?
    reference_status=0
    python3 -m kolmo.reference.security_master --as-of-date "$(date '+%Y%m%d')" || reference_status=$?
    snapshot_status=0
    if [[ "$update_status" -eq 0 && "$health_status" -eq 0 && "$reference_status" -eq 0 ]]; then
        python3 -m kolmo.catalog.profile_snapshot publish --latest-days 80 || snapshot_status=$?
    else
        snapshot_status=2
    fi
    printf '{"event":"scheduled_update_finished","time":"%s","update_status":%s,"health_status":%s,"reference_status":%s,"snapshot_status":%s}\n' \
        "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$update_status" "$health_status" "$reference_status" "$snapshot_status"
    if [[ "$update_status" -ne 0 || "$health_status" -ne 0 || "$reference_status" -ne 0 ]]; then
        exit 1
    fi
    if [[ "$snapshot_status" -ne 0 ]]; then
        exit 1
    fi
} >>"$log_file" 2>&1
