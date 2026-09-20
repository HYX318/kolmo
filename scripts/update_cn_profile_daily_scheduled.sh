#!/usr/bin/env bash
set -euo pipefail

kolmo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="$kolmo_root/.env"
data_root="${KOLMO_DATA_ROOT:-$HOME/dat/all}"

if [[ -f "$env_file" ]]; then
    # User-local configuration for KOLMO_DATA_ROOT and optional PATH overrides.
    # shellcheck disable=SC1090
    source "$env_file"
    data_root="${KOLMO_DATA_ROOT:-$data_root}"
fi

export KOLMO_DATA_ROOT="$data_root"
python_bin="$kolmo_root/.venv/bin/python"
if [[ ! -x "$python_bin" ]]; then
    python_bin="$(command -v python3)"
fi
mkdir -p "$KOLMO_DATA_ROOT/logs/kolmo" "$KOLMO_DATA_ROOT/work/ashare"

lock_dir="$KOLMO_DATA_ROOT/work/ashare/update_cn_profile_daily.lock"
log_file="$KOLMO_DATA_ROOT/logs/kolmo/update_cn_profile_daily.log"
status_file="$KOLMO_DATA_ROOT/logs/kolmo/update_cn_profile_daily_latest.json"
alert_file="$KOLMO_DATA_ROOT/logs/kolmo/update_cn_profile_daily.alert.json"

if [[ -f "$log_file" ]] && [[ "$(wc -c <"$log_file")" -ge 20971520 ]]; then
    mv -f "$log_file" "$log_file.1"
fi

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
trap 'rm -f "$lock_dir/pid" "$lock_dir/started_at" "${status_tmp:-}"; rmdir "$lock_dir"' EXIT

{
    printf '{"event":"scheduled_update_started","time":"%s"}\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')"
    cd "$kolmo_root"
    trade_date="$(date '+%Y%m%d')"
    trading_day=true
    status_tmp="$status_file.tmp.$$"
    printf '{"time":"%s","trading_day":null,"ok":false,"status":"running"}\n' \
        "$(date '+%Y-%m-%dT%H:%M:%S%z')" >"$status_tmp"
    mv -f "$status_tmp" "$status_file"
    status_tmp=""
    qfq_update_status=0
    "$python_bin" -m kolmo.scheduler.update_cn_profile_if_trading_day \
        --calendar baostock \
        --skip-exit-code 20 \
        --calendar-timeout-seconds 15 \
        --update-timeout-seconds 4000 \
        -- --target all --adjust qfq --workers 4 --exchange-timeout-seconds 1800 || qfq_update_status=$?
    raw_update_status=0
    if [[ "$qfq_update_status" -eq 20 ]]; then
        trading_day=false
        qfq_update_status=0
    elif [[ "$qfq_update_status" -eq 0 ]]; then
        "$python_bin" -m kolmo.scheduler.process_timeout \
            --timeout-seconds 4000 --label raw-profile-update -- \
            "$python_bin" -m kolmo.ashare.update_cn_profile_daily \
            --end-date "$trade_date" --target all --adjust raw --workers 4 \
            --exchange-timeout-seconds 1800 || raw_update_status=$?
    else
        # A failed calendar/qfq stage can indicate a provider outage or
        # blacklist. Do not multiply requests by starting the raw stage.
        raw_update_status=99
    fi
    update_status=0
    if [[ "$qfq_update_status" -ne 0 || "$raw_update_status" -ne 0 ]]; then
        update_status=1
    fi
    health_status=0
    health_command=(
        "$python_bin" -m kolmo.ashare.profile_health_check
        --days 20
        --json-output "$KOLMO_DATA_ROOT/logs/kolmo/profile_health_latest.json"
        --csv-output "$KOLMO_DATA_ROOT/logs/kolmo/profile_health_latest.csv"
    )
    if [[ "$trading_day" == true ]]; then
        health_command+=(--expected-latest-date "$trade_date")
    fi
    "$python_bin" -m kolmo.scheduler.process_timeout \
        --timeout-seconds 600 --label profile-health -- "${health_command[@]}" || health_status=$?
    raw_health_status=0
    raw_health_command=(
        "$python_bin" -m kolmo.ashare.profile_health_check
        --profile-root "$KOLMO_DATA_ROOT/profile/daily_raw"
        --days 20
        --json-output "$KOLMO_DATA_ROOT/logs/kolmo/profile_raw_health_latest.json"
        --csv-output "$KOLMO_DATA_ROOT/logs/kolmo/profile_raw_health_latest.csv"
    )
    if [[ "$trading_day" == true ]]; then
        raw_health_command+=(--expected-latest-date "$trade_date")
    fi
    "$python_bin" -m kolmo.scheduler.process_timeout \
        --timeout-seconds 600 --label raw-profile-health -- "${raw_health_command[@]}" || raw_health_status=$?
    reference_status=0
    "$python_bin" -m kolmo.scheduler.process_timeout \
        --timeout-seconds 300 --label security-master -- \
        "$python_bin" -m kolmo.reference.security_master --as-of-date "$trade_date" || reference_status=$?
    snapshot_status=0
    if [[ "$trading_day" == true && "$update_status" -eq 0 && "$health_status" -eq 0 && "$raw_health_status" -eq 0 && "$reference_status" -eq 0 ]]; then
        "$python_bin" -m kolmo.scheduler.process_timeout \
            --timeout-seconds 300 --label profile-snapshot -- \
            "$python_bin" -m kolmo.catalog.profile_snapshot publish --latest-days 80 || snapshot_status=$?
    elif [[ "$trading_day" == true ]]; then
        snapshot_status=2
    fi
    finished_at="$(date '+%Y-%m-%dT%H:%M:%S%z')"
    result_status=0
    if [[ "$update_status" -ne 0 || "$health_status" -ne 0 || "$raw_health_status" -ne 0 || "$reference_status" -ne 0 || "$snapshot_status" -ne 0 ]]; then
        result_status=1
    fi
    printf '{"event":"scheduled_update_finished","time":"%s","trading_day":%s,"update_status":%s,"qfq_update_status":%s,"raw_update_status":%s,"health_status":%s,"raw_health_status":%s,"reference_status":%s,"snapshot_status":%s}\n' \
        "$finished_at" "$trading_day" "$update_status" "$qfq_update_status" "$raw_update_status" "$health_status" "$raw_health_status" "$reference_status" "$snapshot_status"
    status_tmp="$status_file.tmp.$$"
    printf '{"time":"%s","trading_day":%s,"ok":%s,"update_status":%s,"qfq_update_status":%s,"raw_update_status":%s,"health_status":%s,"raw_health_status":%s,"reference_status":%s,"snapshot_status":%s}\n' \
        "$finished_at" "$trading_day" "$([[ "$result_status" -eq 0 ]] && printf true || printf false)" \
        "$update_status" "$qfq_update_status" "$raw_update_status" "$health_status" "$raw_health_status" "$reference_status" "$snapshot_status" >"$status_tmp"
    mv -f "$status_tmp" "$status_file"
    status_tmp=""
    if [[ "$result_status" -eq 0 ]]; then
        rm -f "$alert_file"
    else
        cp "$status_file" "$alert_file"
    fi
    exit "$result_status"
} >>"$log_file" 2>&1
