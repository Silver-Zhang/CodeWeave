#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
python=${CODEWEAVE_PYTHON:-python3}
if [[ -z "${CODEWEAVE_PYTHON:-}" ]]; then
  if [[ -x "$root/.venv/bin/python" ]]; then python="$root/.venv/bin/python"
  elif [[ -x "$root/../.venv/bin/python" ]]; then python="$root/../.venv/bin/python"
  fi
fi
exec "$python" "$root/scripts/run_local.py" "$@"
