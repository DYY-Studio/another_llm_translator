#!/usr/bin/env bash
# Build the bundled Python/FastAPI sidecar with PyInstaller.
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${ANOTHER_LLM_PYTHON:-$PWD/.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then
  echo "缺少 Python 解释器：$PYTHON" >&2
  exit 1
fi

"$PYTHON" -c 'from importlib.metadata import entry_points; expected = {"srt": "another_llm_translator_srt.plugin:descriptor", "term-validation": "another_llm_translator_term_validation.plugin:descriptor"}; found = {entry_point.name: entry_point.value for entry_point in entry_points(group="another_llm_translator.plugins")}; missing = [name for name, value in expected.items() if found.get(name) != value]; raise SystemExit("缺少官方插件 entry point：" + ", ".join(missing) + "，请先安装 requirements-dev.txt" if missing else 0)'

rm -rf build sidecar-dist
"$PYTHON" -m PyInstaller --noconfirm --clean --distpath sidecar-dist packaging/translator.spec
rm -rf build
echo "sidecar 产物：$PWD/sidecar-dist/translator-sidecar"
