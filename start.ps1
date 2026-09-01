# toss-trader 시작 (Windows PowerShell)
# 사용: .\start.ps1        → 드라이런
#       .\start.ps1 live   → 실전 (.env LIVE_TRADING=1 필요)
# 처음 실행 시 스크립트 차단이 뜨면(관리자 아님, 현재 사용자만):
#   Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
param([string]$Mode = "")

Set-Location $PSScriptRoot
New-Item -ItemType Directory -Force -Path logs | Out-Null

# 이전 로그 보존 (Start-Process 리다이렉트는 덮어쓰기)
$log = "logs\watch.log"
if (Test-Path $log) { Move-Item -Force $log "logs\watch.log.1" }

# 한글/이모지 로그가 cp949 인코딩 에러로 봇을 죽이지 않도록 UTF-8 강제
$env:PYTHONUTF8 = "1"

$flags = "--watch"
if ($Mode -eq "live") {
    $flags = "--watch --live"
    Write-Host "⚠️ 실전 모드 — 실제 주문이 나갑니다!" -ForegroundColor Yellow
}

# 창을 닫아도 유지되는 백그라운드 실행 (숨김 창)
$p = Start-Process -FilePath "python" `
    -ArgumentList "-u run_dryrun.py $flags" `
    -RedirectStandardOutput $log -RedirectStandardError "logs\watch.err.log" `
    -WindowStyle Hidden -PassThru
$p.Id | Out-File -Encoding ascii "logs\bot.pid"

Write-Host "✅ 시작됨 (PID $($p.Id))"
Write-Host "   로그 보기:  Get-Content logs\watch.log -Wait -Tail 30"
Write-Host "   중지:       .\stop.ps1   (폰에서는 텔레그램 /stop /flat)"
Write-Host "   ⚠️ 절전 방지: 설정 > 시스템 > 전원 > '전원 연결 시 절대 안 함' 권장"
