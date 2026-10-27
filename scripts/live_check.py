"""真实连接检查：只调用模型和 get_login_info，不收发 QQ 聊天消息。"""
import asyncio
import json
import sys
from pathlib import Path

import httpx
from websockets.asyncio.client import connect

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from config import PROJECT_ROOT, load_dotenv, load_settings


async def main():
    load_dotenv(PROJECT_ROOT / ".env")
    cfg = load_settings(PROJECT_ROOT / "config/config.yaml")

    async def model():
        if (not cfg.deepseek.api_key or not cfg.deepseek.api_key.isascii()
                or any(c.isspace() for c in cfg.deepseek.api_key)
                or "DeepSeekKey" in cfg.deepseek.api_key):
            print("MODEL_CONFIG_ERROR: 密钥缺失或仍含占位文字，请修改 .env", flush=True)
            return
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    cfg.deepseek.base_url + "/chat/completions",
                    headers={"Authorization": "Bearer " + cfg.deepseek.api_key},
                    json={"model": cfg.deepseek.model, "messages": [
                        {"role": "user", "content": "连接测试，请只回复：连接成功"}],
                        "max_tokens": 32, "stream": False})
                print("MODEL_HTTP:", response.status_code, flush=True)
                if response.status_code == 200:
                    data = response.json()
                    content = data.get("choices", [{}])[0].get("message", {}).get("content")
                    print("MODEL_REPLY_PRESENT:", bool(content), flush=True)
        except Exception as exc:
            print("MODEL_ERROR:", type(exc).__name__, flush=True)

    async def qq():
        if cfg.official.enabled or cfg.onebot.mode != "forward":
            print("QQ_CHECK: 当前检查只适用 OneBot 正向 WS", flush=True)
            return
        try:
            headers = {"Authorization": "Bearer " + cfg.onebot.access_token} if cfg.onebot.access_token else {}
            async with connect(cfg.onebot.ws_url, additional_headers=headers, open_timeout=8) as ws:
                await ws.send(json.dumps({"action": "get_login_info", "echo": "diagnostic"}))
                async with asyncio.timeout(10):
                    async for message in ws:
                        data = json.loads(message)
                        if data.get("echo") == "diagnostic":
                            print("QQ_LOGIN_OK:", data.get("retcode") == 0
                                  and bool((data.get("data") or {}).get("user_id")), flush=True)
                            return
        except Exception as exc:
            print("QQ_ERROR:", type(exc).__name__, flush=True)

    await asyncio.gather(model(), qq())


if __name__ == "__main__":
    asyncio.run(main())
