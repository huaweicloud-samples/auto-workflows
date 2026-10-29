#!/usr/bin/env bash
# 本地预检：在推送前确认采集包完整。不接触任何 token，只检查文件与结构。
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

fail=0
check() {
  if [ -e "$2" ]; then
    printf '  ✓ %-40s %s\n' "$1" "$2"
  else
    printf '  ✗ %-40s %s  (缺失)\n' "$1" "$2"
    fail=1
  fi
}

echo "══════ 采集包完整性检查 ══════"
echo ""
check "采集器主程序"        "collector.py"
check "数据库（历史数据）"   "hwc_traffic.db"
check "workflow 定义"       ".github/workflows/collect.yml"
check "云端预检脚本"        "scripts/preflight.sh"
check "说明文档"            "README.md"
check "gitignore"           ".gitignore"

echo ""
echo "── workflow 关键配置 ──"
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'pass' >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -n "$PY" ]; then
  "$PY" - <<'PYEOF'
import sys
try:
    import yaml
except ImportError:
    print("  (跳过：未安装 pyyaml)")
    sys.exit(0)
d = yaml.safe_load(open('.github/workflows/collect.yml', encoding='utf-8'))
on = d.get(True) or d.get('on') or {}
print(f"  触发器     : {list(on.keys())}")
print(f"  定时       : {on.get('schedule')}")
print(f"  权限       : {d.get('permissions')}")
steps = d['jobs']['collect']['steps']
print(f"  步骤数     : {len(steps)}")
names = [s.get('name') or s.get('uses','?') for s in steps]
for n in names:
    print(f"    - {n}")
PYEOF
fi

echo ""
echo "── 数据库当前内容 ──"
if [ -n "$PY" ]; then
  "$PY" - <<'PYEOF'
import sqlite3
try:
    c = sqlite3.connect('hwc_traffic.db')
    for t in ('repo_traffic_daily','repo_snapshot','fork_events'):
        n = c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
        print(f"  {t:<22} {n:>6} 行")
    d = c.execute('SELECT MIN(date),MAX(date),COUNT(DISTINCT date) FROM repo_traffic_daily').fetchone()
    print(f"  traffic 日期           {d[0]} ~ {d[1]}  ({d[2]} 天)")
except Exception as e:
    print(f"  数据库读取失败: {e}")
PYEOF
fi

echo ""
if [ "$fail" -eq 0 ]; then
  echo "✅ 采集包完整，可以推送"
  echo ""
  echo "推送后记得在仓库 Settings → Actions → General"
  echo "把 Workflow permissions 设为 Read and write permissions"
else
  echo "❌ 有文件缺失"
  exit 1
fi
