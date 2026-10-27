# 21 · WebUI 提醒管理

> 提醒功能之前只能在群里加、在群里删；这一轮把「近期提醒」搬到了网页上，能直接点取消。

---

## 一、成绩单页面（`/dashboard`）

「待办提醒」卡片现在会列出**所有会话**未完成的提醒：

```
未完成提醒  2
#3  10-25 22:00  提醒 770 睡觉           [取消]
#4  10-26 08:00  提醒小明交报告          [取消]
```

- 点「取消」会调 `POST /api/reminders/{id}/cancel`，成功后自动刷新；
- 取消不存在的提醒返回 `{"ok": false}`，不会误报成功。

## 二、新增接口

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/reminders` | 列出所有会话未完成提醒（id/session_id/user_id/content/due_at） |
| POST | `/api/reminders/{id}/cancel` | 取消指定提醒（管理员 token 即可，跨会话） |

两个接口都走 `require_token`，与其它 WebUI 接口一致。

## 三、实现

- `src/memory.py`：新增 `all_pending_reminders()`、`cancel_reminder()`（先查后删，不存在返回 False）；
- `src/chat.py`：`dashboard_data()` 的 `reminders` 从「只有数量」扩成 `{pending, items[]}`（最多 20 条，带易读时间）；
- `src/webui.py`：两个新端点；
- `src/dashboard.html`：待办卡片改成列表 + 取消按钮（`button.ghost` 样式）、`api()` 支持传 method。

## 四、验证

```powershell
cd D:\蓝瑟大肥鱼\qq-deepseek-bot
python scripts/smoke_test.py     # 全量 312 项，其中 [35] WebUI 提醒管理 9 项
```

`[35]` 覆盖：列表、管理员取消、取消后为空、取消不存在返回 False、成绩单含提醒明细、
接口列出/取消/清空（ASGI 内测）、页面里有取消按钮。

## 五、涉及文件

| 文件 | 改动 |
| --- | --- |
| `src/memory.py` | `all_pending_reminders()`、`cancel_reminder()` |
| `src/chat.py` | `dashboard_data()` 提醒明细 |
| `src/webui.py` | `/api/reminders`、`/api/reminders/{id}/cancel` |
| `src/dashboard.html` | 待办提醒列表 + 取消按钮 |
| `scripts/smoke_test.py` | 新增 `[35]` 9 项（全量 312 项） |

## 六、下一步

群报图片叠今日词云；或 WebUI 里加「手动触发一次群报」按钮。
