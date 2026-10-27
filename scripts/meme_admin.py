"""表情包图库管理：待删除标记 / 隐私扫描 / 确认清理。

用法：
  python scripts/meme_admin.py pending
  python scripts/meme_admin.py purge
  python scripts/meme_admin.py privacy-scan            # 只看
  python scripts/meme_admin.py privacy-scan --apply    # 删除命中隐私关键词的图
  python scripts/meme_admin.py mark 100000001 <文件名> [原因]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from config import load_settings  # noqa: E402
from memes import MemeStore  # noqa: E402


def build_store() -> MemeStore:
    settings = load_settings(ROOT / "config/config.yaml")
    return MemeStore(settings.chat.memes)


def cmd_pending(store: MemeStore) -> int:
    items = store.pending_deletions()
    if not items:
        print("没有待删除的表情包。")
        return 0
    print(f"待删除 {len(items)} 张：")
    for item in items:
        print(
            f"- 群 {item.get('group_id')} / {item.get('name')} / "
            f"申请人 {item.get('requester')} / 原因：{item.get('reason') or '未写'}"
        )
    return 0


def cmd_purge(store: MemeStore) -> int:
    removed = store.purge_pending()
    print(f"已删除 {removed} 张待删表情包，剩余 {len(store.pending_deletions())} 张。")
    return 0


def cmd_privacy_scan(store: MemeStore, apply: bool) -> int:
    hits = 0
    for group_id in sorted(
        int(p.name) for p in store.root.iterdir() if p.is_dir() and p.name.isdigit()
    ):
        for item in store.items(group_id):
            text = " ".join(
                str(item.get(key) or "") for key in ("description", "context", "tags")
            )
            if store.looks_private(text):
                hits += 1
                print(
                    f"- 命中：群 {group_id} / {item.get('name')} / "
                    f"{(item.get('description') or item.get('context') or '')[:100]}"
                )
                if apply:
                    store.delete_by_name(group_id, item["name"])
    if not hits:
        print("未发现带隐私关键词的图库图片。")
    elif not apply:
        print(f"共命中 {hits} 张；确认后可加 --apply 删除。")
    else:
        print(f"已删除 {hits} 张隐私风险图片。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="表情包图库管理")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("pending", help="列出待删除标记")
    sub.add_parser("purge", help="删除所有待删标记")
    scan = sub.add_parser("privacy-scan", help="扫描隐私关键词")
    scan.add_argument("--apply", action="store_true", help="直接删除命中图片")
    mark = sub.add_parser("mark", help="手动标记一张图待删")
    mark.add_argument("group_id", type=int)
    mark.add_argument("name")
    mark.add_argument("reason", nargs="?", default="手动标记")
    args = parser.parse_args()
    store = build_store()
    if args.command == "pending":
        return cmd_pending(store)
    if args.command == "purge":
        return cmd_purge(store)
    if args.command == "privacy-scan":
        return cmd_privacy_scan(store, args.apply)
    if args.command == "mark":
        ok = store.mark_by_name(args.group_id, args.name, args.reason)
        print("已标记待删。" if ok else "标记失败：图库没有这张图。")
        return 0 if ok else 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
