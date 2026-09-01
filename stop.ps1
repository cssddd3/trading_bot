# toss-trader 중지 (Windows PowerShell)
Set-Location $PSScriptRoot
if (Test-Path "logs\bot.pid") {
    $botPid = Get-Content "logs\bot.pid"
    Stop-Process -Id $botPid -ErrorAction SilentlyContinue
    Remove-Item "logs\bot.pid"
    Write-Host "중지했습니다. (PID $botPid)"
} else {
    Get-Process python -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match "run_dryrun" } |
        Stop-Process -ErrorAction SilentlyContinue
    Write-Host "PID 파일 없음 — run_dryrun 프로세스를 찾아 중지했습니다."
}
