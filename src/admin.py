"""管理员命令。

只有 config.yaml -> chat.admin.qq 里的 QQ 号能用。
在群里发命令建议先 @机器人，命令前面也可以带 @ 标签（适配层会剥掉）。
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any

from budget import BudgetStore
from config import Settings
from memory import GroupHistory, MemoryStore
from onebot import Incoming
from prompt import render_dated_notes

HELP_TEXT = """蓝色大肥鱼可用命令：
【谁都能用】
/mood 看看她现在心情
/card 或 /成绩单 让她发一张成绩单图片
/doctor 让她自检（连接/记忆/图库/预算/心情）
/remind 明天8点 交报告 让她到点提醒你
/reminders 看这个群还没完成的提醒
/forget 3 取消 #3 号提醒
/report 立刻生成一次今日群报
/affinity 看你和她的亲密度
/penna 雨夜霓虹街头的白发少女 生成文生图提示词（正向/负向/中文说明）
/search 今天上海天气 联网搜索并打开来源页总结
/neko 摸鱼 发一张大肥鱼自己的表情包（也可写情绪词）

【管理员】
/help 查看帮助
/status 查看运行状态
/notes 查看我记住的事（带日期）
/todos 查看还没做完的事
/reset 清空当前会话记忆（含会话笔记与待办）
/重置额度 [群号|全部] 重置今天聊天额度（群里默认只重置本群）"""


# 主人也可以用大白话重置额度，不必记命令。
# 判定条件：同时出现「额度/预算/花费」和动作词，且不是疑问/否定句，
# 免得把「额度怎么算」「别重置」当成命令。
BUDGET_RESET_COMMANDS = {
    "/resetbudget",
    "/reset_budget",
    "/重置额度",
    "/额度重置",
    "/清空额度",
    "/清除额度",
}
_BUDGET_NOUNS = ("额度", "预算", "花费", "消费")
_BUDGET_ACTIONS = ("重置", "清空", "清零", "刷新", "恢复", "重开")
_BUDGET_NEGATIONS = (
    "不用",
    "不要",
    "别重置",
    "别清",
    "不准",
    "不能",
    "没必要",
    "不需要",
    "恢复没",
    "怎么",
    "为何",
    "如何",
    "为什么",
    "什么",
    "啥",
    "吗",
    "呢",
    "？",
    "?",
)
_BUDGET_ALL_WORDS = ("全部", "所有群", "所有", "全群", "每个群")
_GROUP_ID_RE = re.compile(r"(?<!\d)(\d{6,12})(?!\d)")


def _parse_budget_reset(
    text: str, *, natural: bool
) -> tuple[str, int | None] | None:
    """识别「重置额度」请求，返回 (范围, 群号)。

    范围是 "auto"（群里=本群、私聊=全部）/"group"（指定群号）/"all"（全部群）。
    不像重置请求时返回 None。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    if natural:
        if raw.startswith("/"):
            return None
        compact = re.sub(r"\s+", "", raw)
        if any(word in compact for word in _BUDGET_NEGATIONS):
            return None
        if not any(word in compact for word in _BUDGET_NOUNS):
            return None
        if not any(word in compact for word in _BUDGET_ACTIONS):
            return None
        scope_text = compact
    else:
        if not raw.startswith("/"):
            return None
        parts = raw.split()
        if parts[0].lower() not in BUDGET_RESET_COMMANDS:
            return None
        scope_text = "".join(parts[1:])
    if any(word in scope_text for word in _BUDGET_ALL_WORDS):
        return ("all", None)
    match = _GROUP_ID_RE.search(scope_text)
    if match:
        return ("group", int(match.group(1)))
    return ("auto", None)


