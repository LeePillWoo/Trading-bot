# 봇 중지 + 자동 시작 해제
$task = Get-ScheduledTask -TaskName "TradingBot" -ErrorAction SilentlyContinue
if ($task) {
    Stop-ScheduledTask -TaskName "TradingBot"
    Unregister-ScheduledTask -TaskName "TradingBot" -Confirm:$false
}
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*trading_bot service*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Write-Host "봇을 중지했고 자동 시작도 해제했습니다."
