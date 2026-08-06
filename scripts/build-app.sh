#!/usr/bin/env bash
# Build the macOS Tauri app bundle (ad-hoc, unsigned) with the frozen sidecar.
set -euo pipefail
cd "$(dirname "$0")/.."

npm run typecheck --prefix web
npm run build --prefix web
bash scripts/build-sidecar.sh
cargo tauri build
APP="$(find /Volumes/990EP/Caches/Cargo/cargo_targets/release/bundle/macos -maxdepth 1 -name '*.app' | head -1)"
if [ -z "$APP" ]; then
  echo "未找到 bundle 产物" >&2
  exit 1
fi
VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP/Contents/Info.plist" 2>/dev/null || echo unknown)"
DIST="dist/minimal-llm-translator-${VERSION}-macos-arm64"
rm -rf "$DIST"
mkdir -p "$DIST"
cp -R "$APP" "$DIST/"
ditto -c -k --keepParent "$APP" "$DIST/minimal-llm-translator-${VERSION}-macos-arm64.zip"
echo "产物：$PWD/$DIST"
