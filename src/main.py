"""启动入口。

Windows 下直接双击 start.bat，或：
    python src/main.py
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# 以 `python src/main.py` 运行时，确保能 import 同目录模块
SRC_DIR = Path(__file__).resolve().parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from logging.handlers import RotatingFileHandler

from adapters.official_qq import OfficialQQClient  # noqa: E402
from chat import ChatService  # noqa: E402
from config import PROJECT_ROOT, load_dotenv, load_settings  # noqa: E402
from instance_lock import InstanceLock  # noqa: E402
from llm import DeepSeekClient  # noqa: E402
from memory import MemoryStore  # noqa: E402
from metrics import MetricsStore  # noqa: E402
from onebot import OneBotClient  # noqa: E402
from proactive import ProactiveScheduler  # noqa: E402
from care import CareScheduler  # noqa: E402
from daily_report import DailyReportScheduler  # noqa: E402
from weekly import WeeklyReportScheduler  # noqa: E402
from reminder import ReminderScheduler  # noqa: E402
from prompt import load_persona  # noqa: E402
from vision import VisionClient  # noqa: E402
from webui import start_webui  # noqa: E402

log = logging.getLogger("main")


def setup_logging(level: str, log_file: Path | None = None) -> None:
    """控制台 + 滚动文件双写：后台/隐藏启动时也能事后查日志。"""
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(
                RotatingFileHandler(
                    log_file,
                    maxBytes=1_000_000,
                    backupCount=3,
                    encoding="utf-8",
                )
            )
        except OSError:
            pass  # 文件日志失败不影响聊天
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)-8s | %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )
    for noisy in ("httpx", "httpcore", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def amain() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    settings = load_settings(PROJECT_ROOT / "config" / "config.yaml")
    setup_logging(settings.log_level, PROJECT_ROOT / "data" / "bot.log")

    if not settings.deepseek.api_key:
        raise SystemExit(
            "没有找到 DeepSeek API Key：请复制 .env.example 为 .env 并填入 DEEPSEEK_API_KEY"
        )

    persona_path = PROJECT_ROOT / settings.persona.file
    persona = load_persona(persona_path)
    group_personas = {
        group_id: load_persona(PROJECT_ROOT / relative_path)
        for group_id, relative_path in settings.persona.group_overrides.items()
    }
    db_path = PROJECT_ROOT / settings.chat.memory.db_path

    # Phase 4.5：防多开（同一 data 目录只允许一个实例）
    instance_lock = InstanceLock(db_path.parent / "instance.lock")
    if not instance_lock.acquire():
        raise SystemExit(
            f"已有一个实例在运行（{db_path.parent / 'instance.lock'}），拒绝重复启动"
        )

    memory = MemoryStore(db_path)
    await memory.init()
    if settings.chat.memory.archive_keep_days > 0:
        try:
            pruned = await memory.prune_archive(settings.chat.memory.archive_keep_days)
            if pruned:
                log.info("清理过期原文归档：%s 条", pruned)
        except Exception:
            log.exception("清理原文归档失败")
    llm = DeepSeekClient(settings.deepseek)

    metrics = None
    if settings.metrics.enabled:
        metrics_path = PROJECT_ROOT / settings.metrics.db_path
        metrics = MetricsStore(metrics_path)
        try:
            metrics.init()
            metrics.prune(settings.metrics.keep_days)
            # llm 回调传的是 dict，MetricsStore.record 是关键字参数接口
            llm.set_usage_listener(lambda record: metrics.record(**record))
        except Exception:
            log.exception("用量统计初始化失败，已关闭")
            metrics = None

    official_client: OfficialQQClient | None = None
    if settings.official.enabled:
        if not settings.official.configured:
            raise SystemExit(
                "official.enabled=true，但缺少 OFFICIAL_APP_ID / OFFICIAL_APP_SECRET"
            )
        official_client = OfficialQQClient(settings.official)
        sender = official_client
        protocol_label = f"官方 QQ 机器人（AppID {settings.official.app_id}）"
    else:
        sender = OneBotClient(settings.onebot)
        protocol_label = f"NapCat（{settings.onebot.ws_url}）"

    vision = VisionClient(settings.vision) if settings.vision.enabled else None
    service = ChatService(
        settings,
        llm=llm,
        sender=sender,
        memory=memory,
        persona=persona,
        group_personas=group_personas,
        vision=vision,
        metrics=metrics,
    )
    sender.set_handler(service.handle)
    proactive = ProactiveScheduler(settings.chat.proactive, service)
    reminders = ReminderScheduler(
        service,
        interval=settings.chat.remind.check_interval,
    )
    daily_report = DailyReportScheduler(service, settings.chat.daily_report)
    cares = CareScheduler(service, interval=settings.chat.care.check_interval)
    weekly = WeeklyReportScheduler(service, settings.chat.weekly_report)

    log.info("人格设定：%s（%s）", settings.persona.name, persona_path)
    log.info("协议端：%s", protocol_label)
    if settings.official.enabled:
        log.warning("官方接口只处理群内 @ 与私聊；不支持唤醒词/主动发言")
    if group_personas:
        log.info("多群人格：%s 个群使用专属人格", len(group_personas))
    if settings.persona.group_rules:
        log.info(
            "群专属口吻规则：%s 个群（%s）",
            len(settings.persona.group_rules),
            "、".join(str(gid) for gid in settings.persona.group_rules),
        )
    log.info("工具调用：%s", "开启" if settings.deepseek.enable_tools else "关闭")
    if settings.chat.memory.curate_every > 0:
        log.info(
            "记忆管家：每 %s 次回复后台整理一次长期记忆（上下文水位 %.0f%%）",
            settings.chat.memory.curate_every,
            settings.chat.memory.summary_ratio * 100,
        )
    else:
        log.info(
            "记忆管家：关闭（curate_every=0），上下文水位 %.0f%% 时做检查点",
            settings.chat.memory.summary_ratio * 100,
        )
    if vision is not None and vision.enabled:
        log.info(
            "图片理解：%s（%s，有人明确问图时才识别）",
            settings.vision.model,
            settings.vision.base_url,
        )
    else:
        log.info("图片理解：关闭（图片按 [图片] 占位）")
    log.info(
        "主动发言：%s",
        f"开启（{len(settings.chat.proactive.groups)} 个群）"
        if proactive.enabled
        else "关闭",
    )
    log.info("模型：%s | 数据库：%s", settings.deepseek.model, db_path)
    log.info(
        "用量统计：%s",
        settings.metrics.db_path if metrics is not None else "关闭",
    )
    log.info(
        "WebUI：%s",
        (
            f"http://{settings.web.host}:{settings.web.port}/"
            if settings.web.enabled
            else "关闭"
        ),
    )
    if official_client is None:
        if settings.onebot.mode == "reverse":
            log.info(
                "反向 OneBot 监听：ws://%s:%s",
                settings.onebot.reverse_host,
                settings.onebot.reverse_port,
            )
        else:
            log.info("等待连接 NapCat：%s", settings.onebot.ws_url)
    else:
        log.info("等待连接官方网关：%s", settings.official.gateway_url)

    if settings.official.enabled:
        # 官方接口不支持主动群发，启动调度器只会报错，直接不启动
        log.warning("官方接口不支持主动群发，主动发言调度器不启动")
    else:
        proactive.start()
    if settings.chat.remind.enabled:
        reminders.start()
    else:
        log.info("定时提醒：关闭")
    daily_report.start()
    if settings.chat.care.enabled:
        cares.start()
    else:
        log.info("主动关心：关闭")
    weekly.start()
    # 启动时先清理一次旧数据，之后每天一次
    await service.maintenance()
    maintenance_task = asyncio.create_task(_maintenance_loop(service))
    web_server = await start_webui(settings, service, metrics=metrics)
    if settings.web.enabled and web_server is None:
        log.warning("WebUI 未启动：检查 token / 端口 / host 配置")
    try:
        await sender.run()
    except KeyboardInterrupt:
        log.info("收到 Ctrl+C，正在退出……")
    finally:
        if web_server is not None:
            await web_server.stop()
        await proactive.stop()
        await reminders.stop()
        await daily_report.stop()
        await cares.stop()
        await weekly.stop()
        maintenance_task.cancel()
        await sender.close()
        if official_client is not None:
            await official_client.aclose()
        await service.shutdown()
        if vision is not None:
            await vision.aclose()
        await llm.aclose()
        if metrics is not None:
            metrics.close()
        await memory.close()
        instance_lock.release()
        log.info("已退出")


async def _maintenance_loop(service) -> None:
    """后台每天清理一次旧数据（归档/图片记忆/已完成提醒/已发送关心）。"""
    while True:
        await asyncio.sleep(24 * 3600)
        try:
            await service.maintenance()
        except Exception:
            log.exception("定期数据清理失败")


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:  # pragma: no cover
        pass


if __name__ == "__main__":
    main()
