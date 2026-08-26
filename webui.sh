#!/usr/bin/env sh
# Launch the Shorts Maker UI. Raises the macOS file-descriptor limit first,
# since parallel ffmpeg encodes can otherwise hit "Errno 24 Too many open files".
set -e
CURRENT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
HARD=$(ulimit -Hn); WANT=10240
if [ "$HARD" != "unlimited" ] && [ "$HARD" -lt "$WANT" ] 2>/dev/null; then WANT="$HARD"; fi
ulimit -n "$WANT" 2>/dev/null || true
PORT="${SHORTS_PORT:-8502}"
echo "***** Shorts Maker: http://127.0.0.1:$PORT *****"
exec "$CURRENT_DIR/.venv/bin/python" -m streamlit run "$CURRENT_DIR/webui.py" \
  --server.address=127.0.0.1 --server.port="$PORT" \
  --browser.gatherUsageStats=False --client.toolbarMode=minimal \
  --logger.hideWelcomeMessage=True --server.showEmailPrompt=False
