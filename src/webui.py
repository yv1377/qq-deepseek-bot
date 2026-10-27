"""Phase 4.1：WebUI（FastAPI + 原生页面）。

能做的事：
- 看运行状态 / 统计 / 模型 / 图片理解 / 工具 / 主动发言状态；
- 看会话列表、最近消息、摘要、会话笔记、用户笔记；
- 临时静音某个群、清空某个会话；
- 运行时改 `ambient_chance`、主动发言开关（不写回 config.yaml，重启恢复）。

安全：
- 默认只监听 127.0.0.1；
- 配置了 `WEB_TOKEN` 后，API 需要 `Authorization: Bearer <token>` 或 `?token=`；
- 绑定非本机地址但没配 token 时拒绝启动。
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import json
import re
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel, Field

from chat import ChatService
from config import Settings

log = logging.getLogger("webui")


class MuteRequest(BaseModel):
    group_id: int
    muted: bool = True


class ResetRequest(BaseModel):
    session_id: str


class RuntimeRequest(BaseModel):
    ambient_chance: float | None = Field(default=None, ge=0.0, le=1.0)
    proactive_enabled: bool | None = None


class GallerySearch(BaseModel):
    group_id: int = Field(gt=0)
    query: str = Field(min_length=1, max_length=150)


class ReportRequest(BaseModel):
    group_id: int


class GalleryTags(BaseModel):
    tags: str = Field(max_length=200)


def create_app(
    service: ChatService,
    settings: Settings,
    token: str | None = None,
    metrics: Any | None = None,
) -> FastAPI:
    app = FastAPI(title="DeepSeek QQ Bot", docs_url=None, redoc_url=None)
    app.state.service = service
    app.state.settings = settings
    app.state.token = (token if token is not None else settings.web.token).strip()

    def require_token(request: Request) -> None:
        expected: str = app.state.token
        if not expected:
            return
        supplied = request.headers.get("authorization", "")
        if supplied.lower().startswith("bearer "):
            supplied = supplied[7:].strip()
        if not supplied:
            supplied = request.query_params.get("token", "")
        if not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="token 无效")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return INDEX_HTML

    @app.get("/api/status")
    async def status(_: None = Depends(require_token)) -> dict[str, Any]:
        data = service.runtime_status()
        data["ok"] = True
        data["vision_model"] = settings.vision.model if settings.vision.configured else ""
        data["web"] = {
            "host": settings.web.host,
            "port": settings.web.port,
        }
        return data

    @app.get("/api/usage")
    async def usage(days: int = 7, _: None = Depends(require_token)) -> dict[str, Any]:
        days = max(1, min(30, days))
        if metrics is None:
            return {"ok": True, "enabled": False, "today": None, "daily": []}
        return {
            "ok": True,
            "enabled": True,
            "today": metrics.today_summary(),
            "daily": metrics.daily(days),
        }

    @app.get("/api/sessions")
    async def sessions(
        limit: int = 20, _: None = Depends(require_token)
    ) -> dict[str, Any]:
        limit = max(1, min(100, limit))
        return {"ok": True, "sessions": await service.memory.list_sessions(limit)}

    @app.get("/api/session")
    async def session_detail(
        session_id: str, limit: int = 30, _: None = Depends(require_token)
    ) -> dict[str, Any]:
        limit = max(1, min(200, limit))
        return {
            "ok": True,
            "session_id": session_id,
            "messages": await service.memory.recent_messages(session_id, limit),
            "summary": await service.memory.get_summary(session_id),
            "notes": await service.memory.recent_notes(session_id, 20),
        }

    @app.post("/api/session/reset")
    async def reset_session(
        payload: ResetRequest, _: None = Depends(require_token)
    ) -> dict[str, Any]:
        await service.memory.reset_session(payload.session_id)
        if payload.session_id.startswith("group:"):
            try:
                group_id = int(payload.session_id.split(":", 1)[1])
            except ValueError:
                group_id = 0
            if group_id:
                service.history.clear(group_id)
        log.info("WebUI 清空会话：%s", payload.session_id)
        return {"ok": True}

    @app.post("/api/mute")
    async def mute(
        payload: MuteRequest, _: None = Depends(require_token)
    ) -> dict[str, Any]:
        service.set_group_muted(payload.group_id, payload.muted)
        log.info(
            "WebUI %s群：%s", "静音" if payload.muted else "恢复", payload.group_id
        )
        return {"ok": True, "muted_groups": service.runtime_status()["muted_groups"]}

    @app.post("/api/runtime")
    async def runtime(
        payload: RuntimeRequest, _: None = Depends(require_token)
    ) -> dict[str, Any]:
        applied = service.update_runtime(
            ambient_chance=payload.ambient_chance,
            proactive_enabled=payload.proactive_enabled,
        )
        log.info("WebUI 修改运行时参数：%s", applied)
        return {"ok": True, **applied}

    @app.get("/api/user_notes")
    async def user_notes(
        user_id: int, limit: int = 10, _: None = Depends(require_token)
    ) -> dict[str, Any]:
        limit = max(1, min(50, limit))
        return {
            "ok": True,
            "user_id": user_id,
            "notes": await service.memory.recent_user_notes(user_id, limit),
        }

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard_page():
        return Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8")

    @app.post("/api/report")
    async def report(
        payload: ReportRequest, _: None = Depends(require_token)
    ) -> dict[str, Any]:
        ok = await service.send_daily_report(payload.group_id, force=True)
        log.info("WebUI 手动群报：群 %s -> %s", payload.group_id, ok)
        return {"ok": ok}

    @app.get("/api/reminders")
    async def reminders(_: None = Depends(require_token)) -> dict[str, Any]:
        return {"items": await service.memory.all_pending_reminders(50)}

    @app.post("/api/reminders/{reminder_id}/cancel")
    async def cancel_reminder(
        reminder_id: int, _: None = Depends(require_token)
    ) -> dict[str, Any]:
        ok = await service.memory.cancel_reminder(reminder_id)
        log.info("WebUI 取消提醒 #%s：%s", reminder_id, ok)
        return {"ok": ok}

    @app.get("/api/dashboard")
    async def dashboard(_: None = Depends(require_token)) -> dict[str, Any]:
        return await service.dashboard_data()

    @app.get("/gallery", response_class=HTMLResponse)
    async def gallery_page():
        return Path(__file__).with_name("gallery.html").read_text(encoding="utf-8")

    @app.get("/api/gallery")
    async def gallery_items(group_id: int, _: None = Depends(require_token)):
        return {"items": service.memes.items(group_id),
                "vision_enabled": bool(service.vision and getattr(service.vision, "enabled", False))}

    def gallery_path(group_id: int, name: str):
        if group_id <= 0 or not re.fullmatch(r"[a-zA-Z0-9_-]+\.(png|jpg|gif|webp)", name):
            raise HTTPException(404)
        path = service.memes.root / str(group_id) / name
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(service.memes.root.resolve()):
            raise HTTPException(404)
        return path

    @app.get("/api/gallery/{group_id}/{name}")
    async def gallery_image(group_id: int, name: str, _: None = Depends(require_token)):
        return FileResponse(gallery_path(group_id, name), headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.delete("/api/gallery/{group_id}/{name}")
    async def gallery_delete(group_id: int, name: str, _: None = Depends(require_token)):
        path = gallery_path(group_id, name)
        if not service.memes.delete_by_name(group_id, path.name):
            raise HTTPException(500, "删除失败，请检查文件权限")
        log.info("WebUI 从图库删除图片：%s/%s", group_id, path.name)
        return {"ok": True}

    @app.post("/api/gallery/{group_id}/{name}/tags")
    async def gallery_tags(group_id: int, name: str, payload: GalleryTags, _: None = Depends(require_token)):
        path = gallery_path(group_id, name)
        meta = service.memes.metadata(path)
        meta["tags"] = payload.tags
        path.with_suffix(path.suffix + ".json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return {"ok": True}

    @app.post("/api/gallery/{group_id}/{name}/recognize")
    async def gallery_recognize(group_id: int, name: str, _: None = Depends(require_token)):
        path = gallery_path(group_id, name)
        if not service.vision or not getattr(service.vision, "enabled", False):
            raise HTTPException(409, "请先配置视觉模型并重启智能体")
        if path.stat().st_size > settings.chat.memes.max_bytes:
            raise HTTPException(413, "图片过大")
        try:
            description = await service.vision.describe_bytes(
                path.read_bytes(),
                on_usage=lambda usage: service.record_vision_usage(group_id, usage),
            )
        except Exception:
            raise HTTPException(502, "视觉识别失败，请检查模型配置")
        meta = service.memes.metadata(path)
        meta["description"] = description
        path.with_suffix(path.suffix + ".json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return {"ok": True, "description": description}

    @app.post("/api/gallery/search")
    async def gallery_search(payload: GallerySearch, _: None = Depends(require_token)):
        from image_search import search_images
        return {"result": await search_images(payload.query, payload.group_id, service.memes)}

    return app


class WebUIServer:
    def __init__(self, server: uvicorn.Server, task: asyncio.Task[None]):
        self.server = server
        self.task = task

    async def stop(self) -> None:
        self.server.should_exit = True
        try:
            await asyncio.wait_for(self.task, timeout=8)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass


async def start_webui(
    settings: Settings, service: ChatService, metrics: Any | None = None
) -> WebUIServer | None:
    cfg = settings.web
    if not cfg.enabled:
        return None
    if not cfg.token and not cfg.is_loopback:
        log.error(
            "WebUI 绑定 %s 但没有配置 WEB_TOKEN，为安全起见不启动", cfg.host
        )
        return None

    app = create_app(service, settings, metrics=metrics)
    config = uvicorn.Config(
        app,
        host=cfg.host,
        port=cfg.port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(), name="webui")
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.02)

    if not server.started:
        if task.done():
            exc = task.exception()
            if exc is not None:
                log.error("WebUI 启动失败：%s", exc)
        else:
            log.error("WebUI 未能在 %s:%s 启动（可能端口被占用）", cfg.host, cfg.port)
            task.cancel()
        return None

    log.info("WebUI：http://%s:%s/", cfg.host, cfg.port)
    return WebUIServer(server, task)


INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DeepSeek QQ Bot · 控制台</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: system-ui, "Segoe UI", "Microsoft YaHei", sans-serif;
         background: #12141a; color: #e6e8ee; }
  header { padding: 18px 22px; border-bottom: 1px solid #262a35;
           display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
  h1 { font-size: 18px; margin: 0; font-weight: 600; }
  .muted { color: #8b93a7; font-size: 13px; }
  main { padding: 18px 22px; display: grid; gap: 18px;
         grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); }
  .card { background: #181b23; border: 1px solid #262a35; border-radius: 10px;
          padding: 14px 16px; }
  .card h2 { font-size: 14px; margin: 0 0 10px; color: #9aa3b8; font-weight: 600; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 10px; }
  .stat { background: #11141b; border-radius: 8px; padding: 10px; }
  .stat b { display: block; font-size: 18px; margin-bottom: 2px; }
  .stat span { font-size: 12px; color: #8b93a7; }
  button { background: #2b6cff; color: #fff; border: 0; border-radius: 6px;
           padding: 7px 12px; cursor: pointer; font-size: 13px; }
  button.ghost { background: #262a35; color: #cdd3e0; }
  button.danger { background: #a33; }
  button:disabled { opacity: .5; cursor: not-allowed; }
  input { background: #11141b; color: #e6e8ee; border: 1px solid #2c3140;
          border-radius: 6px; padding: 7px 9px; font-size: 13px; }
  .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-top: 10px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  td, th { text-align: left; padding: 6px 8px; border-bottom: 1px solid #232733; }
  th { color: #8b93a7; font-weight: 500; }
  tr.clickable { cursor: pointer; }
  tr.clickable:hover { background: #1d2130; }
  #detail { grid-column: 1 / -1; }
  .msg { padding: 7px 0; border-bottom: 1px solid #232733; font-size: 13px;
         white-space: pre-wrap; word-break: break-word; }
  .who { color: #7ea2ff; font-size: 12px; margin-right: 6px; }
  .who.assistant { color: #58d68d; }
  .mono { font-family: ui-monospace, Consolas, monospace; }
  .tag { display: inline-block; background: #232733; border-radius: 4px;
         padding: 2px 6px; font-size: 12px; margin-right: 4px; }
  .mute { color: #ffb86b; }
  .err { color: #ff7a7a; font-size: 13px; margin-top: 8px; white-space: pre-wrap; }
</style>
</head>
<body>
<header>
  <h1>🐟 DeepSeek QQ Bot 控制台</h1>
  <a href="/dashboard" style="margin-left:12px;color:#8fd0f5;text-decoration:none">📊 成绩单</a>
  <span class="muted">令牌</span>
  <input id="token" type="password" placeholder="WEB_TOKEN" style="width:180px">
  <button class="ghost" onclick="saveToken()">保存</button>
  <button onclick="refreshAll()">刷新</button>
  <span id="hint" class="muted"></span>
</header>

<main>
  <section class="card">
    <h2>运行状态</h2>
    <div class="grid" id="stats"></div>
    <div class="row" id="flags"></div>
    <div class="err" id="error"></div>
  </section>

  <section class="card">
    <h2>运行时开关</h2>
    <div class="row">
      <label class="muted">主动插话概率</label>
      <input id="ambient" type="number" step="0.005" min="0" max="1" style="width:100px">
      <button onclick="saveRuntime()">保存</button>
    </div>
    <div class="row">
      <label class="muted">
        <input id="proactive" type="checkbox" style="vertical-align:-1px">
        主动发言（需要 config 里已配置 groups）
      </label>
    </div>
    <p class="muted" style="font-size:12px;margin:8px 0 0">
      这里只改内存，不写回 config.yaml，重启恢复。
    </p>
  </section>

  <section class="card">
    <h2>会话（最近活跃）</h2>
    <table>
      <thead><tr><th>会话</th><th>条数</th><th>最后活跃</th></tr></thead>
      <tbody id="sessions"></tbody>
    </table>
  </section>

  <section class="card">
    <h2>用量（近 7 天）</h2>
    <div id="usageToday" class="muted" style="font-size:13px"></div>
    <table style="margin-top:8px">
      <thead><tr><th>日期</th><th>调用</th><th>tokens</th><th>均耗时</th></tr></thead>
      <tbody id="usage"></tbody>
    </table>
  </section>

  <section class="card" id="detail">
    <h2>会话详情 <span class="muted mono" id="detailTitle"></span></h2>
    <div class="row">
      <button class="ghost" id="muteBtn" onclick="toggleMute()" style="display:none">静音该群</button>
      <button class="danger" id="resetBtn" onclick="resetSession()" style="display:none">清空该会话</button>
    </div>
    <div id="summary" class="muted" style="margin-top:10px"></div>
    <div id="notes" style="margin-top:8px"></div>
    <div id="messages" style="margin-top:10px"></div>
  </section>
</main>

<script>
const $ = (id) => document.getElementById(id);
let token = localStorage.getItem("dsh_web_token") || "";
let current = null;
let mutedGroups = [];

function saveToken() {
  token = $("token").value.trim();
  localStorage.setItem("dsh_web_token", token);
  refreshAll();
}

function headers() {
  const h = { "Content-Type": "application/json" };
  if (token) h["Authorization"] = "Bearer " + token;
  return h;
}

async function api(path, options = {}) {
  const resp = await fetch(path, { ...options, headers: headers() });
  if (resp.status === 401) throw new Error("token 无效，请在右上角填写 WEB_TOKEN");
  if (!resp.ok) throw new Error("HTTP " + resp.status + " " + (await resp.text()));
  return resp.json();
}

function fmtDuration(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return h > 0 ? h + " 小时 " + m + " 分" : m + " 分 " + (sec % 60) + " 秒";
}

function fmtTime(ts) {
  if (!ts) return "-";
  return new Date(ts * 1000).toLocaleString();
}

function renderStats(s, flags) {
  const stats = s.stats || {};
  const items = [
    ["运行时长", fmtDuration(s.uptime_seconds)],
    ["收到消息", stats.received || 0],
    ["已回复", stats.replied || 0],
    ["主动发言", stats.proactive || 0],
    ["跳过", stats.skipped || 0],
    ["出错", stats.errors || 0],
  ];
  if (s.usage) {
    items.push(
      ["今日调用", s.usage.calls || 0],
      ["今日 tokens", (s.usage.total_tokens || 0).toLocaleString()],
      ["平均耗时", (s.usage.avg_duration_ms || 0) + " ms"],
    );
  }
  $("stats").innerHTML = items.map(([k, v]) =>
    `<div class="stat"><b>${v}</b><span>${k}</span></div>`).join("");
  const tags = [
    ["模型", s.model], ["人格", s.persona],
    ["工具", s.tools_enabled ? "开" : "关"],
    ["图片理解", s.vision_enabled ? "开" : "关"],
    ["主动发言", s.proactive && s.proactive.enabled ? "开" : "关"],
    ["插话概率", s.ambient_chance],
    ["配图概率", s.memes_chance],
    ["频率保护", s.reply_burst && s.reply_burst.limit
      ? s.reply_burst.limit + " 条/" + s.reply_burst.window_seconds + " 秒"
      : "关"],
    ["每日预算", s.budget && s.budget.enabled
      ? s.budget.daily_cny_per_group + " 元/群"
      : "关"],
  ];
  $("flags").innerHTML = tags.map(([k, v]) =>
    `<span class="tag">${k}: ${v}</span>`).join("");
  $("ambient").value = s.ambient_chance;
  $("proactive").checked = !!(s.proactive && s.proactive.enabled);
  mutedGroups = s.muted_groups || [];
  $("hint").textContent = s.ok ? "" : "";
}

async function refreshAll() {
  try {
    $("error").textContent = "";
    const s = await api("/api/status");
    renderStats(s, null);
    await loadSessions();
    await loadUsage();
    if (current) await openSession(current);
  } catch (e) {
    $("error").textContent = String(e.message || e);
  }
}

async function loadSessions() {
  const data = await api("/api/sessions");
  $("sessions").innerHTML = (data.sessions || []).map((row) => {
    const muted = row.session_id.startsWith("group:") &&
      mutedGroups.includes(Number(row.session_id.split(":")[1]));
    return `<tr class="clickable" onclick="openSession('${row.session_id}')">
      <td class="mono">${row.session_id}${muted ? ' <span class="tag mute">静音</span>' : ''}</td>
      <td>${row.count}</td><td>${fmtTime(row.last_ts)}</td></tr>`;
  }).join("") || `<tr><td colspan="3" class="muted">还没有会话</td></tr>`;
}

async function loadUsage() {
  const data = await api("/api/usage?days=7");
  if (!data.enabled) {
    $("usageToday").textContent = "用量统计未开启";
    $("usage").innerHTML = "";
    return;
  }
  const today = data.today || {};
  $("usageToday").textContent =
    `今日 ${today.calls || 0} 次调用，token ${(today.total_tokens || 0).toLocaleString()}，` +
    `平均 ${today.avg_duration_ms || 0} ms`;
  $("usage").innerHTML = (data.daily || []).map((row) =>
    `<tr><td>${row.date}</td><td>${row.calls}</td>` +
    `<td>${(row.total_tokens || 0).toLocaleString()}</td>` +
    `<td>${row.avg_duration_ms || 0} ms</td></tr>`).join("");
}

async function openSession(id) {
  current = id;
  const data = await api("/api/session?session_id=" + encodeURIComponent(id));
  $("detailTitle").textContent = id;
  $("summary").textContent = data.summary ? "摘要：" + data.summary : "（暂无摘要）";
  $("notes").innerHTML = (data.notes || []).map((n) =>
    `<div class="tag">笔记：${escapeHtml(n)}</div>`).join("");
  $("messages").innerHTML = (data.messages || []).map((m) =>
    `<div class="msg"><span class="who ${m.role === "assistant" ? "assistant" : ""}">` +
    `${escapeHtml(m.name || m.role)}</span>${escapeHtml(m.content)}</div>`).join("")
    || `<div class="muted">还没有消息</div>`;
  const isGroup = id.startsWith("group:");
  $("muteBtn").style.display = isGroup ? "inline-block" : "none";
  $("resetBtn").style.display = "inline-block";
  if (isGroup) {
    const gid = Number(id.split(":")[1]);
    const muted = mutedGroups.includes(gid);
    $("muteBtn").textContent = muted ? "恢复该群" : "静音该群";
  }
}

async function toggleMute() {
  if (!current || !current.startsWith("group:")) return;
  const gid = Number(current.split(":")[1]);
  const muted = !mutedGroups.includes(gid);
  await api("/api/mute", { method: "POST", body: JSON.stringify({ group_id: gid, muted }) });
  await refreshAll();
}

async function resetSession() {
  if (!current) return;
  if (!confirm("确定清空 " + current + " 的消息、摘要和会话笔记？")) return;
  await api("/api/session/reset", { method: "POST", body: JSON.stringify({ session_id: current }) });
  await refreshAll();
}

async function saveRuntime() {
  const body = {
    ambient_chance: Number($("ambient").value),
    proactive_enabled: $("proactive").checked,
  };
  await api("/api/runtime", { method: "POST", body: JSON.stringify(body) });
  await refreshAll();
}

function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

$("token").value = token;
refreshAll();
</script>
</body>
</html>
"""
