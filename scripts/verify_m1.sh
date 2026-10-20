#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
llvm_prefix=${CODEWEAVE_LLVM_PREFIX:?Set the LLVM prefix}
python=${PYTHON:-python3}
build=$(mktemp -d "${CODEWEAVE_BUILD_PARENT:-${TMPDIR:-/tmp}}/codeweave-m1-build-XXXXXX")
cmake -S "$root/analyzer" -B "$build/analyzer" -DCMAKE_PREFIX_PATH="$llvm_prefix" -DCMAKE_BUILD_RPATH="$llvm_prefix/lib"
cmake --build "$build/analyzer" -j2
export CODEWEAVE_ANALYZER="$build/analyzer/codeweave-analyzer"
if [[ -z "${CODEWEAVE_RESOURCE_DIR:-}" ]]; then
  CODEWEAVE_RESOURCE_DIR=$("$python" - "$llvm_prefix" <<'PY'
from pathlib import Path
import sys
files = list((Path(sys.argv[1])/'lib/clang').glob('*/include/stddef.h'))
if len(files) != 1:
    raise SystemExit('Set CODEWEAVE_RESOURCE_DIR explicitly')
print(files[0].parent.parent)
PY
)
fi
export CODEWEAVE_RESOURCE_DIR
export PYTHONPATH="$root/backend"
export PYTHON="$python"
sha256sum "$CODEWEAVE_ANALYZER" "$root/analyzer/main.cpp"
"$python" -m pytest "$root/backend/tests" --junitxml="$build/results.xml" -q
"$python" - "$build/results.xml" <<'PY'
import sys, xml.etree.ElementTree as ET
suites = list(ET.parse(sys.argv[1]).getroot().iter('testsuite'))
assert sum(int(s.get('tests',0)) for s in suites) > 0
assert not any(int(s.get(k,0)) for s in suites for k in ('failures','errors','skipped')), 'Actual execution with no skips is required'
PY
"$python" -m unittest discover -s "$root/tests" -p 'test_*.py' -v
"$python" "$root/scripts/check_repository.py"
cd "$root/frontend"
npm ci --no-audit --no-fund
npm run build
npm test
npm run test:e2e
