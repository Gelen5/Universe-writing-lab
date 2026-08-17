#!/usr/bin/env bash
# kb_autosync 一键初始化（macOS / Linux）
set -e
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

echo "[1/4] 创建虚拟环境 .venv ..."
python3 -m venv .venv || { echo "请先安装 Python 3.10+"; exit 1; }
PY=.venv/bin/python

echo "[2/4] 安装依赖 ..."
$PY -m pip install --quiet --upgrade pip
$PY -m pip install --quiet -e .

echo "[3/4] 生成 config.json（请打开填入飞书/微信凭证）..."
[ -f config.json ] || cp config.example.json config.json

echo "[4/4] 运行 demo 验证流水线 ..."
$PY -m kb_autosync.cli demo

echo ""
echo "✅ 初始化完成。下一步：编辑 config.json 填入凭证，然后跑："
echo "  .venv/bin/python -m kb_autosync.cli sync --all --no-dry-run"
