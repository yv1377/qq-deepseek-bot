"""真实视觉链路验证：图片 / GIF 动图 / 表情包。

和 smoke_test 不同，这个脚本会真的下载图片、真的调用 config/config.yaml 里配置的
视觉模型。需要 .env 里有可用的 DEEPSEEK_API_KEY（或单独的 VISION_API_KEY）。

    python scripts/vision_live_test.py

检查内容：
1. 静态图片（PNG）识别；
2. GIF 动画抽帧识别（描述应体现“动/移动/变化”）；
3. 表情包（带中文文字的梗图）识别；
4. 图库收藏时自动写描述、保留原动图；
5. 把图片描述注入聊天上下文，DeepSeek 能顺着图片内容回复（会调用一次文字模型）。
"""
from __future__ import annotations

import asyncio
import functools
import http.server
import io
import shutil
import sys
import threading
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from chat import ChatService  # noqa: E402
from config import (  # noqa: E402
    MemeConfig,
    PROJECT_ROOT,
    load_dotenv,
    load_settings,
)
from llm import DeepSeekClient  # noqa: E402
from memes import MemeStore  # noqa: E402
from memory import MemoryStore  # noqa: E402
from onebot import Incoming  # noqa: E402
from vision import VisionClient  # noqa: E402

PASSED = 0
FAILED = 0

WORK = PROJECT_ROOT / "data" / "vision-live-test"
GALLERY = PROJECT_ROOT / "data" / "vision-live-gallery"
MEMORY_DB = PROJECT_ROOT / "data" / "vision-live.db"


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  [OK]   {name}" + (f"：{detail}" if detail else ""))
    else:
        FAILED += 1
        print(f"  [FAIL] {name}" + (f"：{detail}" if detail else ""))


def font(size: int) -> ImageFont.ImageFont:
    for path in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def make_photo() -> bytes:
    """一张卡通橘猫 PNG（不用外网素材，避免测试依赖第三方图床）。"""
    image = Image.new("RGB", (480, 360), (245, 245, 250))
    draw = ImageDraw.Draw(image)
    # 头、耳朵
    draw.ellipse((110, 110, 370, 330), fill=(240, 160, 70), outline=(120, 70, 20), width=4)
    draw.polygon([(130, 140), (165, 55), (225, 125)], fill=(240, 160, 70), outline=(120, 70, 20))
    draw.polygon([(350, 140), (315, 55), (255, 125)], fill=(240, 160, 70), outline=(120, 70, 20))
    # 眼睛、鼻子、胡须
    draw.ellipse((185, 190, 215, 230), fill=(40, 40, 40))
    draw.ellipse((265, 190, 295, 230), fill=(40, 40, 40))
    draw.polygon([(230, 245), (250, 245), (240, 260)], fill=(230, 110, 110))
    for dy in (0, 25):
        draw.line((90, 235 + dy, 175, 245 + dy), fill=(90, 90, 90), width=2)
        draw.line((305, 245 + dy, 390, 235 + dy), fill=(90, 90, 90), width=2)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def make_gif() -> bytes:
    """四帧 GIF：红球从左滚到右，另外画一行“GIF 动画”。"""
    frames = []
    title_font = font(26)
    for index in range(4):
        frame = Image.new("RGB", (400, 240), (250, 250, 250))
        draw = ImageDraw.Draw(frame)
        x = 40 + index * 90
        draw.ellipse((x, 120, x + 70, 190), fill=(220, 60, 60))
        draw.text((120, 30), "GIF 动画", font=title_font, fill=(30, 30, 30))
        draw.text((10, 210), f"frame {index + 1}", font=font(18), fill=(90, 90, 90))
        frames.append(frame)
    buffer = io.BytesIO()
    frames[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=250,
        loop=0,
    )
    return buffer.getvalue()


def make_sticker() -> bytes:
    """表情包：黄底黑字“摸鱼中”+ 简笔画笑脸。"""
    image = Image.new("RGB", (400, 300), (255, 214, 64))
    draw = ImageDraw.Draw(image)
    draw.text((60, 170), "摸鱼中", font=font(72), fill=(20, 20, 20))
    draw.ellipse((70, 50, 150, 130), fill=(255, 255, 255), outline=(20, 20, 20), width=5)
    draw.ellipse((250, 50, 330, 130), fill=(255, 255, 255), outline=(20, 20, 20), width=5)
    draw.ellipse((100, 80, 120, 100), fill=(20, 20, 20))
    draw.ellipse((280, 80, 300, 100), fill=(20, 20, 20))
    draw.arc((130, 90, 270, 180), start=20, end=160, fill=(20, 20, 20), width=6)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # 不刷屏
        pass


def serve(directory: Path) -> tuple[http.server.ThreadingHTTPServer, int]:
    handler = functools.partial(QuietHandler, directory=str(directory))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


class CaptureSender:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_group_text(self, group_id, text, reply_to=None):
        self.sent.append(text)
        return f"msg-{len(self.sent)}"

    async def send_private_text(self, user_id, text, reply_to=None):
        self.sent.append(text)
        return f"msg-{len(self.sent)}"

    async def send_group_image(self, group_id, image):
        return f"img-{len(self.sent)}"


