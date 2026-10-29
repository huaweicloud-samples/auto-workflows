"""数据查看工具：无需记忆 SQL，直接看采集结果

用法：
  python view.py                 # 总览
  python view.py daily           # 每日趋势
  python view.py top 20          # 浏览量 Top 20
  python view.py forks 15        # fork Top 15
  python view.py forks-new 30    # 最近 30 天新增 fork
  python view.py repo <名字>     # 单仓库详情
  python view.py export          # 导出全部为 CSV
"""
import sqlite3
import sys
import os
from datetime import datetime, timedelta, timezone

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hwc_traffic.db")


def conn():
    if not os.path.exists(DB):
        sys.exit("数据库不存在: %s" % DB)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def bar(v, mx, width=40):
    if not mx:
        return ""
    return "#" * max(0, int(round(v / mx * width)))


def cmd_overview(c):
    print("=" * 64)
    print("采集数据总览")
    print("=" * 64)

    r = c.execute("""SELECT MIN(date) a, MAX(date) b, COUNT(DISTINCT date) d,
                            COUNT(DISTINCT repo) n FROM repo_traffic_daily""").fetchone()
    if not r["d"]:
        print("  尚无 traffic 数据")
        return
    print("  时间范围   : %s ~ %s  (%d 天)" % (r["a"], r["b"], r["d"]))
    print("  覆盖仓库   : %d 个" % r["n"])

    t = c.execute("""SELECT SUM(views) v, SUM(unique_visitors) u,
                            SUM(clones) c, SUM(unique_cloners) cu
                     FROM repo_traffic_daily""").fetchone()
    print()
    print("  ── 累计（区间内）──")
    print("  浏览量 views    : %8s   ← 精确口径" % format(t["v"], ","))
    print("  访客 uniques    : %8s   ← 上限（跨天重复会计多次）" % format(t["u"], ","))
    print("  克隆量 clones   : %8s   ← 精确口径" % format(t["c"], ","))
    print("  克隆独立        : %8s   ← 上限" % format(t["cu"], ","))

    f = c.execute("SELECT SUM(forks) f, SUM(stars) s FROM repo_snapshot WHERE date=(SELECT MAX(date) FROM repo_snapshot)").fetchone()
    fe = c.execute("SELECT COUNT(*) n FROM fork_events").fetchone()["n"]
    print()
    print("  ── fork / star ──")
    print("  当前 forks      : %8s" % format(f["f"], ","))
    print("  当前 stars      : %8s" % format(f["s"], ","))
    print("  fork 事件记录   : %8s   ← 含 created_at，可回溯历史" % format(fe, ","))

    logs = c.execute("""SELECT run_at, mode, notes FROM collect_log
                        ORDER BY id DESC LIMIT 3""").fetchall()
    print()
    print("  ── 最近采集 ──")
    for l in logs:
        print("  %s  %-22s %s" % (l["run_at"], l["mode"], l["notes"]))


def cmd_daily(c):
    rows = c.execute("""
        SELECT date,
               SUM(views) v, SUM(unique_visitors) u,
               SUM(clones) cl
        FROM repo_traffic_daily
        GROUP BY date ORDER BY date
    """).fetchall()
    if not rows:
        print("  无数据")
        return
    mx = max(r["v"] or 0 for r in rows)
    print("=" * 64)
    print("每日趋势")
    print("=" * 64)
    print("  %-12s %8s %8s %8s  %s" % ("日期", "views", "访客", "clones", ""))
    for r in rows:
        print("  %-12s %8s %8s %8s  %s" % (
            r["date"], format(r["v"] or 0, ","), format(r["u"] or 0, ","),
            format(r["cl"] or 0, ","), bar(r["v"] or 0, mx)))


def cmd_top(c, n=20):
    rows = c.execute("""
        SELECT repo, SUM(views) v, SUM(unique_visitors) u, SUM(clones) cl
        FROM repo_traffic_daily GROUP BY repo
        ORDER BY SUM(views) DESC LIMIT ?
    """, (n,)).fetchall()
    print("=" * 64)
    print("浏览量 Top %d" % n)
    print("=" * 64)
    print("  %-44s %7s %7s %7s" % ("仓库", "views", "访客", "clones"))
    for i, r in enumerate(rows, 1):
        print("  %2d. %-40s %7s %7s %7s" % (
            i, r["repo"][:40], format(r["v"], ","), format(r["u"], ","), format(r["cl"], ",")))


