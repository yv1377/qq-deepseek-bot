"""配置层：config.yaml + .env -> 强类型配置对象。

优先级：真实环境变量 > .env 文件 > config.yaml 里的明文 > 代码默认值。
密钥建议只放在 .env，不要把 API Key 写进 config.yaml 或提交到 git。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> None:
    """极简 .env 加载器：KEY=VALUE，# 开头为注释；已存在的环境变量优先。"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _secret(raw: dict[str, Any], env_key: str, inline_key: str) -> str:
    """先读「环境变量名」，再读配置里的明文字段，最后兜底常见环境变量。"""
    env_name = str(raw.get(env_key) or "").strip()
    if env_name:
        value = os.environ.get(env_name, "").strip()
        if value:
            return value
    return str(raw.get(inline_key) or "").strip()


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_str_list(value: Any) -> list[str]:
    if not value:
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _as_int_list(value: Any) -> list[int]:
    if not value:
        return []
    result: list[int] = []
    for item in value:
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            continue
    return result


def _parse_pins(value: Any) -> dict[str, list[str]]:
    """把 YAML 的 pins 解析成 {session_id: [固定记忆...]}。"""
    if not isinstance(value, dict):
        return {}
    pins: dict[str, list[str]] = {}
    for key, items in value.items():
        session_id = str(key).strip()
        if not session_id:
            continue
        if isinstance(items, str):
            items = [items]
        if not isinstance(items, list):
            continue
        lines = [str(item).strip() for item in items if str(item).strip()]
        if lines:
            pins[session_id] = lines
    return pins


# ---------------------------------------------------------------------------
# 各配置段
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DeepSeekConfig:
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    temperature: float = 1.1
    max_tokens: int = 500
    timeout: float = 60.0
    max_retries: int = 2
    enable_tools: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "DeepSeekConfig":
        raw = data or {}
        api_key = _secret(raw, "api_key_env", "api_key")
        if not api_key:
            api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        return cls(
            api_key=api_key,
            base_url=str(raw.get("base_url") or "https://api.deepseek.com").rstrip("/"),
            model=str(raw.get("model") or "deepseek-chat"),
            temperature=_as_float(raw.get("temperature"), 1.1),
            max_tokens=_as_int(raw.get("max_tokens"), 500),
            timeout=_as_float(raw.get("timeout"), 60.0),
            max_retries=_as_int(raw.get("max_retries"), 2),
            enable_tools=_as_bool(raw.get("enable_tools"), False),
        )


@dataclass(slots=True)
class VisionConfig:
    """Phase 3.3 图片理解：接任意 OpenAI 兼容的视觉模型。

    DeepSeek-V4.1-Flash（模型名 deepseek-flash）已支持图片输入，所以默认直接
    复用 DEEPSEEK_API_KEY，不必再单独买视觉模型；仍可指到其他 OpenAI 兼容服务。
    视觉识别默认关闭思考（thinking disabled）：深度思考的推理 token 会占满
    max_tokens，导致真正要的描述正文为空。未启用时图片仍以 [图片] 占位。
    """

    enabled: bool = False
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    timeout: float = 45.0
    max_images: int = 2
    max_image_bytes: int = 32_000_000
    cache_size: int = 50
    max_tokens: int = 150
    disable_thinking: bool = True
    # 结合前后聊天推断图片/表情包想表达什么（额外一次很小的文本模型调用）
    infer_expression: bool = True
    expression_max_tokens: int = 80
    prompt: str = (
        "用不超过 40 字说清画面主体和上面的文字；不评价、不猜动作、不分点。"
        "静态图不要编造变化。"
    )

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.base_url and self.model)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "VisionConfig":
        raw = data or {}
        api_key = _secret(raw, "api_key_env", "api_key")
        if not api_key:
            # 没单独配视觉 Key 时自动复用 DeepSeek 的 Key（官方视觉模型同一把 Key）
            api_key = os.environ.get("VISION_API_KEY", "").strip() or os.environ.get(
                "DEEPSEEK_API_KEY", ""
            ).strip()
        return cls(
            enabled=_as_bool(raw.get("enabled"), True),
            base_url=str(raw.get("base_url") or "").rstrip("/"),
            model=str(raw.get("model") or ""),
            api_key=api_key,
            timeout=_as_float(raw.get("timeout"), 45.0),
            max_images=max(1, _as_int(raw.get("max_images"), 2)),
            max_image_bytes=max(100_000, _as_int(raw.get("max_image_bytes"), 32_000_000)),
            cache_size=max(0, _as_int(raw.get("cache_size"), 50)),
            max_tokens=max(32, _as_int(raw.get("max_tokens"), 150)),
            disable_thinking=_as_bool(raw.get("disable_thinking"), True),
            infer_expression=_as_bool(raw.get("infer_expression"), True),
            expression_max_tokens=max(
                24, _as_int(raw.get("expression_max_tokens"), 80)
            ),
            prompt=str(
                raw.get("prompt")
                or "用不超过 40 字说清画面主体和上面的文字；不评价、不猜动作、不分点。"
                "静态图不要编造变化。"
            ),
        )