def soft_keyword(label: str, description: str, keywords: tuple[str, ...]) -> None:
    if any(word in description for word in keywords):
        print(f"  [INFO] {label} 命中关键词：{keywords}")
    else:
        print(f"  [WARN] {label} 未命中关键词 {keywords}（描述本身仍可用，人工确认即可）")


async def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    settings = load_settings(PROJECT_ROOT / "config" / "config.yaml")
    vision = VisionClient(settings.vision)
    if not vision.enabled:
        print("SKIP：vision 未启用（config/config.yaml 的 vision.enabled / base_url / model）")
        await vision.aclose()
        return 0
    print(
        f"视觉模型：{settings.vision.model} @ {settings.vision.base_url}"
        f"（thinking={'关闭' if settings.vision.disable_thinking else '开启'}）"
    )

    shutil.rmtree(WORK, ignore_errors=True)
    shutil.rmtree(GALLERY, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)
    GALLERY.mkdir(parents=True, exist_ok=True)
    try:
        (WORK / "cat.png").write_bytes(make_photo())
        (WORK / "anim.gif").write_bytes(make_gif())
        (WORK / "sticker.png").write_bytes(make_sticker())
        server, port = serve(WORK)
        root_url = f"http://127.0.0.1:{port}"
        try:
            print("\n[1] 静态图片（PNG）")
            started = time.perf_counter()
            cat = await vision.describe([f"{root_url}/cat.png"])
            print(f"  描述：{cat or '（空）'}  （{time.perf_counter() - started:.1f}s）")
            check("静态图片得到描述", bool(cat), f"{len(cat)} 字")
            soft_keyword("静态图片", cat, ("猫", "橘", "卡通", "动物", "cat"))

            print("\n[2] GIF 动图（四帧抽样）")
            started = time.perf_counter()
            anim = await vision.describe([f"{root_url}/anim.gif"])
            print(f"  描述：{anim or '（空）'}  （{time.perf_counter() - started:.1f}s）")
            check("GIF 得到描述", bool(anim), f"{len(anim)} 字")
            soft_keyword("GIF", anim, ("动", "滚", "移", "帧", "球", "变化", "GIF"))

            print("\n[3] 表情包（带中文文字的梗图）")
            started = time.perf_counter()
            sticker = await vision.describe([f"{root_url}/sticker.png"])
            print(f"  描述：{sticker or '（空）'}  （{time.perf_counter() - started:.1f}s）")
            check("表情包得到描述", bool(sticker), f"{len(sticker)} 字")
            soft_keyword("表情包", sticker, ("摸鱼", "文字", "表情", "笑", "脸"))

            print("\n[4] 图库收藏：自动写描述、保留原动图")
            gallery_cfg = MemeConfig(
                enabled=True,
                directory=str(GALLERY),
                chance=1.0,
                max_per_group=10,
                max_bytes=3_000_000,
                collect_all_images=True,
            )
            gallery = MemeStore(gallery_cfg)
            gallery.vision = vision
            gif_bytes = make_gif()
            path = await gallery.save(123456, gif_bytes, source="test", context="GIF 动画")
            check("动图原样保存", bool(path) and path.read_bytes() == gif_bytes)
            meta = gallery.metadata(path) if path else {}
            check("收藏时写入描述", bool(meta.get("description")), meta.get("description", ""))
            check("标记为动图", meta.get("animated") is True)
            check("图库列表可见", len(gallery.items(123456)) == 1)

            print("\n[5] 端到端：图片描述进入聊天上下文（会调用一次 DeepSeek 文字模型）")
            memory = MemoryStore(MEMORY_DB)
            await memory.init()
            llm = DeepSeekClient(settings.deepseek)
            sender = CaptureSender()
            service = ChatService(
                settings,
                llm=llm,
                sender=sender,
                memory=memory,
                vision=vision,
                persona="你是群友大肥鱼，说话简短、像真人。",
            )
            incoming = parse_group_event(
                "看看这张图是什么，说一句就行。", f"{root_url}/sticker.png"
            )
            await service.handle(incoming)
            reply = sender.sent[-1] if sender.sent else ""
            print(f"  机器人回复：{reply or '（空）'}")
            check("机器人有回复", bool(reply), f"{len(reply)} 字")
            rows = await memory.recent_messages("group:100000002", 2)
            stored = " ".join(row.get("content") or "" for row in rows)
            check("图片描述写进记忆", "[图片：" in stored and len(stored) > 10)
            await service.shutdown()
            await memory.close()
            await llm.aclose()
        finally:
            server.shutdown()
            server.server_close()
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
        shutil.rmtree(GALLERY, ignore_errors=True)
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(MEMORY_DB) + suffix).unlink()
            except OSError:
                pass

    await vision.aclose()
    print(f"\n结果：{PASSED} 项通过，{FAILED} 项失败")
    return 0 if FAILED == 0 else 1


def parse_group_event(text: str, image_url: str) -> Incoming:
    return Incoming(
        kind="group",
        session_id="group:100000002",
        user_id=20001,
        message_id="live-vision-1",
        nickname="测试群友",
        text=f"{text}[图片]",
        at_self=True,
        group_id=100000002,
        raw={
            "message": [
                {"type": "text", "data": {"text": text}},
                {"type": "image", "data": {"url": image_url, "sub_type": 1}},
            ]
        },
        images=[image_url],
    )


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
