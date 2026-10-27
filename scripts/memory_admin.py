"""查看 / 修改大肥鱼的记忆（SQLite）。

记忆分类：
- summaries    会话摘要（长期事实、未完成的事、关键原话）
- notes        会话长期笔记（记忆管家自动整理，也有手动来源）
- user_notes   用户画像（关于某个群友的长期事实）
- todos        待办 / 承诺
- image_memory 图片记忆（看过并理解过的图）
另：archive 是压缩前的原文归档，messages 是最近对话，不算长期记忆，只统计/导出。

用法示例：
    python scripts/memory_admin.py stats
    python scripts/memory_admin.py export
    python scripts/memory_admin.py summary list
    python scripts/memory_admin.py summary show group:100000001
    python scripts/memory_admin.py summary set group:100000001 --text "新的摘要内容"
    python scripts/memory_admin.py note list --session group:100000001 --limit 20
    python scripts/memory_admin.py note edit 674 --text "改成这句"
    python scripts/memory_admin.py note delete 674 --yes
    python scripts/memory_admin.py note add --session group:100000001 --text "新的记忆"
    python scripts/memory_admin.py note search --query "主人"
    python scripts/memory_admin.py image list
    python scripts/memory_admin.py image edit 1 --text "新的图片描述"
    python scripts/memory_admin.py backup

修改直接写 data/bot.db，Bot 下一轮读取记忆时就会用新内容，不需要重启。
删除/修改前建议先跑一次 backup。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from config import PROJECT_ROOT, load_settings  # noqa: E402

STALE_KEYWORDS = (
    "看不了图",
    "看不到图",
    "没腿",
    "没眼睛",
    "删不了图",
    "图库常抽风",
    "不会发图",
    "主人",
    "喵～",
)


def db_path() -> Path:
    settings = load_settings(PROJECT_ROOT / "config/config.yaml")
    path = Path(settings.chat.memory.db_path)
    return path if path.is_absolute() else PROJECT_ROOT / path


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    return conn


def fmt_time(value: float | None) -> str:
    if not value:
        return "-"
    try:
        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "-"


def count(conn: sqlite3.Connection, table: str) -> int:
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except sqlite3.Error:
        return 0


def stats(conn: sqlite3.Connection) -> None:
    print(f"数据库：{db_path()}")
    print(f"生成时间：{datetime.now():%Y-%m-%d %H:%M}\n")
    print("== 记忆分类 ==")
    print(f"会话摘要 summaries：{count(conn, 'summaries')} 条")
    print(f"长期笔记 notes：{count(conn, 'notes')} 条")
    print(f"用户画像 user_notes：{count(conn, 'user_notes')} 条")
    print(f"待办 todos：{count(conn, 'todos')} 条")
    print(f"图片记忆 image_memory：{count(conn, 'image_memory')} 条")
    print("\n== 非长期记忆（参考） ==")
    print(f"原文归档 archive：{count(conn, 'archive')} 条")
    print(f"最近消息 messages：{count(conn, 'messages')} 条")

    print("\n== 需要关注的内容 ==")
    for row in conn.execute(
        "SELECT session_id, content FROM summaries ORDER BY updated_at DESC"
    ):
        for line in str(row["content"]).splitlines():
            if any(marker in line for marker in ("固定设定", "3871324658", "作废")):
                continue
            hits = [word for word in STALE_KEYWORDS if word in line]
            if hits:
                print(f"- 摘要 {row['session_id']} 命中：{'、'.join(hits)}")
                break
    for row in conn.execute(
        "SELECT id, session_id, content, source FROM notes ORDER BY id DESC"
    ):
        if row["source"] == "手动" or "固定设定" in row["content"]:
            continue
        hits = [word for word in STALE_KEYWORDS if word in row["content"]]
        if hits:
            print(f"- 笔记 #{row['id']}（{row['session_id']}）命中：{'、'.join(hits)}")
    print("\n提示：先 `export` 看全量，再用 `note edit/delete`、`summary set` 修改；改前 `backup`。")


def export(conn: sqlite3.Connection, out: Path) -> None:
    lines: list[str] = []
    add = lines.append
    add("# 大肥鱼记忆清单")
    add("")
    add(f"- 数据库：`{db_path()}`")
    add(f"- 导出时间：{datetime.now():%Y-%m-%d %H:%M}")
    add("")
    add("## 分类统计")
    add("")
    add(f"| 分类 | 条数 | 说明 |")
    add(f"| --- | ---: | --- |")
    add(f"| 会话摘要 | {count(conn, 'summaries')} | 长期事实 / 未完成的事 / 关键原话 |")
    add(f"| 长期笔记 | {count(conn, 'notes')} | 记忆管家自动整理 + 手动来源 |")
    add(f"| 用户画像 | {count(conn, 'user_notes')} | 关于某个群友的长期事实 |")
    add(f"| 待办 | {count(conn, 'todos')} | 答应过、还没做完的事 |")
    add(f"| 图片记忆 | {count(conn, 'image_memory')} | 看过并理解过的图（文字描述） |")
    add(f"| 原文归档 | {count(conn, 'archive')} | 压缩前原文，可回溯，不算长期记忆 |")
    add(f"| 最近消息 | {count(conn, 'messages')} | 当前会话缓存，不算长期记忆 |")
    add("")

    add("## 一、会话摘要")
    add("")
    rows = list(conn.execute("SELECT * FROM summaries ORDER BY updated_at DESC"))
    if not rows:
        add("（暂无）")
    for row in rows:
        add(f"### {row['session_id']}")
        add(f"- 更新时间：{fmt_time(row['updated_at'])}")
        add("")
        add(str(row["content"]))
        add("")

    add("## 二、长期笔记")
    add("")
    rows = list(conn.execute(
        "SELECT * FROM notes ORDER BY session_id, id"
    ))
    if not rows:
        add("（暂无）")
    current = None
    for row in rows:
        session = str(row["session_id"])
        if session != current:
            current = session
            add(f"### {session}")
            add("")
        add(
            f"- #{row['id']}（{fmt_time(row['created_at'])}"
            f"{'，来源：' + str(row['source']) if row['source'] else ''}）"
            f"{str(row['content'])}"
        )
    add("")

    add("## 三、用户画像")
    add("")
    rows = list(conn.execute("SELECT * FROM user_notes ORDER BY user_id, id"))
    if not rows:
        add("（暂无）")
    for row in rows:
        add(
            f"- QQ {row['user_id']}（{fmt_time(row['created_at'])}"
            f"{'，来源：' + str(row['source']) if row['source'] else ''}）"
            f"{str(row['content'])}"
        )
    add("")

    add("## 四、待办 / 承诺")
    add("")
    rows = list(conn.execute("SELECT * FROM todos ORDER BY id"))
    if not rows:
        add("（暂无）")
    for row in rows:
        status = "已完成" if row["status"] == "done" else "未完成"
        add(f"- #{row['id']}（{status}，{fmt_time(row['created_at'])}）{row['content']}")
    add("")

    add("## 五、图片记忆")
    add("")
    rows = list(conn.execute("SELECT * FROM image_memory ORDER BY id DESC"))
    if not rows:
        add("（暂无）")
    for row in rows:
        add(f"### 图片 #{row['id']} | {row['name']}（QQ {row['user_id']}）")
        add(f"- 时间：{fmt_time(row['created_at'])}")
        add(f"- 会话：{row['session_id']}")
        add(f"- 描述：{row['description']}")
        add("")

    add("## 六、原文归档（只读参考）")
    add("")
    add(f"共 {count(conn, 'archive')} 条压缩前原文，存放在 `archive` 表；")
    add("需要逐条找回时用 `recall_chat` 或直接查库，本清单不展开。")
    add("")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已导出 Markdown：{out}（{len(lines)} 行）")

    json_path = out.with_suffix(".json")
    data = {
        "db": str(db_path()),
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "summaries": [dict(row) for row in conn.execute("SELECT * FROM summaries")],
        "notes": [dict(row) for row in conn.execute("SELECT * FROM notes ORDER BY id")],
        "user_notes": [dict(row) for row in conn.execute("SELECT * FROM user_notes")],
        "todos": [dict(row) for row in conn.execute("SELECT * FROM todos")],
        "image_memory": [dict(row) for row in conn.execute("SELECT * FROM image_memory")],
    }
    json_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"已导出 JSON：{json_path}")


def backup(conn: sqlite3.Connection) -> None:
    folder = PROJECT_ROOT / "data" / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"bot-{datetime.now():%Y%m%d-%H%M%S}.db"
    source = db_path()
    try:
        conn.execute("PRAGMA wal_checkpoint(FULL)")
    except sqlite3.Error:
        pass
    shutil.copy2(source, target)
    print(f"已备份数据库：{target}")


def summary_cmd(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    if args.action == "list":
        rows = list(conn.execute("SELECT session_id, content, updated_at FROM summaries ORDER BY updated_at DESC"))
        for row in rows:
            text = " ".join(str(row["content"]).split())
            print(f"{row['session_id']}（{fmt_time(row['updated_at'])}，{len(row['content'])} 字）：{text[:100]}")
    elif args.action == "show":
        row = conn.execute("SELECT * FROM summaries WHERE session_id = ?", (args.session,)).fetchone()
        if not row:
            print("没有这个会话的摘要")
            return
        print(f"会话：{row['session_id']}（更新于 {fmt_time(row['updated_at'])}）\n")
        print(row["content"])
    elif args.action == "set":
        text = Path(args.file).read_text(encoding="utf-8") if args.file else args.text
        if not text:
            print("需要 --text 或 --file")
            return
        conn.execute(
            "INSERT INTO summaries(session_id, content, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET content = excluded.content, "
            "updated_at = excluded.updated_at",
            (args.session, text, datetime.now().timestamp()),
        )
        conn.commit()
        print(f"已更新摘要：{args.session}（{len(text)} 字）")
    elif args.action == "delete":
        if not args.yes:
            print("删除摘要需要加 --yes")
            return
        conn.execute("DELETE FROM summaries WHERE session_id = ?", (args.session,))
        conn.commit()
        print(f"已删除摘要：{args.session}")


def note_cmd(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    if args.action == "list":
        sql = "SELECT * FROM notes WHERE 1=1"
        params: list[object] = []
        if args.session:
            sql += " AND session_id = ?"
            params.append(args.session)
        if args.source:
            sql += " AND source = ?"
            params.append(args.source)
        sql += " ORDER BY id DESC"
        if args.limit:
            sql += " LIMIT ?"
            params.append(args.limit)
        for row in conn.execute(sql, params):
            print(
                f"#{row['id']} [{row['session_id']}] {fmt_time(row['created_at'])}"
                f"{' [' + row['source'] + ']' if row['source'] else ''}：{row['content']}"
            )
    elif args.action == "show":
        row = conn.execute("SELECT * FROM notes WHERE id = ?", (args.id,)).fetchone()
        if not row:
            print("没有这条笔记")
            return
        print(f"#{row['id']} | {row['session_id']} | {fmt_time(row['created_at'])} | {row['source']}\n{row['content']}")
    elif args.action == "edit":
        if not args.text:
            print("需要 --text")
            return
        cur = conn.execute("UPDATE notes SET content = ? WHERE id = ?", (args.text, args.id))
        conn.commit()
        print(f"已修改笔记 #{args.id}（{cur.rowcount} 行）")
    elif args.action == "delete":
        if not args.yes:
            print("删除笔记需要加 --yes")
            return
        cur = conn.execute("DELETE FROM notes WHERE id = ?", (args.id,))
        conn.commit()
        print(f"已删除笔记 #{args.id}（{cur.rowcount} 行）")
    elif args.action == "add":
        if not args.session or not args.text:
            print("需要 --session 和 --text")
            return
        cur = conn.execute(
            "INSERT INTO notes(session_id, content, created_at, source) VALUES(?,?,?,?)",
            (args.session, args.text, datetime.now().timestamp(), args.source or "手动"),
        )
        conn.commit()
        print(f"已新增笔记 #{cur.lastrowid}")
    elif args.action == "search":
        if not args.query:
            print("需要 --query")
            return
        pattern = f"%{args.query}%"
        for row in conn.execute(
            "SELECT * FROM notes WHERE content LIKE ? ORDER BY id DESC LIMIT 50", (pattern,)
        ):
            print(f"#{row['id']} [{row['session_id']}] {fmt_time(row['created_at'])}：{row['content']}")


def image_cmd(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    if args.action == "list":
        for row in conn.execute("SELECT * FROM image_memory ORDER BY id DESC"):
            print(f"#{row['id']} [{row['session_id']}] {row['name']}（QQ {row['user_id']}）{fmt_time(row['created_at'])}")
            print(f"  {row['description']}")
    elif args.action == "edit":
        if not args.text:
            print("需要 --text")
            return
        cur = conn.execute(
            "UPDATE image_memory SET description = ? WHERE id = ?", (args.text, args.id)
        )
        conn.commit()
        print(f"已修改图片记忆 #{args.id}（{cur.rowcount} 行）")
    elif args.action == "delete":
        if not args.yes:
            print("删除图片记忆需要加 --yes")
            return
        cur = conn.execute("DELETE FROM image_memory WHERE id = ?", (args.id,))
        conn.commit()
        print(f"已删除图片记忆 #{args.id}（{cur.rowcount} 行）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="大肥鱼记忆查看/修改工具")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("stats", help="统计各分类条数并检测可疑旧记忆")

    p_export = sub.add_parser("export", help="导出完整记忆清单（Markdown + JSON）")
    p_export.add_argument("--out", default=str(PROJECT_ROOT / "data" / "记忆清单.md"))

    sub.add_parser("backup", help="备份数据库到 data/backups/")

    p_sum = sub.add_parser("summary", help="会话摘要")
    sum_sub = p_sum.add_subparsers(dest="action", required=True)
    sum_sub.add_parser("list")
    p_show = sum_sub.add_parser("show")
    p_show.add_argument("session")
    p_set = sum_sub.add_parser("set")
    p_set.add_argument("session")
    p_set.add_argument("--text")
    p_set.add_argument("--file")
    p_del = sum_sub.add_parser("delete")
    p_del.add_argument("session")
    p_del.add_argument("--yes", action="store_true")

    p_note = sub.add_parser("note", help="长期笔记")
    note_sub = p_note.add_subparsers(dest="action", required=True)
    p_nl = note_sub.add_parser("list")
    p_nl.add_argument("--session")
    p_nl.add_argument("--source")
    p_nl.add_argument("--limit", type=int, default=30)
    p_ns = note_sub.add_parser("show")
    p_ns.add_argument("id", type=int)
    p_ne = note_sub.add_parser("edit")
    p_ne.add_argument("id", type=int)
    p_ne.add_argument("--text", required=True)
    p_nd = note_sub.add_parser("delete")
    p_nd.add_argument("id", type=int)
    p_nd.add_argument("--yes", action="store_true")
    p_na = note_sub.add_parser("add")
    p_na.add_argument("--session", required=True)
    p_na.add_argument("--text", required=True)
    p_na.add_argument("--source")
    p_nq = note_sub.add_parser("search")
    p_nq.add_argument("--query", required=True)

    p_img = sub.add_parser("image", help="图片记忆")
    img_sub = p_img.add_subparsers(dest="action", required=True)
    img_sub.add_parser("list")
    p_ie = img_sub.add_parser("edit")
    p_ie.add_argument("id", type=int)
    p_ie.add_argument("--text", required=True)
    p_id = img_sub.add_parser("delete")
    p_id.add_argument("id", type=int)
    p_id.add_argument("--yes", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    conn = connect()
    try:
        if args.command == "stats":
            stats(conn)
        elif args.command == "export":
            export(conn, Path(args.out))
        elif args.command == "backup":
            backup(conn)
        elif args.command == "summary":
            summary_cmd(conn, args)
        elif args.command == "note":
            note_cmd(conn, args)
        elif args.command == "image":
            image_cmd(conn, args)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
