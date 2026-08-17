# kb_autosync 一键初始化（Windows PowerShell）
# 用法：在 kb_autosync 目录下，右键「使用 PowerShell 运行」或：
#   powershell -ExecutionPolicy Bypass -File init.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

# 1) 虚拟环境
$venv = Join-Path $root ".venv"
if (-not (Test-Path $venv)) {
    Write-Host "[1/4] 创建虚拟环境 .venv ..." -ForegroundColor Cyan
    python -m venv $venv
} else {
    Write-Host "[1/4] 虚拟环境已存在，跳过" -ForegroundColor DarkGray
}

$py = Join-Path $venv "Scripts/python.exe"
if (-not (Test-Path $py)) { Write-Error "Python 虚拟环境创建失败，请确认已安装 Python 3.10+"; exit 1 }

# 2) 依赖
Write-Host "[2/4] 安装依赖 ..." -ForegroundColor Cyan
& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet -e .

# 3) 配置
$cfg = Join-Path $root "config.json"
$cfgExample = Join-Path $root "config.example.json"
if (-not (Test-Path $cfg)) {
    Write-Host "[3/4] 生成 config.json（请打开填入飞书/微信凭证）..." -ForegroundColor Cyan
    Copy-Item $cfgExample $cfg
} else {
    Write-Host "[3/4] config.json 已存在，跳过" -ForegroundColor DarkGray
}

# 4) 验证
Write-Host "[4/4] 运行 demo 验证流水线 ..." -ForegroundColor Cyan
& $py -m kb_autosync.cli demo

Write-Host ""
Write-Host "✅ 初始化完成。" -ForegroundColor Green
Write-Host "下一步：编辑 config.json 填入 FEISHU_APP_ID / FEISHU_APP_SECRET / space_id，然后跑："
Write-Host "  .venv\Scripts\python.exe -m kb_autosync.cli sync --all --no-dry-run" -ForegroundColor Yellow
