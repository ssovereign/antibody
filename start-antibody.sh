#!/usr/bin/env bash
# Set up and start ANTIBODY (macOS / Linux). Needs nothing preinstalled:
# uv (a small Python manager) is installed if missing, and it downloads Python 3.12 for ANTIBODY.
set -e
cd "$(dirname "$0")"

find_uv() {
  if command -v uv >/dev/null 2>&1; then command -v uv; return; fi
  for p in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
    if [ -x "$p" ]; then echo "$p"; return; fi
  done
}

UV="$(find_uv || true)"
if [ -z "$UV" ]; then
  echo "Installing uv (Python manager, from astral.sh)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  UV="$(find_uv || true)"
fi
if [ -z "$UV" ]; then
  echo "uv could not be installed. Install it from https://docs.astral.sh/uv/ and run this again."
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "Setting up Python 3.12 for ANTIBODY..."
  "$UV" venv --python 3.12 .venv
fi
HASH="$( (sha256sum requirements.lock 2>/dev/null || shasum -a 256 requirements.lock) | cut -d' ' -f1)"
if [ ! -f .venv/.antibody-deps ] || [ "$(cat .venv/.antibody-deps)" != "$HASH" ]; then
  echo "Installing ANTIBODY's packages..."
  "$UV" pip install --quiet --python .venv/bin/python -r requirements.lock
  echo "$HASH" > .venv/.antibody-deps
fi

exec .venv/bin/python scripts/launch.py
