#!/usr/bin/env bash
# Run the Tauri desktop shell in development mode.
# The Rust app spawns the staged managed Python runtime.
set -euo pipefail
cd "$(dirname "$0")/.."

RUNTIME="$PWD/build/managed-runtime-dist"
if [ ! -x "$RUNTIME/bin/python3" ]; then
  echo "缺少 managed runtime：$RUNTIME（请先完成本地 managed runtime 构建）" >&2
  exit 1
fi
export ANOTHER_LLM_MANAGED_RUNTIME_DIR="$RUNTIME"

cargo run --manifest-path src-tauri/Cargo.toml "$@"
