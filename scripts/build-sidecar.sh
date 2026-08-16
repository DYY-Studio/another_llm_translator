#!/usr/bin/env bash
# Build the bundled Python/FastAPI sidecar with PyInstaller.
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${MINIMAL_LLM_PYTHON:-$PWD/.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then
  echo "缺少 Python 解释器：$PYTHON" >&2
  exit 1
fi

"$PYTHON" -c 'from importlib.metadata import entry_points; matches = [entry_point for entry_point in entry_points(group="minimal_llm_translator.plugins") if entry_point.name == "srt" and entry_point.value == "minimal_llm_translator_srt.plugin:descriptor"]; raise SystemExit("缺少官方 SRT 插件 entry point，请先安装 requirements-dev.txt" if not matches else 0)'

rm -rf build sidecar-dist
"$PYTHON" -m PyInstaller --noconfirm --clean --distpath sidecar-dist packaging/translator.spec
rm -rf build
echo "sidecar 产物：$PWD/sidecar-dist/translator-sidecar"
