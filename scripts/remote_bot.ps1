# 远程查看/控制云端 bot（通过 SSH，适合 Linux 云服务器）
#
# 用法：
#   .\remote_bot.ps1                      # 看进程和端口
#   .\remote_bot.ps1 -Action logs         # 看最近日志
#   .\remote_bot.ps1 -Action restart      # 重启 bot
#   .\remote_bot.ps1 -Action shell        # 打开交互式 SSH
#   .\remote_bot.ps1 -Server 1.2.3.4 -User ubuntu -KeyFile C:\Users\you\.ssh\id_ed25519
#
# 注意：云端部署方式不同时，用 -StartCommand / -StopCommand 覆盖默认命令。

param(
    [string]$Server = "123.45.67.89",
    [string]$User = "root",
    [int]$Port = 22,
    [string]$RemotePath = "/opt/qq-deepseek-bot",
    [string]$KeyFile = "",
    [string]$StartCommand = "",
    [string]$StopCommand = "",
    [ValidateSet("status", "logs", "start", "stop", "restart", "shell")]
    [string]$Action = "status"
)

$ErrorActionPreference = "Stop"
$sshTarget = "$User@$Server"
$sshArgs = @("-p", "$Port", "-o", "StrictHostKeyChecking=accept-new")
if ($KeyFile) { $sshArgs += @("-i", $KeyFile) }

if (-not $StartCommand) {
    $StartCommand = "cd '$RemotePath' && mkdir -p data && (nohup python3 scripts/startup.py >> data/bot.log 2>&1 &) && echo started"
}
if (-not $StopCommand) {
    $StopCommand = "pkill -f 'scripts/startup.py' && echo stopped || echo '没有找到运行中的 bot'"
}

$commands = @{
    status  = "echo '--- 进程 ---'; ps aux | grep -E 'startup\.py|napcat|NapCat' | grep -v grep || true; echo '--- 端口 ---'; (ss -ltnp 2>/dev/null || netstat -ltnp 2>/dev/null) | grep -E ':3001|:8080' || true"
    logs    = "cd '$RemotePath' && tail -n 100 data/bot.log"
    start   = $StartCommand
    stop    = $StopCommand
    restart = "$StopCommand; sleep 2; $StartCommand"
    shell   = ""
}

switch ($Action) {
    "shell" {
        Write-Host "==> 连接 $sshTarget（输入 exit 退出）"
        & ssh @sshArgs $sshTarget
        break
    }
    default {
        Write-Host "==> $sshTarget : $Action"
        & ssh @sshArgs $sshTarget $commands[$Action]
    }
}
