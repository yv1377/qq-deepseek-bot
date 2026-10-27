"""Phase 4.5 单实例锁测试。

验证：同一路径重复 acquire 失败、release 后可以重新获取、锁文件记录 pid。
运行：python scripts/instance_test.py
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from instance_lock import InstanceLock  # noqa: E402

PASSED = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if not condition:
        raise AssertionError(f"FAIL: {name} {detail}")
    PASSED += 1
    print(f"  ok  {name}")


def main() -> None:
    data_dir = ROOT / "data"
    data_dir.mkdir(exist_ok=True)
    path = data_dir / f"instance-{uuid.uuid4().hex[:8]}.lock"

    first = InstanceLock(path)
    check("首次获取成功", first.acquire())
    second = InstanceLock(path)
    check("重复获取失败", not second.acquire())
    first.release()

    # Windows 的文件锁会阻止其他句柄读取，所以释放后再检查 pid
    content = path.read_text(encoding="ascii", errors="ignore").strip("\x00")
    check("锁文件写入进程号", str(os.getpid()) in content)

    third = InstanceLock(path)
    check("释放后可重新获取", third.acquire())
    third.release()

    try:
        path.unlink()
    except OSError:
        pass
    print(f"\n单实例锁测试通过：{PASSED} 项检查")


if __name__ == "__main__":
    main()
