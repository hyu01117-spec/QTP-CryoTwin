# 构建自包含 Python 运行时（.runtime\python）
#
# 什么时候需要跑：
#   - 硬盘插到一台新电脑，且 .runtime 目录不存在/损坏
#   - 换了 Python 版本或依赖
#
# 前提：需要有一个临时的 Python（3.10+）来驱动构建脚本。
#       构建完成后，本项目的运行就不再需要它了。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File .\scripts\setup_runtime.ps1
#   powershell -ExecutionPolicy Bypass -File .\scripts\setup_runtime.ps1 -Force   # 强制重建
param([switch]$Force)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Py = Join-Path $Root '.runtime\python\python.exe'

Write-Host ''
Write-Host '  Cryo-floods 运行时构建' -ForegroundColor Cyan
Write-Host '  ──────────────────────────────────────────' -ForegroundColor DarkGray

if ((Test-Path $Py) -and -not $Force) {
    $ver = & $Py -c 'import sys;print("%d.%d.%d" % sys.version_info[:3])'
    Write-Host "  运行时已存在：Python $ver" -ForegroundColor Green
    Write-Host '  如需重建请加 -Force 参数'
    Write-Host ''
    exit 0
}

# 找一个能跑构建脚本的临时 Python
$boot = $null
foreach ($cmd in @('py -3.12', 'py -3.11', 'python3.12', 'python')) {
    $parts = $cmd -split ' '
    $exe = if ($parts.Count -gt 1) { Get-Command $parts[0] -ErrorAction SilentlyContinue } else { Get-Command $parts[0] -ErrorAction SilentlyContinue }
    if (-not $exe) { continue }
    try {
        $v = if ($parts.Count -gt 1) { & $parts[0] $parts[1] -c 'import sys;print(sys.version_info[:2])' }
             else { & $parts[0] -c 'import sys;print(sys.version_info[:2])' }
        if ($v -match '\(3, (1[0-9]|[2-9][0-9])\)') { $boot = $cmd; break }
    }
    catch { }
}

if (-not $boot) {
    Write-Host '  未找到可用的 Python（需要 3.10 以上）。' -ForegroundColor Red
    Write-Host ''
    Write-Host '  选一个办法：' -ForegroundColor Yellow
    Write-Host '    1) 临时装一个 Python 3.12（构建完就可卸载）'
    Write-Host '       https://www.python.org/downloads/release/python-3126/'
    Write-Host '    2) 从一台已经有 .runtime 的机器，把 .runtime 整个目录拷过来'
    Write-Host ''
    exit 1
}

Write-Host "  引导解释器: $boot"
Write-Host '  正在提取 CPython 内核并安装依赖（torch 较大，请耐心）...'
Write-Host ''

$bp = $boot -split ' '
if ($bp.Count -gt 1) { & $bp[0] $bp[1] "$Root\scripts\build_runtime.py" }
else { & $bp[0] "$Root\scripts\build_runtime.py" }

if ($LASTEXITCODE -eq 0) {
    Write-Host ''
    Write-Host '  运行时构建完成。现在可以启动：  .\run.ps1' -ForegroundColor Green
    Write-Host ''
}
else {
    Write-Host ''
    Write-Host "  构建失败（代码 $LASTEXITCODE）" -ForegroundColor Red
    Write-Host ''
}
exit $LASTEXITCODE
