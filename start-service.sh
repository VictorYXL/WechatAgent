#!/bin/sh
set -eu
umask 077
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if command -v uv >/dev/null 2>&1; then
    UV=uv
elif [ -x "$HOME/.local/bin/uv" ]; then
    UV="$HOME/.local/bin/uv"
else
    printf '%s\n' 'uv is required. Install it and run uv sync --project app first.' >&2
    exit 1
fi
exec "$UV" run --locked --project "$ROOT/app" python "$ROOT/scripts/control.py" start "$@"