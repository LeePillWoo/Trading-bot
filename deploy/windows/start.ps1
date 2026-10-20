# 봇 시작 + 윈도우 로그인 시 자동 시작 등록 (작업 스케줄러 "TradingBot")
# ⚠️ 휴대폰 봇을 먼저 끄고, /export → import-state 로 상태를 옮긴 뒤 실행하세요.
$ErrorActionPreference = "Stop"
$Dir = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$run = Join-Path $PSScriptRoot "run.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$run`"" -WorkingDirectory $Dir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Days 0) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "TradingBot" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName "TradingBot"
Write-Host "봇을 시작했고, 윈도우에 로그인할 때마다 자동으로 시작됩니다. 텔레그램 시작 메시지를 확인하세요."
