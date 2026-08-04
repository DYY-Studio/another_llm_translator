from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator

if os.name == "nt":
    import ctypes
    import msvcrt
else:
    import fcntl

from .errors import UsageError


@contextmanager
def project_write_lock(project: Path) -> Iterator[None]:
    path = project / ".write.lock"
    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.CreateFileW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        # Allow project deletion to finish while this lock handle is open.
        native_handle = kernel32.CreateFileW(
            str(path),
            0xC0000000,
            0x00000007,
            None,
            4,
            0x00000080,
            None,
        )
        if native_handle == ctypes.c_void_p(-1).value:
            raise OSError(f"无法打开项目锁文件：{path}")
        descriptor = msvcrt.open_osfhandle(native_handle, os.O_RDWR | os.O_BINARY)
        handle: BinaryIO = os.fdopen(descriptor, "r+b")
    else:
        handle = path.open("a+b")
    locked = False
    try:
        if os.name == "nt":
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                try:
                    handle.write(b"\0")
                    handle.flush()
                except OSError as exc:
                    raise UsageError(
                        f"项目正在被另一个写入任务使用：{project.name}"
                    ) from exc
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise UsageError(
                    f"项目正在被另一个写入任务使用：{project.name}"
                ) from exc
        else:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise UsageError(
                    f"项目正在被另一个写入任务使用：{project.name}"
                ) from exc
        locked = True
        yield
    finally:
        try:
            if locked:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
