#!/usr/bin/env bash
#
# 测试 MCP Server 是否可用（Streamable HTTP + Bearer 鉴权 + 记账工具 + Apps UI）
#
# 不需要 Casdoor：直接复用眼镜端 QR Binding 换 token（认证双形态之一）。
#
# 用法（方式 1：从 Web 端绑定码换 token，推荐）：
#   1. 打开 Web 端 /space/devices → "绑定新设备" → 复制 binding_code（BIND- 开头）
#   2. BINDING_CODE="BIND-xxx" API_BASE=http://127.0.0.1:8000 bash scripts/test_mcp.sh
#
# 用法（方式 2：已有 token）：
#   ACCESS_TOKEN="eyJ..." API_BASE=http://127.0.0.1:8000 bash scripts/test_mcp.sh
#
# 用法（方式 3：走完整 OAuth 浏览器流程）：
#   配置好 Casdoor MCP 应用后用 MCP Inspector 或 Claude Desktop 连接
#   http://127.0.0.1:8000/mcp 即可，无需本脚本。

set -euo pipefail

API_BASE="${API_BASE:-http://127.0.0.1:8000}"
MCP_URL="$API_BASE/mcp"
ACCEPT="application/json, text/event-stream"
MCP_VERSION="2025-06-18"
TS=$(date +%s)

echo "=========================================="
echo "  Moment One — MCP Server 测试"
echo "=========================================="
echo "MCP URL:      $MCP_URL"
echo ""

# ---- Step 0: 无 token 应返回规范 401 ----
echo "[0/6] 无 token 调 /mcp（期望 401 + WWW-Authenticate）..."
STATUS=$(curl -sS -o /tmp/mcp_401.json -w "%{http_code}" -X POST \
  "$MCP_URL" \
  -H "Content-Type: application/json" \
  -H "Accept: $ACCEPT" \
  -H "MCP-Protocol-Version: $MCP_VERSION" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}')
echo "  HTTP $STATUS"
if [ "$STATUS" = "401" ]; then
  WWW=$(curl -sS -D - -o /dev/null -X POST "$MCP_URL" \
    -H "Content-Type: application/json" \
    -H "Accept: $ACCEPT" \
    -H "MCP-Protocol-Version: $MCP_VERSION" \
    -d '{}' | grep -i "www-authenticate" | head -1 | tr -d '\r')
  echo "  ✅ 401 + $WWW"
else
  echo "  ❌ 预期 401，实际 $STATUS"
  exit 1
fi
echo ""

