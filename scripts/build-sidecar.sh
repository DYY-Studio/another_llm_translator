#!/usr/bin/env bash
# Build the bundled Python/FastAPI sidecar with PyInstaller.
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${MINIMAL_LLM_PYTHON:-$PWD/.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then
  echo "缺少 Python 解释器：$PYTHON" >&2
  exit 1
fi

rm -rf build sidecar-dist
"$PYTHON" -m PyInstaller --noconfirm --clean --distpath sidecar-dist packaging/translator.spec
rm -rf build
echo "sidecar 产物：$PWD/sidecar-dist/translator-sidecar"
