"""把成绩单渲染成一张可以发到群里的图片（Pillow 绘制，无外部素材）。

输入就是 `ChatService.dashboard_data()` 的结果，输出 PNG bytes。
"""
from __future__ import annotations

import io
import time
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 860, 1120

BG_TOP = (12, 34, 62)
BG_BOTTOM = (4, 10, 22)
CARD = (15, 39, 67)
CARD_EDGE = (32, 72, 110)
TEXT = (222, 236, 250)
MUTED = (127, 154, 184)
ACCENT = (155, 227, 255)
ACCENT2 = (111, 182, 232)
PINK = (255, 148, 170)

_FONT_CANDIDATES = (
    (r"C:\Windows\Fonts\msyhbd.ttc", 0),
    (r"C:\Windows\Fonts\msyh.ttc", 0),
    (r"C:\Windows\Fonts\simhei.ttf", 0),
    (r"C:\Windows\Fonts\simsun.ttc", 0),
    (r"C:\Windows\Fonts\Deng.ttf", 0),
)


@lru_cache(maxsize=64)
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = (
        [c for c in _FONT_CANDIDATES if "bd" in c[0].lower()]
        + [c for c in _FONT_CANDIDATES if "bd" not in c[0].lower()]
        if bold
        else list(_FONT_CANDIDATES)
    )
    for path, index in candidates:
        try:
            if Path(path).exists():
                return ImageFont.truetype(path, size, index=index)
        except OSError:
            continue
    return ImageFont.load_default()


def _text(draw: ImageDraw.ImageDraw, x: float, y: float, body: str,
          size: int = 18, *, bold: bool = False, fill=TEXT) -> None:
    draw.text((x, y), str(body), font=_font(size, bold), fill=fill)


def _text_len(draw: ImageDraw.ImageDraw, body: str, size: int, bold: bool = False) -> float:
    return draw.textlength(str(body), font=_font(size, bold))


def _clip(draw: ImageDraw.ImageDraw, body: str, size: int, max_width: float,
          bold: bool = False) -> str:
    body = str(body)
    if _text_len(draw, body, size, bold) <= max_width:
        return body
    while body and _text_len(draw, body + "…", size, bold) > max_width:
        body = body[:-1]
    return body + "…"


def _card(draw: ImageDraw.ImageDraw, box, radius: int = 14) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=CARD, outline=CARD_EDGE, width=1)


