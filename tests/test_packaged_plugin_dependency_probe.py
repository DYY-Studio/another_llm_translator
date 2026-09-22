from __future__ import annotations

import importlib.util
import json
import os
import plistlib
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
PROBE = ROOT / "scripts" / "probe_packaged_plugin_dependencies.py"


def _make_executable(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _make_app(
    tmp_path: Path,
    *,
    dependency_failure: bool = False,
    require_clean_env: bool = False,
    require_no_bytecode: bool = False,
    wheel_tag: str = "cp313-cp313-macosx_11_0_arm64",
    private_dependency_bundled: bool = False,
) -> Path:
    app = tmp_path / "Another LLM Translator.app"
    contents = app / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    plistlib.dump(
        {"CFBundleExecutable": "Another LLM Translator"},
        (contents / "Info.plist").open("wb"),
    )
    _make_executable(
        contents / "MacOS" / "Another LLM Translator", "#!/bin/sh\nexit 0\n"
    )

    runtime_root = contents / "Resources" / "managed-runtime"
    site_packages = runtime_root / "lib" / "python3.13" / "site-packages"
    for relative in (
        "plugins/srt/__init__.py",
        "plugins/srt/adapter.py",
        "plugins/srt/plugin.py",
        "plugins/srt/plugin.toml",
        "plugins/term_validation/__init__.py",
        "plugins/term_validation/plugin.py",
        "plugins/term_validation/plugin.toml",
    ):
        resource = runtime_root / relative
        resource.parent.mkdir(parents=True, exist_ok=True)
        resource.write_text("", encoding="utf-8")
    (site_packages / "regex-2026.9.10.dist-info").mkdir(parents=True)
    (site_packages / "regex-2026.9.10.dist-info" / "WHEEL").write_text(
        f"Wheel-Version: 1.0\nTag: {wheel_tag}\n", encoding="utf-8"
    )
    (site_packages / "regex").mkdir()
    (site_packages / "regex" / "__init__.py").write_text(
        "from . import _regex\n"
        "def fullmatch(pattern, value):\n"
        "    return _regex.fullmatch(pattern, value)\n",
        encoding="utf-8",
    )
    (site_packages / "regex" / "_regex.py").write_text(
        "import re\n"
        "def fullmatch(pattern, value):\n"
        "    return re.fullmatch(pattern, value)\n",
        encoding="utf-8",
    )
    if private_dependency_bundled:
        (runtime_root / "probe_private_dependency.py").write_text(
            "MARKER = 'must be injected after build'\n", encoding="utf-8"
        )

    root_literal = repr(str(ROOT))
    failure = (
        "os.environ['ANOTHER_LLM_PROBE_DEPENDENCY_PATH'] = '/path/that/does/not/exist'"
        if dependency_failure
        else ""
    )
    clean_env_guard = (
        "if os.environ.get('PYTHONPATH') or os.environ.get('VIRTUAL_ENV'):\n"
        "    raise SystemExit('host-only sentinel leaked into packaged runtime')\n"
        if require_clean_env
        else ""
    )
    no_bytecode_guard = (
        "if os.environ.get('PYTHONDONTWRITEBYTECODE') != '1':\n"
        "    raise SystemExit('bundled runtime may not write bytecode into the signed app')\n"
        if require_no_bytecode
        else ""
    )
    runtime_python = (
        f"#!{sys.executable}\n"
        "import os, runpy, sys\n"
        f"sys.executable = {str(runtime_root / 'bin/python3')!r}\n"
        f"sys.path.insert(0, {str(site_packages)!r})\n"
        f"sys.path.insert(0, {root_literal})\n"
        f"{clean_env_guard}"
        f"{no_bytecode_guard}"
        f"{failure}\n"
        "if sys.argv[1:2] == ['-c']:\n"
        "    code = sys.argv[2]\n"
        "    sys.argv = ['-c', *sys.argv[3:]]\n"
        "    import types\n"
        "    binary = types.ModuleType('regex._regex')\n"
        f"    binary.__file__ = {str(site_packages / 'regex' / '_regex.cpython-313-darwin.so')!r}\n"
        "    import re\n"
        "    binary.fullmatch = re.fullmatch\n"
        "    sys.modules['regex._regex'] = binary\n"
        "    exec(code, {'__name__': '__main__'})\n"
        "elif sys.argv[1:3] == ['-m', 'app.web']:\n"
        "    sys.argv = sys.argv[2:]\n"
        "    runpy.run_module('app.web', run_name='__main__')\n"
        "else:\n"
        "    raise SystemExit(f'unexpected bundled Python arguments: {sys.argv[1:]}')\n"
    )
    _make_executable(runtime_root / "bin" / "python3", runtime_python)
    return app


def _run_probe(
    app: Path, *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PROBE), str(app)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _payload(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    return json.loads(result.stdout)


def _probe_module():
    spec = importlib.util.spec_from_file_location("packaged_probe", PROBE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_probe_reports_missing_app(tmp_path: Path) -> None:
    missing = tmp_path / "Missing.app"

    result = _run_probe(missing)

    assert result.returncode != 0
    payload = _payload(result)
    assert payload["status"] == "error"
    assert str(missing) in str(payload)


@pytest.mark.parametrize("missing", ["Info.plist", "MacOS/Another LLM Translator"])
def test_probe_reports_missing_bundle_entrypoints(tmp_path: Path, missing: str) -> None:
    app = _make_app(tmp_path)
    (app / "Contents" / missing).unlink()

    result = _run_probe(app)

    assert result.returncode != 0
    payload = _payload(result)
    assert payload["status"] == "error"
    assert str(app / "Contents" / missing) in str(payload)


def test_probe_reports_missing_official_plugin_resource(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    resource = (
        app
        / "Contents/Resources/managed-runtime/plugins/srt/plugin.toml"
    )
    resource.unlink()

    result = _run_probe(app)

    assert result.returncode != 0
    payload = _payload(result)
    assert payload["status"] == "error"
    assert str(resource) in str(payload)


def test_probe_reports_missing_bundled_python(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    bundled = app / "Contents/Resources/managed-runtime/bin/python3"
    bundled.unlink()

    result = _run_probe(app)

    assert result.returncode != 0
    payload = _payload(result)
    assert payload["status"] == "error"
    assert payload["code"] == "runtime_python_missing"
    assert str(bundled) in str(payload)


def test_probe_rejects_external_dependency_prebundled_with_app(tmp_path: Path) -> None:
    app = _make_app(tmp_path, private_dependency_bundled=True)

    result = _run_probe(app)

    assert result.returncode != 0
    payload = _payload(result)
    assert payload["code"] == "private_dependency_bundled"
    bundled = app / "Contents/Resources/managed-runtime/probe_private_dependency.py"
    assert str(bundled) in str(payload)


def test_probe_reports_missing_runtime_site_packages(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    site_packages = app / "Contents/Resources/managed-runtime/lib/python3.13/site-packages"
    import shutil

    shutil.rmtree(site_packages)

    result = _run_probe(app)

    assert result.returncode != 0
    payload = _payload(result)
    assert payload["code"] == "site_packages_missing"
    assert str(site_packages) in str(payload)


def test_probe_rejects_incompatible_binary_wheel_tag(tmp_path: Path) -> None:
    app = _make_app(tmp_path, wheel_tag="cp312-cp312-macosx_11_0_arm64")

    result = _run_probe(app)

    assert result.returncode != 0
    payload = _payload(result)
    assert payload["code"] == "binary_wheel_incompatible"
    assert "cp312-cp312-macosx_11_0_arm64" in str(payload)


def test_probe_reports_packaged_private_dependency_import_failure(
    tmp_path: Path,
) -> None:
    app = _make_app(tmp_path, dependency_failure=True)

    result = _run_probe(app)

    assert result.returncode != 0
    payload = _payload(result)
    assert payload["status"] == "error"
    assert payload["code"] == "web_exit"
    assert "probe_private_dependency" in str(payload)
    assert payload["process_exited"] is True
    assert payload["port_released"] is True
    assert payload["temporary_root_removed"] is True


def test_probe_discovers_external_adapter_and_imports_file(tmp_path: Path) -> None:
    app = _make_app(tmp_path)

    result = _run_probe(app, env=os.environ.copy())

    assert result.returncode == 0, result.stdout + result.stderr
    payload = _payload(result)
    assert payload["status"] == "ok"
    assert payload["adapter_discovered"] is True
    assert payload["file_imported"] is True
    assert payload["project_created"] is True
    assert payload["segments_queried"] is True
    assert payload["forced_kill"] is False
    assert payload["port_released"] is True
    assert payload["temporary_root_removed"] is True
    assert not Path(str(payload["temporary_root"])).exists()


def test_probe_clears_host_paths_before_bundled_runtime(tmp_path: Path) -> None:
    app = _make_app(tmp_path, require_clean_env=True)
    host_root = tmp_path / "host-only"
    host_root.mkdir()
    (host_root / "probe_private_dependency.py").write_text(
        "MARKER = 'host-only-sentinel'\n", encoding="utf-8"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(host_root)
    env["VIRTUAL_ENV"] = str(tmp_path / "host-venv")

    result = _run_probe(app, env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = _payload(result)
    assert payload["status"] == "ok"
    assert payload["adapter_discovered"] is True
    assert payload["file_imported"] is True
    assert payload["binary_imported"] is True
    assert payload["binary_match"] == "313"
    assert payload["wheel_tags"] == ["cp313-cp313-macosx_11_0_arm64"]
    assert str(app / "Contents/Resources/managed-runtime") in str(
        payload["binary_import_path"]
    )


def test_probe_disables_bytecode_for_bundled_python_children(tmp_path: Path) -> None:
    app = _make_app(tmp_path, require_no_bytecode=True)

    result = _run_probe(app)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = _payload(result)
    assert payload["status"] == "ok"
    assert payload["binary_imported"] is True
    assert payload["adapter_discovered"] is True


def test_probe_reports_missing_external_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _probe_module()
    fixture = module._fixture_module()
    monkeypatch.setattr(fixture, "_write_external_plugin", lambda *_args: None)
    monkeypatch.setattr(module, "_fixture_module", lambda: fixture)

    payload = module.probe(_make_app(tmp_path))

    assert payload["status"] == "error"
    assert payload["code"] == "adapter_missing"
    assert payload["process_exited"] is True
    assert payload["port_released"] is True
    assert payload["temporary_root_removed"] is True


def test_probe_reports_file_import_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _probe_module()
    request_json = module._request_json

    def failed_import(url: str, **kwargs: object) -> dict[str, object]:
        if url.endswith("/api/v1/projects") and kwargs.get("method") == "POST":
            return {
                "document_adapter": "web-dependency-probe-adapter",
                "segment_count": 0,
            }
        return request_json(url, **kwargs)

    monkeypatch.setattr(module, "_request_json", failed_import)

    payload = module.probe(_make_app(tmp_path))

    assert payload["status"] == "error"
    assert payload["code"] == "file_import_failed"
    assert payload["process_exited"] is True
    assert payload["port_released"] is True
    assert payload["temporary_root_removed"] is True


def test_stop_process_reports_forced_kill() -> None:
    module = _probe_module()

    class HungProcess:
        returncode = None

        def poll(self):
            return None if self.returncode is None else self.returncode

        def terminate(self):
            pass

        def wait(self, timeout):
            if self.returncode is None:
                raise subprocess.TimeoutExpired("sidecar", timeout)
            return self.returncode

        def kill(self):
            self.returncode = -9

    stopped, forced_kill = module._stop_process(HungProcess())

    assert stopped is True
    assert forced_kill is True


def test_probe_reports_port_release_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _probe_module()
    app = _make_app(tmp_path)
    monkeypatch.setattr(module, "_wait_for_port_release", lambda _port: False)

    payload = module.probe(app)

    assert payload["status"] == "error"
    assert payload["code"] == "port_release"
    assert payload["process_exited"] is True
    assert payload["port_released"] is False
    assert payload["temporary_root_removed"] is True


def test_probe_reports_temporary_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _probe_module()
    app = _make_app(tmp_path)
    monkeypatch.setattr(module.shutil, "rmtree", lambda *_args, **_kwargs: None)

    payload = module.probe(app)

    assert payload["status"] == "error"
    assert payload["code"] == "cleanup_failed"
    assert payload["process_exited"] is True
    assert payload["port_released"] is True
    assert payload["temporary_root_removed"] is False
    import shutil

    monkeypatch.undo()
    shutil.rmtree(str(payload["temporary_root"]))
