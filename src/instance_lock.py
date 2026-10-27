"""Phase 4.5：单实例保护。

同一个 QQ 号跑两个进程会抢 WS 连接、抢 SQLite 写锁、重复回复。
这里用文件锁（Windows `msvcrt` / Linux `fcntl`）保证一个 data 目录只跑一个实例；
进程崩溃时操作系统会自动释放锁，不会留下需要手动删除的“僵尸锁”。

注意：这是“防多开”，不是水平扩展。真正的多实例（Redis 锁 / Postgres 记忆）属于可选扩展。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("instance")


class InstanceLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._file: object | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")  # noqa: SIM115 - 需要长期持有
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                if not handle.read(1):
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._file = handle
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()).encode("ascii"))
            handle.flush()
        except OSError:  # pragma: no cover - 写 pid 失败不影响锁
            pass
        return True

    def release(self) -> None:
        handle, self._file = self._file, None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:  # pragma: no cover
            pass
        try:
            handle.close()
        except OSError:  # pragma: no cover
            pass
