# 本机 → Git 远程仓库（Gitee/GitHub）一键推送
#
# 首次使用前需要先有仓库（私有即可）：
#   Gitee：新建仓库 → 复制地址 https://gitee.com/你的用户名/qq-deepseek-bot.git
#   GitHub：https://github.com/你的用户名/qq-deepseek-bot.git
# 令牌（项目令牌/个人访问令牌，只需 repo 写权限）不要写进代码，用参数或环境变量传入。
#
# 用法：
#   .\scripts\push_to_git.ps1 -RepoUrl "https://gitee.com/you/qq-deepseek-bot.git" -Token "your_token"
#   $env:GIT_PUSH_TOKEN="xxx"; .\scripts\push_to_git.ps1 -RepoUrl "..."
#
# 说明：只推送代码（data/.ssh/.venv 已在 .gitignore 排除）。
# 本机环境注意：全局 git 配了死代理 + Windows schannel 不可用，脚本里统一加
#   -c http.proxy= -c https.proxy= -c http.sslBackend=openssl
# 跳过代理并改用 OpenSSL 后端，否则会报 SEC_E_NO_CREDENTIALS。

param(
    [Parameter(Mandatory = $true)][string]$RepoUrl,
    [string]$Token = $env:GIT_PUSH_TOKEN,
    [string]$Branch = "master",
    [string]$Message = ""
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $root

if (-not $Token) { throw "缺少 Token（-Token 或环境变量 GIT_PUSH_TOKEN）" }
$authUrl = $RepoUrl -replace '^https://', "https://oauth2:$Token@"
$gitArgs = @("-c", "http.proxy=", "-c", "https.proxy=", "-c", "http.sslBackend=openssl")

git config user.name "dsh-agent" | Out-Null
git config user.email "dsh@local" | Out-Null

$existing = git remote 2>$null
if ($LASTEXITCODE -ne 0 -or -not $existing) {
    git remote add origin $authUrl
} else {
    git remote set-url origin $authUrl
}

git add -A
if (-not $Message) { $Message = "sync: $(Get-Date -Format 'yyyy-MM-dd HH:mm')" }
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
    git commit -m $Message
} else {
    Write-Host "没有新的改动，直接推送现有提交"
}

Write-Host "==> 推送到 $RepoUrl ($Branch)"
git @gitArgs push -u origin $Branch 2>&1 | Select-Object -Last 6
if ($LASTEXITCODE -ne 0) { throw "推送失败（检查仓库地址/令牌权限）" }
Write-Host "==> 推送完成"
