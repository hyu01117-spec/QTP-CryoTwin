# Cryo-floods 换机自检脚本
#
# 硬盘插到新电脑后先跑这个，一条命令定位"为什么起不来"。
#   powershell -ExecutionPolicy Bypass -File .\diagnose.ps1
#
# 检查项：盘符 / 内嵌运行时 / 依赖 / 数据完整性 / 写入权限 / .env / 端口
param([switch]$Full)

$ErrorActionPreference = 'Continue'
Set-Location $PSScriptRoot

$Root = $PSScriptRoot
$Py   = Join-Path $Root '.runtime\python\python.exe'
$ok = 0; $warn = 0; $bad = 0

function Check($label, $detail, $state) {
    # state: ok / warn / bad
    $script:cnt = @{ ok = 0; warn = 0; bad = 0 }
    switch ($state) {
        'ok'   { $script:ok++;   $mark = '√'; $c = 'Green' }
        'warn' { $script:warn++; $mark = '!'; $c = 'Yellow' }
        'bad'  { $script:bad++;  $mark = '×'; $c = 'Red' }
    }
    $line = '  [{0}] {1}' -f $mark, $label
    $pad = ' ' * [Math]::Max(1, 34 - $label.Length)
    Write-Host ($line + $pad + $detail) -ForegroundColor $c
}

Write-Host ''
Write-Host '  Cryo-floods 换机自检' -ForegroundColor Cyan
Write-Host '  ────────────────────────────────────────────────────' -ForegroundColor DarkGray

# ---- 1. 位置与盘符 --------------------------------------------------------
Check '项目路径' $Root 'ok'
Check '当前盘符' "$($Root.Substring(0,2))  (代码全部相对解析，任意盘符均可)" 'ok'

# ---- 2. 内嵌运行时 --------------------------------------------------------
if (Test-Path $Py) {
    $ver = & $Py -c 'import sys;print("%d.%d.%d" % sys.version_info[:3])' 2>$null
    $pfx = & $Py -c 'import sys;print(sys.prefix)' 2>$null
    if ($pfx -eq (Join-Path $Root '.runtime\python')) {
        Check '内嵌运行时' "Python $ver · 自包含" 'ok'
    }
    else {
        Check '内嵌运行时' "Python $ver · 但 sys.prefix 异常: $pfx" 'warn'
    }
}
else {
    Check '内嵌运行时' '.runtime\python\python.exe 缺失' 'bad'
    Write-Host ''
    Write-Host '  构建命令：  .\scripts\setup_runtime.ps1' -ForegroundColor Yellow
}

# ---- 3. 依赖 --------------------------------------------------------------
if (Test-Path $Py) {
    $env:PYTHONNOUSERSITE = '1'
    $deps = & $Py -c @'
import importlib.util as u, importlib.metadata as md
mods = ["flask","torch","rasterio","geopandas","shapely","matplotlib","pandas","numpy","dotenv","netCDF4"]
for m in mods:
    if u.find_spec(m) is None:
        print("MISS|%s|未安装" % m)
    else:
        try: print("OK|%s|%s" % (m, md.version(m)))
        except Exception: print("OK|%s|已安装" % m)
'@
    $hardMissing = @()
    foreach ($d in $deps) {
        $parts = $d -split '\|'
        if ($parts[0] -eq 'OK') {
            Check ('  ' + $parts[1]) $parts[2] 'ok'
        }
        else {
            $hardMissing += $parts[1]
            Check ('  ' + $parts[1]) $parts[2] 'bad'
        }
    }
}