@dataclass(slots=True)
class OneBotConfig:
    # forward：机器人主动连 NapCat；reverse：机器人监听，NapCat 来连
    mode: str = "forward"
    ws_url: str = "ws://127.0.0.1:3001"
    reverse_host: str = "0.0.0.0"
    reverse_port: int = 3002
    access_token: str = ""
    reconnect_interval: float = 3.0
    request_timeout: float = 15.0

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "OneBotConfig":
        raw = data or {}
        ws_url = str(raw.get("ws_url") or "ws://127.0.0.1:3001")
        # Docker Compose / 部署脚本可以用环境变量覆盖
        ws_url = os.environ.get("ONEBOT_WS_URL", ws_url).strip() or ws_url
        mode = os.environ.get("ONEBOT_MODE", str(raw.get("mode") or "forward"))
        mode = mode.strip().lower()
        if mode not in {"forward", "reverse"}:
            mode = "forward"
        return cls(
            mode=mode,
            ws_url=ws_url,
            reverse_host=os.environ.get(
                "ONEBOT_REVERSE_HOST",
                str(raw.get("reverse_host") or "0.0.0.0"),
            ).strip()
            or "0.0.0.0",
            reverse_port=max(
                1,
                min(
                    65535,
                    _as_int(
                        os.environ.get("ONEBOT_REVERSE_PORT"),
                        _as_int(raw.get("reverse_port"), 3002),
                    ),
                ),
            ),
            access_token=_secret(raw, "access_token_env", "access_token"),
            reconnect_interval=_as_float(raw.get("reconnect_interval"), 3.0),
            request_timeout=_as_float(raw.get("request_timeout"), 15.0),
        )


