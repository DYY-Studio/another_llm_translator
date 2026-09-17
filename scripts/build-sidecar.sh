#!/usr/bin/env bash
# Build the bundled Python/FastAPI sidecar with PyInstaller.
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${ANOTHER_LLM_PYTHON:-$PWD/.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then
  echo "缺少 Python 解释器：$PYTHON" >&2
  exit 1
fi

"$PYTHON" -c 'from pathlib import Path; root = Path("plugins"); required = ("srt/plugin.toml", "srt/__init__.py", "srt/adapter.py", "srt/plugin.py", "term_validation/plugin.toml", "term_validation/__init__.py", "term_validation/plugin.py"); missing = [str(root / path) for path in required if not (root / path).is_file()]; raise SystemExit("缺少官方插件资源：" + ", ".join(missing) if missing else 0)'

rm -rf build sidecar-dist
"$PYTHON" -m PyInstaller --noconfirm --clean --distpath sidecar-dist packaging/translator.spec
rm -rf build
echo "sidecar 产物：$PWD/sidecar-dist/translator-sidecar"
