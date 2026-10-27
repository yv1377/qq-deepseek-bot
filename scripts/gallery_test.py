"""GIF、语境选图、图库鉴权和搜索收藏的离线回归。"""
import asyncio
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from config import MemeConfig, VisionConfig, Settings
from memes import MemeStore
from vision import VisionClient
from image_search import search_images
from chat import ChatService
from webui import create_app


def gif():
    frames = [Image.new("RGB", (16, 16), color) for color in ["red", "green", "blue", "yellow"]]
    out = io.BytesIO()
    frames[0].save(out, format="GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    return out.getvalue()


class GalleryTests(unittest.IsolatedAsyncioTestCase):
    async def test_gif_multiframe_and_original_preserved(self):
        payloads = []
        def handler(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": "开心大笑，适合庆祝"}}]})
        vision = VisionClient(VisionConfig(enabled=True, base_url="https://vision.test", model="test"),
                              http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        with tempfile.TemporaryDirectory() as folder:
            store = MemeStore(MemeConfig(directory=folder, chance=1))
            store.vision = vision
            original = gif()
            path = await store.save(1, original)
            self.assertEqual(path.read_bytes(), original)
            self.assertTrue(store.metadata(path)["animated"])
            self.assertEqual(len(payloads[0]["messages"][0]["content"]), 5)
            self.assertEqual(store.choose(1, "今天好开心"), original)
            # 没匹配到关键词时：默认不发；允许兜底时随机发一张（避免永远发不出图）
            self.assertIsNone(store.choose(1, "电脑报错"))
            self.assertEqual(store.choose(1, "电脑报错", any_if_no_match=True), original)
            store.cfg.chance = 0
            self.assertIsNone(store.choose(1, "开心"))
            self.assertIn("开心", MemeStore(store.cfg).items(1)[0]["description"])
        await vision.aclose()

    async def test_gallery_auth_file_tags_and_traversal(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = Settings()
            settings.chat.memes.directory = folder
            service = ChatService(settings, llm=AsyncMock(), sender=AsyncMock(), memory=AsyncMock(), persona="测试")
            path = await service.memes.save(123, gif())
            app = create_app(service, settings, token="test")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                self.assertEqual((await client.get("/gallery")).status_code, 200)
                self.assertEqual((await client.get("/api/gallery?group_id=123")).status_code, 401)
                headers = {"Authorization": "Bearer test"}
                result = await client.get("/api/gallery?group_id=123", headers=headers)
                self.assertEqual(len(result.json()["items"]), 1)
                image = await client.get(f"/api/gallery/123/{path.name}", headers=headers)
                self.assertEqual(image.content, gif())
                await client.post(f"/api/gallery/123/{path.name}/tags", headers=headers, json={"tags": "开心 大笑"})
                service.memes.cfg.chance = 1
                self.assertEqual(service.memes.choose(123, "大笑"), gif())
                deleted = await client.delete(f"/api/gallery/123/{path.name}", headers=headers)
                self.assertEqual(deleted.status_code, 200)
                self.assertEqual(service.memes.items(123), [])
                self.assertEqual(
                    (await client.delete(f"/api/gallery/123/{path.name}", headers=headers)).status_code,
                    404,
                )
                self.assertEqual((await client.get(f"/api/gallery/124/{path.name}", headers=headers)).status_code, 404)
                self.assertEqual((await client.get("/api/gallery/123/other.json", headers=headers)).status_code, 404)

    async def test_image_search_save_with_source(self):
        def handler(request):
            if request.url.host == "commons.wikimedia.org":
                return httpx.Response(200, json={"query": {"pages": {"1": {
                    "title": "Cat", "imageinfo": [{"url": "https://upload.wikimedia.org/cat.gif",
                    "descriptionurl": "https://commons.wikimedia.org/wiki/File:Cat.gif"}]}}}})
            return httpx.Response(200, content=gif())
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with tempfile.TemporaryDirectory() as folder:
            store = MemeStore(MemeConfig(directory=folder))
            with patch("image_search.httpx.AsyncClient", return_value=client):
                result = await search_images("cat", 123, store)
            self.assertIn("已收藏", result)
            self.assertEqual(store.items(123)[0]["source"], "search")
            self.assertIn("commons.wikimedia.org", store.items(123)[0]["origin"])


if __name__ == "__main__":
    unittest.main()
