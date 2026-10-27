"""表情包离线回归测试：python scripts/memes_test.py。"""
import base64
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from config import MemeConfig, OneBotConfig, Settings
from memes import MemeStore
from onebot import OneBotClient, Incoming
from chat import ChatService

GIF = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")


class MemeTests(unittest.IsolatedAsyncioTestCase):
    def incoming(self, subtype=1, url="https://gchat.qpic.cn/test"):
        return Incoming("group", "group:123", 42, "1", "群友", "[图片]",
                        group_id=123, raw={"message": [{"type": "image", "data": {
                            "sub_type": subtype, "url": url}}]})

    async def test_download_dedup_restart_and_group_isolation(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = MemeConfig(directory=folder, chance=1)
            store = MemeStore(cfg)
            client = httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=GIF)))
            with patch("memes.httpx.AsyncClient", return_value=client):
                await store.collect(self.incoming())
            client = httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=GIF)))
            with patch("memes.httpx.AsyncClient", return_value=client):
                await store.collect(self.incoming())
            self.assertEqual(len(store.items(123)), 1)
            self.assertEqual(MemeStore(cfg).choose(123), GIF)
            self.assertIsNone(store.choose(124))
            cfg.chance = 0
            self.assertIsNone(store.choose(123))

    async def test_filter_normal_photos_and_invalid_urls(self):
        store = MemeStore(MemeConfig())
        store.cfg.collect_all_images = False
        self.assertEqual(store.candidates(self.incoming(subtype=0)), [])
        # 表情包类（动画表情/商城表情）即使不开 collect_all_images 也要收
        self.assertEqual(len(store.candidates(self.incoming(subtype=1))), 1)
        self.assertEqual(len(store.candidates(self.incoming(subtype=7))), 1)
        for url in ["http://127.0.0.1/x", "https://qpic.cn.evil.org/x",
                    "https://gchat.qpic.cn:bad/x", "http://[bad"]:
            self.assertEqual(store.candidates(self.incoming(url=url)), [])
        # QQNT 新图片 CDN 与商城表情原图必须能收集
        for url in ["https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=x",
                    "https://multimedia.nt.qq.com/download?appid=1407&fileid=x",
                    "https://gchat.qpic.cn/gchatpic_new/1/x.jpg",
                    "https://gxh.vip.qq.com/club/item/parcel/item/aa/x/raw300.gif"]:
            self.assertEqual(len(store.candidates(self.incoming(url=url))), 1, url)
        for url in ["https://multimedia.nt.qq.com.cn.evil.org/x",
                    "https://gxh.vip.qq.com.evil.org/x",
                    "https://vip.qq.com/x"]:
            self.assertEqual(store.candidates(self.incoming(url=url)), [], url)
        store.cfg.collect_all_images = True
        self.assertEqual(len(store.candidates(self.incoming(subtype=0))), 1)
        self.assertEqual(len(store.candidates(self.incoming(subtype=7))), 1)

    async def test_reject_large_and_non_images_and_enforce_capacity(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = MemeConfig(directory=folder, chance=1, max_bytes=100, max_per_group=1)
            store = MemeStore(cfg)
            for content in [b"not an image", GIF + bytes(100)]:
                client = httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, content=content)))
                with patch("memes.httpx.AsyncClient", return_value=client):
                    await store.collect(self.incoming())
                self.assertIsNone(store.choose(123))
            path = Path(folder) / "123"
            path.mkdir()
            (path / "manual.gif").write_bytes(GIF)
            client = httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: self.fail("图库已满时不应下载")))
            with patch("memes.httpx.AsyncClient", return_value=client):
                await store.collect(self.incoming())

    async def test_onebot_image_payload(self):
        sender = OneBotClient(OneBotConfig())
        sender.call = AsyncMock(return_value={"message_id": 99})
        self.assertEqual(await sender.send_group_image(123, GIF), "99")
        args = sender.call.await_args
        self.assertEqual(args.args, ("send_group_msg",))
        self.assertEqual(args.kwargs["group_id"], 123)
        encoded = args.kwargs["message"][0]["data"]["file"]
        self.assertEqual(base64.b64decode(encoded.removeprefix("base64://")), GIF)

    async def test_send_tracks_reply_and_handles_failure(self):
        sender = AsyncMock()
        sender.send_group_image.return_value = "99"
        service = ChatService(Settings(), llm=AsyncMock(), sender=sender,
                              memory=AsyncMock(), persona="测试")
        with patch.object(service.memes, "choose", return_value=GIF):
            await service._send_meme(self.incoming())
            self.assertIn("99", service._bot_message_ids)
            service.set_group_muted(123, True)
            await service._send_meme(self.incoming())
            self.assertEqual(sender.send_group_image.await_count, 1)
            service.set_group_muted(123, False)
            sender.send_group_image.side_effect = RuntimeError("模拟发送失败")
            with self.assertLogs("chat", level="ERROR"):
                await service._send_meme(self.incoming())
            self.assertEqual(service.stats["errors"], 1)

    async def test_force_send_when_user_asks(self):
        """群友明确要表情时强制发一张，不受 chance 影响。"""
        with tempfile.TemporaryDirectory() as folder:
            settings = Settings.from_dict(
                {"chat": {"memes": {"directory": folder, "chance": 0}}}
            )
            sender = AsyncMock()
            sender.send_group_image.return_value = "99"
            service = ChatService(settings, llm=AsyncMock(), sender=sender,
                                  memory=AsyncMock(), persona="测试")
            await service.memes.save(123, GIF)
            asked = self.incoming()
            asked.text = "发个表情"
            self.assertTrue(service._wants_meme(asked))
            await service._send_meme(asked, force=True)
            self.assertEqual(sender.send_group_image.await_count, 1)
            sender.reset_mock()
            normal = self.incoming()
            normal.text = "随便聊聊"
            self.assertFalse(service._wants_meme(normal))
            await service._send_meme(normal)
            sender.send_group_image.assert_not_awaited()

    async def test_config(self):
        settings = Settings.from_dict({"chat": {"memes": {"chance": 2}}})
        self.assertEqual(settings.chat.memes.chance, 1)
        self.assertEqual(settings.chat.group.ambient_chance, 0.015 * 0.8)
        self.assertEqual(MemeConfig().chance, 0.4)

    async def test_same_url_downloaded_once(self):
        """同一张图重复出现时只下载/入库一次（“存一次就行”）。"""
        with tempfile.TemporaryDirectory() as folder:
            store = MemeStore(MemeConfig(directory=folder))
            calls = []

            def handler(request):
                calls.append(str(request.url))
                return httpx.Response(200, content=GIF)

            real_client = httpx.AsyncClient

            def factory(*args, **kwargs):
                return real_client(transport=httpx.MockTransport(handler))

            with patch("memes.httpx.AsyncClient", side_effect=factory):
                await store.collect(self.incoming())
                await store.collect(self.incoming())
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(store.items(123)), 1)

    async def test_delete_gallery_image(self):
        with tempfile.TemporaryDirectory() as folder:
            store = MemeStore(MemeConfig(directory=folder, chance=1))
            path = await store.save(123, GIF)
            self.assertIsNotNone(path)
            self.assertTrue(path.is_file())
            json_path = path.with_suffix(path.suffix + ".json")
            self.assertTrue(json_path.is_file())
            # 按内容删除：第一次成功，第二次已经没有了
            self.assertIsNotNone(store.delete_by_content(123, GIF))
            self.assertIsNone(store.delete_by_content(123, GIF))
            self.assertFalse(path.exists())
            self.assertFalse(json_path.exists())
            self.assertEqual(store.items(123), [])
            # 按文件名删除 + 删除最近发出去的那张
            other = await store.save(123, GIF)
            self.assertTrue(store.delete_by_name(123, other.name))
            self.assertFalse(other.exists())
            third = await store.save(123, GIF)
            store._last[123] = third.name
            self.assertEqual(store.delete_last(123), third.name)
            self.assertFalse(third.exists())
            self.assertFalse(store.delete_by_name(123, "../evil.png"))

    async def test_at_delete_image_command(self):
        from chat import ChatService
        from onebot import Incoming
        with tempfile.TemporaryDirectory() as folder:
            settings = Settings()
            settings.chat.memes.directory = folder
            settings.chat.memes.chance = 1
            sender = AsyncMock()
            sender.get_message.return_value = {
                "message": [{"type": "image", "data": {"url": "https://gchat.qpic.cn/x.gif"}}]
            }
            service = ChatService(settings, llm=AsyncMock(), sender=sender,
                                  memory=AsyncMock(), persona="测试")
            path = await service.memes.save(123, GIF)
            self.assertIsNotNone(path)
            quoted = Incoming("group", "group:123", 42, "1", "群友", "删掉这张图",
                              at_self=True, reply_to="99", group_id=123)
            client = httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=GIF)))
            with patch("memes.httpx.AsyncClient", return_value=client):
                reply = await service._handle_delete_image(quoted)
            self.assertIn("删掉", reply or "")
            sender.get_message.assert_awaited_once_with("99")
            self.assertFalse(path.exists())
            self.assertEqual(service.memes.items(123), [])
            # 普通文字里的“删”不该被当成删图指令
            plain = Incoming("group", "group:123", 42, "2", "群友", "删除线怎么打",
                             at_self=True, group_id=123)
            self.assertIsNone(await service._handle_delete_image(plain))

    async def test_collects_without_reply_but_respects_blacklist(self):
        settings = Settings()
        settings.chat.group.ambient_chance = 0
        service = ChatService(settings, llm=AsyncMock(), sender=AsyncMock(),
                              memory=AsyncMock(), persona="测试")
        with patch.object(service.memes, "collect", new_callable=AsyncMock) as collect:
            await service.handle(self.incoming())
            import asyncio
            await asyncio.gather(*list(service._background))
            collect.assert_awaited_once()
            collect.reset_mock()
            settings.chat.filter.blacklist_groups = [123]
            await service.handle(self.incoming())
            collect.assert_not_awaited()
            settings.chat.filter.blacklist_groups = []
            service.set_group_muted(123, True)
            await service.handle(self.incoming())
            collect.assert_not_awaited()
        service.llm.chat.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
