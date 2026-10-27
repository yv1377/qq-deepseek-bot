"""搜索词清洗：去口语命令词、补相对日期、做基本相关性检查。"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

_RELATIVE_DAYS = (("大后天", 3), ("后天", 2), ("明天", 1), ("今天", 0), ("昨天", -1))
_SYNONYMS = (
    (r"赛事安排|比赛安排|赛事日程|比赛日程|赛事时间", "赛程"),
    (r"比赛结果|比分情况", "比分"),
    (r"最新消息|最新情况", "最新"),
)
_SLANG = {
    "🍬": "唐 梗 意思 出处",
    "唐": "唐 梗 意思 出处",
    "fvv": "fvv 梗 意思",
    "fw": "fw 梗 意思",
    "yyds": "yyds 梗 意思",
    "xswl": "xswl 梗 意思",
}
_FILLERS = re.compile(
    r"(?:帮我搜一下|帮我查一下|帮我看看|帮我搜|帮我查|帮忙搜|帮忙查|"
    r"搜一下|搜一搜|查一下|查一查|搜索一下|搜索|网上搜|去网上|上网|"
    r"告诉我|给我|看看|看一下|找一下|找找|关于|有关|"
    r"你现在|现在|去搜集|搜集|收集|他们有什么意见|有什么意见|意见|"
    r"请问|请|麻烦|能不能|可不可以|可以|帮我|帮忙|一下|"
    r"怎么样|怎么|什么|的|了|吗|呢|啊|呀|吧|哦|嘛|呗|啦)"
)
_TRAIL = re.compile(r"[?？!！。，,、：:；;~～…]+")


def normalize_search_query(text: str, now: datetime | None = None) -> str:
    """把“帮我搜一下明天的……赛事安排”清洗成可搜索的关键词。"""
    original = " ".join(str(text or "").split())
    original = re.sub(r"^(?:@\S+\s*)+", "", original)
    original = re.sub(
        r"^(?:蓝色大肥鱼|大肥鱼|肥鱼|小鱼|鲸鱼娘|/search|/搜索|/查一下|/penna)[，,：:\s]*",
        "",
        original,
        flags=re.I,
    )
    text = original
    # 相对日期不要塞进搜索词：搜索引擎对“2026-10-27”反而容易跑偏。
    # 日期只作为总结时的上下文，由 relative_date_hint 提供。
    for word, _delta in _RELATIVE_DAYS:
        text = text.replace(word, " ")
    for slang, expansion in _SLANG.items():
        if slang in text:
            text = expansion
            break
    for pattern, replacement in _SYNONYMS:
        text = re.sub(pattern, replacement, text)
    if re.search(r"(梗|什么意思|什么梗|出处|黑话)", original):
        text = f"{text} 梗 意思 出处"
    text = _FILLERS.sub(" ", text)
    text = _TRAIL.sub(" ", text)
    text = " ".join(dict.fromkeys(text.split())).strip()
    # 全被清掉就退回原话，至少别变成空查询
    return (text or original)[:120]


def relative_date_hint(text: str, now: datetime | None = None) -> str:
    """给总结用的日期提示，例如“明天 2026-10-27”。"""
    original = str(text or "")
    today = (now or datetime.now()).date()
    for word, delta in _RELATIVE_DAYS:
        if word in original:
            day: date = today + timedelta(days=delta)
            return f"{word} {day.isoformat()}"
    return f"今天 {today.isoformat()}"


def query_terms(query: str) -> list[str]:
    """中文按 2 字切片 + 英文/数字词，方便判断结果是否沾边。"""
    terms: list[str] = []
    for part in re.split(r"\s+", query or ""):
        part = part.strip()
        if not part:
            continue
        if re.fullmatch(r"[A-Za-z0-9_+.-]{2,}", part):
            if part.lower() not in terms:
                terms.append(part.lower())
            continue
        if len(part) >= 2 and part not in terms:
            terms.append(part)
        for index in range(len(part) - 1):
            gram = part[index:index + 2]
            if gram.strip() and gram not in terms:
                terms.append(gram)
    return terms[:16]


def results_look_relevant(query: str, results: str, *, min_hits: int = 1) -> bool:
    """搜索结果里至少命中查询的核心词，避免把“帮字怎么写”当答案。"""
    terms = query_terms(query)
    if not terms:
        return False
    text = (results or "").lower()
    hits = sum(1 for term in terms if term in text)
    # 长查询至少命中 2 个中文双字词；短查询至少 1 个
    need = min(max(int(min_hits), 1 if len(terms) <= 3 else 2), len(terms))
    return hits >= need
