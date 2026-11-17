#!/usr/bin/env bash
# Linux / macOS install: creates a virtualenv next to the code and installs dependencies.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python3}"
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else "Python 3.10+ is required")'
"$PYTHON" -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
[ -f .env ] || cp .env.example .env
echo
echo "Installed. Start with:  ./scripts/run.sh"
echo "Try it without any model or key:  NETAGENT_DEMO=1 ./scripts/run.sh"
