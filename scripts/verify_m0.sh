#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
llvm_prefix=${CODEWEAVE_LLVM_PREFIX:?Set CODEWEAVE_LLVM_PREFIX to the LLVM prefix containing lib/cmake}
python=${PYTHON:-python3}

cmake -S "$root/analyzer" -B "$root/.build/analyzer" \
  -DCMAKE_PREFIX_PATH="$llvm_prefix" \
  -DCMAKE_BUILD_RPATH="$llvm_prefix/lib"
cmake --build "$root/.build/analyzer" -j2
PYTHONPATH="$root/backend" "$python" -m pytest \
  "$root/backend/tests/test_m0_index.py" \
  "$root/backend/tests/test_m0_correction.py" -q
