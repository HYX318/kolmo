#!/usr/bin/env bash
set -euo pipefail

kolmo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
frontend_root="$kolmo_root/web/frontend"

if ! command -v npm >/dev/null 2>&1; then
    echo "npm is required to build the Kolmo market terminal frontend." >&2
    exit 2
fi

cd "$frontend_root"
if [[ -f package-lock.json ]]; then
    npm ci
else
    npm install
fi
npm run build

echo "built: $frontend_root/dist"
