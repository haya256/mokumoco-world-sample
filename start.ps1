# もくもくカメラを起動し、Cloudflare のクイックトンネルで公開する(Windows 用)
# 使い方: powershell -ExecutionPolicy Bypass -File start.ps1
param(
    [string]$Python = "python",
    [string]$Cloudflared = "cloudflared"
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$missing = @(
    @{ Cmd = $Python; Hint = "Python 3.11 以上を入れてください(winget install Python.Python.3.12)" },
    @{ Cmd = $Cloudflared; Hint = "winget install Cloudflare.cloudflared で入れ、PowerShell を開き直してください" }
) | Where-Object { -not (Get-Command $_.Cmd -ErrorAction SilentlyContinue) }
if ($missing) {
    foreach ($m in $missing) { Write-Host "$($m.Cmd) が見つかりません。$($m.Hint)" -ForegroundColor Red }
    exit 1
}

$port = 8787
$adminPort = 8788
if (Test-Path config.json) {
    $cfg = Get-Content config.json -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($cfg.port) { $port = $cfg.port }
    if ($cfg.admin_port) { $adminPort = $cfg.admin_port }
}

$log = Join-Path $env:TEMP "mokumoco-camera-tunnel.log"
Remove-Item $log -ErrorAction SilentlyContinue
$server = $null
$tunnel = $null
try {
    $server = Start-Process $Python -ArgumentList "camera_world.py" -NoNewWindow -PassThru
    # 公開用のポートだけをトンネルに渡す(管理用のポートは渡さない)
    $tunnel = Start-Process $Cloudflared -ArgumentList "tunnel", "--url", "http://127.0.0.1:$port" `
        -NoNewWindow -PassThru -RedirectStandardError $log

    $url = $null
    for ($i = 0; $i -lt 60 -and -not $url; $i++) {
        Start-Sleep -Seconds 1
        if (Test-Path $log) {
            $m = Select-String -Path $log -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' | Select-Object -First 1
            if ($m) { $url = $m.Matches[0].Value }
        }
    }
    Write-Host ""
    if ($url) {
        Write-Host "公開URL: $url" -ForegroundColor Green
        Write-Host "  → もくもく会チャットの「ワールド設定」でこのURLを登録してください(起動のたびに変わります)"
    } else {
        Write-Host "トンネルのURLが取れませんでした。$log を確認してください" -ForegroundColor Yellow
    }
    Write-Host "管理画面: http://127.0.0.1:$adminPort"
    Write-Host "止めるときは Ctrl+C"
    Write-Host ""
    while (-not $server.HasExited -and -not $tunnel.HasExited) { Start-Sleep -Seconds 1 }
} finally {
    foreach ($p in @($tunnel, $server)) {
        if ($p -and -not $p.HasExited) { Stop-Process -Id $p.Id -Force }
    }
}
