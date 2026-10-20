#!/usr/bin/env bash
# Complete local gate: fresh analyzer, all backend contracts, M1/M2 real browsers.
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export PYTHON=${PYTHON:-python3}
export PYTHONPATH="$root/backend"
: "${CODEWEAVE_LLVM_PREFIX:?Set the matching LLVM/Clang installation prefix}"
# Retain compiler/test outputs in an owned directory, outside analyzed source.
export CODEWEAVE_BUILD_PARENT=${CODEWEAVE_BUILD_PARENT:-${TMPDIR:-/tmp}}
proof=$(mktemp -d "$CODEWEAVE_BUILD_PARENT/codeweave-m2-acceptance-XXXXXX")
printf 'M2 acceptance evidence: %s\n' "$proof"
cmake -S "$root/analyzer" -B "$proof/analyzer" -DCMAKE_PREFIX_PATH="$CODEWEAVE_LLVM_PREFIX" -DCMAKE_BUILD_RPATH="$CODEWEAVE_LLVM_PREFIX/lib"
cmake --build "$proof/analyzer" -j2
export CODEWEAVE_ANALYZER="$proof/analyzer/codeweave-analyzer"
if [[ -z "${CODEWEAVE_RESOURCE_DIR:-}" ]]; then
 CODEWEAVE_RESOURCE_DIR=$("$PYTHON" - "$CODEWEAVE_LLVM_PREFIX" <<'PY'
import sys
from pathlib import Path
files=list((Path(sys.argv[1])/'lib/clang').glob('*/include/stddef.h'))
assert len(files)==1,'Set CODEWEAVE_RESOURCE_DIR to the matching Clang resource directory'
print(files[0].parent.parent)
PY
)
fi
export CODEWEAVE_RESOURCE_DIR
sha256sum "$CODEWEAVE_ANALYZER" "$root/analyzer/main.cpp"
"$PYTHON" -m pytest "$root/backend/tests" -q --junitxml="$proof/backend.xml" --basetemp="$proof/backend"
"$PYTHON" - "$proof/backend.xml" <<'PY'
import sys,xml.etree.ElementTree as ET
suites=list(ET.parse(sys.argv[1]).getroot().iter('testsuite'))
assert sum(int(s.get('tests',0)) for s in suites)>0
assert not any(int(s.get(k,0)) for s in suites for k in ('failures','errors','skipped')),'No skipped required tests allowed'
PY
"$PYTHON" -m unittest discover -s "$root/tests" -p 'test_*.py' -v
"$PYTHON" "$root/scripts/check_repository.py"
cd "$root/frontend"
npm ci --no-audit --no-fund
npm run build
npm test
npm run test:e2e
export CODEWEAVE_BROWSER_WORKDIR="$proof/browser"
npx playwright test --config playwright.m2.config.ts
printf 'PASS: complete M0/M1/M2-A gate. Evidence retained: %s\n' "$proof"
