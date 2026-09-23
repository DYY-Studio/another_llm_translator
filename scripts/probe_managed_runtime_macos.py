"""Probe a locally downloaded PBS macOS arm64 runtime archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any


class ProbeFailure(Exception):
    """A required lock, archive, runtime, or platform check failed."""


INFO_CODE = """\
import json, platform, sys, sysconfig
from pathlib import Path
print(json.dumps({
    "version": sys.version.split()[0],
    "implementation": sys.implementation.name,
    "executable": str(Path(sys.executable).resolve()),
    "prefix": str(Path(sys.prefix).resolve()),
    "host_gnu_type": sysconfig.get_config_var("HOST_GNU_TYPE"),
    "machine": platform.machine(),
    "gil_disabled": sysconfig.get_config_var("Py_GIL_DISABLED"),
}))
"""


def _fail(message: str) -> None:
    raise ProbeFailure(message)


def _load_lock(path: Path) -> dict[str, Any]:
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fail(f"lock {path}: {exc}")
    if not isinstance(lock, dict):
        _fail(f"lock {path}: expected a JSON object")
    required = (
        "source",
        "release_page",
        "tag",
        "python_version",
        "target",
        "gil_enabled",
        "archive_flavor",
        "asset_filename",
        "asset_url",
        "sha256",
        "sha256_source",
        "minimum_macos",
        "license_sources",
    )
    missing = [key for key in required if key not in lock]
    if missing:
        _fail(f"lock {path}: missing required field(s): {', '.join(missing)}")
    if lock["source"] != "python-build-standalone":
        _fail(f"lock {path}: unsupported source {lock['source']!r}")
    if lock["target"] != "aarch64-apple-darwin" or lock["gil_enabled"] is not True:
        _fail(f"lock {path}: expected GIL-enabled aarch64-apple-darwin target")
    if lock["archive_flavor"] != "install_only_stripped":
        _fail(f"lock {path}: expected install_only_stripped archive")
    if not isinstance(lock["python_version"], str) or not re.fullmatch(
        r"3\.13\.\d+", lock["python_version"]
    ):
        _fail(
            f"lock {path}: python_version must be an exact CPython 3.13 patch version"
        )
    if not isinstance(lock["tag"], str) or not lock["tag"].isdigit():
        _fail(f"lock {path}: tag must be a dated numeric PBS release tag")
    if lock["minimum_macos"] != "13.0":
        _fail(f"lock {path}: minimum_macos must be 13.0")
    expected_asset = f"cpython-{lock['python_version']}+{lock['tag']}-{lock['target']}-install_only_stripped.tar.gz"
    if lock["asset_filename"] != expected_asset:
        _fail(f"lock {path}: asset_filename must be {expected_asset}")
    expected_asset_url = (
        f"https://github.com/astral-sh/python-build-standalone/releases/download/{lock['tag']}/"
        f"{expected_asset.replace('+', '%2B')}"
    )
    if lock["asset_url"] != expected_asset_url:
        _fail(f"lock {path}: asset_url must be {expected_asset_url}")
    if (
        lock["release_page"]
        != f"https://github.com/astral-sh/python-build-standalone/releases/tag/{lock['tag']}"
    ):
        _fail(f"lock {path}: release_page does not match tag {lock['tag']}")
    if lock["sha256_source"] != (
        f"https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags/{lock['tag']}"
    ):
        _fail(
            f"lock {path}: sha256_source must be the official PBS release API for tag {lock['tag']}"
        )
    if not isinstance(lock["sha256"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", lock["sha256"]
    ):
        _fail(f"lock {path}: sha256 must be 64 lowercase hexadecimal characters")
    if not isinstance(lock["license_sources"], list) or not lock["license_sources"]:
        _fail(f"lock {path}: license_sources must be a non-empty list")
    return lock


def _run(args: list[str], *, env: dict[str, str] | None = None) -> str:
    try:
        result = subprocess.run(
            args, check=False, capture_output=True, text=True, env=env
        )
    except OSError as exc:
        _fail(f"{args[0]}: {exc}")
    if result.returncode:
        _fail(
            f"{args[0]} failed for {args[-1]}: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout.strip()


def _runtime_info(python: Path, expected_root: Path) -> dict[str, Any]:
    env = os.environ.copy()
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        env.pop(name, None)
    output = _run([str(python), "-I", "-c", INFO_CODE], env=env)
    try:
        info = json.loads(output)
    except json.JSONDecodeError as exc:
        _fail(f"bundled Python {python}: invalid runtime JSON: {exc}")
    if not isinstance(info, dict):
        _fail(f"bundled Python {python}: expected runtime JSON object")
    resolved_root = expected_root.resolve()
    for field in ("executable", "prefix"):
        value = info.get(field)
        try:
            Path(value).resolve().relative_to(resolved_root)
        except (TypeError, ValueError):
            _fail(
                f"bundled Python {python}: {field} is outside relocated runtime: {value!r}"
            )
    return info


def _version_tuple(value: str, path: Path) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError:
        _fail(f"Mach-O {path}: invalid macOS deployment version {value!r}")


def _mach_o_files(root: Path) -> list[Path]:
    candidates = {root / "bin" / "python3"}
    candidates.update(root.rglob("*.dylib"))
    candidates.update(root.rglob("*.so"))
    files = sorted(path for path in candidates if path.is_file())
    if len(files) < 2:
        _fail(f"runtime {root}: expected Python executable and Mach-O libraries")
    return files


def _inspect_mach_o(root: Path, minimum_macos: str) -> dict[str, Any]:
    binaries: list[dict[str, Any]] = []
    minimum = _version_tuple(minimum_macos, root)
    for path in _mach_o_files(root):
        file_description = _run(["file", "-b", str(path)])
        if "Mach-O" not in file_description:
            _fail(f"Mach-O {path}: file reports {file_description!r}")
        architectures = _run(["lipo", "-archs", str(path)]).split()
        if "arm64" not in architectures:
            _fail(f"Mach-O {path}: expected arm64, found {architectures}")
        load_commands = _run(["otool", "-l", str(path)])
        deployment_versions: list[str] = []
        command = ""
        for line in load_commands.splitlines():
            stripped = line.strip()
            if stripped.startswith("cmd "):
                command = stripped[4:]
            elif (
                command == "LC_BUILD_VERSION"
                and stripped.startswith("minos ")
                or command == "LC_VERSION_MIN_MACOSX"
                and stripped.startswith("version ")
            ):
                deployment_versions.append(stripped.split()[1])
        if not deployment_versions:
            _fail(f"Mach-O {path}: no macOS deployment target in load commands")
        for version in deployment_versions:
            if _version_tuple(version, path) > minimum:
                _fail(
                    f"Mach-O {path}: deployment target {version} exceeds macOS {minimum_macos}"
                )
        linked_libraries = _run(["otool", "-L", str(path)])
        binaries.append(
            {
                "path": path.relative_to(root).as_posix(),
                "architectures": architectures,
                "deployment_targets": deployment_versions,
                "linked_libraries": linked_libraries.splitlines()[1:],
            }
        )
    return {"architecture": "arm64", "binaries": binaries}


def probe(lock_path: Path, archive: Path) -> dict[str, Any]:
    lock = _load_lock(lock_path)
    if not archive.name.endswith(".tar.gz"):
        _fail(f"archive {archive}: expected a .tar.gz file")
    if not archive.is_file():
        _fail(f"archive {archive}: file does not exist")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != lock["sha256"]:
        _fail(
            f"archive {archive}: sha256 mismatch (expected {lock['sha256']}, got {digest})"
        )

    with tempfile.TemporaryDirectory(prefix="pbs-runtime-probe-") as temp_dir:
        extracted = Path(temp_dir) / "extracted"
        extracted.mkdir()
        try:
            with tarfile.open(archive, "r:gz") as tar:
                tar.extractall(extracted, filter="data")
        except (OSError, tarfile.TarError, ValueError) as exc:
            _fail(f"archive {archive}: cannot safely extract .tar.gz: {exc}")
        root = extracted / "python"
        if not root.is_dir():
            _fail(f"archive {archive}: missing python/ installation root")
        python = root / "bin" / "python3"
        if not python.is_file() or not os.access(python, os.X_OK):
            _fail(f"archive {archive}: missing executable bin/python3 at {python}")
        license_files = sorted(
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file()
            and re.search(r"(?:license|copying|notice)", path.name, re.IGNORECASE)
        )
        required_license = root / "lib" / "python3.13" / "LICENSE.txt"
        if not license_files or not required_license.is_file():
            _fail(
                f"archive {archive}: missing PBS license material ({required_license})"
            )

        original_info = _runtime_info(python, root)
        expected_version = lock["python_version"]
        if (
            original_info.get("implementation") != "cpython"
            or original_info.get("version") != expected_version
        ):
            _fail(
                f"bundled Python {python}: version mismatch; expected CPython {expected_version}, got "
                f"{original_info.get('implementation')} {original_info.get('version')}"
            )
        if (
            original_info.get("host_gnu_type") != lock["target"]
            or original_info.get("machine") != "arm64"
        ):
            _fail(
                f"bundled Python {python}: expected target {lock['target']} arm64, got "
                f"{original_info.get('host_gnu_type')} {original_info.get('machine')}"
            )
        gil_disabled = original_info.get("gil_disabled")
        if type(gil_disabled) is not int or gil_disabled != 0:
            _fail(
                f"bundled Python {python}: expected GIL-enabled build, Py_GIL_DISABLED={original_info['gil_disabled']}"
            )

        relocated = Path(temp_dir) / "relocated"
        shutil.move(str(root), str(relocated))
        if root.exists():
            _fail(
                f"runtime relocation left the original extracted path in place: {root}"
            )
        moved_info = _runtime_info(relocated / "bin" / "python3", relocated)
        if moved_info.get("version") != expected_version:
            _fail(
                f"relocated Python {relocated}: version changed to {moved_info.get('version')}"
            )
        if (
            moved_info.get("host_gnu_type") != lock["target"]
            or moved_info.get("machine") != "arm64"
        ):
            _fail(f"relocated Python {relocated}: target changed after relocation")
        if (
            type(moved_info.get("gil_disabled")) is not int
            or moved_info["gil_disabled"] != 0
        ):
            _fail(f"relocated Python {relocated}: GIL mode changed after relocation")
        mach_o = _inspect_mach_o(relocated, lock["minimum_macos"])
        return {
            "status": "ok",
            "source": lock["source"],
            "tag": lock["tag"],
            "python_version": expected_version,
            "target": lock["target"],
            "archive": str(archive),
            "sha256": digest,
            "minimum_macos": lock["minimum_macos"],
            "license_files": license_files,
            "relocatable": True,
            "mach_o": mach_o,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args()
    try:
        evidence = probe(args.lock, args.archive)
    except ProbeFailure as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