# ---- 4. 数据完整性 --------------------------------------------------------
Write-Host '  ────────────────────────────────────────────────────' -ForegroundColor DarkGray
$dataChecks = @(
    @{ p = 'data\raw\geo';                        n = '矢量底图' },
    @{ p = 'data\raw\dem\wholeYarkant.tif';       n = 'DEM' },
    @{ p = 'data\raw\models';                     n = 'LSTM 模型' },
    @{ p = 'data\raw\thresholds';                 n = '淹没阈值' },
    @{ p = 'data\processed\db';                   n = '索引数据库' },
    @{ p = 'data\raw\TibetanPlateau';             n = 'ERA5 驱动栅格' },
    @{ p = 'data\raw\GLDAS025_Snowmelt';          n = 'GLDAS 融雪' },
    @{ p = 'data\processed\GLDAS_snowmelt_max';   n = '融雪极值' }
)
foreach ($c in $dataChecks) {
    $full = Join-Path $Root $c.p
    if (Test-Path $full) {
        $n = (Get-ChildItem -Path $full -Recurse -File -ErrorAction SilentlyContinue | Measure-Object).Count
        Check ('  ' + $c.n) "$n 个文件" 'ok'
    }
    else {
        # 缺失但非启动必需的，降级为警告
        if ($c.p -match 'TibetanPlateau|GLDAS') { Check ('  ' + $c.n) '缺失（影响模拟/融雪功能）' 'warn' }
        else { Check ('  ' + $c.n) '缺失（影响核心功能）' 'bad' }
    }
}

# ---- 5. 写入权限（移动硬盘跨机最常见的问题）--------------------------------
Write-Host '  ────────────────────────────────────────────────────' -ForegroundColor DarkGray
foreach ($d in @('data\webgis\outputs', 'data\webgis\uploads', 'data\processed')) {
    $full = Join-Path $Root $d
    if (-not (Test-Path $full)) {
        try { New-Item -ItemType Directory -Path $full -Force | Out-Null; Check "  写入 $d" '已创建' 'ok' }
        catch { Check "  写入 $d" '无法创建' 'bad' }
        continue
    }
    $t = Join-Path $full "._wtest_$PID.tmp"
    try {
        [IO.File]::WriteAllText($t, 'x')
        Remove-Item $t -Force
        Check "  写入 $d" '可写' 'ok'
    }
    catch { Check "  写入 $d" '不可写（移动盘权限问题）' 'bad' }
}

# ---- 6. .env --------------------------------------------------------------
Write-Host '  ────────────────────────────────────────────────────' -ForegroundColor DarkGray
if (Test-Path '.env') {
    $envText = Get-Content '.env' -Raw -Encoding UTF8
    $placeholders = ([regex]::Matches($envText, '请填写|请修改|change-me')).Count
    if ($placeholders -gt 0) {
        Check '  .env' "存在，但仍有 $placeholders 处占位符未填" 'warn'
    }
    else { Check '  .env' '已配置' 'ok' }
}
else { Check '  .env' '缺失（地图/智能体不可用）' 'warn' }

# ---- 7. 端口 --------------------------------------------------------------
$p = if ($env:PORT) { [int]$env:PORT } else { 5000 }
$busy = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
if ($busy) { Check '  端口' "$p 被占用（用 .\run.ps1 -Port 5001 换端口）" 'warn' }
else { Check '  端口' "$p 空闲" 'ok' }

# ---- 8. 可选：冒烟测试 ------------------------------------------------------
if ($Full -and (Test-Path $Py)) {
    Write-Host '  ────────────────────────────────────────────────────' -ForegroundColor DarkGray
    Write-Host '  运行冒烟测试...' -ForegroundColor DarkGray
    & $Py 'tests\smoke_test.py' 2>&1 | Select-Object -Last 12
}

Write-Host '  ────────────────────────────────────────────────────' -ForegroundColor DarkGray
Write-Host ("  通过 $ok 项 · 警告 $warn 项 · 失败 $bad 项") -ForegroundColor $(
    if ($bad -gt 0) { 'Red' } elseif ($warn -gt 0) { 'Yellow' } else { 'Green' })
Write-Host ''
if ($bad -eq 0) { Write-Host '  可以启动：  .\run.ps1' -ForegroundColor Green }
Write-Host ''
