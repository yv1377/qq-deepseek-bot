"""启动前诊断；--check 仅检查，不登录 QQ、不调用模型或发送消息。"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    os.chdir(ROOT)
    print(f"项目目录：{ROOT}")
    print(f"Python：{sys.executable}")
    if sys.version_info < (3, 11):
        print("[失败] 需要 Python 3.11 或以上版本。")
        return 1
    missing = [name for name in ("httpx", "websockets", "yaml", "fastapi", "uvicorn")
               if importlib.util.find_spec(name) is None]
    if missing:
        print("[失败] 缺少依赖：" + ", ".join(missing))
        print(f'请执行："{sys.executable}" -m pip install -r "{ROOT / "requirements.txt"}"')
        return 1
    print("[正常] Python 依赖齐全")
    from config import load_dotenv, load_settings
    try:
        load_dotenv(ROOT / ".env")
        settings = load_settings(ROOT / "config" / "config.yaml")
    except Exception as exc:
        # 配置解析异常可能包含原文，不将潜在密钥打印出来。
        print(f"[失败] 配置文件读取失败（{type(exc).__name__}），请检查 config/config.yaml。")
        return 1
    errors = False
    if not settings.deepseek.api_key:
        errors = True
        print("[失败] 尚未配置 DeepSeek API Key。")
        print(f"将 {ROOT / '.env.example'} 复制为 {ROOT / '.env'}。")
        print("用记事本打开 .env，在 DEEPSEEK_API_KEY= 后填入真实密钥并保存。")
        print("如果使用兼容模型服务，同时在 config/config.yaml 修改 base_url 和 model。")
    elif (not settings.deepseek.api_key.isascii()
          or any(c.isspace() for c in settings.deepseek.api_key)
          or any(word in settings.deepseek.api_key for word in ("你的", "真实", "DeepSeekKey"))):
        errors = True
        print("[失败] API Key 仍包含占位文字、中文或空白，请在 .env 中替换为完整真实密钥。")
    else:
        print("[正常] 模型密钥已配置（不显示内容）")
    if settings.official.enabled:
        if not settings.official.configured:
            errors = True
            print("[失败] 官方 QQ 接口缺少 APP ID / APP SECRET。")
    else:
        print("[提示] NapCat 启动器只负责 QQ 连接，机器人需单独运行本启动器。")
        print(f"NapCat 入口：{ROOT.parent / 'NapCatQQ' / 'shell' / 'launcher-user.bat'}")
        print("先在 NapCat 中登录机器人 QQ，并配置 OneBot WebSocket。")
        print(f"[提示] 当前连接模式：{settings.onebot.mode}；请与 config/config.yaml 保持一致。")
        print("未连接 NapCat 时机器人会等待重连，这不代表机器人程序无法启动。")
    if settings.vision.enabled:
        if settings.vision.configured:
            print(
                f"[正常] 图片理解：{settings.vision.model} @ {settings.vision.base_url}"
                "（有人明确问图片内容时才识别；表情包入库时写短描述）"
            )
            if not settings.vision.api_key and not settings.vision.base_url.startswith(
                ("http://127.0.0.1", "http://localhost")
            ):
                errors = True
                print("[失败] 视觉模型缺少 API Key：在 .env 填 VISION_API_KEY，或复用 DEEPSEEK_API_KEY。")
        else:
            errors = True
            print("[失败] vision.enabled=true，但缺少 base_url / model。")
    else:
        print("[提示] 图片理解关闭：群里的图片只会显示成 [图片]，不会识别内容。")
    if errors:
        print("\n请完成以上配置后重新启动；尚未登录 QQ 或发送任何消息。")
        return 1
    if "--check" in sys.argv[1:]:
        print("\n启动配置检查通过；尚未验证模型服务及 QQ 的实际连接。")
        return 0
    print("\n正在启动智能体，关闭此窗口或按 Ctrl+C 停止。", flush=True)
    try:
        return subprocess.call([sys.executable, str(ROOT / "src" / "main.py")], cwd=ROOT)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
