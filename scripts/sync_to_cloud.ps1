# 把本机代码同步到云端服务器（Windows → Linux/macOS SSH）
#
# 用法：
#   .\sync_to_cloud.ps1                      # 只同步代码
#   .\sync_to_cloud.ps1 -Restart             # 同步后远程重启 bot
#   .\sync_to_cloud.ps1 -Server 1.2.3.4 -User root -RemotePath /opt/qq-deepseek-bot
#   .\sync_to_cloud.ps1 -KeyFile C:\Users\you\.ssh\id_ed25519   # 用密钥登录
#
# 依赖：Windows 自带的 ssh / scp（OpenSSH Client）。首次连接会问 yes，按提示输入即可。
# 说明：不同步 data/（云端自己的数据库/表情包/画像），不同步 .venv/.git/__pycache__。

param(
    [string]$Server = "123.45.67.89",
    [string]$User = "root",
    [int]$Port = 22,
    [string]$RemotePath = "/opt/qq-deepseek-bot",
    [string]$KeyFile = "",
    [string]$RestartCommand = "cd $RemotePath && (test -f restart.sh && bash restart.sh || echo '请先在云端准备 restart.sh')",
    [switch]$Restart,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$sshTarget = "$User@$Server"
$sshArgs = @("-p", "$Port", "-o", "StrictHostKeyChecking=accept-new")
$scpArgs = @("-P", "$Port", "-o", "StrictHostKeyChecking=accept-new")
if ($KeyFile) {
    $sshArgs += @("-i", $KeyFile)
    $scpArgs += @("-i", $KeyFile)
}

Write-Host "==> 项目目录: $projectRoot"
Write-Host "==> 目标: $sshTarget : $RemotePath"

# 需要同步的内容（只同步代码与配置，不覆盖云端数据）
$items = @(
    "src",
    "scripts",
    "config",
    "docs",
    "requirements.txt",
    "README.md"
)
# 只同步存在的项
$present = $items | Where-Object { Test-Path (Join-Path $projectRoot $_) }
if (-not $present) { throw "没有可同步的文件，检查脚本位置" }

if ($DryRun) {
    Write-Host "[DryRun] 将同步：" ($present -join ", ")
    return
}

# 1) 确保远端目录存在
& ssh @sshArgs $sshTarget "mkdir -p '$RemotePath'"
if ($LASTEXITCODE -ne 0) { throw "SSH 连接失败（检查 IP/用户/端口/密码或密钥）" }

# 2) 逐个 scp（先打包临时 tar 再传，避免逐文件慢；Windows 有 tar）
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$tarPath = Join-Path $env:TEMP "bot-sync-$stamp.tar.gz"
Push-Location $projectRoot
try {
    Write-Host "==> 打包代码..."
    & tar -czf $tarPath @present
    if ($LASTEXITCODE -ne 0) { throw "打包失败" }

    Write-Host "==> 上传到云端..."
    & scp @scpArgs $tarPath "${sshTarget}:/tmp/bot-sync.tar.gz"
    if ($LASTEXITCODE -ne 0) { throw "上传失败" }

    Write-Host "==> 云端解包覆盖（保留 data/）..."
    $remoteCmd = "cd '$RemotePath' && tar -xzf /tmp/bot-sync.tar.gz && rm -f /tmp/bot-sync.tar.gz && echo '同步完成'"
    & ssh @sshArgs $sshTarget $remoteCmd
    if ($LASTEXITCODE -ne 0) { throw "云端解包失败" }
}
finally {
    Pop-Location
    if (Test-Path $tarPath) { Remove-Item $tarPath -Force -ErrorAction SilentlyContinue }
}

# 3) 可选：远程重启
if ($Restart) {
    Write-Host "==> 远程重启 bot..."
    & ssh @sshArgs $sshTarget $RestartCommand
}

Write-Host "==> 全部完成"
