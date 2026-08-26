#!/usr/bin/env sh
# One-time setup: creates .venv and installs dependencies.
# Works with uv (fast) or falls back to python -m venv + pip.
set -e
CURRENT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$CURRENT_DIR"

if command -v uv >/dev/null 2>&1; then
  echo "==> using uv"
  uv venv --python 3.11 .venv
  uv pip install --python .venv/bin/python -r requirements.txt
else
  echo "==> uv not found, using python3 -m venv"
  python3 -m venv .venv
  ./.venv/bin/python -m pip install --upgrade pip
  ./.venv/bin/python -m pip install -r requirements.txt
fi

echo
echo "Done. Start the UI with:  ./webui.sh"
echo "Or the CLI with:          ./run_shorts.sh --help"
if ! command -v deno >/dev/null 2>&1; then
  echo
  echo "NOTE: no JavaScript runtime found. YouTube downloads need one:"
  echo "        brew install deno          (macOS)"
  echo "      Not needed if you only use local video files (--video)."
fi
