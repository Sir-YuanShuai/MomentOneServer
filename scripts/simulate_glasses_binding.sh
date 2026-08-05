#!/usr/bin/env bash
#
# 模拟眼镜端设备绑定流程（纯眼镜端视角，不需要 Casdoor token）
#
# 前置条件：
#   1. 打开 Web 端 https://moment-one.yuanshuai.fun/space/devices
#   2. 点击"绑定新设备"，页面上会显示二维码和 binding_code
#   3. 复制 binding_code（BIND- 开头的字符串）
#
# 用法：
#   BINDING_CODE="BIND-xxxxxxxx" \
#   API_BASE=https://moment-one-api.yuanshuai.fun \
#   bash scripts/simulate_glasses_binding.sh
#
# 眼镜端不需要任何密钥/证书。JWT 的签名和验签都是 Server 内部完成的。
# 眼镜端只管：扫码拿 binding_code → 换 token → 带 token 调 API。

set -euo pipefail

API_BASE="${API_BASE:-https://moment-one-api.yuanshuai.fun}"
RAW_INPUT="${BINDING_CODE:?请设置 BINDING_CODE 环境变量（从 Web 端绑定页面复制）}"

# 自动从 qr_payload 中提取 binding_code
# 用户可能粘贴 "BIND-xxx" 或 "momentone://bind?code=BIND-xxx"
if [[ "$RAW_INPUT" == momentone://* ]]; then
  BINDING_CODE="${RAW_INPUT#*code=}"
else
  BINDING_CODE="$RAW_INPUT"
fi

DEVICE_ID="${DEVICE_ID:-glasses-test-$(date +%s)}"
DEVICE_NAME="${DEVICE_NAME:-Rokid Max Pro}"
DEVICE_TYPE="${DEVICE_TYPE:-glasses}"

echo "=========================================="
echo "  Moment One — 眼镜端绑定模拟"
echo "=========================================="
echo "API:          $API_BASE"
echo "binding_code: $BINDING_CODE"
echo "Device ID:    $DEVICE_ID"
echo "Device:       $DEVICE_NAME ($DEVICE_TYPE)"
echo ""

# ---- Step 1: 用 binding_code 换 token（模拟眼镜端扫码）----
echo "[1/3] 扫码换 token（POST /oauth/token）..."
TOKEN_RESP=$(curl -sS -X POST \
  "$API_BASE/oauth/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=urn:momentone:oauth:grant-type:qr-binding" \
  -d "binding_code=$BINDING_CODE" \
  -d "device_id=$DEVICE_ID" \
  -d "device_name=$DEVICE_NAME" \
  -d "device_type=$DEVICE_TYPE")

echo "  响应: $TOKEN_RESP"

ACCESS_TOKEN=$(echo "$TOKEN_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])" 2>/dev/null || echo "")
REFRESH_TOKEN=$(echo "$TOKEN_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['refresh_token'])" 2>/dev/null || echo "")
BINDING_ID=$(echo "$TOKEN_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['binding_id'])" 2>/dev/null || echo "")

if [ -z "$ACCESS_TOKEN" ]; then
  echo "  ❌ 换 token 失败"
  exit 1
fi

echo "  ✅ binding_id:   $BINDING_ID"
echo "  ✅ access_token:  ${ACCESS_TOKEN:0:50}..."
echo "  ✅ refresh_token: ${REFRESH_TOKEN:0:50}..."
echo ""

# ---- Step 2: 用 access_token 访问 Moment API（模拟眼镜端日常请求）----
echo "[2/3] 用 access_token 访问 Moment API（GET /v1/moments）..."
MOMENTS_RESP=$(curl -sS -X GET \
  "$API_BASE/v1/moments?limit=5" \
  -H "Authorization: Bearer $ACCESS_TOKEN")

echo "  响应: ${MOMENTS_RESP:0:200}..."
echo "  ✅ 眼镜端成功用 token 访问了 Moment API"
echo ""

# ---- Step 3: 用 refresh_token 刷新（模拟 token 过期后续期）----
echo "[3/3] 用 refresh_token 刷新 access_token..."
REFRESH_RESP=$(curl -sS -X POST \
  "$API_BASE/oauth/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=refresh_token" \
  -d "refresh_token=$REFRESH_TOKEN")

echo "  响应: $REFRESH_RESP"

NEW_ACCESS_TOKEN=$(echo "$REFRESH_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])" 2>/dev/null || echo "")

if [ -z "$NEW_ACCESS_TOKEN" ]; then
  echo "  ❌ 刷新 token 失败"
  exit 1
fi

echo "  ✅ 新 access_token: ${NEW_ACCESS_TOKEN:0:50}..."
echo ""

echo "=========================================="
echo "  ✅ 眼镜端绑定全流程模拟完成！"
echo "=========================================="
echo ""
echo "绑定关系："
echo "  binding_id: $BINDING_ID"
echo "  device_id:  $DEVICE_ID"
echo ""
echo "去 Web 端 /space/devices 页面可以看到这台设备。"
