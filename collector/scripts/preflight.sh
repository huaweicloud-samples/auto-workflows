#!/usr/bin/env bash
# 采集前自检：确认 token 权限真的到位，避免静默采到空数据
# 在 GitHub Actions 里跑，也可本地手动执行验证。
set -uo pipefail

API="https://api.github.com"
ORG="${ORG:-huaweicloud-samples}"
fail=0

if [ -z "${GITHUB_TOKEN:-}" ]; then
  echo "✗ 未设置 GITHUB_TOKEN"
  exit 1
fi

AUTH=(-H "Authorization: Bearer ${GITHUB_TOKEN}" \
      -H "Accept: application/vnd.github+json" \
      -H "X-GitHub-Api-Version: 2022-11-28" \
      -H "User-Agent: hwc-preflight")

# ---- 解释器自适应：Ubuntu runner 用 python3，Windows/Git Bash 用 python ----
PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'pass' >/dev/null 2>&1; then
    PY="$cand"; break
  fi
done
if [ -z "$PY" ]; then
  echo "✗ 找不到可用的 python 解释器"
  exit 1
fi

# 从 stdin 的 JSON 取顶层字段
jget() {
  "$PY" -c 'import sys,json
try:
    d=json.load(sys.stdin)
except Exception:
    print(""); sys.exit(0)
v = d.get(sys.argv[1],"") if isinstance(d,dict) else ""
print(v)' "$1" 2>/dev/null
}

echo "══════════ 采集前自检 ══════════"

# ---- 1. token 身份 ----
echo ""
echo "[1/4] token 身份"
login=$(curl -sS "${AUTH[@]}" "$API/user" | jget login)
echo "     login = ${login:-<none>}"
if [ -z "$login" ]; then
  echo "     ✗ token 无效或网络不通"
  exit 1
fi

# ---- 2. 组织成员角色 ----
echo ""
echo "[2/4] 组织成员身份"
mem_json=$(curl -sS "${AUTH[@]}" "$API/user/memberships/orgs/$ORG")
role=$(echo "$mem_json" | jget role)
state=$(echo "$mem_json" | jget state)
echo "     $ORG: state=${state:-?} role=${role:-?}"
case "$role" in
  admin)  echo "     ✓ org owner → 对全部仓库有 admin，traffic 应可用" ;;
  member) echo "     · member（traffic 是否能读取决于该仓库的 push/admin，见第 4 步）" ;;
  *)      echo "     · 无法判定角色（token 可能缺 read:org），继续测 traffic" ;;
esac

# ---- 3. 枚举仓库 ----
echo ""
echo "[3/4] 仓库枚举"
repos_json=$(curl -sS "${AUTH[@]}" "$API/orgs/$ORG/repos?per_page=100&page=1&type=all")
n=$(echo "$repos_json" | "$PY" -c 'import sys,json
try:
    d=json.load(sys.stdin)
    print(len(d) if isinstance(d,list) else 0)
except Exception:
    print(0)')
echo "     第一页取得 $n 个仓库"
if [ "$n" -eq 0 ]; then
  echo "     ✗ 无法枚举仓库（权限或限流）"
  exit 1
fi
first=$(echo "$repos_json" | "$PY" -c 'import sys,json
d=json.load(sys.stdin)
print(d[0]["name"] if isinstance(d,list) and d else "")')

# ---- 4. traffic 权限（关键）----
echo ""
echo "[4/4] traffic 接口（关键）"
body=$(curl -sS -w '\n%{http_code}' "${AUTH[@]}" "$API/repos/$ORG/$first/traffic/views?per=day")
code=$(echo "$body" | tail -n1)
payload=$(echo "$body" | sed '$d')

if [ "$code" = "200" ]; then
  views=$(echo "$payload" | jget count)
  uniqs=$(echo "$payload" | jget uniques)
  echo "     ✓ 可读 traffic（$first: views=$views uniques=$uniqs）"
else
  msg=$(echo "$payload" | jget message)
  echo "     ✗ HTTP $code — $msg"
  echo ""
  echo "     traffic 需要目标仓库的 push/admin 权限。请确认："
  echo "       • GitHub App 已授予 Administration: Read，且已安装到 $ORG"
  echo "       • 或 PAT 所属账号是 $ORG 的 owner / 目标仓库的 admin"
  traffic_fail=1
fi

echo ""
echo "══════════ 自检结束 ══════════"

# 退出码约定：
#   0 = 全部可用（traffic + forks）
#   2 = 仅 traffic 不可用，forks/stars 仍可采（降级模式）
#   1 = 致命错误（token 无效 / 无法枚举仓库），不应继续
if [ "$fail" -ne 0 ]; then
  echo "❌ 致命错误：token 无效或无法枚举仓库，终止"
  exit 1
fi
if [ "${traffic_fail:-0}" -ne 0 ]; then
  echo "⚠ traffic 不可用，将降级为仅采集 forks/stars"
  exit 2
fi
echo "✅ 全部通过，开始采集"
exit 0
