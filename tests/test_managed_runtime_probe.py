import hashlib
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.packaging

SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "probe_managed_runtime_macos.py"
)


def _run(lock: Path, archive: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--lock", str(lock), "--archive", str(archive)],
        capture_output=True,
        text=True,
        check=False,
    )


def _lock(sha256: str) -> dict[str, object]:
    return {
        "source": "python-build-standalone",
        "release_page": "https://github.com/astral-sh/python-build-standalone/releases/tag/20260901",
        "tag": "20260901",
        "python_version": "3.13.15",
        "target": "aarch64-apple-darwin",
        "gil_enabled": True,
        "archive_flavor": "install_only_stripped",
        "asset_filename": "cpython-3.13.15+20260901-aarch64-apple-darwin-install_only_stripped.tar.gz",
        "asset_url": "https://github.com/astral-sh/python-build-standalone/releases/download/20260901/cpython-3.13.15%2B20260901-aarch64-apple-darwin-install_only_stripped.tar.gz",
        "sha256": sha256,
        "sha256_source": "https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags/20260901",
        "minimum_macos": "13.0",
        "license_sources": [
            "https://github.com/astral-sh/python-build-standalone/blob/main/docs/running.rst#licensing",
            "https://github.com/astral-sh/python-build-standalone/blob/main/docs/distributions.rst#distribution-archives",
        ],
    }


def _write_lock(path: Path, values: dict[str, object]) -> None:
    path.write_text(json.dumps(values), encoding="utf-8")


@pytest.mark.parametrize(
    ("case", "diagnostic"),
    [
        ("missing_fields", "tag"),
        ("bad_asset_filename", "asset_filename"),
        ("missing_archive", "missing.tar.gz"),
        ("non_tar_gzip", ".tar.gz"),
        ("digest_mismatch", "sha256"),
    ],
)
def test_probe_rejects_invalid_lock_or_archive(
    tmp_path: Path, case: str, diagnostic: str
) -> None:
    lock = tmp_path / "runtime.lock.json"
    archive = tmp_path / ("runtime.zip" if case == "non_tar_gzip" else "runtime.tar.gz")
    if case != "missing_archive":
        archive.write_bytes(b"archive bytes")
    if case == "missing_fields":
        _write_lock(lock, {"source": "python-build-standalone"})
    elif case == "bad_asset_filename":
        values = _lock("0" * 64)
        values["asset_filename"] = (
            "cpython-3.13.14+20260901-aarch64-apple-darwin-install_only_stripped.tar.gz"
        )
        _write_lock(lock, values)
    elif case == "non_tar_gzip":
        _write_lock(lock, _lock(hashlib.sha256(archive.read_bytes()).hexdigest()))
    else:
        _write_lock(lock, _lock("0" * 64))
    if case == "missing_archive":
        archive = tmp_path / "missing.tar.gz"

    result = _run(lock, archive)

    assert result.returncode != 0
    assert diagnostic in result.stderr.lower() or diagnostic in result.stderr


def _runtime_script(
    version: str = "3.13.15",
    target: str = "aarch64-apple-darwin",
    fail_after_move: bool = False,
    require_original_gone: bool = False,
    gil_disabled: int | None = 0,
) -> bytes:
    fail_moved = 'case "$0" in *relocated*) exit 7;; esac' if fail_after_move else ""
    original_check = (
        'case "$0" in *relocated*) [ ! -e "$(dirname "$0")/../../extracted/python" ] || exit 8;; esac'
        if require_original_gone
        else ""
    )
    payload = json.dumps(
        {
            "version": version,
            "implementation": "cpython",
            "executable": "%s",
            "prefix": "%s",
            "host_gnu_type": target,
            "machine": "arm64",
            "gil_disabled": gil_disabled,
        }
    )
    script = f"""#!/bin/sh
{fail_moved}
{original_check}
exe="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
prefix="$(cd "$(dirname "$0")/.." && pwd)"
printf '{payload}\\n' "$exe" "$prefix"
"""
    return script.encode()


