#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT_DIR"

source "$ROOT_DIR/venv/bin/activate"
uvicorn enterprise_app.app.main:app --host 0.0.0.0 --port 8020 --reload

