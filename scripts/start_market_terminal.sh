#!/usr/bin/env bash
set -euo pipefail

kolmo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="$kolmo_root/.env"

if [[ -f "$env_file" ]]; then
    # shellcheck disable=SC1090
    source "$env_file"
fi

if [[ ! -f "$kolmo_root/web/frontend/dist/index.html" ]]; then
    "$kolmo_root/scripts/build_market_terminal.sh"
fi

cd "$kolmo_root"
python_bin="$kolmo_root/.venv/bin/python"
if [[ ! -x "$python_bin" ]]; then
    python_bin="$(command -v python3)"
fi
exec "$python_bin" -m kolmo.viz.market_terminal "$@"