# ---- Step 1: 获取 Bearer token ----
if [ -z "${ACCESS_TOKEN:-}" ]; then
  RAW="${BINDING_CODE:?请设置 BINDING_CODE 或 ACCESS_TOKEN 环境变量}"
  if [[ "$RAW" == momentone://* ]]; then
    BINDING_CODE="${RAW#*code=}"
  else
    BINDING_CODE="$RAW"
  fi
  DEVICE_ID="${DEVICE_ID:-mcp-test-$(date +%s)}"
  echo "[1/6] 用 binding_code 换 token（POST /oauth/token）..."
  TOKEN_RESP=$(curl -sS -X POST "$API_BASE/oauth/token" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "grant_type=urn:momentone:oauth:grant-type:qr-binding" \
    -d "binding_code=$BINDING_CODE" \
    -d "device_id=$DEVICE_ID" \
    -d "device_name=MCP-Test" \
    -d "device_type=cli")
  ACCESS_TOKEN=$(echo "$TOKEN_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])" 2>/dev/null || echo "")
  if [ -z "$ACCESS_TOKEN" ]; then
    echo "  ❌ 换 token 失败：$TOKEN_RESP"
    exit 1
  fi
  echo "  ✅ access_token: ${ACCESS_TOKEN:0:40}..."
else
  echo "[1/6] 使用已提供的 ACCESS_TOKEN"
fi
echo ""

# ---- Step 2: MCP 握手 ----
echo "[2/6] initialize（MCP 握手）..."
INIT_RESP=$(curl -sS -i -X POST "$MCP_URL" \
  -H "Content-Type: application/json" \
  -H "Accept: $ACCEPT" \
  -H "MCP-Protocol-Version: $MCP_VERSION" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"$MCP_VERSION\",\"capabilities\":{},\"clientInfo\":{\"name\":\"mcp-test\",\"version\":\"1.0\"}}}")
SESSION_ID=$(echo "$INIT_RESP" | grep -i "mcp-session-id" | awk '{print $2}' | tr -d '\r' | head -1)
BODY=$(echo "$INIT_RESP" | tail -1)
if [ -z "$SESSION_ID" ]; then
  echo "  ❌ 握手失败（无 Mcp-Session-Id）：$BODY"
  exit 1
fi
SERVER_NAME=$(echo "$BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); r=d.get('result',{}); print(r.get('serverInfo',{}).get('name',''))" 2>/dev/null)
echo "  ✅ session: ${SESSION_ID:0:16}... | server: $SERVER_NAME"
AUTH="Authorization: Bearer $ACCESS_TOKEN"
SESSION_HDR="Mcp-Session-Id: $SESSION_ID"

# notifications/initialized
curl -sS -o /dev/null -X POST "$MCP_URL" \
  -H "Content-Type: application/json" -H "Accept: $ACCEPT" \
  -H "MCP-Protocol-Version: $MCP_VERSION" -H "$AUTH" -H "$SESSION_HDR" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}'
echo ""

# ---- Step 3: 列出工具 ----
echo "[3/6] tools/list ..."
TOOLS_BODY=$(curl -sS -X POST "$MCP_URL" \
  -H "Content-Type: application/json" -H "Accept: $ACCEPT" \
  -H "MCP-Protocol-Version: $MCP_VERSION" -H "$AUTH" -H "$SESSION_HDR" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}')
TOOLS=$(echo "$TOOLS_BODY" | python3 -c "
import sys, json
d = json.load(sys.stdin)
tools = d.get('result', {}).get('tools', [])
print(' '.join(t['name'] for t in tools))
" 2>/dev/null)
echo "  工具: $TOOLS"
if ! echo "$TOOLS" | grep -q "bookkeeping_create"; then
  echo "  ❌ 缺少 bookkeeping_create：$TOOLS_BODY"
  exit 1
fi
echo "  ✅ 工具齐全"
echo ""

# ---- Step 4: 写一笔账（幂等） ----
echo "[4/6] bookkeeping_create（写一笔 33 元的支出）..."
CREATE_BODY=$(curl -sS -X POST "$MCP_URL" \
  -H "Content-Type: application/json" -H "Accept: $ACCEPT" \
  -H "MCP-Protocol-Version: $MCP_VERSION" -H "$AUTH" -H "$SESSION_HDR" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"tools/call\",\"params\":{\"name\":\"bookkeeping_create\",\"arguments\":{\"amount\":33,\"flow\":\"expense\",\"occurredAt\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"category\":\"餐饮\",\"merchant\":\"MCP 测试\",\"idempotencyKey\":\"mcp-test-$TS\"}}}")
echo "$CREATE_BODY" | python3 -c "
import sys, json
d = json.load(sys.stdin)
res = d.get('result', {})
if res.get('isError'):
    print('  ❌ 创建失败:', json.dumps(res.get('structuredContent', {}), ensure_ascii=False))
    sys.exit(1)
sc = res.get('structuredContent', {})
print(f\"  ✅ 已创建 id={sc['id'][:8]}... {sc['category']} ¥{sc['amount']} flow={sc['flow']}\")
" || { echo "  原始响应: $CREATE_BODY"; exit 1; }
echo ""

# ---- Step 5: 统计 + 列表（读） ----
echo "[5/6] bookkeeping_summary + bookkeeping_list ..."
SUM_BODY=$(curl -sS -X POST "$MCP_URL" \
  -H "Content-Type: application/json" -H "Accept: $ACCEPT" \
  -H "MCP-Protocol-Version: $MCP_VERSION" -H "$AUTH" -H "$SESSION_HDR" \
  -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"bookkeeping_summary","arguments":{"period":"month"}}}')
echo "$SUM_BODY" | python3 -c "
import sys, json
sc = json.load(sys.stdin)['result']['structuredContent']
print(f\"  统计: 支出 ¥{sc['expense']} 收入 ¥{sc['income']} 结余 ¥{sc['balance']} 共 {sc['count']} 笔\")
" || echo "  ❌ summary 失败: $SUM_BODY"
LIST_BODY=$(curl -sS -X POST "$MCP_URL" \
  -H "Content-Type: application/json" -H "Accept: $ACCEPT" \
  -H "MCP-Protocol-Version: $MCP_VERSION" -H "$AUTH" -H "$SESSION_HDR" \
  -d '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"bookkeeping_list","arguments":{"limit":5}}}')
echo "$LIST_BODY" | python3 -c "
import sys, json
sc = json.load(sys.stdin)['result']['structuredContent']
print(f\"  列表: {sc['total']} 条（本页 {len(sc['items'])} 条）\")
" || echo "  ❌ list 失败: $LIST_BODY"
echo ""

# ---- Step 6: MCP Apps UI 资源 ----
echo "[6/6] 读取 Apps UI 资源（ui://moment-one/bookkeeping）..."
RES_BODY=$(curl -sS -X POST "$MCP_URL" \
  -H "Content-Type: application/json" -H "Accept: $ACCEPT" \
  -H "MCP-Protocol-Version: $MCP_VERSION" -H "$AUTH" -H "$SESSION_HDR" \
  -d '{"jsonrpc":"2.0","id":6,"method":"resources/read","params":{"uri":"ui://moment-one/bookkeeping"}}')
echo "$RES_BODY" | python3 -c "
import sys, json
d = json.load(sys.stdin)
contents = d.get('result', {}).get('contents', [])
if not contents:
    print('  ❌ 资源为空:', json.dumps(d, ensure_ascii=False)[:200])
    sys.exit(1)
print(f\"  ✅ 资源 mime={contents[0].get('mimeType')} size={len(contents[0].get('text',''))}B\")
" || echo "  ❌ 资源读取失败: $RES_BODY"
echo ""

echo "=========================================="
echo "  ✅ MCP Server 全链路测试通过！"
echo "=========================================="
echo "下一步（可选）：配置 Casdoor MCP 应用后，用 Claude Desktop 连接"
echo "  $MCP_URL 走完整 OAuth 浏览器流程（见 docs/roadmap/MCP_APPS_PLAN.md §3.1）。"
