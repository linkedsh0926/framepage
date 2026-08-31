#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

# Media helpers such as yt-dlp are installed in the project environment.
if [[ -d "$project_dir/.venv/bin" ]]; then
  export PATH="$project_dir/.venv/bin:$PATH"
fi

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

if [[ -n "${PYTHON_BIN:-}" ]]; then
  python_bin="$PYTHON_BIN"
elif [[ -x .venv/bin/python ]]; then
  python_bin=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  python_bin="python3"
elif command -v python >/dev/null 2>&1; then
  python_bin="python"
else
  echo "Python 3.12 is required." >&2
  exit 127
fi

exec "$python_bin" -m uvicorn app.main:app --host "${FRAMEPAGE_HOST:-127.0.0.1}" --port "${FRAMEPAGE_PORT:-8000}" --reload
