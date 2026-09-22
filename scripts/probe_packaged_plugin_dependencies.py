"""Probe Python plugin dependencies inside a packaged macOS application."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import plistlib
import re
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

    runtime_root = contents / "Resources" / "managed-runtime"
    runtime_python = runtime_root / "bin" / "python3"
    if not runtime_python.is_file() or not os.access(runtime_python, os.X_OK):
        _fail("runtime_python_missing", f"缺少 bundled Python：{runtime_python}")
    site_packages = runtime_root / "lib" / "python3.13" / "site-packages"
    if not site_packages.is_dir():
        _fail("site_packages_missing", f"缺少 bundled site-packages：{site_packages}")
    wheel_files = sorted(site_packages.glob("regex-*.dist-info/WHEEL"))
    if len(wheel_files) != 1:
        _fail(
            "binary_wheel_missing",
            f"bundled site-packages 中需要且只能有一份 regex wheel metadata：{site_packages}",
        )
    try:
        wheel_tags = [
            line.removeprefix("Tag: ").strip()
            for line in wheel_files[0].read_text(encoding="utf-8").splitlines()
            if line.startswith("Tag: ")
        ]
    except OSError as exc:
        _fail(
            "binary_wheel_metadata",
            f"无法读取 regex wheel 标签 {wheel_files[0]}：{exc}",
        )
    compatible_tags = [
        tag
        for tag in wheel_tags
        if re.fullmatch(r"cp313-cp313-macosx_(\d+)_\d+_arm64", tag)
        and int(tag.split("_")[1]) <= 13
    ]
    if not compatible_tags:
        _fail(
            "binary_wheel_incompatible",
            "regex wheel 必须匹配 CPython 3.13/macOS 13 arm64；"
            f"发现标签：{wheel_tags or 'none'}",
        )
    resource_root = runtime_root
    official_resources = [
        resource_root / relative for relative in _OFFICIAL_PLUGIN_FILES
    ]
    for resource in official_resources:
        if not resource.is_file():
            _fail("official_resource_missing", f"缺少官方插件资源：{resource}")
    bundled_private = sorted(
        path for path in runtime_root.rglob(_PRIVATE_DEPENDENCY_FILE) if path.is_file()
    )
    if bundled_private:
        _fail(
            "private_dependency_bundled",
            "外部插件私有依赖不得预先存在于 bundled runtime："
            + ", ".join(str(path) for path in bundled_private),
        )
    return {
        "app": app,
        "bundle_executable": app_executable,
        "runtime_root": runtime_root,
        "runtime_python": runtime_python,
        "compatible_wheel_tags": compatible_tags,
        "official_resources": official_resources,
    }


def _check_binary_import(layout: dict[str, Any]) -> dict[str, Any]:
    runtime_root = Path(layout["runtime_root"])
    runtime_python = Path(layout["runtime_python"])
    code = (
        "import json, pathlib, platform, sys, sysconfig\n"
        "import regex\n"
        "import regex._regex as binary\n"
        "expected = pathlib.Path(sys.argv[1]).resolve()\n"
        "executable = pathlib.Path(sys.executable).resolve()\n"
        "origin = pathlib.Path(binary.__file__).resolve()\n"
        "if executable != expected and expected not in executable.parents:\n"
        "    raise RuntimeError(f'Python did not execute from bundled runtime: {executable}')\n"
        "if sys.version_info[:2] != (3, 13) or platform.machine() != 'arm64':\n"
        "    raise RuntimeError(f'unexpected runtime ABI: {sys.version} {platform.machine()}')\n"
        "if expected not in origin.parents:\n"
        "    raise RuntimeError(f'regex binary did not import from bundled runtime: {origin}')\n"
        "if origin.suffix != '.so':\n"
        "    raise RuntimeError(f'regex import is not a macOS binary extension: {origin}')\n"
        "if regex.fullmatch(r'\\d+', '313') is None:\n"
        "    raise RuntimeError('regex binary import did not execute')\n"
        "print(json.dumps({'python_executable': str(executable), 'binary_import_path': str(origin), 'platform': sysconfig.get_platform()}))\n"
    )
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("VIRTUAL_ENV", None)
    result = subprocess.run(
        [str(runtime_python), "-c", code, str(runtime_root)],
        cwd=runtime_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "无诊断输出"
        _fail("binary_import_failed", f"bundled regex 导入/执行失败：{detail}")
    try:
        evidence = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        _fail(
            "binary_import_failed",
            f"bundled Python 输出无效：{exc}；{result.stdout}",
        )
    if not isinstance(evidence, dict):
        _fail("binary_import_failed", f"bundled Python 输出不是对象：{result.stdout}")
    return evidence


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
                "web_exit",
                f"bundled Web 进程在 server status 可用前退出（code={process.returncode}）：{detail}",
            )
        try:
            _request_json(url)
            return
        except ProbeFailure as exc:
            last_failure = exc
            time.sleep(0.2)
    if last_failure is not None:
        _fail(
            "web_startup_failed", f"bundled Web 启动诊断失败：{last_failure}"
        )
    _fail("web_timeout", f"bundled Web 未在期限内提供响应：{url}")


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
    binary_evidence = _check_binary_import(layout)
    evidence.update(binary_evidence)
    evidence["binary_imported"] = True
    evidence["wheel_tags"] = layout["compatible_wheel_tags"]
    user_root = root / "user-root"
    dependency_root = root / "private-dependency"
    fixture._write_external_plugin(user_root, dependency_root)
    input_path = root / "input.probe"
    input_path.write_text(
        "private dependency\npackaged file import\n", encoding="utf-8"
    )
    port = _free_port()
    log_path = root / "web.log"
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
            [str(layout["runtime_python"]), "-m", "app.web", "--port", str(port)],
            cwd=layout["runtime_root"],
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
        project_name = created.get("project_name")
        if not isinstance(project_name, str) or not project_name:
            _fail("project_missing", f"创建响应缺少项目名称：{created}")
        overview = _request_json(
            f"http://127.0.0.1:{port}/api/v1/projects/{project_name}?offset=0&limit=100"
        )
        if not isinstance(overview.get("files"), list) or len(overview["files"]) != 1:
            _fail("project_missing", f"项目概览未包含导入文件：{overview}")
        if overview["files"][0].get("document_adapter_id") != adapter_id:
            _fail("project_adapter_mismatch", f"项目文件 Adapter 不匹配：{overview}")
        segments = _request_json(
            f"http://127.0.0.1:{port}/api/v1/projects/{project_name}/segments/query",
            method="POST",
            data=json.dumps({"offset": 0, "limit": 100}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        ).get("segments")
        if not isinstance(segments, list) or [
            item.get("source") for item in segments
        ] != ["private dependency", "packaged file import"]:
            _fail("segment_query_failed", f"Segment 查询结果不匹配：{segments}")
        return {
            "adapter_discovered": True,
            "file_imported": True,
            "project_created": True,
            "segments_queried": True,
        }
    finally:
        stopped, forced_kill = _stop_process(process)
        log_stream.close()
        evidence["process_exited"] = stopped
        evidence["forced_kill"] = forced_kill
        evidence["port_released"] = _wait_for_port_release(port)
        if not stopped:
            _fail("process_exit", "bundled Web 进程未能退出")
        if not evidence["port_released"]:
            _fail("port_release", f"bundled Web 退出后端口仍被占用：{port}")
        if forced_kill:
            _fail("forced_kill", "bundled Web 进程需要强制 KILL 才退出")


def probe(app: Path) -> dict[str, Any]:
    app = app.expanduser().resolve()
    temporary_root = Path(tempfile.mkdtemp(prefix="another-llm-packaged-probe-"))
    result: dict[str, Any] = {
        "status": "error",
        "app": str(app),
        "temporary_root": str(temporary_root),
        "temporary_root_removed": False,
        "forced_kill": False,
        "process_exited": False,
        "port_released": False,
    }
    try:
        layout = _validate_app(app)
        result.update(
            {
                "bundle_executable": str(layout["bundle_executable"]),
                "python_executable": str(layout["runtime_python"]),
                "official_plugin_resources": [
                    str(path) for path in layout["official_resources"]
                ],
            }
        )
        result.update(_run_packaged_smoke(layout, temporary_root, result))
        result.update({"status": "ok", "port_released": True})
    except ProbeFailure as exc:
        result.update({"code": exc.code, "message": str(exc)})
    except (
        KeyError,
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
        TypeError,
        ValueError,
    ) as exc:
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