def cmd_forks(c, n=15):
    latest = c.execute("SELECT MAX(date) d FROM repo_snapshot").fetchone()["d"]
    rows = c.execute("""
        SELECT repo, forks, stars, open_issues FROM repo_snapshot
        WHERE date=? ORDER BY forks DESC LIMIT ?
    """, (latest, n)).fetchall()
    print("=" * 64)
    print("Fork Top %d（快照日 %s）" % (n, latest))
    print("=" * 64)
    print("  %-44s %6s %6s %6s" % ("仓库", "forks", "stars", "issues"))
    for i, r in enumerate(rows, 1):
        print("  %2d. %-40s %6d %6d %6d" % (
            i, r["repo"][:40], r["forks"] or 0, r["stars"] or 0, r["open_issues"] or 0))

    tot = c.execute("SELECT SUM(forks) f, COUNT(*) n FROM repo_snapshot WHERE date=?", (latest,)).fetchone()
    print()
    print("  合计: %d 个 fork / %d 个仓库" % (tot["f"] or 0, tot["n"]))


def cmd_forks_new(c, days=30):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = c.execute("""
        SELECT repo, fork_full_name, created_at FROM fork_events
        WHERE substr(created_at,1,10) >= ?
        ORDER BY created_at DESC
    """, (since,)).fetchall()
    print("=" * 64)
    print("最近 %d 天新增 fork（since %s）" % (days, since))
    print("=" * 64)
    if not rows:
        print("  无新增")
        return
    print("  共 %d 个" % len(rows))
    for r in rows[:50]:
        print("  %s  %-40s <- %s" % (r["created_at"][:10], r["repo"][:40], r["fork_full_name"]))
    if len(rows) > 50:
        print("  ... 还有 %d 个" % (len(rows) - 50))


def cmd_repo(c, name):
    rows = c.execute("""SELECT date, views, unique_visitors, clones, unique_cloners
                        FROM repo_traffic_daily WHERE repo=? ORDER BY date""", (name,)).fetchall()
    snap = c.execute("""SELECT date, forks, stars FROM repo_snapshot
                        WHERE repo=? ORDER BY date DESC LIMIT 3""", (name,)).fetchall()
    forks = c.execute("""SELECT fork_full_name, created_at FROM fork_events
                         WHERE repo=? ORDER BY created_at DESC LIMIT 10""", (name,)).fetchall()
    print("=" * 64)
    print("仓库详情: %s" % name)
    print("=" * 64)
    if snap:
        s = snap[0]
        print("  forks=%s  stars=%s  (快照 %s)" % (s["forks"], s["stars"], s["date"]))
    if not rows:
        print("  无 traffic 数据")
    else:
        tv = sum(r["views"] for r in rows)
        tc = sum(r["clones"] for r in rows)
        print("  区间 views=%s  clones=%s" % (format(tv, ","), format(tc, ",")))
        print()
        print("  %-12s %7s %7s %7s %7s" % ("日期", "views", "访客", "clones", "克隆独立"))
        for r in rows:
            print("  %-12s %7d %7d %7d %7d" % (
                r["date"], r["views"], r["unique_visitors"], r["clones"], r["unique_cloners"]))
    if forks:
        print()
        print("  最近 fork:")
        for f in forks:
            print("    %s  %s" % (f["created_at"][:10], f["fork_full_name"]))


def cmd_export(c):
    outdir = os.path.join(os.path.dirname(DB), "reports")
    os.makedirs(outdir, exist_ok=True)
    import csv
    specs = [
        ("traffic_daily.csv", "SELECT * FROM repo_traffic_daily ORDER BY repo, date"),
        ("snapshot.csv", "SELECT * FROM repo_snapshot ORDER BY repo, date"),
        ("fork_events.csv", "SELECT * FROM fork_events ORDER BY repo, created_at"),
    ]
    for fname, sql in specs:
        cur = c.execute(sql)
        cols = [d[0] for d in cur.description]
        path = os.path.join(outdir, fname)
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows(cur.fetchall())
        print("  导出 %-22s -> %s" % (fname, path))


def main():
    c = conn()
    args = sys.argv[1:]
    cmd = args[0] if args else "overview"
    try:
        if cmd == "overview":
            cmd_overview(c)
        elif cmd == "daily":
            cmd_daily(c)
        elif cmd == "top":
            cmd_top(c, int(args[1]) if len(args) > 1 else 20)
        elif cmd == "forks":
            cmd_forks(c, int(args[1]) if len(args) > 1 else 15)
        elif cmd == "forks-new":
            cmd_forks_new(c, int(args[1]) if len(args) > 1 else 30)
        elif cmd == "repo":
            if len(args) < 2:
                sys.exit("用法: python view.py repo <仓库名>")
            cmd_repo(c, args[1])
        elif cmd == "export":
            cmd_export(c)
        else:
            print(__doc__)
    finally:
        c.close()


if __name__ == "__main__":
    main()