@dataclass(slots=True)
class PersonaConfig:
    name: str = "大肥鱼"
    file: str = "config/persona.md"
    # Phase 3.1：群号 -> 人格文件路径；未配置的群使用默认人格
    group_overrides: dict[int, str] = field(default_factory=dict)
    # 群号 -> 追加在系统规则后面的专属口吻/规则（可放宽脏话等说话方式）
    group_rules: dict[int, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "PersonaConfig":
        raw = data or {}
        overrides: dict[int, str] = {}
        for key, value in (raw.get("group_overrides") or {}).items():
            try:
                group_id = int(key)
            except (TypeError, ValueError):
                continue
            path = str(value or "").strip()
            if path:
                overrides[group_id] = path
        rules: dict[int, str] = {}
        for key, value in (raw.get("group_rules") or {}).items():
            try:
                group_id = int(key)
            except (TypeError, ValueError):
                continue
            text = str(value or "").strip()
            if text:
                rules[group_id] = text
        return cls(
            name=str(raw.get("name") or "大肥鱼"),
            file=str(raw.get("file") or "config/persona.md"),
            group_overrides=overrides,
            group_rules=rules,
        )


@dataclass(slots=True)
class GroupConfig:
    always_reply_at: bool = False
    enabled: bool = True
    # 群聊里除了 @ 和回复机器人之外，还有哪些「唤醒词」
    wake_words: list[str] = field(default_factory=lambda: ["大肥鱼", "肥鱼"])
    # 没人叫它时，每条群消息以多大概率主动插一句（0 = 关闭；建议 <= 0.02）
    ambient_chance: float = 0.012
    # 两次主动插话的最小间隔（秒）
    ambient_min_interval: float = 180.0
    # 同一群两次回复的最小间隔（秒）
    cooldown: float = 6.0
    # 同一群每分钟最多回复几条
    per_minute: int = 8
    # 群聊背景缓存条数（也会作为上下文背景注入）
    history_size: int = 25
    # 群友一句话拆成几条短消息时，等待合并的窗口（秒）；0 = 关闭
    merge_window: float = 2.5
    # 一次最多合并几条碎片消息
    merge_max_messages: int = 5
    # 回复突发限流：窗口内回复达到上限后，暂停一段时间不再回（降低刷屏频率）
    burst_limit: int = 0          # 0 = 关闭
    burst_window: float = 60.0    # 统计窗口（秒）
    burst_pause: float = 15.0     # 达到上限后暂停多久（秒）

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "GroupConfig":
        raw = data or {}
        return cls(
            always_reply_at=_as_bool(raw.get("always_reply_at"), False),
            enabled=_as_bool(raw.get("enabled"), True),
            wake_words=_as_str_list(raw.get("wake_words")) or ["大肥鱼", "肥鱼"],
            ambient_chance=max(0.0, min(1.0, _as_float(raw.get("ambient_chance"), 0.012))),
            ambient_min_interval=_as_float(raw.get("ambient_min_interval"), 180.0),
            cooldown=_as_float(raw.get("cooldown"), 6.0),
            per_minute=_as_int(raw.get("per_minute"), 8),
            history_size=max(0, _as_int(raw.get("history_size"), 25)),
            merge_window=max(0.0, _as_float(raw.get("merge_window"), 2.5)),
            merge_max_messages=max(1, _as_int(raw.get("merge_max_messages"), 5)),
            burst_limit=max(0, _as_int(raw.get("burst_limit"), 0)),
            burst_window=max(1.0, _as_float(raw.get("burst_window"), 60.0)),
            burst_pause=max(0.0, _as_float(raw.get("burst_pause"), 15.0)),
        )


@dataclass(slots=True)
class PrivateConfig:
    enabled: bool = True
    cooldown: float = 1.0
    per_minute: int = 12
    # 私聊白名单，空 = 所有人都能私聊（建议上线后填自己的 QQ，免得被陌生人刷额度）
    whitelist: list[int] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "PrivateConfig":
        raw = data or {}
        return cls(
            enabled=_as_bool(raw.get("enabled"), True),
            cooldown=_as_float(raw.get("cooldown"), 1.0),
            per_minute=_as_int(raw.get("per_minute"), 12),
            whitelist=_as_int_list(raw.get("whitelist")),
        )


@dataclass(slots=True)
class ReplyConfig:
    max_chars: int = 350  # 单次回复总字数上限
    split_chars: int = 160  # 超过就分条发送，单条上限
    quote: bool = True  # 是否引用被回复的消息
    chunk_delay: float = 0.6  # 分条之间的间隔，避免像刷屏

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ReplyConfig":
        raw = data or {}
        return cls(
            max_chars=_as_int(raw.get("max_chars"), 350),
            split_chars=max(20, _as_int(raw.get("split_chars"), 160)),
            quote=_as_bool(raw.get("quote"), True),
            chunk_delay=max(0.0, _as_float(raw.get("chunk_delay"), 0.6)),
        )


@dataclass(slots=True)
class MemoryConfig:
    db_path: str = "data/bot.db"
    # 每次请求带上的最近对话条数
    recent_limit: int = 12
    # 会话消息超过多少条时触发后台摘要
    summary_trigger: int = 20
    # 摘要后保留的最近消息条数
    summary_keep: int = 8
    # 单次请求的上下文预算（近似 token）
    token_budget: int = 3400
    # Phase 3.5：每次注入多少条会话笔记 / 用户笔记
    notes_limit: int = 5
    # 每个说话人单独注入多少条人物笔记（提高对每个人的记忆）
    user_notes_limit: int = 8
    # 固定记忆：session_id -> 永远置顶、不会被自动整理/合并覆盖的短句
    pins: dict[str, list[str]] = field(default_factory=dict)
    # 上下文管家：活动消息估算 token 达到 token_budget * 该比例时提前摘要
    # （不再只按条数，聊得密/字多时更早做检查点，避免撑爆上下文）
    summary_ratio: float = 0.8
    # 原文归档保留天数：摘要压缩后的原文仍留在 archive 表，可被 recall_chat 找回
    archive_keep_days: float = 30.0
    # 记忆管家：每回复 N 次后台自动整理一次长期记忆（0 = 关闭）
    curate_every: int = 0
    # 记忆管家每次回看最近多少条消息
    curate_keep: int = 12
    # recall_chat 单次最多返回多少条原文
    recall_limit: int = 6
    # 每次注入多少条未完成待办
    todos_limit: int = 5
    # 记忆治理：单会话笔记累计到多少条时自动合并成画像（0 = 关闭）
    distill_every: int = 80
    # 合并后保留几条画像
    distilled_limit: int = 8
    # 数据清理：图片记忆 / 已完成提醒 / 已发送关心 的保留天数
    image_memory_keep_days: float = 30.0
    reminder_keep_days: float = 7.0
    care_keep_days: float = 30.0

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "MemoryConfig":
        raw = data or {}
        return cls(
            db_path=str(raw.get("db_path") or "data/bot.db"),
            recent_limit=max(0, _as_int(raw.get("recent_limit"), 12)),
            summary_trigger=max(2, _as_int(raw.get("summary_trigger"), 20)),
            summary_keep=max(0, _as_int(raw.get("summary_keep"), 8)),
            token_budget=max(500, _as_int(raw.get("token_budget"), 3400)),
            notes_limit=max(0, _as_int(raw.get("notes_limit"), 5)),
            user_notes_limit=max(0, _as_int(raw.get("user_notes_limit"), 8)),
            pins=_parse_pins(raw.get("pins")),
            summary_ratio=max(0.0, min(1.5, _as_float(raw.get("summary_ratio"), 0.8))),
            archive_keep_days=max(0.0, _as_float(raw.get("archive_keep_days"), 30.0)),
            curate_every=max(0, _as_int(raw.get("curate_every"), 0)),
            curate_keep=max(2, _as_int(raw.get("curate_keep"), 12)),
            recall_limit=max(1, _as_int(raw.get("recall_limit"), 6)),
            todos_limit=max(0, _as_int(raw.get("todos_limit"), 5)),
            distill_every=max(0, _as_int(raw.get("distill_every"), 80)),
            distilled_limit=max(1, _as_int(raw.get("distilled_limit"), 8)),
            image_memory_keep_days=max(
                0.0, _as_float(raw.get("image_memory_keep_days"), 30.0)
            ),
            reminder_keep_days=max(
                0.0, _as_float(raw.get("reminder_keep_days"), 7.0)
            ),
            care_keep_days=max(0.0, _as_float(raw.get("care_keep_days"), 30.0)),
        )


@dataclass(slots=True)
class FilterConfig:
    whitelist_groups: list[int] = field(default_factory=list)
    blacklist_groups: list[int] = field(default_factory=list)
    blacklist_users: list[int] = field(default_factory=list)
    blocked_words: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "FilterConfig":
        raw = data or {}
        return cls(
            whitelist_groups=_as_int_list(raw.get("whitelist_groups")),
            blacklist_groups=_as_int_list(raw.get("blacklist_groups")),
            blacklist_users=_as_int_list(raw.get("blacklist_users")),
            blocked_words=_as_str_list(raw.get("blocked_words")),
        )


@dataclass(slots=True)
class AdminConfig:
    qq: list[int] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AdminConfig":
        raw = data or {}
        return cls(qq=_as_int_list(raw.get("qq")))


@dataclass(slots=True)
class OfficialConfig:
    """Phase 4.4：QQ 机器人开放平台（官方接口，实验性）。

    官方接口只推送「群里 @ 机器人」和「私聊」消息；主动发言/唤醒词在群里不生效。
    """

    enabled: bool = False
    app_id: str = ""
    app_secret: str = ""
    intents: int = 1 << 25  # GROUP_AND_C2C_EVENT
    api_base: str = "https://api.sgroup.qq.com"
    token_url: str = "https://bots.qq.com/app/getAppAccessToken"
    gateway_url: str = "wss://api.sgroup.qq.com/websocket"
    request_timeout: float = 15.0
    reconnect_interval: float = 5.0

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.app_id and self.app_secret)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "OfficialConfig":
        raw = data or {}
        app_id = os.environ.get(
            str(raw.get("app_id_env") or "OFFICIAL_APP_ID"), ""
        ).strip() or str(raw.get("app_id") or "").strip()
        app_secret = os.environ.get(
            str(raw.get("app_secret_env") or "OFFICIAL_APP_SECRET"), ""
        ).strip() or str(raw.get("app_secret") or "").strip()
        return cls(
            enabled=_as_bool(raw.get("enabled"), False),
            app_id=app_id,
            app_secret=app_secret,
            intents=_as_int(raw.get("intents"), 1 << 25),
            api_base=str(raw.get("api_base") or "https://api.sgroup.qq.com").rstrip(
                "/"
            ),
            token_url=str(
                raw.get("token_url")
                or "https://bots.qq.com/app/getAppAccessToken"
            ),
            gateway_url=str(
                raw.get("gateway_url") or "wss://api.sgroup.qq.com/websocket"
            ),
            request_timeout=_as_float(raw.get("request_timeout"), 15.0),
            reconnect_interval=_as_float(raw.get("reconnect_interval"), 5.0),
        )


