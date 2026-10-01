#!/usr/bin/env bash
# 一条本地命令启动技能授权与排班系统（自动建库并注入含“无证替岗”的样例数据）。
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8077}"
HOST="${HOST:-127.0.0.1}"
RESET="${RESET:-0}"
if [ "$RESET" = "1" ]; then
  rm -rf app/data
  echo "已清空 app/data，将重新生成样例数据。"
fi
exec python3 -u -m app --host "$HOST" --port "$PORT"
