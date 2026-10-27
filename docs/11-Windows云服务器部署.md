# 11 · 部署到 Windows 云服务器（NapCat + 大肥鱼）

> 适用：腾讯云 / 阿里云 / 华为云的 **Windows Server** 轻量应用服务器。
> 本地环境是 Windows 10 家庭版 + Python 3.14；云服务器用 Windows Server 2022 即可。
> 目标：NapCat 和 Bot 跑在同一台服务器上，浏览器打开 `http://公网IP:8080/?token=...` 管理。

## 一、买服务器（用户付款）

| 项目 | 推荐值 | 说明 |
| --- | --- | --- |
| 厂商 | 腾讯云轻量 / 阿里云轻量 | 国内节点，QQ 登录更稳 |
| 镜像 | **Windows Server 2022 数据中心版 64位 中文** | 和本机 Windows 操作一致 |
| 配置 | **2核 4G**、系统盘 60~80G、带宽 5Mbps 左右 | 2核2G 能跑但偏紧；2核4G 稳 |
| 时长 | 先买 1 个月，稳定后续费 | 新用户可看 1 年活动 |
| 地域 | 离自己近的（上海/北京/广州/成都） | 降低延迟 |
| 登录 | 设置 Administrator 密码 | 远程桌面用 |

购买页：

- 腾讯云轻量：<https://buy.cloud.tencent.com/lighthouse>
- 腾讯云轻量活动：<https://cloud.tencent.com/act/pro/lhsale>
- 阿里云轻量：<https://www.aliyun.com/product/swas>

## 二、连上服务器

1. 在控制台找到实例的 **公网 IP**；
2. 本机按 `Win + R` 输入 `mstsc`，连接 `公网IP:3389`，用 Administrator 密码登录；
3. 控制台安全组先放行 **3389**（建议只对自己的 IP 开放）。

## 三、装 QQ + NapCat

1. 服务器里安装官方 QQ（Windows 版），先 **扫码登录机器人 QQ 1234567890**；
2. 下载 NapCat Shell Windows 版：<https://github.com/NapNeko/NapCatQQ/releases/latest>，
   解压后按文档运行 `launcher.bat` 或 Windows 一键安装器：
   <https://napneko.github.io/guide/boot/Shell>
3. NapCat 的 WebUI 默认在 `http://127.0.0.1:6099`，在服务器里打开，登录后：
   - 网络配置 → 新建 **WebSocket 服务器**；
   - 端口 `3001`，Host `127.0.0.1`；
   - `messagePostFormat` 选 **array**；
   - 保存并启用。
4. 如果 NapCat 提示扫码，用手机 QQ 扫服务器屏幕上的二维码登录。

## 四、装 Python 和项目

1. 安装 Python 3.12+（3.13/3.14 也可以）：<https://www.python.org/downloads/windows/>
   - 安装时勾选 **Add python.exe to PATH**。
2. 把本机的 `qq-deepseek-bot` 整个文件夹复制到服务器，例如 `C:\qq-deepseek-bot`。
   - 用远程桌面的剪贴板/共享磁盘传压缩包即可；
   - 不要带 `__pycache__`、`data\backups`、`data\smoke*` 这些测试文件。
3. 在服务器 PowerShell 里：

```powershell
cd C:\qq-deepseek-bot
python -m pip install -r requirements.txt
```

## 五、改配置（关键）

用记事本打开 `C:\qq-deepseek-bot\config\config.yaml`：

```yaml
onebot:
  mode: forward
  ws_url: ws://127.0.0.1:3001   # NapCat 和 Bot 在同一台机器，保持本机地址

web:
  enabled: true
  host: 0.0.0.0                  # 改成 0.0.0.0，浏览器才能从公网访问
  port: 8080
  token_env: WEB_TOKEN
```

确认 `C:\qq-deepseek-bot\.env` 里有：

```env
DEEPSEEK_API_KEY=你的密钥
VISION_API_KEY=你的密钥（可留空，默认复用 DEEPSEEK_API_KEY）
WEB_TOKEN=自己设一个长随机密码
```

> WebUI 绑定 `0.0.0.0` 时**必须**有 `WEB_TOKEN`，否则程序会拒绝启动。
> 不要把自己的 API Key 发到群里或截图里。

## 六、安全组 / 防火墙

在云控制台的安全组放行：

| 端口 | 用途 | 建议 |
| --- | --- | --- |
| 3389 | 远程桌面 | 只对自己的 IP |
| 8080 | Bot WebUI | 对公网开放，但一定带 `WEB_TOKEN` |
| 6099 | NapCat WebUI | 尽量只对自己的 IP，或改走 SSH/内网 |
| 3001 | OneBot WebSocket | **绝对不要对公网开放**，只给本机 |

Windows 防火墙也放行 8080（管理员 PowerShell）：

```powershell
New-NetFirewallRule -DisplayName "DeepSeek Bot WebUI" -Direction Inbound -Protocol TCP -LocalPort 8080 -Action Allow
```

## 七、启动

```powershell
cd C:\qq-deepseek-bot
python scripts\startup.py
```

看到类似日志即成功：

```text
已连接 OneBot 服务：ws://127.0.0.1:3001
机器人账号：蓝瑟大肥鱼（1234567890）
WebUI：http://0.0.0.0:8080/
```

浏览器访问：

- Bot 管理：`http://公网IP:8080/?token=你的WEB_TOKEN`
- NapCat 管理：`http://公网IP:6099`（或服务器内 `http://127.0.0.1:6099`）

## 八、设置开机自启（可选）

1. 新建 `C:\qq-deepseek-bot\start_cloud.bat`：

```bat
@echo off
cd /d C:\qq-deepseek-bot
python scripts\startup.py
```

2. 任务计划程序 → 创建任务 → 触发器“登录时/开机时” → 操作指向 `start_cloud.bat`；
3. 勾选“不管用户是否登录都要运行”（需要保存密码）。

## 九、常见问题

- **Bot 一直提示连接断开**：NapCat 的 WebSocket 端口没启用，或端口不是 3001；
- **浏览器打不开 8080**：安全组/Windows 防火墙没放行，或 `web.host` 还是 `127.0.0.1`；
- **启动报“拒绝绑定非本机地址”**：`.env` 里没有 `WEB_TOKEN`；
- **QQ 登录被风控**：尽量用国内节点、固定 IP，按提示扫码/验证；不要频繁换服务器；
- **NapCat 更新**：保留 `data` 配置目录，替换程序后重启即可。
