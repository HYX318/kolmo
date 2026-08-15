#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
label="com.galoishuang.kolmo.update-cn-profile-daily"
agent_dir="$HOME/Library/LaunchAgents"
plist="$agent_dir/$label.plist"
wrapper="$repo_root/kolmo/scripts/update_cn_profile_daily_scheduled.sh"

usage() {
    cat <<EOF
Usage:
  kolmo/scripts/install_update_launchagent.sh install    Install and load the 17:00 scheduled update.
  kolmo/scripts/install_update_launchagent.sh uninstall  Unload and remove the LaunchAgent.
  kolmo/scripts/install_update_launchagent.sh print      Print the plist path and current launchctl state.

The job runs every day at 17:00 local time. The wrapper checks the A-share
trading calendar before updating, so weekends and exchange holidays are skipped.
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
    <integer>17</integer>
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
