#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
llvm_prefix=${CODEWEAVE_LLVM_PREFIX:?Set CODEWEAVE_LLVM_PREFIX to the LLVM prefix containing lib/cmake}
python=${PYTHON:-python3}
build=$(mktemp -d "${CODEWEAVE_BUILD_PARENT:-${TMPDIR:-/tmp}}/codeweave-m0-build-XXXXXX")
echo "Fresh acceptance build: $build"
cmake -S "$root/analyzer" -B "$build/analyzer" \
  -DCMAKE_PREFIX_PATH="$llvm_prefix" \
  -DCMAKE_BUILD_RPATH="$llvm_prefix/lib"
cmake --build "$build/analyzer" -j2
if [[ -z "${CODEWEAVE_RESOURCE_DIR:-}" ]]; then
  CODEWEAVE_RESOURCE_DIR=$("$python" - "$llvm_prefix" <<'PYTHON'
import pathlib, sys
roots = list((pathlib.Path(sys.argv[1]) / 'lib/clang').glob('*/include/stddef.h'))
if len(roots) != 1:
    raise SystemExit('Set CODEWEAVE_RESOURCE_DIR explicitly for this toolchain')
print(roots[0].parent.parent)
PYTHON
)
fi
export CODEWEAVE_RESOURCE_DIR
export CODEWEAVE_ANALYZER="$build/analyzer/codeweave-analyzer"
sha256sum "$CODEWEAVE_ANALYZER" "$root/analyzer/main.cpp"
PYTHONPATH="$root/backend" "$python" -m pytest \
  "$root/backend/tests/test_m0_index.py" \
  "$root/backend/tests/test_m0_correction.py" \
  "$root/backend/tests/test_m0_r2.py" \
  "$root/backend/tests/test_m0_final_hardening.py" --junitxml="$build/results.xml" -q
"$python" - "$build/results.xml" <<'PY'
import sys
import xml.etree.ElementTree as ET
suites = ET.parse(sys.argv[1]).getroot().iter('testsuite')
counts = {key: 0 for key in ('tests', 'failures', 'errors', 'skipped')}
for suite in suites:
    for key in counts:
        counts[key] += int(suite.get(key, 0))
print('M0 executed:', counts)
if counts['tests'] == 0 or any(counts[k] for k in ('failures', 'errors', 'skipped')):
    raise SystemExit('M0 acceptance requires actual execution without skips')
PY
