#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="$repo_root/.venv/bin/python"
env_file="$repo_root/.env"
data_root="${KOLMO_DATA_ROOT:-$HOME/dat/all}"

if [[ -f "$env_file" ]]; then
    # shellcheck disable=SC1090
    source "$env_file"
    data_root="${KOLMO_DATA_ROOT:-$data_root}"
fi
if [[ -z "$data_root" || "$data_root" == "/" ]]; then
    echo "KOLMO_DATA_ROOT must resolve to a non-root directory." >&2
    exit 2
fi
export KOLMO_DATA_ROOT="$data_root"

if [[ ! -x "$python_bin" ]]; then
    echo "Kolmo virtual environment is missing. Run: $repo_root/install.sh" >&2
    exit 2
fi

if ! "$python_bin" -c "import pyarrow" >/dev/null 2>&1; then
    echo "Kolmo virtual environment is missing pyarrow. Run: $repo_root/install.sh" >&2
    exit 2
fi

cd "$repo_root"
exec "$python_bin" -m kolmo.ashare.build_vendor_minute_profile "$@"
