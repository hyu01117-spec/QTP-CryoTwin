# runpy.ps1 — 用 PowerShell 直接、可靠地驱动内嵌 Python 的包装器
#
# 背景：本环境下 Bash 工具链损坏（python 不在 PATH、cwd 错乱），必须用 PowerShell 驱动
#       .runtime\python\python.exe。但 PowerShell 工具不回显 stdout。
#       此脚本把两个坑一次性封装掉：
#
#   坑1  项目目录名 QTP‑CryoTwin 含非断行连字符 U+2011，手打 "-" 拼不出路径
#        → 用 Get-ChildItem 'E:\' -Directory | ?{ $_.Name -like 'QTP*' } 通配解析，
#          绝不手敲特殊字符，也绝不依赖 cwd。
#
#   坑2  本会话 PowerShell 工具不回显 stdout（exit 0 但无输出）
#        → 所有 Python 输出一律落盘到 <root>\.runtime\last_run.log，再用 Read 读。
#          （在真实 PowerShell 终端里加 -Show 可同时直接看输出。）
#
#   重要：.ps1 脚本须用 `powershell -ExecutionPolicy Bypass -File` 调用，不能用 `&` 直接跑
#         （本环境执行策略会拦掉 `& script.ps1`）。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\runpy.ps1 -Script scripts\start.py -Port 5001 -NoBrowser
#   powershell -ExecutionPolicy Bypass -File scripts\runpy.ps1 -Command "import app; print('ok')"
#   powershell -ExecutionPolicy Bypass -File scripts\runpy.ps1 -Script scripts\start.py -Show
#   说明：-Script/-Command 之后的所有参数（含 -Port 5001、--help）原样透传给 Python。

[CmdletBinding()]
param(
    [string]$Script,
    [string]$Command,
    [switch]$Show,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Remaining
)

$ErrorActionPreference = 'Stop'

# ── 坑1：通配解析项目根（避开 U+2011，不依赖 cwd）────────────────────────
$root = (Get-ChildItem 'E:\' -Directory |
         Where-Object { $_.Name -like 'QTP*' } |
         Select-Object -First 1).FullName
if (-not $root) { Write-Error '找不到 E:\ 下以 QTP 开头的项目目录'; exit 1 }

$py = Join-Path $root '.runtime\python\python.exe'
if (-not (Test-Path $py)) { Write-Error "未找到内嵌运行时：$py"; exit 1 }

$log = Join-Path $root '.runtime\last_run.log'

# ── 透传参数给 Python（若有前导的 "--" 分隔符则去掉）──────────────────
$rest = $Remaining
if ($rest.Count -gt 0 -and $rest[0] -eq '--') { $rest = $rest[1..($rest.Count - 1)] }

# ── 组装 Python 参数 ───────────────────────────────────────────────────
$pyArgs = [System.Collections.Generic.List[string]]::new()
if ($Script)  { $pyArgs.Add((Join-Path $root $Script)) }
if ($Command) { $pyArgs.Add('-c'); $pyArgs.Add($Command) }
foreach ($a in $rest) { $pyArgs.Add($a) }

if ($pyArgs.Count -eq 0) { Write-Error '必须提供 -Script 或 -Command 之一'; exit 1 }

# ── 坑2：输出落盘（本工具不回显 stdout）───────────────────────────────
if ($Show) {
    & $py @pyArgs
    exit $LASTEXITCODE
}
else {
    & $py @pyArgs 2>&1 | Out-File -FilePath $log -Encoding utf8
    # 真实终端里这行可见；本工具里看不到，但日志已写好，用 Read 读 $log 即可。
    Write-Output "OUTPUT_WRITTEN_TO=$log"
    exit $LASTEXITCODE
}
