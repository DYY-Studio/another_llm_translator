"""Reproducibly probe Python plugin dependencies in a Web wheel runtime."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Any

_PLUGIN_ID = "web-dependency-probe"
_ADAPTER_ID = "web-dependency-probe-adapter"
_EXTENSION = ".probe"
_DEPENDENCY_ENV = "ANOTHER_LLM_PROBE_DEPENDENCY_PATH"


class ProbeFailure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> None:
    raise ProbeFailure(code, message)


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 30,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            cwd=cwd,
            env=env,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _fail("subprocess", f"无法执行 {command[0]}：{exc}")


def _python_in(venv: Path) -> Path:
    candidate = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not candidate.is_file():
        _fail("venv", f"临时 venv 未生成解释器：{candidate}")
    return candidate


def _validate_inputs(wheel: Path, interpreter: Path, target: str) -> None:
    if not wheel.is_file():
        _fail("wheel_missing", f"wheel 不存在：{wheel}；目标导入：{target}")
    if wheel.suffix != ".whl":
        _fail(
            "wheel_invalid", f"wheel 路径必须以 .whl 结尾：{wheel}；目标导入：{target}"
        )
    if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        _fail(
            "interpreter_missing",
            f"Python 解释器不存在或不可执行：{interpreter}；目标导入：{target}",
        )
    try:
        with zipfile.ZipFile(wheel) as archive:
            if archive.testzip() is not None:
                _fail(
                    "wheel_invalid", f"wheel 包含损坏文件：{wheel}；目标导入：{target}"
                )
    except (OSError, zipfile.BadZipFile) as exc:
        _fail("wheel_invalid", f"无法读取 wheel {wheel}：{exc}；目标导入：{target}")
    if not target or any(not part.isidentifier() for part in target.split(".")):
        _fail("target_invalid", f"导入目标无效：{target}")


def _wheel_plugin_resources(wheel: Path) -> list[str]:
    try:
        with zipfile.ZipFile(wheel) as archive:
            return sorted(
                name
                for name in archive.namelist()
                if "/plugins/" in f"/{name}" or name.startswith("plugins/")
            )
    except (OSError, zipfile.BadZipFile) as exc:
        _fail("wheel_invalid", f"无法读取 wheel 官方插件资源：{exc}")


def _interpreter_version(interpreter: Path) -> str:
    result = _run([str(interpreter), "--version"])
    if result.returncode != 0:
        _fail("interpreter", f"无法读取解释器版本：{result.stderr.strip()}")
    return (result.stdout or result.stderr).strip()


def _clean_runtime_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("VIRTUAL_ENV", None)
    return env


def _install_wheel(
    wheel: Path, interpreter: Path, venv: Path
) -> tuple[Path, dict[str, str]]:
    result = _run([str(interpreter), "-m", "venv", str(venv)])
    if result.returncode != 0:
        _fail(
            "venv",
            f"创建临时 venv 失败：{result.stderr.strip() or result.stdout.strip()}",
        )
    venv_python = _python_in(venv)
    runtime_env = _clean_runtime_env()
    result = _run(
        [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--only-binary=:all:",
            str(wheel),
        ],
        timeout=120,
    )
    if result.returncode != 0:
        _fail(
            "wheel_install",
            f"安装 wheel 失败：{result.stderr.strip() or result.stdout.strip()}",
        )
    return venv_python, runtime_env


def _check_import(
    venv_python: Path, target: str, env: dict[str, str], cwd: Path
) -> None:
    code = (
        "import importlib, pathlib, sys\n"
        "sys.path.insert(0, str(pathlib.Path(sys.prefix)))\n"
        "importlib.import_module(sys.argv[1])\n"
        "app_path = pathlib.Path(importlib.import_module('app').__file__).resolve()\n"
        "prefix = pathlib.Path(sys.prefix).resolve()\n"
        "if prefix not in app_path.parents:\n"
        "    raise RuntimeError('app did not come from temporary venv')\n"
    )
    result = _run([str(venv_python), "-c", code, target], cwd=cwd, env=env)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "无诊断输出"
        _fail("import_failed", f"导入目标失败 {target}：{detail}")


def _write_external_plugin(user_root: Path, dependency_root: Path) -> Path:
    plugin_root = user_root / "plugins" / _PLUGIN_ID
    plugin_root.mkdir(parents=True)
    dependency_root.mkdir(parents=True)
    (dependency_root / "probe_private_dependency.py").write_text(
        'MARKER = "private-dependency-loaded"\n', encoding="utf-8"
    )
    (plugin_root / "plugin.toml").write_text(
        "schema = 1\n\n"
        "[plugin]\n"
        f'id = "{_PLUGIN_ID}"\n'
        'version = "1.0.0"\n'
        "protocol = 12\n"
        'entrypoint = "plugin:descriptor"\n',
        encoding="utf-8",
    )
    (plugin_root / "__init__.py").write_text("", encoding="utf-8")
    (plugin_root / "plugin.py").write_text(
        "from __future__ import annotations\n"
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        f"dependency_path = os.environ[{_DEPENDENCY_ENV!r}]\n"
        "sys.path.insert(0, dependency_path)\n"
        "from probe_private_dependency import MARKER\n"
        "from app.plugin_api import DocumentImport, ImportedFile, PluginDescriptor\n"
        "if MARKER != 'private-dependency-loaded':\n"
        "    raise RuntimeError('private dependency marker mismatch')\n"
        "\n"
        "class ProbeAdapter:\n"
        f"    adapter_id = {_ADAPTER_ID!r}\n"
        "    version = '1.0.0'\n"
        "    capabilities = frozenset({'import'})\n"
        f"    extensions = frozenset({{{_EXTENSION!r}}})\n"
        "    import_options = ()\n"
        "    run_options = ()\n"
        "\n"
        "    def model_prompt_requirements(self, *, stage, language, opaque_state, run_options):\n"
        "        del stage, language, opaque_state, run_options\n"
        "        return None\n"
        "\n"
        "    def render_model_source(self, *, segment, opaque_state, run_options):\n"
        "        del opaque_state, run_options\n"
        "        return str(segment['source'])\n"
        "\n"
        "    def segment_format_count(self, *, segment, opaque_state):\n"
        "        del segment, opaque_state\n"
        "        return 0\n"
        "\n"
        "    def replacement_options(self, *, opaque_state):\n"
        "        del opaque_state\n"
        "        return {}\n"
        "\n"
        "    def import_sources(self, inputs, *, recursive, config, options):\n"
        "        del recursive, config, options\n"
        "        source_path = Path(inputs[0])\n"
        "        segments = tuple(source_path.read_text(encoding='utf-8').splitlines())\n"
        "        return DocumentImport(files=(ImportedFile(\n"
        "            source_path=source_path, original_name=source_path.name,\n"
        "            segments=segments, encoding_detected='utf-8',\n"
        "            encoding_used='utf-8', encoding_confidence=1.0,\n"
        "            segment_part_ids=('document',) * len(segments),\n"
        "        ),))\n"
        "\n"
        "    def export_sources(self, **kwargs):\n"
        "        del kwargs\n"
        "        raise RuntimeError('probe adapter is import-only')\n"
        "\n"
        "def descriptor():\n"
        "    return PluginDescriptor(\n"
        f"        plugin_id={_PLUGIN_ID!r}, version='1.0.0', protocol_version=12,\n"
        "        document_adapters=(ProbeAdapter(),),\n"
        "    )\n",
        encoding="utf-8",
    )
    return plugin_root


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


def _multipart_file(path: Path) -> tuple[bytes, str]:
    boundary = f"----another-llm-probe-{uuid.uuid4().hex}"
    body = b"".join(
        [
            f'--{boundary}\r\nContent-Disposition: form-data; name="name"\r\n\r\nprobe-project\r\n'.encode(),
            f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{path.name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode(),
            path.read_bytes(),
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
    return body, f"multipart/form-data; boundary={boundary}"


def _wait_for_web(process: subprocess.Popen[str], url: str, log_path: Path) -> None:
    deadline = time.monotonic() + 20
    last_failure: ProbeFailure | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            detail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
            _fail(
                "web_exit",
                f"Web 进程在启动完成前退出（code={process.returncode}）：{detail}",
            )
        try:
            _request_json(url)
            return
        except ProbeFailure as exc:
            last_failure = exc
            time.sleep(0.2)
    if last_failure is not None:
        _fail(
            "web_startup_failed",
            f"Web 启动诊断失败（最后一次）：{last_failure}",
        )
    _fail("web_timeout", f"Web 未在期限内提供响应：{url}")


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _stop_process(process: subprocess.Popen[str] | None) -> bool:
    if process is None:
        return True
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
    return process.returncode is not None


def _run_web_probe(
    venv_python: Path, root: Path, runtime_env: dict[str, str]
) -> dict[str, Any]:
    user_root = root / "user-root"
    dependency_root = root / "private-dependency"
    _write_external_plugin(user_root, dependency_root)
    input_path = root / "input.probe"
    input_path.write_text("private dependency\nweb file import\n", encoding="utf-8")
    port = _free_port()
    log_path = root / "web.log"
    web_entrypoint = venv_python.parent / "another-llm-translator-web"
    if not web_entrypoint.is_file():
        _fail("web_entrypoint", f"wheel 未生成 Web entrypoint：{web_entrypoint}")
    env = runtime_env.copy()
    env.update(
        {
            "ANOTHER_LLM_USER_ROOT": str(user_root),
            _DEPENDENCY_ENV: str(dependency_root),
        }
    )
    process = subprocess.Popen(
        [str(web_entrypoint), "--port", str(port)],
        cwd=root,
        stdout=log_path.open("w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
        env=env,
        text=True,
    )
    try:
        _wait_for_web(
            process, f"http://127.0.0.1:{port}/api/v1/server/status", log_path
        )
        adapters = _request_json(
            f"http://127.0.0.1:{port}/api/v1/document-adapters"
        ).get("adapters")
        if not isinstance(adapters, list) or not any(
            isinstance(item, dict) and item.get("adapter_id") == _ADAPTER_ID
            for item in adapters
        ):
            _fail("adapter_missing", f"未发现外部 Adapter：{_ADAPTER_ID}")
        body, content_type = _multipart_file(input_path)
        created = _request_json(
            f"http://127.0.0.1:{port}/api/v1/projects",
            method="POST",
            data=body,
            headers={"Content-Type": content_type},
        )
        if (
            created.get("document_adapter") != _ADAPTER_ID
            or created.get("segment_count") != 2
        ):
            _fail("file_import_failed", f"文件导入结果不匹配：{created}")
    finally:
        if not _stop_process(process):
            _fail("process_exit", "Web 进程未能退出")
        for _ in range(40):
            if not _port_open(port):
                break
            time.sleep(0.1)
        else:
            _fail("port_release", f"Web 进程退出后端口仍被占用：{port}")
    return {
        "adapter_discovered": True,
        "file_imported": True,
        "web_command": [str(web_entrypoint), "--port", str(port)],
    }


def probe(wheel: Path, interpreter: Path, target: str) -> dict[str, Any]:
    temporary_root = Path(tempfile.mkdtemp(prefix="another-llm-plugin-probe-"))
    result: dict[str, Any] = {
        "status": "error",
        "wheel": str(wheel),
        "interpreter": str(interpreter),
        "import_target": target,
        "temporary_root": str(temporary_root),
        "temporary_root_removed": False,
    }
    try:
        _validate_inputs(wheel, interpreter, target)
        result["interpreter_version"] = _interpreter_version(interpreter)
        result["wheel_plugin_resources"] = _wheel_plugin_resources(wheel)
        venv_python, runtime_env = _install_wheel(
            wheel, interpreter, temporary_root / "venv"
        )
        _check_import(venv_python, target, runtime_env, temporary_root)
        result["dependency_imported"] = True
        result.update(_run_web_probe(venv_python, temporary_root, runtime_env))
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
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--python", dest="interpreter", type=Path, required=True)
    parser.add_argument("--import-target", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = probe(args.wheel, args.interpreter, args.import_target)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
