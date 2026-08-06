#!/usr/bin/env bash
# Run the Tauri desktop shell in development mode.
# The Rust app spawns `python -m app.web` as the sidecar.
set -euo pipefail
cd "$(dirname "$0")/.."

export MINIMAL_LLM_REPO_ROOT="$PWD"
export MINIMAL_LLM_PYTHON="${MINIMAL_LLM_PYTHON:-$PWD/.venv/bin/python}"

if [ ! -x "$MINIMAL_LLM_PYTHON" ]; then
  echo "缺少 Python 解释器：$MINIMAL_LLM_PYTHON（请先创建 .venv 并安装依赖）" >&2
  exit 1
fi
"$MINIMAL_LLM_PYTHON" -c "import app.web" 2>/dev/null || {
  echo "Python 依赖不完整：在 $MINIMAL_LLM_PYTHON 中运行 pip install -r requirements.txt" >&2
  exit 1
}

cargo run --manifest-path src-tauri/Cargo.toml "$@"