@dataclass(slots=True)
class WebConfig:
    """Phase 4.1 WebUI：默认关闭，默认只监听本机。"""

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8080
    token: str = ""
    token_env: str = "WEB_TOKEN"

    @property
    def is_loopback(self) -> bool:
        return self.host in {"127.0.0.1", "localhost", "::1"}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "WebConfig":
        raw = data or {}
        host = str(raw.get("host") or "127.0.0.1")
        host = os.environ.get("WEB_HOST", host).strip() or host
        port = os.environ.get("WEB_PORT")
        token_env = str(raw.get("token_env") or "WEB_TOKEN")
        token = os.environ.get(token_env, "").strip() or str(
            raw.get("token") or ""
        ).strip()
        return cls(
            enabled=_as_bool(raw.get("enabled"), False),
            host=host,
            port=max(1, min(65535, _as_int(port, _as_int(raw.get("port"), 8080)))),
            token=token,
            token_env=token_env,
        )


@dataclass(slots=True)
class ProactiveGroupConfig:
    interval_minutes: float = 180.0
    quiet_start: int = 0
    quiet_end: int = 7
    max_per_day: int = 3
    min_silence_minutes: float = 5.0  # 群里最近 N 分钟有人说话就不冒泡
    prompt: str = ""  # 该群专属的主动发言指令


