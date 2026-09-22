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

    sidecar_root = (
        contents / "Resources" / "_up_" / "sidecar-dist" / "translator-sidecar"
    )
    for relative in (
        "plugins/srt/__init__.py",
        "plugins/srt/adapter.py",
        "plugins/srt/plugin.py",
        "plugins/srt/plugin.toml",
        "plugins/term_validation/__init__.py",
        "plugins/term_validation/plugin.py",
        "plugins/term_validation/plugin.toml",
    ):
        resource = sidecar_root / "_internal" / relative
        resource.parent.mkdir(parents=True, exist_ok=True)
        resource.write_text("", encoding="utf-8")

    root_literal = repr(str(ROOT))
    failure = (
        "os.environ['ANOTHER_LLM_PROBE_DEPENDENCY_PATH'] = '/path/that/does/not/exist'"
        if dependency_failure
        else ""
    )
    clean_env_guard = (
        "if os.environ.get('PYTHONPATH') or os.environ.get('VIRTUAL_ENV'):\n"
        "    raise SystemExit('host-only sentinel leaked into packaged sidecar')\n"
        if require_clean_env
        else ""
    )
    sidecar = (
        f"#!{sys.executable}\n"
        "import os, runpy, sys\n"
        f"sys.path.insert(0, {root_literal})\n"
        f"{clean_env_guard}"
        f"{failure}\n"
        "runpy.run_module('app.web', run_name='__main__')\n"
    )
    _make_executable(sidecar_root / "translator-sidecar", sidecar)
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
        / "Contents/Resources/_up_/sidecar-dist/translator-sidecar/_internal/plugins/srt/plugin.toml"
    )
    resource.unlink()

    result = _run_probe(app)

    assert result.returncode != 0
    payload = _payload(result)
    assert payload["status"] == "error"
    assert str(resource) in str(payload)


def test_probe_rejects_private_dependency_bundled_in_app(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    bundled = (
        app
        / "Contents/Resources/_up_/sidecar-dist/translator-sidecar/_internal"
        / "probe_private_dependency.py"
    )
    bundled.write_text("MARKER = 'must not be bundled'\n", encoding="utf-8")

    result = _run_probe(app)

    assert result.returncode != 0
    payload = _payload(result)
    assert payload["status"] == "error"
    assert payload["code"] == "private_dependency_bundled"
    assert str(bundled) in str(payload)


def test_probe_reports_packaged_private_dependency_import_failure(
    tmp_path: Path,
) -> None:
    app = _make_app(tmp_path, dependency_failure=True)

    result = _run_probe(app)

    assert result.returncode != 0
    payload = _payload(result)
    assert payload["status"] == "error"
    assert payload["code"] == "sidecar_exit"
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
    assert payload["forced_kill"] is False
    assert payload["port_released"] is True
    assert payload["temporary_root_removed"] is True
    assert not Path(str(payload["temporary_root"])).exists()


def test_probe_clears_host_paths_before_packaged_sidecar(tmp_path: Path) -> None:
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