def _bar(draw: ImageDraw.ImageDraw, x: float, y: float, width: float, height: float,
         ratio: float, fill=ACCENT) -> None:
    ratio = max(0.0, min(1.0, ratio))
    radius = int(height // 2)
    draw.rounded_rectangle(
        [x, y, x + width, y + height], radius=radius, fill=(18, 40, 63)
    )
    if ratio > 0:
        draw.rounded_rectangle(
            [x, y, x + max(height, width * ratio), y + height],
            radius=radius,
            fill=fill,
        )


def _fmt_uptime(seconds: float) -> str:
    seconds = int(max(0, seconds))
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    return f"{hours}h{minutes}m" if hours else f"{minutes}m"


def _group_label(gid: object) -> str:
    text = str(gid)
    if text.startswith("group:"):
        text = text.split(":", 1)[1]
    return text[-10:] if len(text) > 10 else text


def render_card(data: dict) -> bytes:
    """渲染成绩单 PNG；data 结构与 `/api/dashboard` 一致。"""
    status = data.get("status") or {}
    stats = status.get("stats") or {}
    memory = data.get("memory") or {}
    gallery = data.get("gallery") or {}
    affinity = data.get("affinity") or {}
    mood_states = (status.get("mood") or {}).get("states") or {}
    budget = (status.get("budget") or {}).get("today") or {}
    limit = (status.get("budget") or {}).get("daily_cny_per_group") or 0
    words = data.get("word_cloud") or []
    pending = (data.get("reminders") or {}).get("pending", 0)

    h_max = 1500
    image = Image.new("RGB", (W, h_max), BG_TOP)
    draw = ImageDraw.Draw(image)
    for y in range(h_max):
        t = y / h_max
        draw.line(
            [(0, y), (W, y)],
            fill=tuple(
                int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3)
            ),
        )

    # ---- 标题 ----
    _text(draw, 40, 34, "蓝色大肥鱼 · 成绩单", 34, bold=True, fill=ACCENT)
    now = time.localtime()
    _text(
        draw,
        42,
        82,
        f"{time.strftime('%Y-%m-%d %H:%M', now)} · 运行 {_fmt_uptime(status.get('uptime_seconds', 0))} · "
        f"模型 {status.get('model', '-')}",
        15,
        fill=MUTED,
    )
    draw.line([(40, 118), (W - 40, 118)], fill=CARD_EDGE, width=1)

    # ---- 四个数字块 ----
    blocks = [
        ("收到消息", stats.get("received", 0)),
        ("回复", stats.get("replied", 0)),
        ("戳戳回复", stats.get("pokes", 0)),
        ("出错", stats.get("errors", 0)),
    ]
    block_w, block_h, gap = 182, 104, 16
    left = (W - (block_w * 4 + gap * 3)) / 2
    for index, (label, value) in enumerate(blocks):
        x = left + index * (block_w + gap)
        _card(draw, [x, 136, x + block_w, 136 + block_h])
        _text(draw, x + 16, 150, label, 15, fill=MUTED)
        _text(draw, x + 16, 172, str(value), 40, bold=True, fill=TEXT)

    # ---- 心情 ----
    y = 264
    _text(draw, 40, y, "心情", 20, bold=True, fill=ACCENT)
    y += 34
    mood_items = list(mood_states.items())[:3]
    if not mood_items:
        _text(draw, 44, y, "还没有心情记录", 16, fill=MUTED)
        y += 30
    for key, state in mood_items:
        energy = float(state.get("energy") or 0)
        label = str(state.get("label") or "")
        _text(draw, 44, y - 3, f"群 {_group_label(key)}", 14, fill=MUTED)
        _bar(draw, 210, y + 2, 380, 14, energy / 100.0)
        _text(draw, 604, y - 3, f"{int(energy)} {label}", 14, fill=TEXT)
        y += 30

    # ---- 亲密度 ----
    y += 14
    _text(draw, 40, y, "亲密度", 20, bold=True, fill=ACCENT)
    y += 34
    any_affinity = False
    for gid, items in list(affinity.items())[:2]:
        _text(draw, 44, y - 3, f"群 {_group_label(gid)}", 14, fill=MUTED)
        parts = [
            f"{item.get('name', '?')} {item.get('score', 0)}（{item.get('level', '')}）"
            for item in items[:3]
        ]
        _text(draw, 210, y - 3, _clip(draw, "｜".join(parts), 14, 600), 14, fill=TEXT)
        y += 28
        any_affinity = True
    if not any_affinity:
        _text(draw, 44, y, "还没有互动记录", 16, fill=MUTED)
        y += 28

    # ---- 预算 ----
    y += 20
    _text(draw, 40, y, "今日预算", 20, bold=True, fill=ACCENT)
    y += 34
    if budget:
        parts = [
            f"群 {_group_label(gid)} {cny}/{limit} 元"
            for gid, cny in list(budget.items())[:3]
        ]
        _text(draw, 44, y, _clip(draw, "｜".join(parts), 15, W - 100), 15, fill=TEXT)
    else:
        _text(draw, 44, y, "今天还没花钱", 15, fill=MUTED)

    # ---- 记忆 / 图库 ----
    y += 26
    _text(draw, 40, y, "记忆与图库", 20, bold=True, fill=ACCENT)
    y += 34
    _text(
        draw,
        44,
        y,
        f"消息 {memory.get('messages', 0)} · 归档 {memory.get('archive', 0)} · "
        f"笔记 {memory.get('notes', 0)}（画像 {memory.get('portraits', 0)}）· "
        f"待办 {memory.get('todos', 0)}",
        15,
        fill=TEXT,
    )
    y += 26
    _text(
        draw,
        44,
        y,
        f"表情包 {gallery.get('memes', 0)} 张（{gallery.get('bytes_mb', 0)} MB）· "
        f"自画像 {gallery.get('portraits', 0)} 张 · 未完成提醒 {pending}",
        15,
        fill=TEXT,
    )

    # ---- 近 7 天活跃 ----
    activity = data.get("activity_days") or []
    y += 30
    _text(draw, 40, y, "近 7 天活跃", 20, bold=True, fill=ACCENT)
    y += 38
    if activity:
        counts = [int(item.get("count") or 0) for item in activity]
        peak = max(counts) if counts else 0
        chart_h = 64
        base_y = y + chart_h
        bar_w = (W - 120) / max(1, len(counts))
        for index, count in enumerate(counts):
            height = 3 if peak <= 0 else max(3, int((count / peak) * chart_h))
            xx = 60 + index * bar_w
            color = ACCENT if index == len(counts) - 1 else ACCENT2
            draw.rounded_rectangle(
                [xx + 5, base_y - height, xx + bar_w - 9, base_y],
                radius=4,
                fill=color,
            )
            _text(draw, xx + 5, base_y + 6, str(count), 12, fill=MUTED)
        _text(draw, 44, y - 4, f"共 {sum(counts)} 条", 14, fill=MUTED)
        y = base_y + 36
    else:
        _text(draw, 44, y, "还没有活跃数据", 15, fill=MUTED)
        y += 30

    # ---- 热点词 ----
    y += 22
    _text(draw, 40, y, "本群热点词（近 7 天）", 20, bold=True, fill=ACCENT)
    y += 38
    line_y = y
    if words:
        counts = [max(1, int(item.get("count") or 1)) for item in words]
        peak = max(counts)
        x = 44
        palette = [ACCENT, ACCENT2, (168, 216, 255), (139, 197, 245), (200, 233, 255)]
        for index, item in enumerate(words[:14]):
            word = str(item.get("word") or "")
            count = max(1, int(item.get("count") or 1))
            size = 15 + round((count / peak) * 20)
            width = _text_len(draw, word, size, True) + 18
            if x + width > W - 44:
                x = 44
                line_y += 42
            _text(
                draw, x, line_y, word, size, bold=True,
                fill=palette[index % len(palette)],
            )
            x += width
            if line_y > y + 84:
                break
    else:
        _text(draw, 44, y, "最近群里话太少，没攒出词", 16, fill=MUTED)
    y = line_y + 80

    # ---- 自适应高度 + 页脚 ----
    final_h = max(820, min(h_max, int(y) + 130))
    image = image.crop((0, 0, W, final_h))
    draw = ImageDraw.Draw(image)
    draw.line([(40, final_h - 62), (W - 40, final_h - 62)], fill=CARD_EDGE, width=1)
    _text(
        draw,
        40,
        final_h - 48,
        "由大肥鱼自己生成 · 数据快照 · /dashboard 可看详细版",
        13,
        fill=MUTED,
    )

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()