@dataclass(slots=True)
class ProactiveConfig:
    """Phase 3.4 主动发言：默认关闭，只对显式列出的群生效。"""

    enabled: bool = False
    check_interval: float = 60.0
    interval_minutes: float = 180.0
    quiet_start: int = 0
    quiet_end: int = 7
    max_per_day: int = 3
    min_silence_minutes: float = 5.0
    prompt: str = ""
    groups: dict[int, ProactiveGroupConfig] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ProactiveConfig":
        raw = data or {}
        default_interval = _as_float(raw.get("interval_minutes"), 180.0)
        default_quiet_start = _as_int(raw.get("quiet_start"), 0)
        default_quiet_end = _as_int(raw.get("quiet_end"), 7)
        default_max = _as_int(raw.get("max_per_day"), 3)
        default_silence = _as_float(raw.get("min_silence_minutes"), 5.0)
        default_prompt = str(raw.get("prompt") or "")

        groups: dict[int, ProactiveGroupConfig] = {}
        for key, value in (raw.get("groups") or {}).items():
            try:
                group_id = int(key)
            except (TypeError, ValueError):
                continue
            item = value or {}
            groups[group_id] = ProactiveGroupConfig(
                interval_minutes=_as_float(
                    item.get("interval_minutes"), default_interval
                ),
                quiet_start=_as_int(item.get("quiet_start"), default_quiet_start),
                quiet_end=_as_int(item.get("quiet_end"), default_quiet_end),
                max_per_day=_as_int(item.get("max_per_day"), default_max),
                min_silence_minutes=_as_float(
                    item.get("min_silence_minutes"), default_silence
                ),
                prompt=str(item.get("prompt") or default_prompt),
            )
        return cls(
            enabled=_as_bool(raw.get("enabled"), False),
            check_interval=max(10.0, _as_float(raw.get("check_interval"), 60.0)),
            interval_minutes=default_interval,
            quiet_start=default_quiet_start,
            quiet_end=default_quiet_end,
            max_per_day=default_max,
            min_silence_minutes=default_silence,
            prompt=default_prompt,
            groups=groups,
        )


@dataclass(slots=True)
class MemeConfig:
    enabled: bool = True
    chance: float = 0.4
    # 群号 -> 单独发图概率；没列出的群用 chance
    group_chances: dict[int, float] = field(default_factory=dict)
    # 群号 -> 两次发图最短间隔（秒）；0 = 不限制
    group_cooldowns: dict[int, float] = field(default_factory=dict)
    directory: str = "data/memes"
    max_per_group: int = 200
    max_bytes: int = 5_000_000
    collect_all_images: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "MemeConfig":
        raw = data or {}
        chances: dict[int, float] = {}
        for key, value in (raw.get("group_chances") or {}).items():
            try:
                group_id = int(key)
                chance = float(value)
            except (TypeError, ValueError):
                continue
            chances[group_id] = max(0.0, min(1.0, chance))
        cooldowns: dict[int, float] = {}
        for key, value in (raw.get("group_cooldowns") or {}).items():
            try:
                group_id = int(key)
                seconds = float(value)
            except (TypeError, ValueError):
                continue
            cooldowns[group_id] = max(0.0, seconds)
        return cls(
            enabled=_as_bool(raw.get("enabled"), True),
            chance=max(0.0, min(1.0, _as_float(raw.get("chance"), 0.4))),
            group_chances=chances,
            group_cooldowns=cooldowns,
            directory=str(raw.get("directory") or "data/memes"),
            max_per_group=max(1, _as_int(raw.get("max_per_group"), 200)),
            max_bytes=max(1024, _as_int(raw.get("max_bytes"), 5_000_000)),
            collect_all_images=_as_bool(raw.get("collect_all_images"), False),
        )


@dataclass(slots=True)
class BudgetConfig:
    """每个群每天的花费上限（按 DeepSeek 官方 token 单价估算）。"""

    enabled: bool = False
    daily_cny_per_group: float = 0.3
    # 群号 -> 单独日额度；没列出的群用 daily_cny_per_group
    group_limits: dict[int, float] = field(default_factory=dict)
    db_path: str = "data/budget.db"
    # DeepSeek deepseek-flash 官方价（美元 / 1M token）换算成人民币，默认按非高峰价
    input_cny_per_million: float = 1.1        # cache miss，$0.15
    cached_input_cny_per_million: float = 0.022  # cache hit，$0.003
    output_cny_per_million: float = 4.35      # $0.6
    peak_multiplier: float = 2.0              # 高峰时段价格翻倍

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "BudgetConfig":
        raw = data or {}
        limits: dict[int, float] = {}
        for key, value in (raw.get("group_limits") or {}).items():
            try:
                group_id = int(key)
                amount = float(value)
            except (TypeError, ValueError):
                continue
            if amount >= 0:
                limits[group_id] = amount
        return cls(
            enabled=_as_bool(raw.get("enabled"), False),
            daily_cny_per_group=max(0.0, _as_float(raw.get("daily_cny_per_group"), 0.3)),
            group_limits=limits,
            db_path=str(raw.get("db_path") or "data/budget.db"),
            input_cny_per_million=max(
                0.0, _as_float(raw.get("input_cny_per_million"), 1.1)
            ),
            cached_input_cny_per_million=max(
                0.0, _as_float(raw.get("cached_input_cny_per_million"), 0.022)
            ),
            output_cny_per_million=max(
                0.0, _as_float(raw.get("output_cny_per_million"), 4.35)
            ),
            peak_multiplier=max(1.0, _as_float(raw.get("peak_multiplier"), 2.0)),
        )


