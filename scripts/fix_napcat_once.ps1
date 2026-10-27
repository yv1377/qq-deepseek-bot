# 一次性修复：清 QQ → 注入 NapCat 文件 → 以管理员重启 NapCat（需 UAC 确认）
$ErrorActionPreference = 'SilentlyContinue'

Write-Output '[1/3] 结束所有 QQ / NapCat 进程...'
taskkill /F /IM QQ.exe /T | Out-Null
taskkill /F /IM NapCatWinBootMain.exe /T | Out-Null
Start-Sleep -Seconds 2

Write-Output '[2/3] 把 loadNapCat.js 注入 QQ 的 resources\app ...'
$copied = 0
Get-ChildItem 'C:\Program Files\Tencent\QQNT\versions' -Directory |
    ForEach-Object {
        $app = Join-Path $_.FullName 'resources\app'
        if (Test-Path $app) {
            Copy-Item 'D:\NapCat\loadNapCat.js' (Join-Path $app 'loadNapCat.js') -Force
            if ($?) { $copied += 1 }
        }
    }
Write-Output "      注入完成：$copied 个目录"

Write-Output '[3/3] 启动 NapCat（会拉起 QQ，请用手机 QQ 扫码登录 1234567890）...'
Start-Process 'D:\NapCat\launcher.bat' -WorkingDirectory 'D:\NapCat'
Start-Sleep -Seconds 5
Write-Output '完成。NapCat 控制台窗口会显示二维码。'
