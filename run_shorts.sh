#!/usr/bin/env sh
# Convenience launcher. macOS caps open file descriptors low by default and
# parallel ffmpeg encodes can hit it, same as MoneyPrinterTurbo does.
set -e
CURRENT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
HARD=$(ulimit -Hn); WANT=10240
if [ "$HARD" != "unlimited" ] && [ "$HARD" -lt "$WANT" ] 2>/dev/null; then WANT="$HARD"; fi
ulimit -n "$WANT" 2>/dev/null || true
exec "$CURRENT_DIR/.venv/bin/python" "$CURRENT_DIR/make_shorts.py" "$@"