def _wrap(
    draw: ImageDraw.ImageDraw, text: str, size: int, max_width: float,
    bold: bool = False,
) -> list[str]:
    rows: list[str] = []
    for paragraph in str(text).splitlines() or [""]:
        current = ""
        for char in paragraph:
            if current and _text_len(draw, current + char, size, bold) > max_width:
                rows.append(current)
                current = char
            else:
                current += char
        rows.append(current)
    return rows


def render_report_card(
    lines: list[str],
    *,
    stats: dict | None = None,
    now: float | None = None,
    activity: list[dict] | None = None,
    word_cloud: list[dict] | None = None,
    care_note: str = "",
) -> bytes:
    """群报图片：标题 + 今日看点 + 活跃小图 + 今日热词 + 关心小注 + 数据脚注。"""
    items = [str(line).strip() for line in lines if str(line).strip()][:5]
    if not items:
        items = ["今天群里没什么特别的。"]
    moment = time.localtime(time.time() if now is None else now)
    h_max = 1400
    image = Image.new("RGB", (W, h_max), BG_TOP)
    draw = ImageDraw.Draw(image)
    for row in range(h_max):
        ratio = row / h_max
        draw.line(
            [(0, row), (W, row)],
            fill=tuple(
                int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * ratio) for i in range(3)
            ),
        )

    _text(draw, 40, 42, "今日群报", 36, bold=True, fill=ACCENT)
    _text(
        draw,
        42,
        94,
        f"{time.strftime('%Y-%m-%d %H:%M', moment)} · 蓝色大肥鱼整理",
        15,
        fill=MUTED,
    )
    draw.line([(40, 130), (W - 40, 130)], fill=CARD_EDGE, width=1)

    y = 168
    for index, item in enumerate(items):
        dot_color = ACCENT if index == 0 else ACCENT2
        draw.ellipse([46, y + 11, 56, y + 21], fill=dot_color)
        for row in _wrap(draw, item, 19, W - 150):
            _text(draw, 76, y, row, 19, fill=TEXT)
            y += 33
        y += 24
    y += 8

    activity_counts = [
        int(item.get("count") or 0) for item in (activity or [])
    ]
    if activity_counts:
        y += 18
        _text(draw, 40, y, "近 7 天活跃", 14, fill=MUTED)
        y += 24
        peak = max(activity_counts) or 1
        chart_h = 34
        bar_w = (W - 120) / max(1, len(activity_counts))
        for index, count in enumerate(activity_counts):
            height = max(2, int((count / peak) * chart_h))
            xx = 48 + index * bar_w
            draw.rounded_rectangle(
                [xx + 4, y + chart_h - height, xx + bar_w - 8, y + chart_h],
                radius=3,
                fill=ACCENT if index == len(activity_counts) - 1 else ACCENT2,
            )
        y += chart_h + 8

    cloud_items = [
        item for item in (word_cloud or []) if str(item.get("word") or "").strip()
    ][:10]
    if cloud_items:
        y += 14
        _text(draw, 40, y, "今日热词", 14, fill=MUTED)
        y += 24
        peak = max(int(item.get("count") or 1) for item in cloud_items) or 1
        x = 48
        for item in cloud_items:
            word = str(item.get("word") or "")
            size = 13 + round((int(item.get("count") or 1) / peak) * 9)
            width = _text_len(draw, word, size, True) + 16
            if x + width > W - 44:
                x = 48
                y += 28
            _text(draw, x, y, word, size, bold=True, fill=ACCENT2)
            x += width

    if care_note:
        y += 12
        _text(draw, 40, y, str(care_note)[:80], 13, fill=PINK)
        y += 22

    total = int((stats or {}).get("messages") or 0)
    foot = f"今日消息 {total} 条 · 由大肥鱼自己生成"
    final_h = max(480, min(h_max, y + 100))
    image = image.crop((0, 0, W, final_h))
    draw = ImageDraw.Draw(image)
    draw.line([(40, final_h - 70), (W - 40, final_h - 70)], fill=CARD_EDGE, width=1)
    _text(draw, 40, final_h - 56, foot, 13, fill=MUTED)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()

