"""Penna 羽毛笔的轻量提示词适配：把中文需求转成 Danbooru/Stable Diffusion 提示词。

Penna 本身是 Windows Flutter 桌面应用，不是 OneBot 插件；这里移植它最核心的
“文生图提示词生成”能力，供 Bot 在所有群使用。
"""
from __future__ import annotations

from typing import Any

PENNA_SYSTEM = """你是 Penna 羽毛笔的文生图提示词助手。把群友的中文需求整理成可直接用于
Stable Diffusion / NovelAI 的 Danbooru 风格提示词。

输出严格三段，不要 markdown 代码块：
【正向提示词】
英文逗号分隔的 Danbooru 标签，按以下顺序：主体与人数、角色/作品（知道才写）、外观与服装、
表情与动作、场景与背景、构图与镜头、光影、风格与画质。只写标签，不要句子。
【负向提示词】
lowres, bad anatomy, bad hands, extra fingers, missing fingers, blurry, watermark,
text, signature, worst quality, low quality（可按题材补充）。
【中文说明】
2-4 句中文，说明画面构图和可调整项；不要复述标签。

硬性边界：不生成露骨色情、未成年性化、真实人物隐私/身份、仇恨暴力内容；
遇到这类需求改成安全版本并简短说明。不要声称会直接生成图片，你只负责提示词。
"""


async def generate_prompt(
    llm: Any,
    idea: str,
    style: str = "",
    *,
    max_tokens: int = 600,
    on_usage=None,  # noqa: ANN001
) -> str:
    idea = " ".join(str(idea or "").split())[:500]
    if not idea:
        return "要生成什么画面？发一句描述，例如：/penna 雨夜霓虹街头的白发少女。"
    if llm is None:
        return "提示词功能暂时不可用：模型未初始化。"
    user = f"需求：{idea}"
    if style.strip():
        user += f"\n风格偏好：{style.strip()[:120]}"
    try:
        raw = await llm.chat(
            [
                {"role": "system", "content": PENNA_SYSTEM},
                {"role": "user", "content": user},
            ],
            temperature=0.6,
            max_tokens=max_tokens,
            kind="penna",
            on_usage=on_usage,
        )
    except Exception:
        return "提示词生成失败，稍后再试；不要编造提示词。"
    text = str(raw or "").strip()
    return text[:1800] if text else "模型没有返回提示词，换个描述再试。"
