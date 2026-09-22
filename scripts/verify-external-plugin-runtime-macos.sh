#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 || ${1:-} != *.app || ! -d ${1:-} ]]; then
  echo "用法：$0 /path/to/Another\ LLM\ Translator.app" >&2
  exit 2
fi

for command_name in bash curl pgrep python3; do
  command -v "$command_name" >/dev/null || {
    echo "缺少依赖：$command_name" >&2
    exit 1
  }
done
PLIST_BUDDY=/usr/libexec/PlistBuddy
if [[ ! -x $PLIST_BUDDY ]]; then
  echo "缺少依赖：$PLIST_BUDDY" >&2
  exit 1
fi

APP="$(cd "$1" && pwd -P)"
PLIST="$APP/Contents/Info.plist"
if [[ ! -f $PLIST ]]; then
  echo "缺少 Info.plist：$PLIST" >&2
  exit 1
fi
BUNDLE_EXECUTABLE="$("$PLIST_BUDDY" -c 'Print :CFBundleExecutable' "$PLIST")"
APP_EXECUTABLE="$APP/Contents/MacOS/$BUNDLE_EXECUTABLE"
if [[ ! -x $APP_EXECUTABLE ]]; then
  echo "缺少可执行文件：$APP_EXECUTABLE" >&2
  exit 1
fi

PORT="$(python3 - <<'PY'
import socket

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
)"
BASE_URL="http://127.0.0.1:$PORT"
TEMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/another-llm-plugin-smoke.XXXXXX")"
APP_PID=""
APP_CHILD_PIDS=""

stop_pid() {
  local pid=$1
  local attempt

  if ! kill -0 "$pid" 2>/dev/null; then
    wait "$pid" 2>/dev/null || true
    return 0
  fi
  kill -TERM "$pid" 2>/dev/null || true
  for attempt in $(seq 1 20); do
    if ! kill -0 "$pid" 2>/dev/null; then
      wait "$pid" 2>/dev/null || true
      return 0
    fi
    sleep 0.1
  done
  echo "进程未在短时间内响应 TERM，发送 KILL：$pid" >&2
  kill -KILL "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
  if kill -0 "$pid" 2>/dev/null; then
    echo "无法终止进程：$pid" >&2
    return 1
  fi
}

stop_app() {
  local stop_status=0
  if [[ -n $APP_PID ]]; then
    APP_CHILD_PIDS="$(pgrep -P "$APP_PID" || true)"
    stop_pid "$APP_PID" || stop_status=1
    APP_PID=""
  fi
  for child_pid in $APP_CHILD_PIDS; do
    stop_pid "$child_pid" || stop_status=1
  done
  APP_CHILD_PIDS=""
  return "$stop_status"
}

cleanup() {
  local exit_code=$?
  local cleanup_status=0
  set +e
  stop_app || cleanup_status=1
  wait_for_port_release || cleanup_status=1
  if ! rm -rf -- "$TEMP_ROOT"; then
    echo "无法删除临时目录：$TEMP_ROOT" >&2
    cleanup_status=1
  fi
  if ((cleanup_status != 0)); then
    echo "smoke 清理失败" >&2
    if ((exit_code == 0)); then
      exit_code=1
    fi
  fi
  trap - EXIT
  exit "$exit_code"
}
trap cleanup EXIT

