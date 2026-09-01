#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

if [[ -f "$repo_root/.env" ]]; then
    # shellcheck disable=SC1091
    set -a
    source "$repo_root/.env"
    set +a
fi

python_bin="$repo_root/.venv/bin/python"
if [[ ! -x "$python_bin" ]]; then
    python_bin="$(command -v python3)"
fi

exec "$python_bin" -m kolmo.fundamental.fetch_sec_edgar "$@"
