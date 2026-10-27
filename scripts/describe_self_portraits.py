# -*- coding: utf-8 -*-
"""让视觉模型「看一遍自己」，把描述写进 data/self/portrait.json。

- 每张自画像调用一次视觉模型（复用 config.yaml 的 vision 配置，默认 DeepSeek）；
- 写入 items[].description / tags，供图库展示与"她知道这是自己"；
- appearance（外观设定）若为空则用内置兜底，不吃模型。

跑法：
    python scripts/describe_self_portraits.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from config import PROJECT_ROOT, load_dotenv, load_settings  # noqa: E402
from vision import VisionClient  # noqa: E402

MANIFEST = ROOT / "data" / "self" / "portrait.json"

FALLBACK_APPEARANCE = (
    "深蓝到浅蓝渐变的长发，头顶一圈白色蕾丝女仆头饰，头侧有一对小鲸鳍、一根呆毛；"
    "蓝色大眼睛，脸颊有腮红；穿白色女仆裙，深蓝蝴蝶结领结中间镶一颗蓝宝石；"
    "身后有一条蓝色鲸尾，住在深海，偶尔浮上 QQ 群透气。"
)


async def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    settings = load_settings(PROJECT_ROOT / "config" / "config.yaml")
    if not settings.vision.enabled:
        print("视觉模型未启用，跳过（portrait.json 保留兜底描述）")
        return
    if not settings.vision.configured:
        print("视觉模型未配置完整，跳过")
        return

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if not manifest.get("appearance"):
        manifest["appearance"] = FALLBACK_APPEARANCE
    vision = VisionClient(settings.vision)
    total = 0
    try:
        for item in manifest.get("items") or []:
            path = ROOT / "data" / "self" / str(item.get("file") or "")
            if not path.is_file():
                print(f"跳过（不存在）：{path.name}")
                continue
            try:
                description = await vision.describe_bytes(path.read_bytes())
            except Exception as exc:
                print(f"识别失败 {path.name}：{type(exc).__name__} {exc}")
                continue
            description = (description or "").strip()[:200]
            if description:
                item["description"] = description
                item["tags"] = "自画像 " + str(item.get("mood") or "")
                total += 1
                print(f"{path.name} -> {description}")
            else:
                print(f"{path.name} -> （空描述，保留原值）")
    finally:
        await vision.aclose()

    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n完成：{total} 张有描述，清单已写回 {MANIFEST}")


if __name__ == "__main__":
    asyncio.run(main())