def render_weekly_card(
    lines: list[str],
    *,
    stats: dict | None = None,
    activity: list[dict] | None = None,
    word_cloud: list[dict] | None = None,
    affinity: list[dict] | None = None,
    week_range: str = "",
    now: float | None = None,
) -> bytes:
    """周报图片：本周数据 + 活跃柱状 + 热词 + 最熟的人 + 本周看点。"""
    items = [str(line).strip() for line in lines if str(line).strip()][:4]
    if not items:
        items = ["这一周群里没什么特别的。"]
    moment = time.localtime(time.time() if now is None else now)
    h_max = 1600
    image = Image.new("RGB", (W, h_max), BG_TOP)
    draw = ImageDraw.Draw(image)
    for row in range(h_max):
        ratio = row / h_max
        draw.line(
            [(0, row), (W, row)],
            fill=tuple(
                int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * ratio) for i in range(3)
            ),
        )

    _text(draw, 40, 42, "本周群报", 36, bold=True, fill=ACCENT)
    _text(
        draw,
        42,
        94,
        f"{week_range or '最近 7 天'} · 蓝色大肥鱼整理",
        15,
        fill=MUTED,
    )
    draw.line([(40, 130), (W - 40, 130)], fill=CARD_EDGE, width=1)

    y = 156
    total = int((stats or {}).get("messages") or 0)
    active_days = int((stats or {}).get("active_days") or 0)
    cares = int((stats or {}).get("care") or 0)
    _text(
        draw,
        42,
        y,
        f"消息 {total} 条 · 活跃 {active_days} 天 · 关心 {cares} 人次",
        16,
        fill=TEXT,
    )
    y += 44

    if activity:
        counts = [int(item.get("count") or 0) for item in activity]
        peak = max(counts) if counts else 0
        chart_h = 62
        base_y = y + chart_h
        bar_w = (W - 100) / max(1, len(counts))
        for index, item in enumerate(activity):
            count = int(item.get("count") or 0)
            height = 3 if peak <= 0 else max(3, int((count / peak) * chart_h))
            xx = 50 + index * bar_w
            color = ACCENT if index == len(activity) - 1 else ACCENT2
            draw.rounded_rectangle(
                [xx + 5, base_y - height, xx + bar_w - 9, base_y],
                radius=4,
                fill=color,
            )
            _text(draw, xx + 5, base_y + 6, str(item.get("day", ""))[-5:], 11, fill=MUTED)
        y = base_y + 34

    if word_cloud:
        y += 10
        _text(draw, 42, y, "本周热词", 14, fill=MUTED)
        y += 24
        peak = max(int(item.get("count") or 1) for item in word_cloud) or 1
        x = 50
        for item in word_cloud[:8]:
            word = str(item.get("word") or "")
            size = 13 + round((int(item.get("count") or 1) / peak) * 9)
            width = _text_len(draw, word, size, True) + 16
            if x + width > W - 44:
                x = 50
                y += 28
            _text(draw, x, y, word, size, bold=True, fill=ACCENT2)
            x += width
        y += 30

    if affinity:
        y += 8
        _text(draw, 42, y, "本周最熟", 14, fill=MUTED)
        y += 24
        parts = [
            f"{item.get('name')} {item.get('score')}（{item.get('level')}）"
            for item in affinity[:3]
        ]
        _text(draw, 50, y, _clip(draw, "｜".join(parts), 14, W - 100), 14, fill=TEXT)
        y += 30

    y += 12
    _text(draw, 42, y, "本周看点", 17, bold=True, fill=ACCENT)
    y += 34
    for index, item in enumerate(items):
        draw.ellipse([48, y + 9, 56, y + 17], fill=ACCENT if index == 0 else ACCENT2)
        for row in _wrap(draw, item, 17, W - 150):
            _text(draw, 74, y, row, 17, fill=TEXT)
            y += 30
        y += 18
    y += 6

    final_h = max(560, min(h_max, y + 110))
    image = image.crop((0, 0, W, final_h))
    draw = ImageDraw.Draw(image)
    draw.line([(40, final_h - 70), (W - 40, final_h - 70)], fill=CARD_EDGE, width=1)
    _text(
        draw,
        40,
        final_h - 56,
        "由大肥鱼自己生成 · 每周一张 · /card 可看成绩单",
        13,
        fill=MUTED,
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
