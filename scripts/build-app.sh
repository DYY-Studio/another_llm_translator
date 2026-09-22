#!/usr/bin/env bash
# Build the macOS Tauri app bundle with bundled managed Python and a local ad-hoc signature.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -n "${ANOTHER_LLM_BUILD_PYTHON:-}" ]; then
  BUILD_PYTHON="$ANOTHER_LLM_BUILD_PYTHON"
elif [ -x "$PWD/.venv/bin/python" ]; then
  BUILD_PYTHON="$PWD/.venv/bin/python"
else
  echo "缺少项目构建解释器：请创建 .venv 或设置 ANOTHER_LLM_BUILD_PYTHON" >&2
  exit 1
fi
if [ ! -x "$BUILD_PYTHON" ]; then
  echo "构建解释器不可执行：$BUILD_PYTHON" >&2
  exit 1
fi
BUILD_PYTHON="$(cd "$(dirname "$BUILD_PYTHON")" && pwd -P)/$(basename "$BUILD_PYTHON")"
BUILD_PYTHON_VERSION="$("$BUILD_PYTHON" -c 'import platform, setuptools, sys, wheel; version = sys.version_info; implementation = platform.python_implementation(); assert implementation == "CPython" and version >= (3, 11), f"requires CPython >=3.11, got {implementation} {version.major}.{version.minor}.{version.micro}"; print(f"{implementation} {version.major}.{version.minor}.{version.micro}")' 2>&1)" || {
  echo "构建解释器不兼容或缺少 setuptools/wheel：$BUILD_PYTHON：$BUILD_PYTHON_VERSION" >&2
  exit 1
}
echo "构建解释器：$BUILD_PYTHON ($BUILD_PYTHON_VERSION)"

npm run typecheck --prefix web
npm run build --prefix web
rm -rf build/project-wheel
mkdir -p build/project-wheel
"$BUILD_PYTHON" -m pip wheel --no-deps --no-build-isolation --wheel-dir build/project-wheel .
shopt -s nullglob
PROJECT_WHEELS=(build/project-wheel/*.whl)
if [ "${#PROJECT_WHEELS[@]}" -ne 1 ]; then
  echo "当前 checkout 必须生成恰好一个项目 wheel（实际：${#PROJECT_WHEELS[@]}）" >&2
  exit 1
fi
PROJECT_WHEEL="${PROJECT_WHEELS[0]}"
PBS_ASSET="$("$BUILD_PYTHON" -c 'import json; from pathlib import Path; print(json.loads(Path("packaging/managed-runtime/pbs-macos-arm64.lock.json").read_text())["asset_filename"])')"
bash scripts/build_managed_runtime_macos.sh \
  --runtime-source build/managed-runtime-staging \
  --pbs-lock packaging/managed-runtime/pbs-macos-arm64.lock.json \
  --pbs-archive "build/managed-runtime-download/$PBS_ASSET" \
  --project-wheel "$PROJECT_WHEEL" \
  --wheelhouse build/managed-runtime-wheelhouse \
  --requirements-lock packaging/managed-runtime/requirements-macos-arm64.lock \
  --output build/managed-runtime-dist
cargo tauri build
TARGET_DIR="$(cargo metadata --manifest-path src-tauri/Cargo.toml --format-version 1 --no-deps 2>/dev/null | "$BUILD_PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["target_directory"])')"
APP="$(find "$TARGET_DIR/release/bundle/macos" -maxdepth 1 -name '*.app' | head -1)"
if [ -z "$APP" ]; then
  echo "未找到 bundle 产物" >&2
  exit 1
fi
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"
VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP/Contents/Info.plist" 2>/dev/null || echo unknown)"
DIST="dist/another-llm-translator-${VERSION}-macos-arm64"
rm -rf "$DIST"
mkdir -p "$DIST"
cp -R "$APP" "$DIST/"
DIST_APP="$DIST/$(basename "$APP")"
codesign --verify --deep --strict --verbose=2 "$DIST_APP"
ditto -c -k --keepParent "$DIST_APP" "$DIST/another-llm-translator-${VERSION}-macos-arm64.zip"
echo "产物：$PWD/$DIST"