@dataclass(slots=True)
class SafetyConfig:
    """尺度与安全边界（见 docs/08）。

    允许轻度撒娇暧昧（16+，不露骨），但明确要露骨内容时不进模型，用本地角色化台词化解。
    """

    enabled: bool = True
    # deflect = 用角色化台词化解；silence = 直接不出声
    deflect_mode: str = "deflect"
    # 同一个群/人不重复化解的最小间隔（分钟），避免"荤话就搭话"
    deflect_cooldown_minutes: float = 3.0
    # 同一人多少次越界后静默；0 = 不静默
    strike_limit: int = 3
    strike_window_minutes: float = 30.0
    mute_minutes: float = 60.0
    # 允许轻度撒娇/暧昧提示（抱抱、亲亲这类），只影响提示词，不产生露骨内容
    flirt_enabled: bool = True
    # 模型输出兜底：命中露骨词就替换成本地化解台词
    output_guard: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SafetyConfig":
        raw = data or {}
        mode = str(raw.get("deflect_mode") or "deflect").strip().lower()
        if mode not in {"deflect", "silence"}:
            mode = "deflect"
        return cls(
            enabled=_as_bool(raw.get("enabled"), True),
            deflect_mode=mode,
            deflect_cooldown_minutes=max(
                0.0, _as_float(raw.get("deflect_cooldown_minutes"), 3.0)
            ),
            strike_limit=max(0, _as_int(raw.get("strike_limit"), 3)),
            strike_window_minutes=max(
                1.0, _as_float(raw.get("strike_window_minutes"), 30.0)
            ),
            mute_minutes=max(0.0, _as_float(raw.get("mute_minutes"), 60.0)),
            flirt_enabled=_as_bool(raw.get("flirt_enabled"), True),
            output_guard=_as_bool(raw.get("output_guard"), True),
        )


@dataclass(slots=True)
class SelfConfig:
    """自画像图库：让她知道自己长什么样，也能发自己的照片（见 docs/09）。"""

    enabled: bool = True
    directory: str = "data/self"
    # 同一个群/人两次发自己照片的最小间隔（分钟），防止刷屏
    cooldown_minutes: float = 3.0

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SelfConfig":
        raw = data or {}
        return cls(
            enabled=_as_bool(raw.get("enabled"), True),
            directory=str(raw.get("directory") or "data/self"),
            cooldown_minutes=max(0.0, _as_float(raw.get("cooldown_minutes"), 3.0)),
        )


@dataclass(slots=True)
class AntiSpamConfig:
    """防刷屏 / 防复读：群友连续刷同一句、或自己连续发同一句时先闭嘴。"""

    enabled: bool = True
    # 同一人在窗口内重复同一句话达到该次数后不再接（0 = 关闭）
    repeat_limit: int = 3
    repeat_window: float = 60.0
    # 自己的回复和上一条完全一样就跳过（防复读机）
    dedupe_replies: bool = True
    reply_history: int = 5

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AntiSpamConfig":
        raw = data or {}
        return cls(
            enabled=_as_bool(raw.get("enabled"), True),
            repeat_limit=max(0, _as_int(raw.get("repeat_limit"), 3)),
            repeat_window=max(1.0, _as_float(raw.get("repeat_window"), 60.0)),
            dedupe_replies=_as_bool(raw.get("dedupe_replies"), True),
            reply_history=max(2, _as_int(raw.get("reply_history"), 5)),
        )


@dataclass(slots=True)
class PokeConfig:
    """戳一戳自动回复（见 docs/12）。"""

    enabled: bool = True
    # 连续戳戳的统计窗口（秒）
    window_seconds: float = 120.0
    # 窗口内第几次开始改用强攻击性回复
    angry_after: int = 3

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "PokeConfig":
        raw = data or {}
        return cls(
            enabled=_as_bool(raw.get("enabled"), True),
            window_seconds=max(1.0, _as_float(raw.get("window_seconds"), 120.0)),
            angry_after=max(1, _as_int(raw.get("angry_after"), 3)),
        )


@dataclass(slots=True)
class MoodConfig:
    """心情系统：她会被夸、被戳烦、被骂，也会慢慢平复（见 docs/13）。"""

    enabled: bool = True
    baseline: float = 55.0
    decay_per_minute: float = 0.5

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "MoodConfig":
        raw = data or {}
        return cls(
            enabled=_as_bool(raw.get("enabled"), True),
            baseline=max(0.0, min(100.0, _as_float(raw.get("baseline"), 55.0))),
            decay_per_minute=max(0.01, _as_float(raw.get("decay_per_minute"), 0.5)),
        )


