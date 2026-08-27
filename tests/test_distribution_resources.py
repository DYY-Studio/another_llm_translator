from __future__ import annotations

import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from pathlib import PurePosixPath

from app.project import PROMPT_NAMES


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
        wheel_paths = {
            "/".join(PurePosixPath(name).parts[-2:])
            for name in archive.namelist()
            if len(PurePosixPath(name).parts) >= 2
        }
    assert expected <= wheel_paths

    sdist_path = next(tmp_path.glob("*.tar.gz"))
    with tarfile.open(sdist_path, "r:gz") as archive:
        sdist_paths = {
            "/".join(PurePosixPath(name).parts[-2:])
            for name in archive.getnames()
            if len(PurePosixPath(name).parts) >= 2
        }
    assert expected <= sdist_paths
