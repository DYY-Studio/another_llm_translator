"""Probe Python plugin dependencies inside a packaged macOS application."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import plistlib
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import ModuleType
from typing import Any

_OFFICIAL_PLUGIN_FILES = (
    "plugins/srt/__init__.py",
    "plugins/srt/adapter.py",
    "plugins/srt/plugin.py",
    "plugins/srt/plugin.toml",
    "plugins/term_validation/__init__.py",
    "plugins/term_validation/plugin.py",
    "plugins/term_validation/plugin.toml",
)
_PRIVATE_DEPENDENCY_FILE = "probe_private_dependency.py"


class ProbeFailure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> None:
    raise ProbeFailure(code, message)


def _fixture_module() -> ModuleType:
    """Load the Web probe's fixture so both probes exercise one plugin definition."""
    fixture_path = Path(__file__).with_name("probe_plugin_dependencies.py")
    spec = importlib.util.spec_from_file_location(
        "web_dependency_probe_fixture", fixture_path
    )
    if spec is None or spec.loader is None:
        _fail("fixture", f"无法加载共享插件探针 fixture：{fixture_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _validate_app(app: Path) -> dict[str, Any]:
    if not app.is_dir():
        _fail("app_missing", f"macOS 应用目录不存在：{app}")
    contents = app / "Contents"
    plist_path = contents / "Info.plist"
    if not plist_path.is_file():
        _fail("plist_missing", f"缺少 Info.plist：{plist_path}")
    try:
        with plist_path.open("rb") as stream:
            plist = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException) as exc:
        _fail("plist_invalid", f"无法读取 Info.plist {plist_path}：{exc}")
    executable_name = (
        plist.get("CFBundleExecutable") if isinstance(plist, dict) else None
    )
    if not isinstance(executable_name, str) or not executable_name:
        _fail("bundle_executable", f"Info.plist 缺少 CFBundleExecutable：{plist_path}")
    app_executable = contents / "MacOS" / executable_name
    if not app_executable.is_file() or not os.access(app_executable, os.X_OK):
        _fail("app_executable", f"缺少可执行应用入口：{app_executable}")

    sidecar_root = (
        contents / "Resources" / "_up_" / "sidecar-dist" / "translator-sidecar"
    )
    sidecar_executable = sidecar_root / "translator-sidecar"
    if not sidecar_executable.is_file() or not os.access(sidecar_executable, os.X_OK):
        _fail("sidecar_missing", f"缺少 packaged sidecar 入口：{sidecar_executable}")
    resource_root = sidecar_root / "_internal"
    official_resources = [
        resource_root / relative for relative in _OFFICIAL_PLUGIN_FILES
    ]
    for resource in official_resources:
        if not resource.is_file():
            _fail("official_resource_missing", f"缺少官方插件资源：{resource}")
    bundled_private = sorted(
        path for path in app.rglob(_PRIVATE_DEPENDENCY_FILE) if path.is_file()
    )
    if bundled_private:
        _fail(
            "private_dependency_bundled",
            "私有依赖不得存在于 .app/PyInstaller bundle："
            + ", ".join(str(path) for path in bundled_private),
        )
    return {
        "app": app,
        "bundle_executable": app_executable,
        "sidecar_root": sidecar_root,
        "sidecar_executable": sidecar_executable,
        "official_resources": official_resources,
    }


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request_json(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url, data=data, method=method, headers=headers or {}
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        _fail("web_request", f"Web 请求失败 {method} {url}：{exc}")
    if not isinstance(payload, dict):
        _fail("web_response", f"Web 响应不是 JSON 对象：{url}")
    return payload


def _wait_for_status(process: subprocess.Popen[str], url: str, log_path: Path) -> None:
    deadline = time.monotonic() + 20
    last_failure: ProbeFailure | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            detail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
            _fail(
                "sidecar_exit",
                f"packaged sidecar 在 server status 可用前退出（code={process.returncode}）：{detail}",
            )
        try:
            _request_json(url)
            return
        except ProbeFailure as exc:
            last_failure = exc
            time.sleep(0.2)
    if last_failure is not None:
        _fail(
            "sidecar_startup_failed", f"packaged sidecar 启动诊断失败：{last_failure}"
        )
    _fail("sidecar_timeout", f"packaged sidecar 未在期限内提供响应：{url}")


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _stop_process(process: subprocess.Popen[str] | None) -> tuple[bool, bool]:
    if process is None:
        return True, False
    forced_kill = False
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            forced_kill = True
            process.kill()
            process.wait(timeout=3)
    return process.returncode is not None, forced_kill


def _wait_for_port_release(port: int) -> bool:
    for _ in range(80):
        if not _port_open(port):
            return True
        time.sleep(0.25)
    return False


def _run_packaged_smoke(
    layout: dict[str, Any], root: Path, evidence: dict[str, Any]
) -> dict[str, Any]:
    fixture = _fixture_module()
    user_root = root / "user-root"
    dependency_root = root / "private-dependency"
    fixture._write_external_plugin(user_root, dependency_root)
    input_path = root / "input.probe"
    input_path.write_text(
        "private dependency\npackaged file import\n", encoding="utf-8"
    )
    port = _free_port()
    log_path = root / "sidecar.log"
    env = os.environ.copy()
    env.update(
        {
            "ANOTHER_LLM_USER_ROOT": str(user_root),
            "ANOTHER_LLM_PROBE_DEPENDENCY_PATH": str(dependency_root),
            "ANOTHER_LLM_WEB_PORT": str(port),
        }
    )
    env.pop("PYTHONPATH", None)
    env.pop("VIRTUAL_ENV", None)
    process: subprocess.Popen[str] | None = None
    log_stream = log_path.open("w", encoding="utf-8")
    try:
        process = subprocess.Popen(
            [str(layout["sidecar_executable"]), "--port", str(port)],
            cwd=layout["sidecar_root"],
            env=env,
            stdout=log_stream,
            stderr=subprocess.STDOUT,
            text=True,
        )
        _wait_for_status(
            process, f"http://127.0.0.1:{port}/api/v1/server/status", log_path
        )
        adapters = _request_json(
            f"http://127.0.0.1:{port}/api/v1/document-adapters"
        ).get("adapters")
        adapter_id = fixture._ADAPTER_ID
        if not isinstance(adapters, list) or not any(
            isinstance(item, dict) and item.get("adapter_id") == adapter_id
            for item in adapters
        ):
            _fail("adapter_missing", f"未发现外部 Adapter：{adapter_id}")
        body, content_type = fixture._multipart_file(input_path)
        created = _request_json(
            f"http://127.0.0.1:{port}/api/v1/projects",
            method="POST",
            data=body,
            headers={"Content-Type": content_type},
        )
        if (
            created.get("document_adapter") != adapter_id
            or created.get("segment_count") != 2
        ):
            _fail("file_import_failed", f"文件导入结果不匹配：{created}")
        return {"adapter_discovered": True, "file_imported": True}
    finally:
        stopped, forced_kill = _stop_process(process)
        log_stream.close()
        evidence["process_exited"] = stopped
        evidence["forced_kill"] = forced_kill
        evidence["port_released"] = _wait_for_port_release(port)
        if not stopped:
            _fail("process_exit", "packaged sidecar 未能退出")
        if not evidence["port_released"]:
            _fail("port_release", f"packaged sidecar 退出后端口仍被占用：{port}")
        if forced_kill:
            _fail("forced_kill", "packaged sidecar 需要强制 KILL 才退出")


def probe(app: Path) -> dict[str, Any]:
    app = app.expanduser().resolve()
    temporary_root = Path(tempfile.mkdtemp(prefix="another-llm-packaged-probe-"))
    result: dict[str, Any] = {
        "status": "error",
        "app": str(app),
        "temporary_root": str(temporary_root),
        "temporary_root_removed": False,
        "forced_kill": False,
        "port_released": False,
    }
    try:
        layout = _validate_app(app)
        result.update(
            {
                "bundle_executable": str(layout["bundle_executable"]),
                "sidecar_executable": str(layout["sidecar_executable"]),
                "official_plugin_resources": [
                    str(path) for path in layout["official_resources"]
                ],
            }
        )
        result.update(_run_packaged_smoke(layout, temporary_root, result))
        result.update({"status": "ok", "port_released": True})
    except ProbeFailure as exc:
        result.update({"code": exc.code, "message": str(exc)})
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        result.update({"code": "unexpected", "message": f"探针异常：{exc}"})
    finally:
        try:
            shutil.rmtree(temporary_root)
        except OSError as exc:
            result.update(
                {
                    "status": "error",
                    "code": "cleanup_failed",
                    "message": f"临时目录清理失败：{temporary_root}：{exc}",
                }
            )
        result["temporary_root_removed"] = not temporary_root.exists()
        if not result["temporary_root_removed"]:
            result.update(
                {
                    "status": "error",
                    "code": "cleanup_failed",
                    "message": f"临时目录仍存在：{temporary_root}",
                }
            )
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path, help="已构建的 macOS .app 路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = probe(args.app)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
