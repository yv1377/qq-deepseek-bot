"""自画像图库：让 bot 知道自己长什么样，也能把自己的照片发出来。

- `data/self/*.png`：一套程序化生成的角色图（无版权素材，脚本见
  `scripts/make_self_portraits.py`），配套 `portrait.json` 清单；
- 清单里的 `appearance` 是她的外观设定，会注入系统提示词，让她"知道自己长什么样"；
- 被问到「你长什么样 / 发张自拍 / 看看你」时，从图库挑一张发出去（带冷却，防刷屏）。
"""
from __future__ import annotations

import json
import logging
import random
from pathlib import Path

from config import PROJECT_ROOT, SelfConfig

log = logging.getLogger("self")

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

FALLBACK_APPEARANCE = (
    "蓝色长发从深海蓝渐变到浅天蓝，发丝柔顺微卷；头顶白色蕾丝女仆头饰，头侧一对蓝色鲸鳍耳饰，"
    "一根呆毛；湛蓝大眼睛、长睫毛、脸颊腮红；穿蓝白女仆装：深蓝胸衣束腰、白色泡泡袖，"
    "深蓝领结镶水滴形蓝宝石，白围裙绣小蓝鲸，深蓝长裙摆带银色花纹，白色连裤袜、"
    "深蓝色玛丽珍小皮鞋，身后一条蓝色鲸尾；招牌动作一手提裙摆、一手比耶。"
)


class SelfGallery:
    def __init__(self, cfg: SelfConfig):
        self.cfg = cfg
        self.root = Path(cfg.directory)
        if not self.root.is_absolute():
            self.root = PROJECT_ROOT / self.root
        self._manifest: dict = {}
        self._last: dict[object, str] = {}
        self.reload()

    # -- 清单 -------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled)

    def reload(self) -> None:
        path = self.root / "portrait.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._manifest = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            self._manifest = {}

    @property
    def appearance(self) -> str:
        text = str(self._manifest.get("appearance") or "").strip()
        return text or FALLBACK_APPEARANCE

    def items(self) -> list[dict]:
        """清单里真实存在的图片 + 描述。"""
        result: list[dict] = []
        for item in self._manifest.get("items") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("file") or "")
            path = self.root / name
            try:
                if not path.is_file() or path.is_symlink():
                    continue
                if path.suffix.lower() not in IMAGE_SUFFIXES:
                    continue
            except OSError:
                continue
            result.append(
                {
                    "file": name,
                    "path": path,
                    "mood": str(item.get("mood") or ""),
                    "description": str(item.get("description") or ""),
                    "tags": str(item.get("tags") or ""),
                    "primary": bool(item.get("primary")),
                }
            )
        return result

    def mood_options(self) -> list[str]:
        return [item["mood"] for item in self.items() if item["mood"]]

    def about(self) -> str:
        """给提示词用的一句自我介绍（她知道自己的样子和表情）。"""
        items = self.items()
        if not items:
            return ""
        moods = "、".join(dict.fromkeys(item["mood"] for item in items if item["mood"]))
        return f"{self.appearance}（手头有 {len(items)} 张自己的图，表情/场景：{moods}）"

    # -- 取图 -------------------------------------------------------------
    def pick(self, key: object = None, mood: str = "") -> tuple[bytes, dict] | None:
        """挑一张自画像；指定 mood 时优先匹配，否则优先发主形象（primary）。"""
        items = self.items()
        if not items:
            return None
        if mood:
            candidates = [item for item in items if item["mood"] == mood] or items
        else:
            candidates = [item for item in items if item["primary"]] or items
        fresh = [item for item in candidates if item["file"] != self._last.get(key)]
        chosen = random.choice(fresh or candidates)
        try:
            data = chosen["path"].read_bytes()
        except OSError:
            log.warning("读取自画像失败：%s", chosen["file"])
            return None
        self._last[key] = chosen["file"]
        return data, chosen
