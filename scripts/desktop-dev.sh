#!/usr/bin/env bash
# Run the Tauri desktop shell in development mode.
# The Rust app spawns `python -m app.web` as the sidecar.
set -euo pipefail
cd "$(dirname "$0")/.."

export ANOTHER_LLM_REPO_ROOT="$PWD"
export ANOTHER_LLM_PYTHON="${ANOTHER_LLM_PYTHON:-$PWD/.venv/bin/python}"

if [ ! -x "$ANOTHER_LLM_PYTHON" ]; then
  echo "缺少 Python 解释器：$ANOTHER_LLM_PYTHON（请先创建 .venv 并安装依赖）" >&2
  exit 1
fi
"$ANOTHER_LLM_PYTHON" -c "import app.web" 2>/dev/null || {
  echo "Python 依赖不完整：在 $ANOTHER_LLM_PYTHON 中运行 pip install -r requirements.txt" >&2
  exit 1
}

cargo run --manifest-path src-tauri/Cargo.toml "$@"
