"""Phase 4.2 Docker 配置静态校验（不启动容器）。

覆盖：
- docker-compose 的依赖关系、端口、卷、环境变量；
- Dockerfile 的构建关键行；
- .dockerignore；
- 容器环境变量对配置的覆盖（ONEBOT_WS_URL / WEB_HOST / WEB_PORT / WEB_TOKEN）。

真正的 `docker compose up` 需要本机 Docker daemon 与网络拉镜像，不在离线测试里做。
运行：python scripts/docker_check.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from config import Settings  # noqa: E402

PASSED = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if not condition:
        raise AssertionError(f"FAIL: {name} {detail}")
    PASSED += 1
    print(f"  ok  {name}")


def main() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose.get("services", {})
    bot = services.get("bot", {})
    napcat = services.get("napcat", {})

    print("[1] docker-compose 结构")
    check("包含 bot / napcat 两个服务", {"bot", "napcat"} <= set(services))
    check("bot 用 Dockerfile 构建", bot.get("build", {}).get("dockerfile") == "Dockerfile")
    check(
        "bot 通过服务名访问 NapCat",
        "ONEBOT_WS_URL=ws://napcat:3001" in (bot.get("environment") or []),
    )
    check(
        "bot 挂载 config 与 data",
        {"./config:/app/config", "./data:/app/data"} <= set(bot.get("volumes") or []),
    )
    check("bot 读取 .env", ".env" in (bot.get("env_file") or []))
    check("bot 依赖 napcat 启动顺序", "napcat" in (bot.get("depends_on") or []))
    check("bot 的 WebUI 端口只绑本机", "127.0.0.1:8080:8080" in (bot.get("ports") or []))
    check(
        "NapCat WebUI 只绑本机",
        "127.0.0.1:6099:6099" in (napcat.get("ports") or []),
    )
    check(
        "NapCat 配置目录持久化",
        "./napcat/config:/app/napcat/config" in (napcat.get("volumes") or []),
    )

    print("[2] Dockerfile 与 .dockerignore")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    for snippet, label in [
        ("FROM python:3", "基于 python:3 镜像"),
        ("COPY requirements.txt", "先复制依赖清单"),
        ("pip install", "安装依赖"),
        ("COPY src ./src", "复制源码"),
        ("COPY config ./config", "复制默认配置"),
        ('CMD ["python", "src/main.py"]', "启动命令"),
    ]:
        check(label, snippet in dockerfile)

    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    check(".dockerignore 排除 .env", ".env" in ignore.split())
    check(".dockerignore 排除 data", "data/" in ignore.split())

    print("[3] 容器环境变量覆盖")
    keys = ["ONEBOT_WS_URL", "WEB_HOST", "WEB_PORT", "WEB_TOKEN"]
    saved = {key: os.environ.get(key) for key in keys}
    try:
        os.environ.update(
            {
                "ONEBOT_WS_URL": "ws://napcat:3001",
                "WEB_HOST": "0.0.0.0",
                "WEB_PORT": "8080",
                "WEB_TOKEN": "docker-token",
            }
        )
        settings = Settings.from_dict({})
        check("ONEBOT_WS_URL 覆盖生效", settings.onebot.ws_url == "ws://napcat:3001")
        check("WEB_HOST 覆盖生效", settings.web.host == "0.0.0.0")
        check("WEB_PORT 覆盖生效", settings.web.port == 8080)
        check("WEB_TOKEN 覆盖生效", settings.web.token == "docker-token")
        check(
            "容器内 WebUI 需要 token 才允许非本机绑定",
            not settings.web.is_loopback and bool(settings.web.token),
        )
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    print(f"\nDocker 配置校验通过：{PASSED} 项检查")


if __name__ == "__main__":
    main()