def handle_budget_reset(
    incoming: Incoming,
    settings: Settings,
    budget: BudgetStore | None,
    *,
    natural: bool = False,
) -> str | None:
    """主人让重置今天的聊天额度。

    - 命令式（`/重置额度`）和自然语言（「把额度重置一下」）都认；
    - 群里说默认只重置本群，私聊说默认重置全部群；
    - 带群号只重置那个群，带「全部/所有群」重置全部群。

    不是主人、或不像重置请求时返回 None，消息仍走普通聊天流程。
    """
    if incoming.user_id not in settings.chat.admin.qq:
        return None
    parsed = _parse_budget_reset(incoming.text, natural=natural)
    if parsed is None:
        return None
    scope, group_id = parsed
    if scope == "auto":
        if incoming.kind == "group" and incoming.group_id is not None:
            scope, group_id = "group", int(incoming.group_id)
        else:
            scope = "all"

    if budget is None:
        return "预算功能没开（chat.budget.enabled=false），今天不用重置额度。"

    if scope == "all":
        before = budget.reset_all_today()
        if not before:
            return "今天还没有群产生花费，不用重置额度。"
        total = sum(before.values())
        ranked = sorted(before.items(), key=lambda item: -item[1])
        detail = "、".join(f"{gid}（{amount:.4f} 元）" for gid, amount in ranked[:5])
        more = f" 等 {len(before)} 个群" if len(before) > 5 else ""
        return (
            f"已重置今天全部群额度：{len(before)} 个群共清掉 {total:.4f} 元。\n"
            f"- {detail}{more}"
        )

    assert group_id is not None
    limit = budget.limit_for(group_id)
    before = budget.today_cny(group_id)
    if before <= 0:
        return f"群 {group_id} 今天还没花钱（额度 {limit:.2f} 元），不用重置。"
    budget.reset_today(group_id)
    tag = (
        "本群"
        if incoming.kind == "group" and incoming.group_id == group_id
        else f"群 {group_id}"
    )
    return (
        f"已重置{tag}今天的额度：清掉 {before:.4f} 元，"
        f"额度 {limit:.2f} 元重新满格，继续聊吧。"
    )


async def handle_command(
    incoming: Incoming,
    settings: Settings,
    memory: MemoryStore,
    history: GroupHistory,
    stats: dict[str, Any],
    *,
    usage: dict[str, Any] | None = None,
) -> str | None:
    text = incoming.text.strip()
    if not text.startswith("/"):
        return None

    admins = settings.chat.admin.qq
    if incoming.user_id not in admins:
        return None  # 非管理员：当普通聊天处理

    command = text.split()[0].lower()
    if command in {"/help", "/帮助", "/?"}:
        return HELP_TEXT

    if command in {"/status", "/状态"}:
        uptime = int(time.time() - float(stats.get("started") or time.time()))
        hours, remainder = divmod(uptime, 3600)
        minutes = remainder // 60
        session_count = await memory.count_messages(incoming.session_id)
        archive_count = await memory.archive_count(incoming.session_id)
        usage_line = ""
        if usage:
            usage_line = (
                f"- 今日模型调用：{usage.get('calls', 0)} 次，"
                f"token {usage.get('total_tokens', 0):,}，"
                f"平均 {usage.get('avg_duration_ms', 0)} ms\n"
            )
        return (
            "运行状态\n"
            f"- 模型：{settings.deepseek.model}\n"
            f"- 已运行：{hours} 小时 {minutes} 分\n"
            f"{usage_line}"
            f"- 收到消息：{stats.get('received', 0)}\n"
            f"- 已回复：{stats.get('replied', 0)}\n"
            f"- 主动发言：{stats.get('proactive', 0)}\n"
            f"- 跳过：{stats.get('skipped', 0)}\n"
            f"- 出错：{stats.get('errors', 0)}\n"
            f"- 当前会话记忆：{session_count} 条（原文归档 {archive_count} 条）"
        )

    if command in {"/notes", "/记忆"}:
        now = datetime.now()
        session_entries = await memory.recent_note_entries(incoming.session_id, 10)
        user_entries = await memory.recent_user_note_entries(incoming.user_id, 10)
        lines = ["我记住的事："]
        if session_entries:
            lines.append("【本会话】")
            lines.extend(render_dated_notes(session_entries, now=now))
        if user_entries:
            lines.append("【关于你】")
            lines.extend(render_dated_notes(user_entries, now=now))
        if len(lines) == 1:
            return "目前还没记什么。"
        return "\n".join(lines)

    if command in {"/todos", "/待办"}:
        todos = await memory.list_todos(incoming.session_id, 15)
        if not todos:
            return "没有未完成的待办。"
        lines = ["还没做完的事："]
        for todo in todos:
            stamp = datetime.fromtimestamp(float(todo["created_at"])).strftime("%m-%d")
            lines.append(f"- #{todo['id']} {todo['content']}（{stamp} 记的）")
        return "\n".join(lines)

    if command in {"/reset", "/重置记忆"}:
        await memory.reset_session(incoming.session_id)
        if incoming.kind == "group" and incoming.group_id is not None:
            history.clear(incoming.group_id)
        return "记忆已清空，咱们重新认识一下。"
    return None
