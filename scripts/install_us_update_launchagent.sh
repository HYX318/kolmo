#!/usr/bin/env bash
set -euo pipefail

kolmo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
label="com.galoishuang.kolmo.update-us-daily"
agent_dir="$HOME/Library/LaunchAgents"
plist="$agent_dir/$label.plist"
wrapper="$kolmo_root/scripts/update_us_daily_scheduled.sh"

usage() {
    cat <<EOF
Usage:
  scripts/install_us_update_launchagent.sh install
  scripts/install_us_update_launchagent.sh uninstall
  scripts/install_us_update_launchagent.sh print

The job runs every day at 10:00 local time. This is after the US session and
Tiingo's overnight correction window in Asia/Shanghai. Weekend and US-holiday
runs are harmless overlapping updates because no synthetic sessions are added.
EOF
}

install_agent() {
    mkdir -p "$agent_dir"
    cat >"$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>$wrapper</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>10</integer>
    <key>Minute</key>
    <integer>0</integer>
  </dict>
  <key>RunAtLoad</key>
  <false/>
  <key>StandardOutPath</key>
  <string>/tmp/$label.out</string>
  <key>StandardErrorPath</key>
  <string>/tmp/$label.err</string>
</dict>
</plist>
EOF
    chmod +x "$wrapper"
    launchctl unload "$plist" >/dev/null 2>&1 || true
    launchctl load "$plist"
    echo "installed: $plist"
    echo "label: $label"
}

uninstall_agent() {
    launchctl unload "$plist" >/dev/null 2>&1 || true
    rm -f "$plist"
    echo "removed: $plist"
}

print_state() {
    echo "plist: $plist"
    echo "label: $label"
    launchctl list | grep "$label" || true
}

command="${1:-}"
case "$command" in
    install) install_agent ;;
    uninstall) uninstall_agent ;;
    print) print_state ;;
    help|-h|--help) usage ;;
    *)
        usage >&2
        exit 2
        ;;
esac
