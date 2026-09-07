# Cryo-floods 启动器（PowerShell 薄壳，全部逻辑在 scripts\start.py）
# 用法：powershell -ExecutionPolicy Bypass -File .\run.ps1 [-Port 5001] [-NoBrowser]
param([int]$Port = 0, [switch]$NoBrowser)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$py = Join-Path $PSScriptRoot '.runtime\python\python.exe'
if (-not (Test-Path $py)) {
    Write-Host ''
    Write-Host '  [x] 未找到内嵌运行时 .runtime\python\python.exe' -ForegroundColor Red
    Write-Host '      请先构建：powershell -ExecutionPolicy Bypass -File .\scripts\setup_runtime.ps1' -ForegroundColor Yellow
    Write-Host ''
    Read-Host '按回车键退出'
    exit 1
}

$argsList = @((Join-Path $PSScriptRoot 'scripts\start.py'))
if ($Port -gt 0) { $argsList += @('--port', $Port) }
if ($NoBrowser)  { $argsList += '--no-browser' }

& $py $argsList
exit $LASTEXITCODE