def _fake_tools(
    tmp_path: Path, *, arch: str = "arm64", minos: str = "11.0"
) -> dict[str, str]:
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    for name, body in {
        "file": "#!/bin/sh\nprintf 'Mach-O 64-bit executable arm64\\n'\n",
        "lipo": f"#!/bin/sh\nprintf '{arch}\\n'\n",
        "otool": f"#!/bin/sh\nif [ \"$1\" = '-L' ]; then printf '%s:\\n\\t@rpath/libpython3.13.dylib (compatibility version 3.13.0, current version 3.13.15)\\n' \"$2\"; else printf 'cmd LC_BUILD_VERSION\\n minos {minos}\\n'; fi\n",
    }.items():
        path = tools_dir / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    return {**os.environ, "PATH": f"{tools_dir}:{os.environ['PATH']}"}


def _archive(
    tmp_path: Path,
    *,
    include_python: bool = True,
    include_license: bool = True,
    version: str = "3.13.15",
    target: str = "aarch64-apple-darwin",
    fail_after_move: bool = False,
    require_original_gone: bool = False,
    gil_disabled: int | None = 0,
) -> Path:
    tree = tmp_path / "fixture" / "python"
    (tree / "bin").mkdir(parents=True)
    (tree / "lib" / "python3.13").mkdir(parents=True)
    (tree / "lib" / "libpython3.13.dylib").write_bytes(b"fixture dylib")
    if include_python:
        executable = tree / "bin" / "python3"
        executable.write_bytes(
            _runtime_script(
                version, target, fail_after_move, require_original_gone, gil_disabled
            )
        )
        executable.chmod(0o755)
    if include_license:
        (tree / "lib" / "python3.13" / "LICENSE.txt").write_text(
            "fixture license", encoding="utf-8"
        )
    archive = tmp_path / "fixture.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(tree, arcname="python")
    return archive


def _run_fixture(
    tmp_path: Path,
    archive: Path,
    *,
    arch: str = "arm64",
    minos: str = "11.0",
) -> subprocess.CompletedProcess[str]:
    lock = tmp_path / "runtime.lock.json"
    _write_lock(lock, _lock(hashlib.sha256(archive.read_bytes()).hexdigest()))
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--lock", str(lock), "--archive", str(archive)],
        capture_output=True,
        text=True,
        check=False,
        env=_fake_tools(tmp_path, arch=arch, minos=minos),
    )


@pytest.mark.parametrize(
    ("archive_kwargs", "run_kwargs", "diagnostic"),
    [
        ({"include_python": False}, {}, "bin/python3"),
        ({"version": "3.13.14"}, {}, "version"),
        ({"target": "x86_64-apple-darwin"}, {}, "target"),
        ({"gil_disabled": None}, {}, "py_gil_disabled"),
        ({"fail_after_move": True}, {}, "relocat"),
        ({}, {"arch": "x86_64"}, "arm64"),
        ({}, {"minos": "14.0"}, "14.0"),
        ({"include_license": False}, {}, "license"),
    ],
)
def test_probe_rejects_invalid_bundled_runtime(
    tmp_path: Path,
    archive_kwargs: dict[str, object],
    run_kwargs: dict[str, str],
    diagnostic: str,
) -> None:
    result = _run_fixture(tmp_path, _archive(tmp_path, **archive_kwargs), **run_kwargs)

    assert result.returncode != 0
    assert diagnostic in result.stderr.lower() or diagnostic in result.stderr


def test_probe_reports_evidence_for_a_minimal_runtime_moved_from_its_archive_path(
    tmp_path: Path,
) -> None:
    archive = _archive(tmp_path, require_original_gone=True)

    result = _run_fixture(tmp_path, archive)

    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["status"] == "ok"
    assert evidence["source"] == "python-build-standalone"
    assert evidence["tag"] == "20260901"
    assert evidence["python_version"] == "3.13.15"
    assert evidence["target"] == "aarch64-apple-darwin"
    assert evidence["sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert evidence["minimum_macos"] == "13.0"
    assert evidence["relocatable"] is True
    assert evidence["license_files"] == ["lib/python3.13/LICENSE.txt"]
    assert evidence["mach_o"]["architecture"] == "arm64"
