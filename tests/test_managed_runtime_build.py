from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import subprocess
import sys
import sysconfig
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "scripts" / "build_managed_runtime_macos.sh"


def _wheel(path: Path, name: str, version: str, files: dict[str, bytes]) -> str:
    normalized_name = name.replace("-", "_")
    dist_info = f"{normalized_name}-{version}.dist-info"
    wheel_files = {
        **files,
        f"{dist_info}/METADATA": (
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\n"
        ).encode(),
        f"{dist_info}/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: test\n"
            b"Root-Is-Purelib: true\nTag: py3-none-any\n\n"
        ),
    }
    record_path = f"{dist_info}/RECORD"
    output = io.StringIO(newline="")
    rows = csv.writer(output, lineterminator="\n")
    for filename, content in sorted(wheel_files.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest())
        rows.writerow(
            (filename, f"sha256={digest.rstrip(b'=').decode()}", len(content))
        )
    rows.writerow((record_path, "", ""))
    wheel_files[record_path] = output.getvalue().encode()
    with zipfile.ZipFile(path, "w") as archive:
        for filename, content in wheel_files.items():
            archive.writestr(filename, content)
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def runtime_inputs(tmp_path: Path) -> dict[str, Path]:
    runtime = tmp_path / "pbs-install"
    (runtime / "bin").mkdir(parents=True)
    python = runtime / "bin" / "python3"
    python.write_text(
        '#!/bin/sh\nexec "$MANAGED_RUNTIME_TEST_PYTHON" "$@"\n',
        encoding="utf-8",
    )
    python.chmod(0o755)

    pbs_lock = tmp_path / "pbs.lock.json"
    pbs_archive = tmp_path / "cpython-fixture.tar.gz"
    pbs_archive.write_bytes(b"verified PBS archive fixture")
    pbs_lock.write_text(
        json.dumps(
            {
                "python_version": sys.version.split()[0],
                "target": sysconfig.get_config_var("HOST_GNU_TYPE"),
                "gil_enabled": sysconfig.get_config_var("Py_GIL_DISABLED") == 0,
                "asset_filename": pbs_archive.name,
                "sha256": hashlib.sha256(pbs_archive.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )

    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    dependency_wheel = wheelhouse / "fixture_dependency-1.0-py3-none-any.whl"
    dependency_hash = _wheel(
        dependency_wheel,
        "fixture-dependency",
        "1.0",
        {"fixture_dependency.py": b"VALUE = 'installed from the local wheelhouse'\n"},
    )
    requirements_lock = tmp_path / "requirements.lock"
    requirements_lock.write_text(
        "# fixture_dependency-1.0-py3-none-any.whl; tag=py3-none-any\n"
        f"fixture-dependency==1.0 --hash=sha256:{dependency_hash}\n",
        encoding="utf-8",
    )

    host_site_packages = tmp_path / "host-site-packages"
    host_site_packages.mkdir()
    (host_site_packages / "host_probe.py").write_text("SHOULD_NOT_COPY = True\n")
    host_venv = tmp_path / "host-venv"
    host_venv_site = (
        host_venv
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    host_venv_site.mkdir(parents=True)
    (host_venv_site / "venv_probe.py").write_text("SHOULD_NOT_COPY = True\n")
    user_root = tmp_path / "user-data"
    user_plugin = user_root / "plugins" / "user_adapter"
    user_plugin.mkdir(parents=True)
    (user_plugin / "plugin.toml").write_text("id = 'user-adapter'\n")

    project_wheel = tmp_path / "translator_fixture-1.0-py3-none-any.whl"
    _wheel(
        project_wheel,
        "translator-fixture",
        "1.0",
        {
            "app/__init__.py": b"\n",
            "app/web.py": b"def main():\n    return 'fixture app'\n",
            "translator_fixture-1.0.dist-info/entry_points.txt": (
                b"[console_scripts]\nfixture-command = app.web:main\n"
            ),
            "translator_fixture-1.0.data/data/config/config.toml": b"[server]\nport = 0\n",
            "translator_fixture-1.0.data/data/prompts/translation.en.middle.txt": b"Translate.\n",
            "translator_fixture-1.0.data/data/llm_adapters/openai-compatible.json": b"{}\n",
            "translator_fixture-1.0.data/data/llm_presets/default.json": b"{}\n",
            "translator_fixture-1.0.data/data/plugins/srt/__init__.py": b"\n",
            "translator_fixture-1.0.data/data/plugins/srt/adapter.py": b"\n",
            "translator_fixture-1.0.data/data/plugins/srt/plugin.py": b"\n",
            "translator_fixture-1.0.data/data/plugins/srt/plugin.toml": (
                b"schema = 1\n\n[plugin]\nid = 'srt'\n"
            ),
            "translator_fixture-1.0.data/data/plugins/term_validation/__init__.py": b"\n",
            "translator_fixture-1.0.data/data/plugins/term_validation/plugin.py": b"\n",
            "translator_fixture-1.0.data/data/plugins/term_validation/plugin.toml": (
                b"schema = 1\n\n[plugin]\nid = 'term_validation'\n"
            ),
        },
    )
    return {
        "runtime": runtime,
        "pbs_archive": pbs_archive,
        "pbs_lock": pbs_lock,
        "wheelhouse": wheelhouse,
        "requirements_lock": requirements_lock,
        "project_wheel": project_wheel,
        "output": tmp_path / "build" / "managed-runtime-dist",
        "tmp_path": tmp_path,
        "user_root": user_root,
        "host_venv": host_venv,
    }


def _run_build(
    inputs: dict[str, Path], *, omit: str | None = None, output_arg: str | None = None
) -> subprocess.CompletedProcess[str]:
    args = [
        "bash",
        str(BUILD_SCRIPT),
        "--runtime-source",
        str(inputs["runtime"]),
        "--pbs-lock",
        str(inputs["pbs_lock"]),
        "--pbs-archive",
        str(inputs["pbs_archive"]),
        "--project-wheel",
        str(inputs["project_wheel"]),
        "--wheelhouse",
        str(inputs["wheelhouse"]),
        "--requirements-lock",
        str(inputs["requirements_lock"]),
        "--output",
        output_arg or "build/managed-runtime-dist",
    ]
    if omit:
        option = "--" + omit.replace("_", "-")
        args = args[: args.index(option)]
    environment = os.environ.copy()
    environment.update(
        {
            "MANAGED_RUNTIME_TEST_PYTHON": sys.executable,
            "PYTHONPATH": str(inputs["tmp_path"] / "host-site-packages"),
            "VIRTUAL_ENV": str(inputs["host_venv"]),
            "ANOTHER_LLM_USER_ROOT": str(inputs["user_root"]),
        }
    )
    return subprocess.run(
        args,
        cwd=inputs["tmp_path"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_build_installs_project_wheel_locked_dependency_and_official_plugin_resources(
    runtime_inputs: dict[str, Path],
) -> None:
    result = _run_build(runtime_inputs)

    assert result.returncode == 0, result.stderr
    output = runtime_inputs["output"]
    assert (output / "bin" / "python3").is_file()
    assert json.loads(
        (output / "share" / "managed-runtime" / "pbs-macos-arm64.lock.json").read_text(
            encoding="utf-8"
        )
    ) == json.loads(runtime_inputs["pbs_lock"].read_text(encoding="utf-8"))
    site_packages = (
        output
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    assert (site_packages / "app" / "web.py").is_file()
    assert (site_packages / "fixture_dependency.py").is_file()
    assert not (output / "bin" / "fixture-command").exists()
    assert (output / "config" / "config.toml").is_file()
    assert (output / "prompts" / "translation.en.middle.txt").is_file()
    assert (output / "llm_adapters" / "openai-compatible.json").is_file()
    assert (output / "llm_presets" / "default.json").is_file()
    assert (output / "plugins" / "srt" / "plugin.toml").is_file()
    assert (output / "plugins" / "srt" / "__init__.py").is_file()
    assert (output / "plugins" / "srt" / "adapter.py").is_file()
    assert (output / "plugins" / "srt" / "plugin.py").is_file()
    assert (output / "plugins" / "term_validation" / "plugin.toml").is_file()
    assert (output / "plugins" / "term_validation" / "plugin.py").is_file()
    assert not (output / "plugins" / "user_adapter").exists()
    assert not (output / "_internal").exists()
    assert not (output / "translator-sidecar").exists()
    assert not (output / "sidecar-dist").exists()
    assert not (output / "site-packages" / "host_probe.py").exists()
    assert not (site_packages / "host_probe.py").exists()
    assert not (site_packages / "venv_probe.py").exists()
    assert not any("host-site-packages" in str(path) for path in output.rglob("*"))
    assert not any("host-venv" in str(path) for path in output.rglob("*"))
    assert not any("user-data" in str(path) for path in output.rglob("*"))
    assert not any(
        "minimal_llm_translator-plugin-dependency-runtime" in str(path)
        for path in output.rglob("*")
    )


@pytest.mark.parametrize(
    "missing_input",
    [
        "runtime_source",
        "pbs_lock",
        "pbs_archive",
        "project_wheel",
        "wheelhouse",
        "requirements_lock",
    ],
)
def test_build_fails_with_named_diagnostic_when_any_required_input_is_missing(
    runtime_inputs: dict[str, Path], missing_input: str
) -> None:
    result = _run_build(runtime_inputs, omit=missing_input)

    assert result.returncode != 0
    assert missing_input.replace("_", "-") in (result.stderr + result.stdout)


def test_build_rejects_output_outside_staging_without_creating_its_parent(
    runtime_inputs: dict[str, Path],
) -> None:
    output = runtime_inputs["tmp_path"] / "outside" / "managed-runtime-dist"

    result = _run_build(runtime_inputs, output_arg=str(output))

    assert result.returncode != 0
    assert not output.parent.exists()


def test_build_rejects_input_inside_existing_output_without_removing_it(
    runtime_inputs: dict[str, Path],
) -> None:
    output = runtime_inputs["output"]
    output.mkdir(parents=True)
    nested_wheel = output / "translator_fixture-1.0-py3-none-any.whl"
    wheel_contents = runtime_inputs["project_wheel"].read_bytes()
    nested_wheel.write_bytes(wheel_contents)
    runtime_inputs["project_wheel"] = nested_wheel

    result = _run_build(runtime_inputs)

    assert result.returncode != 0
    assert "input is inside output" in (result.stderr + result.stdout)
    assert nested_wheel.read_bytes() == wheel_contents


@pytest.mark.parametrize("archive_state", ["missing", "substituted"])
def test_build_rejects_missing_or_substituted_locked_pbs_archive(
    runtime_inputs: dict[str, Path], archive_state: str
) -> None:
    archive = runtime_inputs["pbs_archive"]
    if archive_state == "missing":
        archive.unlink()
    else:
        archive.write_bytes(b"different PBS archive")

    result = _run_build(runtime_inputs)

    assert result.returncode != 0
    assert "PBS archive SHA-256 mismatch" in (result.stderr + result.stdout)
    assert not runtime_inputs["output"].exists()


def test_build_fails_when_locked_wheel_hash_does_not_match_local_wheel(
    runtime_inputs: dict[str, Path],
) -> None:
    lock = runtime_inputs["requirements_lock"]
    lock.write_text(
        "fixture-dependency==1.0 --hash=sha256:" + "0" * 64 + "\n",
        encoding="utf-8",
    )

    result = _run_build(runtime_inputs)

    assert result.returncode != 0
    assert "HASH" in (result.stderr + result.stdout)
    assert not runtime_inputs["output"].exists()
