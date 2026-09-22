from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import types
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
PROBE = ROOT / "scripts" / "probe_plugin_dependencies.py"


def _probe_module():
    spec = importlib.util.spec_from_file_location("dependency_probe", PROBE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def project_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output_dir = tmp_path_factory.mktemp("project-wheel")
    source_dir = output_dir.parent / "project-source"
    source_dir.mkdir()
    for name in ("README.md", "MANIFEST.in", "pyproject.toml"):
        shutil.copy2(ROOT / name, source_dir / name)
    for name in (
        "app",
        "config",
        "llm_adapters",
        "llm_presets",
        "plugins",
        "prompts",
    ):
        shutil.copytree(
            ROOT / name,
            source_dir / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"),
        )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(output_dir),
        ],
        cwd=source_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    wheels = tuple(output_dir.glob("*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def _run_probe(*args: str) -> subprocess.CompletedProcess[str]:
    return _run_probe_with_env(*args)


def _run_probe_with_env(
    *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PROBE), *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _payload(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    return json.loads(result.stdout)


def test_probe_reports_missing_wheel_and_target(tmp_path: Path) -> None:
    missing_wheel = tmp_path / "missing.whl"

    result = _run_probe(
        "--wheel",
        str(missing_wheel),
        "--python",
        sys.executable,
        "--import-target",
        "plugins.srt.plugin",
    )

    assert result.returncode != 0
    payload = _payload(result)
    assert payload["status"] == "error"
    assert str(missing_wheel) in str(payload)
    assert "plugins.srt.plugin" in str(payload)


def test_probe_reports_missing_interpreter_and_target(tmp_path: Path) -> None:
    wheel = tmp_path / "probe.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("probe-0.0.dist-info/WHEEL", "Wheel-Version: 1.0\n")
    missing_python = tmp_path / "missing-python"

    result = _run_probe(
        "--wheel",
        str(wheel),
        "--python",
        str(missing_python),
        "--import-target",
        "plugins.srt.plugin",
    )

    assert result.returncode != 0
    payload = _payload(result)
    assert payload["status"] == "error"
    assert str(missing_python) in str(payload)
    assert "plugins.srt.plugin" in str(payload)


def test_probe_imports_official_target_and_real_external_adapter(
    project_wheel: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _probe_module()
    write_plugin = module._write_external_plugin

    def write_plugin_with_packaged_only_smoke(
        user_root: Path, dependency_root: Path
    ) -> Path:
        plugin_root = write_plugin(user_root, dependency_root)
        with (plugin_root / "plugin.py").open("a", encoding="utf-8") as stream:
            stream.write(
                "\ndef binary_smoke(expected_runtime_root):\n"
                "    raise RuntimeError('Web probe called packaged-only binary smoke')\n"
            )
        return plugin_root

    monkeypatch.setattr(
        module, "_write_external_plugin", write_plugin_with_packaged_only_smoke
    )
    payload = module.probe(project_wheel, Path(sys.executable), "plugins.srt.plugin")

    assert payload["status"] == "ok"
    assert payload["adapter_discovered"] is True
    assert payload["file_imported"] is True
    assert payload["port_released"] is True
    assert payload["temporary_root_removed"] is True
    assert not Path(str(payload["temporary_root"])).exists()
    assert "binary_imported" not in payload


def test_probe_reports_import_target_failure(project_wheel: Path) -> None:
    target = "plugins.srt.missing_target"
    result = _run_probe(
        "--wheel",
        str(project_wheel),
        "--python",
        sys.executable,
        "--import-target",
        target,
    )

    assert result.returncode != 0
    payload = _payload(result)
    assert payload["status"] == "error"
    assert payload["import_target"] == target
    assert target in str(payload)
    assert payload["temporary_root_removed"] is True


def test_probe_hides_host_only_dependencies_from_clean_venv(
    project_wheel: Path, tmp_path: Path
) -> None:
    host_module = tmp_path / "host_only_probe_dependency.py"
    host_module.write_text("MARKER = 'host-only'\n", encoding="utf-8")
    host_env = dict(os.environ)
    host_env["PYTHONPATH"] = str(tmp_path)

    for target in ("mcp", "host_only_probe_dependency"):
        result = _run_probe_with_env(
            "--wheel",
            str(project_wheel),
            "--python",
            sys.executable,
            "--import-target",
            target,
            env=host_env,
        )

        assert result.returncode != 0
        payload = _payload(result)
        assert payload["code"] == "import_failed"
        assert payload["import_target"] == target


def test_main_returns_nonzero_when_temporary_cleanup_fails(
    project_wheel: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _probe_module()
    monkeypatch.setattr(module.shutil, "rmtree", lambda *_args, **_kwargs: None)

    exit_code = module.main(
        [
            "--wheel",
            str(project_wheel),
            "--python",
            sys.executable,
            "--import-target",
            "plugins.srt.plugin",
        ]
    )

    assert exit_code != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert payload["code"] == "cleanup_failed"
    assert payload["temporary_root_removed"] is False
    cleanup_root = Path(str(payload["temporary_root"]))
    module.shutil.rmtree = shutil.rmtree
    shutil.rmtree(cleanup_root)


def test_wait_for_web_preserves_last_probe_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _probe_module()
    failures = iter(
        [
            module.ProbeFailure("first", "first diagnostic"),
            module.ProbeFailure("last", "last diagnostic"),
        ]
    )

    def request(_url: str) -> None:
        raise next(failures)

    monkeypatch.setattr(module, "_request_json", request)
    clock = iter((0.0, 0.1, 0.2, 21.0))
    monkeypatch.setattr(module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    with pytest.raises(module.ProbeFailure) as raised:
        module._wait_for_web(
            types.SimpleNamespace(poll=lambda: None),
            "http://probe.invalid",
            tmp_path / "web.log",
        )

    assert raised.value.code == "web_startup_failed"
    assert "last diagnostic" in str(raised.value)
