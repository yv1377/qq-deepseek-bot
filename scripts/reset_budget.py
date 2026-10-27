"""重置某个群今天的聊天额度（只清这个群，其他群和历史日期不动）。

平时不用跑这个脚本：主人在 QQ 里说「重置额度」/ 发 `/重置额度` 就会在 Bot 进程里
直接清零、立刻生效。本脚本只在 Bot 没在跑、或想从命令行兜底时用。

用法：
    python scripts/reset_budget.py 100000001
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from budget import BudgetStore  # noqa: E402
from config import PROJECT_ROOT, load_settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="重置某个群今天的聊天额度")
    parser.add_argument("group_id", type=int, help="群号")
    args = parser.parse_args()

    settings = load_settings(PROJECT_ROOT / "config/config.yaml")
    store = BudgetStore(settings.chat.budget)
    try:
        before = store.today_cny(args.group_id)
        limit = store.limit_for(args.group_id)
        if before <= 0:
            print(f"群 {args.group_id} 今天没有花费，无需重置（额度 {limit:.2f} 元）")
            return 0
        store.reset_today(args.group_id)
        print(
            f"群 {args.group_id} 今日额度已重置：{before:.4f} 元 -> 0，"
            f"额度 {limit:.2f} 元"
        )
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