@dataclass(slots=True)
class RemindConfig:
    """定时提醒（见 docs/14）。"""

    enabled: bool = True
    check_interval: float = 30.0

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "RemindConfig":
        raw = data or {}
        return cls(
            enabled=_as_bool(raw.get("enabled"), True),
            check_interval=max(5.0, _as_float(raw.get("check_interval"), 30.0)),
        )


@dataclass(slots=True)
class DailyReportConfig:
    """每日群报（见 docs/15）。"""

    enabled: bool = False
    hour: int = 22
    minute: int = 30
    groups: list[int] = field(default_factory=list)
    min_messages: int = 8
    max_items: int = 3
    # 发图片版群报（图片里带标题/看点/数据脚注），失败自动退回文字
    as_image: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "DailyReportConfig":
        raw = data or {}
        return cls(
            enabled=_as_bool(raw.get("enabled"), False),
            hour=max(0, min(23, _as_int(raw.get("hour"), 22))),
            minute=max(0, min(59, _as_int(raw.get("minute"), 30))),
            groups=_as_int_list(raw.get("groups")),
            min_messages=max(1, _as_int(raw.get("min_messages"), 8)),
            max_items=max(1, min(6, _as_int(raw.get("max_items"), 3))),
            as_image=_as_bool(raw.get("as_image"), True),
        )


@dataclass(slots=True)
class AffinityConfig:
    """亲密度（见 docs/15）。"""

    enabled: bool = True
    bump_per_reply: int = 1

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AffinityConfig":
        raw = data or {}
        return cls(
            enabled=_as_bool(raw.get("enabled"), True),
            bump_per_reply=max(0, _as_int(raw.get("bump_per_reply"), 1)),
        )


@dataclass(slots=True)
class CardConfig:
    """成绩单图片（见 docs/17）。"""

    enabled: bool = True
    cooldown_minutes: float = 5.0

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "CardConfig":
        raw = data or {}
        return cls(
            enabled=_as_bool(raw.get("enabled"), True),
            cooldown_minutes=max(0.0, _as_float(raw.get("cooldown_minutes"), 5.0)),
        )


@dataclass(slots=True)
class CareConfig:
    """主动关心（见 docs/23）。"""

    enabled: bool = True
    per_user_days: float = 3.0
    per_group_daily: int = 2
    check_interval: float = 600.0
    dedupe_hours: float = 24.0

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "CareConfig":
        raw = data or {}
        return cls(
            enabled=_as_bool(raw.get("enabled"), True),
            per_user_days=max(0.0, _as_float(raw.get("per_user_days"), 3.0)),
            per_group_daily=max(0, _as_int(raw.get("per_group_daily"), 2)),
            check_interval=max(60.0, _as_float(raw.get("check_interval"), 600.0)),
            dedupe_hours=max(0.0, _as_float(raw.get("dedupe_hours"), 24.0)),
        )


@dataclass(slots=True)
class WeeklyReportConfig:
    """周报（见 docs/25）。"""

    enabled: bool = False
    weekday: int = 6
    hour: int = 21
    minute: int = 0
    groups: list[int] = field(default_factory=list)
    min_messages: int = 20
    max_items: int = 3

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "WeeklyReportConfig":
        raw = data or {}
        return cls(
            enabled=_as_bool(raw.get("enabled"), False),
            weekday=max(0, min(6, _as_int(raw.get("weekday"), 6))),
            hour=max(0, min(23, _as_int(raw.get("hour"), 21))),
            minute=max(0, min(59, _as_int(raw.get("minute"), 0))),
            groups=_as_int_list(raw.get("groups")),
            min_messages=max(1, _as_int(raw.get("min_messages"), 20)),
            max_items=max(1, min(6, _as_int(raw.get("max_items"), 3))),
        )


@dataclass(slots=True)
class SearchConfig:
    """联网搜索：关键问题自动搜，普通闲聊不搜。"""

    enabled: bool = False
    auto: bool = True
    min_chars: int = 6
    cache_seconds: float = 600.0
    max_results: int = 5

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SearchConfig":
        raw = data or {}
        return cls(
            enabled=_as_bool(raw.get("enabled"), False),
            auto=_as_bool(raw.get("auto"), True),
            min_chars=max(2, _as_int(raw.get("min_chars"), 6)),
            cache_seconds=max(0.0, _as_float(raw.get("cache_seconds"), 600.0)),
            max_results=max(1, min(10, _as_int(raw.get("max_results"), 5))),
        )


@dataclass(slots=True)
class NekoConfig:
    """大肥鱼表情包知识库：手动 /neko 另有入口，这里只控制自动穿插。"""

    enabled: bool = False
    auto_chance: float = 0.0

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "NekoConfig":
        raw = data or {}
        return cls(
            enabled=_as_bool(raw.get("enabled"), False),
            auto_chance=max(
                0.0, min(1.0, _as_float(raw.get("auto_chance"), 0.0))
            ),
        )


