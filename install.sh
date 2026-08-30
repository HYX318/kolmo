#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
venv_root="$project_root/.venv"
local_bin="$HOME/.local/bin"

for command_name in python3 cmake npm; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "Missing required command: $command_name" >&2
        exit 2
    fi
done

if [[ ! -x "$venv_root/bin/python" ]]; then
    python3 -m venv "$venv_root"
fi

# pip needs PySocks before it can use a socks:// proxy. Bootstrap that tiny
# dependency over a direct connection to avoid a fresh-venv chicken-and-egg.
proxy_url="${ALL_PROXY:-${all_proxy:-${HTTPS_PROXY:-${https_proxy:-}}}}"
if [[ "$proxy_url" == socks://* || "$proxy_url" == socks4://* || "$proxy_url" == socks5://* || "$proxy_url" == socks5h://* ]]; then
    if ! "$venv_root/bin/python" -c "import socks" >/dev/null 2>&1; then
        env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy \
            -u HTTPS_PROXY -u https_proxy \
            "$venv_root/bin/python" -m pip install PySocks
    fi
fi

"$venv_root/bin/python" -m pip install --upgrade pip setuptools
"$venv_root/bin/python" -m pip install --editable "$project_root"

cmake --fresh -S "$project_root/tools/raw_scan" \
    -B "$project_root/build/raw_scan" \
    -DCMAKE_BUILD_TYPE=Release
cmake --build "$project_root/build/raw_scan" --parallel

"$project_root/scripts/build_market_terminal.sh"

if [[ ! -f "$project_root/.env" ]]; then
    cp "$project_root/.env.example" "$project_root/.env"
fi
chmod 600 "$project_root/.env"

mkdir -p "$local_bin"
ln -sfn "$venv_root/bin/kolmo" "$local_bin/kolmo"

echo
echo "Kolmo installation complete."
echo "Configuration: $project_root/.env"
if [[ ":$PATH:" != *":$local_bin:"* ]]; then
    echo "Add Kolmo to this shell, then persist the same line in your shell profile:"
    echo "  export PATH=\"$local_bin:\$PATH\""
fi
echo "Start the terminal with: kolmo web"
