# 윈도우 설치: 코드 받기 → 파이썬 가상환경 → 패키지 → (선택) 설정 마법사
# 봇을 시작하거나 자동 시작을 등록하지는 않는다 (나중에 start.ps1 로).
# 사용 (PowerShell):
#   irm https://raw.githubusercontent.com/LeePillWoo/Trading-bot/ccr-b7de564b-nfauhm/deploy/windows/install.ps1 | iex
# 다시 실행하면 코드만 최신으로 덮어쓴다 (.env, 상태 파일, 로그는 그대로).
$ErrorActionPreference = "Stop"
$Branch = "ccr-b7de564b-nfauhm"
$Dir = Join-Path $env:USERPROFILE "Trading-bot"

Write-Host "== 1/4 파이썬 확인"
$py = $null
foreach ($c in @("py", "python")) {
    try { & $c -3 --version *> $null; if ($LASTEXITCODE -eq 0) { $py = $c; break } } catch {}
    try { & $c --version *> $null; if ($LASTEXITCODE -eq 0) { $py = $c; break } } catch {}
}
if (-not $py) {
    Write-Host "파이썬이 없습니다. https://www.python.org/downloads/ 에서 설치하세요."
    Write-Host "설치 첫 화면에서 'Add python.exe to PATH' 를 반드시 체크한 뒤, 이 명령을 다시 실행하세요."
    return
}

Write-Host "== 2/4 코드 받기 ($Dir)"
$zip = Join-Path $env:TEMP "trading-bot.zip"
$tmp = Join-Path $env:TEMP "trading-bot-src"
Invoke-WebRequest "https://github.com/LeePillWoo/Trading-bot/archive/refs/heads/$Branch.zip" -OutFile $zip
if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
Expand-Archive $zip -DestinationPath $tmp
$src = Get-ChildItem $tmp | Select-Object -First 1
New-Item -ItemType Directory -Force -Path $Dir | Out-Null
# robocopy: 기존 폴더에 병합 복사 (.env/상태 파일/로그는 받은 코드에 없으므로 그대로 남음)
robocopy $src.FullName $Dir /E /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "코드 복사 실패 (robocopy $LASTEXITCODE)" }
Remove-Item $zip, $tmp -Recurse -Force
Set-Location $Dir

Write-Host "== 3/4 가상환경과 패키지"
if (-not (Test-Path ".venv")) { & $py -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install --upgrade pip -q
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt -q
& .\.venv\Scripts\python.exe -c "import pandas, numpy, requests, zoneinfo; zoneinfo.ZoneInfo('Asia/Seoul'); print('   패키지 OK')"

Write-Host "== 4/4 설정"
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env" }
$answer = Read-Host "지금 설정 마법사를 실행할까요? (휴대폰과 같은 키를 입력) [y/N]"
if ($answer -eq "y") { & .\.venv\Scripts\python.exe -m trading_bot setup }

Write-Host ""
Write-Host "설치 완료 (봇은 아직 꺼져 있습니다). 컴퓨터로 옮길 때 순서는 WINDOWS.md 를 보세요."
Write-Host "  시작:  powershell -ExecutionPolicy Bypass -File `"$Dir\deploy\windows\start.ps1`""
Write-Host "  중지:  powershell -ExecutionPolicy Bypass -File `"$Dir\deploy\windows\stop.ps1`""
