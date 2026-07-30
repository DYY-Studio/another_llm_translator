from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TextIO

from .errors import UsageError


@contextmanager
def project_write_lock(project: Path) -> Iterator[None]:
    path = project / ".write.lock"
    handle: TextIO = path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UsageError(
                f"项目正在被另一个写入任务使用：{project.name}"
            ) from exc
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
