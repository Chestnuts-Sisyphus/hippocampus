#!/usr/bin/env bash
# Hippocampus 一键演示（F2）：起代理 → 灌数据 → 三格式请求 → 跑评测 → 出结果表。
#
# 用法：
#   bash scripts/demo.sh                # 默认端口 8765、临时数据根
#   HIPPOCAMPUS_DEMO_PORT=8899 bash scripts/demo.sh
#
# 纪律：离线可跑（无 key 时代理回"离线回执"，记忆纪律照常）；不弹窗；只读代码、只写数据根。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON:-python}"
PORT="${HIPPOCAMPUS_DEMO_PORT:-8765}"
HOME_DIR="${HIPPOCAMPUS_DEMO_HOME:-$(mktemp -d 2>/dev/null || echo "$ROOT/.demo-home")}"
export HIPPOCAMPUS_HOME="$HOME_DIR"
export HIPPOCAMPUS_OFFLINE="${HIPPOCAMPUS_OFFLINE:-1}"   # 默认离线跑（CI 与无凭据环境）

echo "== Hippocampus 一键演示 =="
echo "数据根: $HOME_DIR"
echo "端口:   $PORT"
echo

PROXY_PID=""
cleanup() {
  if [ -n "$PROXY_PID" ]; then
    kill "$PROXY_PID" 2>/dev/null || true
    wait "$PROXY_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

echo "-- [1/5] doctor（体检）"
"$PY" -m hippocampus.cli doctor | head -20 || true
echo

echo "-- [2/5] seed（灌入合成示例数据）"
"$PY" -m hippocampus.cli seed | tail -3 || exit 1
echo

echo "-- [3/5] 起代理（后台，端口 $PORT）"
"$PY" -m hippocampus.cli proxy --port "$PORT" >"$HOME_DIR/proxy.log" 2>&1 &
PROXY_PID=$!

READY=0
for _ in $(seq 1 40); do
  if "$PY" - "$PORT" <<'EOF' 2>/dev/null
import sys, urllib.request
port = sys.argv[1]
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
EOF
  then READY=1; break; fi
  sleep 0.5
done
if [ "$READY" != "1" ]; then
  echo "代理未就绪（可能缺 uvicorn：pip install 'hippocampus-agent[proxy]'）；跳过三格式请求，直接跑评测。"
  tail -5 "$HOME_DIR/proxy.log" 2>/dev/null || true
else
  TOKEN="$(cat "$HOME_DIR/instance_token" 2>/dev/null || echo '')"
  AUTH=()
  if [ -n "$TOKEN" ]; then AUTH=(-H "Authorization: Bearer $TOKEN"); fi

  echo "-- [4/5] 三格式请求（chat / responses / anthropic）"
  # 请求体写成 UTF-8 文件再发：Windows 的 shell 会把内联中文按本地代码页编码，
  # 服务端按 UTF-8 解析会失败（实测 git-bash 下 curl -d '中文' 会 400）。
  PAYLOAD_DIR="$HOME_DIR/payloads"
  mkdir -p "$PAYLOAD_DIR"
  "$PY" - "$PAYLOAD_DIR" <<'EOF'
import json, sys
from pathlib import Path

out = Path(sys.argv[1])
q = "我投简历有什么要求？"
(out / "chat.json").write_text(json.dumps({"model": "demo", "messages": [{"role": "user", "content": q}]}, ensure_ascii=False), encoding="utf-8")
(out / "responses.json").write_text(json.dumps({"model": "demo", "instructions": "你是助手", "input": q}, ensure_ascii=False), encoding="utf-8")
(out / "anthropic.json").write_text(json.dumps({"model": "demo", "max_tokens": 128, "system": "你是助手", "messages": [{"role": "user", "content": q}]}, ensure_ascii=False), encoding="utf-8")
EOF
  echo "  chat:"; curl -s "http://127.0.0.1:$PORT/v1/chat/completions" \
    "${AUTH[@]}" -H 'content-type: application/json' --data-binary "@$PAYLOAD_DIR/chat.json" | head -c 220; echo
  echo "  responses:"; curl -s "http://127.0.0.1:$PORT/v1/responses" \
    "${AUTH[@]}" -H 'content-type: application/json' --data-binary "@$PAYLOAD_DIR/responses.json" | head -c 220; echo
  echo "  anthropic:"; curl -s "http://127.0.0.1:$PORT/v1/messages" \
    "${AUTH[@]}" -H 'content-type: application/json' --data-binary "@$PAYLOAD_DIR/anthropic.json" | head -c 220; echo
  echo "  （无凭据时以上为『离线回执』：记忆的检索/注入/固化照常执行）"
  echo
fi

echo "-- [5/5] 评测（20 题 + 记忆开/关对照 + 关键词基线）"
"$PY" -m hippocampus.cli demo --questions 20 --memories --baseline
echo
echo "== 演示结束（代理已停）=="