@dataclass(slots=True)
class ChatConfig:
    group: GroupConfig = field(default_factory=GroupConfig)
    private: PrivateConfig = field(default_factory=PrivateConfig)
    reply: ReplyConfig = field(default_factory=ReplyConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    filter: FilterConfig = field(default_factory=FilterConfig)
    admin: AdminConfig = field(default_factory=AdminConfig)
    proactive: ProactiveConfig = field(default_factory=ProactiveConfig)
    memes: MemeConfig = field(default_factory=MemeConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    self_portrait: SelfConfig = field(default_factory=SelfConfig)
    anti_spam: AntiSpamConfig = field(default_factory=AntiSpamConfig)
    poke: PokeConfig = field(default_factory=PokeConfig)
    mood: MoodConfig = field(default_factory=MoodConfig)
    remind: RemindConfig = field(default_factory=RemindConfig)
    daily_report: DailyReportConfig = field(default_factory=DailyReportConfig)
    affinity: AffinityConfig = field(default_factory=AffinityConfig)
    card: CardConfig = field(default_factory=CardConfig)
    care: CareConfig = field(default_factory=CareConfig)
    weekly_report: WeeklyReportConfig = field(default_factory=WeeklyReportConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    neko: NekoConfig = field(default_factory=NekoConfig)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ChatConfig":
        raw = data or {}
        return cls(
            group=GroupConfig.from_dict(raw.get("group")),
            private=PrivateConfig.from_dict(raw.get("private")),
            reply=ReplyConfig.from_dict(raw.get("reply")),
            memory=MemoryConfig.from_dict(raw.get("memory")),
            filter=FilterConfig.from_dict(raw.get("filter")),
            admin=AdminConfig.from_dict(raw.get("admin")),
            proactive=ProactiveConfig.from_dict(raw.get("proactive")),
            memes=MemeConfig.from_dict(raw.get("memes")),
            budget=BudgetConfig.from_dict(raw.get("budget")),
            safety=SafetyConfig.from_dict(raw.get("safety")),
            self_portrait=SelfConfig.from_dict(raw.get("self_portrait")),
            anti_spam=AntiSpamConfig.from_dict(raw.get("anti_spam")),
            poke=PokeConfig.from_dict(raw.get("poke")),
            mood=MoodConfig.from_dict(raw.get("mood")),
            remind=RemindConfig.from_dict(raw.get("remind")),
            daily_report=DailyReportConfig.from_dict(raw.get("daily_report")),
            affinity=AffinityConfig.from_dict(raw.get("affinity")),
            card=CardConfig.from_dict(raw.get("card")),
            care=CareConfig.from_dict(raw.get("care")),
            weekly_report=WeeklyReportConfig.from_dict(raw.get("weekly_report")),
            search=SearchConfig.from_dict(raw.get("search")),
            neko=NekoConfig.from_dict(raw.get("neko")),
        )


@dataclass(slots=True)
class MetricsConfig:
    """Phase 4.3 用量统计：独立 SQLite，失败不影响聊天。"""

    enabled: bool = True
    db_path: str = "data/metrics.db"
    keep_days: int = 30

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "MetricsConfig":
        raw = data or {}
        return cls(
            enabled=_as_bool(raw.get("enabled"), True),
            db_path=str(raw.get("db_path") or "data/metrics.db"),
            keep_days=max(1, _as_int(raw.get("keep_days"), 30)),
        )


@dataclass(slots=True)
class Settings:
    deepseek: DeepSeekConfig = field(default_factory=DeepSeekConfig)
    onebot: OneBotConfig = field(default_factory=OneBotConfig)
    official: OfficialConfig = field(default_factory=OfficialConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    web: WebConfig = field(default_factory=WebConfig)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)
    persona: PersonaConfig = field(default_factory=PersonaConfig)
    chat: ChatConfig = field(default_factory=ChatConfig)
    log_level: str = "INFO"

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Settings":
        raw = data or {}
        return cls(
            deepseek=DeepSeekConfig.from_dict(raw.get("deepseek")),
            onebot=OneBotConfig.from_dict(raw.get("onebot")),
            official=OfficialConfig.from_dict(raw.get("official")),
            vision=VisionConfig.from_dict(raw.get("vision")),
            web=WebConfig.from_dict(raw.get("web")),
            metrics=MetricsConfig.from_dict(raw.get("metrics")),
            persona=PersonaConfig.from_dict(raw.get("persona")),
            chat=ChatConfig.from_dict(raw.get("chat")),
            log_level=str(raw.get("log_level") or "INFO").upper(),
        )


def load_settings(path: Path | None = None) -> Settings:
    config_path = Path(path) if path else PROJECT_ROOT / "config" / "config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"找不到配置文件：{config_path}")
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"配置文件格式不对（应该是 YAML 映射）：{config_path}")
    return Settings.from_dict(data)
