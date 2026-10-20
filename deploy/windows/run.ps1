# 작업 스케줄러가 실행: 봇이 죽으면 30초 뒤 다시 시작
$Dir = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
Set-Location $Dir
New-Item -ItemType Directory -Force -Path "logs" | Out-Null
while ($true) {
    & .\.venv\Scripts\python.exe -m trading_bot service *>> "logs\service.log"
    Start-Sleep -Seconds 30
}