port_is_open() {
  python3 - "$PORT" <<'PY'
import socket
import sys

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    raise SystemExit(0 if sock.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
}

wait_for_status() {
  local attempt
  for attempt in $(seq 1 120); do
    if [[ -n $APP_PID ]] && ! kill -0 "$APP_PID" 2>/dev/null; then
      echo "应用在 server status 可用前退出" >&2
      tail -n 80 "$TEMP_ROOT/app.log" >&2 || true
      return 1
    fi
    if curl --fail --silent --show-error --connect-timeout 1 --max-time 3 \
      "$BASE_URL/api/v1/server/status" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.25
  done
  echo "应用未在等待时间内提供 server status" >&2
  tail -n 80 "$TEMP_ROOT/app.log" >&2 || true
  return 1
}

wait_for_port_release() {
  local attempt
  for attempt in $(seq 1 80); do
    if ! port_is_open; then
      return 0
    fi
    sleep 0.25
  done
  echo "应用退出后端口仍被占用：$PORT" >&2
  return 1
}

start_app() {
  ANOTHER_LLM_USER_ROOT="$TEMP_ROOT" \
    ANOTHER_LLM_WEB_PORT="$PORT" \
    "$APP_EXECUTABLE" >"$TEMP_ROOT/app.log" 2>&1 &
  APP_PID=$!
  wait_for_status
}

assert_adapter_absent() {
  python3 - "$1" "$2" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
adapter_id = sys.argv[2]
adapters = payload.get("adapters")
if not isinstance(adapters, list) or any(
    isinstance(item, dict) and item.get("adapter_id") == adapter_id
    for item in adapters
):
    raise SystemExit(f"插件未安装前不应出现 Adapter：{adapter_id}")
PY
}

assert_adapter_present() {
  python3 - "$1" "$2" "$3" "$4" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
adapter_id, plugin_id, extension = sys.argv[2:]
adapters = payload.get("adapters")
if not isinstance(adapters, list):
    raise SystemExit("document-adapters 响应缺少 adapters")
matches = [item for item in adapters if isinstance(item, dict) and item.get("adapter_id") == adapter_id]
if len(matches) != 1:
    raise SystemExit(f"未找到唯一外部 Adapter：{adapter_id}")
adapter = matches[0]
if adapter.get("plugin_id") != plugin_id or adapter.get("extensions") != [extension]:
    raise SystemExit(f"外部 Adapter 元数据不匹配：{adapter}")
PY
}

RUN_ID="$$"
PLUGIN_ID="external_plugin_smoke_$RUN_ID"
ADAPTER_ID="external_document_smoke_$RUN_ID"
EXTENSION=".smoke$RUN_ID"
PROJECT_NAME="external-plugin-smoke-$RUN_ID"
INPUT_FILE="$TEMP_ROOT/source$EXTENSION"

echo "首次启动 packaged app，确认用户插件尚未加载"
start_app
curl --fail --silent --show-error "$BASE_URL/api/v1/document-adapters" \
  >"$TEMP_ROOT/adapters-before.json"
assert_adapter_absent "$TEMP_ROOT/adapters-before.json" "$ADAPTER_ID"
stop_app
wait_for_port_release

PLUGIN_ROOT="$TEMP_ROOT/plugins/$PLUGIN_ID"
mkdir -p "$PLUGIN_ROOT"
cat >"$PLUGIN_ROOT/plugin.toml" <<EOF
schema = 1

[plugin]
id = "$PLUGIN_ID"
version = "1.0.0"
protocol = 12
entrypoint = "plugin:descriptor"
EOF
cat >"$PLUGIN_ROOT/__init__.py" <<'EOF'
EOF
cat >"$PLUGIN_ROOT/plugin.py" <<EOF
from pathlib import Path

from app.plugin_api import DocumentImport, ImportedFile, PluginDescriptor


class SmokeDocumentAdapter:
    adapter_id = "$ADAPTER_ID"
    version = "1.0.0"
    capabilities = frozenset({"import"})
    extensions = frozenset({"$EXTENSION"})
    import_options = ()
    run_options = ()

    def model_prompt_requirements(self, *, stage, language, opaque_state, run_options):
        del stage, language, opaque_state, run_options
        return None

    def render_model_source(self, *, segment, opaque_state, run_options):
        del opaque_state, run_options
        return str(segment["source"])

    def segment_format_count(self, *, segment, opaque_state):
        del segment, opaque_state
        return 0

    def replacement_options(self, *, opaque_state):
        del opaque_state
        return {}

    def import_sources(self, inputs, *, recursive, config, options):
        del recursive, config, options
        source_path = Path(inputs[0])
        segments = tuple(source_path.read_text(encoding="utf-8").splitlines())
        if segments != ("外部第一段", "外部第二段"):
            raise ValueError(f"unexpected smoke input: {source_path}")
        return DocumentImport(
            files=(
                ImportedFile(
                    source_path=source_path,
                    original_name=source_path.name,
                    segments=segments,
                    encoding_detected="utf-8",
                    encoding_used="utf-8",
                    encoding_confidence=1.0,
                    segment_part_ids=("document", "document"),
                ),
            ),
        )

    def export_sources(self, **kwargs):
        del kwargs
        raise RuntimeError("smoke adapter is import-only")


def descriptor() -> PluginDescriptor:
    return PluginDescriptor(
        plugin_id="$PLUGIN_ID",
        version="1.0.0",
        protocol_version=12,
        document_adapters=(SmokeDocumentAdapter(),),
    )
EOF
python3 - "$INPUT_FILE" <<'PY'
from pathlib import Path
import sys

Path(sys.argv[1]).write_text("外部第一段\n外部第二段\n", encoding="utf-8")
PY

echo "第二次启动 packaged app，确认并使用外部 Document Adapter"
start_app
curl --fail --silent --show-error "$BASE_URL/api/v1/document-adapters" \
  >"$TEMP_ROOT/adapters-after.json"
assert_adapter_present "$TEMP_ROOT/adapters-after.json" "$ADAPTER_ID" "$PLUGIN_ID" "$EXTENSION"

curl --fail --silent --show-error --request POST \
  --form "name=$PROJECT_NAME" \
  --form "files=@$INPUT_FILE" \
  "$BASE_URL/api/v1/projects" >"$TEMP_ROOT/create-project.json"
python3 - "$TEMP_ROOT/create-project.json" "$ADAPTER_ID" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
expected_adapter = sys.argv[2]
expected = {
    "document_adapter": expected_adapter,
    "file_count": 1,
    "segment_count": 2,
}
for key, value in expected.items():
    if payload.get(key) != value:
        raise SystemExit(f"项目创建响应 {key} 不匹配：{payload}")
PY

curl --fail --silent --show-error \
  "$BASE_URL/api/v1/projects/$PROJECT_NAME?offset=0&limit=100" \
  >"$TEMP_ROOT/project-overview.json"
curl --fail --silent --show-error --request POST \
  --header 'Content-Type: application/json' \
  --data '{"offset":0,"limit":100}' \
  "$BASE_URL/api/v1/projects/$PROJECT_NAME/segments/query" \
  >"$TEMP_ROOT/segments.json"
python3 - "$TEMP_ROOT/project-overview.json" "$TEMP_ROOT/segments.json" "$ADAPTER_ID" <<'PY'
import json
import sys
from pathlib import Path

overview = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
segments_payload = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
expected_adapter = sys.argv[3]
files = overview.get("files")
if not isinstance(files, list) or len(files) != 1 or files[0].get("document_adapter_id") != expected_adapter:
    raise SystemExit(f"项目概览 Adapter 不匹配：{overview}")
segments = segments_payload.get("segments")
sources = [item.get("source") for item in segments] if isinstance(segments, list) else []
if sources != ["外部第一段", "外部第二段"]:
    raise SystemExit(f"Segment 源文不匹配：{segments_payload}")
PY

stop_app
wait_for_port_release
echo "外部插件 packaged macOS runtime smoke 通过"
