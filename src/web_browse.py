"""搜索 + 打开来源页 + 模型总结：给 Bot 一个“浏览器式”搜索总结入口。"""
from __future__ import annotations

import asyncio
import re
from typing import Any

from web_fetch import fetch_url
from search_query import (
    normalize_search_query,
    query_terms,
    relative_date_hint,
    results_look_relevant,
)
from web_search import search_web

SUMMARY_SYSTEM = """你是群聊里的联网搜索助手。根据【搜索摘要】和【网页正文】回答群友的问题。
要求：
- 先给一句直接结论，再给 2-4 条关键事实；能附来源就附来源链接。
- 搜索摘要里出现具体对阵、比分、时间时，即使网页正文打不开，也要整理进答案，
  并用“据搜索摘要”说明来源；不要因为正文为空就说完全没数据。
- 只采用与问题、日期、赛事对应的信息；旧赛季、其他比赛、广告不要混进来。
- 资料不足就明说“没查到可靠信息”，不要编造。
- 不要输出“根据搜索结果”这类套话，像群友一样简短说话；不超过 400 字。
"""


def _extract_urls(results: str, limit: int = 3, query: str = "") -> list[str]:
    """按查询词命中程度给来源排序，优先打开真正相关的页面。"""
    terms = query_terms(query)
    scored: list[tuple[int, int, str]] = []
    for order, block in enumerate(results.split("\n\n")):
        match = re.search(r"来源：(https?://\S+)", block)
        if not match:
            continue
        url = match.group(1).rstrip("。.,，)）")
        if not url.startswith(("http://", "https://")):
            continue
        text = block.lower()
        score = sum(1 for term in terms if term in text)
        if url not in [item[2] for item in scored]:
            scored.append((score, order, url))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [url for _score, _order, url in scored[:limit]]


async def search_and_summarize(
    llm: Any,
    query: str,
    *,
    on_usage=None,  # noqa: ANN001
    max_tokens: int = 700,
) -> str:
    raw_query = " ".join(str(query or "").split())[:200]
    if not raw_query:
        return "要搜什么？用法：/search 今天上海天气"
    query = normalize_search_query(raw_query)
    results = await search_web(query, max_results=5)
    if results.startswith("搜索词不能为空"):
        return results
    if "未找到可用搜索结果" in results or "联网搜索暂时不可用" in results:
        return f"没搜到关于「{raw_query}」的可靠结果，换个说法再试。"
    # 结果完全不沾边时再换一个更直白的查询；仍不沾边就明确说没查到
    if not results_look_relevant(query, results):
        fallback = f"{query} 最新 介绍"
        second = await search_web(fallback, max_results=5)
        if results_look_relevant(query, second):
            query, results = fallback, second
        else:
            return (
                f"没搜到关于「{raw_query}」的可靠结果。"
                "搜索返回的内容和问题不相关，我不拿它硬编；"
                "可以给个更具体的关键词（比如赛事名/日期/官网）再试。"
            )
    urls = _extract_urls(results, limit=3, query=query)
    pages: list[str] = []
    if urls:
        fetched = await asyncio.gather(
            *(fetch_url(url, max_chars=2200) for url in urls),
            return_exceptions=True,
        )
        for item in fetched:
            if isinstance(item, str) and "网页读取失败" not in item:
                pages.append(item[:2500])
    if llm is None:
        return results[:1200]
    user = (
        f"【群友原始问题】{raw_query}\n"
        f"【今天/用户问的时间】{relative_date_hint(raw_query)}\n"
        f"【实际搜索关键词】{query}\n\n"
        f"【搜索摘要】\n{results[:3500]}\n\n"
        f"【网页正文】\n" + ("\n\n".join(pages)[:4500] or "（没有成功打开网页，仅用摘要）")
    )
    try:
        answer = await llm.chat(
            [
                {"role": "system", "content": SUMMARY_SYSTEM},
                {"role": "user", "content": user},
            ],
            temperature=0.3,
            max_tokens=max_tokens,
            kind="web_search_summary",
            on_usage=on_usage,
        )
    except Exception:
        return "搜索到了资料，但总结时出错了；先看这些来源：" + " ".join(urls)
    answer = str(answer or "").strip()
    if not answer:
        return "搜索到了资料，但模型没给出总结；换个关键词再试。"
    return answer[:1600]


async def search_summary_no_llm(query: str) -> str:
    """给没有模型上下文的工具用：返回搜索结果原文。"""
    return await search_web(query, max_results=5)
