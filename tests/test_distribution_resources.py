from __future__ import annotations

import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from pathlib import PurePosixPath

from app.project import PROMPT_NAMES


EXPECTED_PLUGIN_FILES = {
    "plugins/srt/__init__.py",
    "plugins/srt/adapter.py",
    "plugins/srt/plugin.py",
    "plugins/srt/plugin.toml",
    "plugins/term_validation/__init__.py",
    "plugins/term_validation/plugin.py",
    "plugins/term_validation/plugin.toml",
}


def _archive_suffixes(names: list[str], depth: int) -> set[str]:
    return {
        "/".join(PurePosixPath(name).parts[-depth:])
        for name in names
        if len(PurePosixPath(name).parts) >= depth
    }


def test_built_distributions_contain_every_project_prompt(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--sdist",
            "--no-isolation",
            "--outdir",
            str(tmp_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0

    expected = {f"prompts/{name}" for name in PROMPT_NAMES}
    wheel_path = next(tmp_path.glob("*.whl"))
    with zipfile.ZipFile(wheel_path) as archive:
        wheel_names = archive.namelist()
        wheel_paths = {
            "/".join(PurePosixPath(name).parts[-2:])
            for name in wheel_names
            if len(PurePosixPath(name).parts) >= 2
        }
    assert EXPECTED_PLUGIN_FILES <= _archive_suffixes(wheel_names, 3)
    assert not any(
        "plugins/" in name
        and ("/tests/" in name or "/__pycache__/" in name)
        for name in wheel_names
    )
    assert expected <= wheel_paths

    sdist_path = next(tmp_path.glob("*.tar.gz"))
    with tarfile.open(sdist_path, "r:gz") as archive:
        sdist_names = archive.getnames()
        sdist_paths = {
            "/".join(PurePosixPath(name).parts[-2:])
            for name in sdist_names
            if len(PurePosixPath(name).parts) >= 2
        }
    assert EXPECTED_PLUGIN_FILES <= _archive_suffixes(sdist_names, 3)
    assert not any(
        "plugins/" in name
        and ("/tests/" in name or "/__pycache__/" in name)
        for name in sdist_names
    )
    assert expected <= sdist_paths
