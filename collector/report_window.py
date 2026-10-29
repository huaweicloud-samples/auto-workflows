"""固定 N 天滚动窗口报告（默认 30 天）

与 collector.py report 的区别：
  · collector.py report --days N  → 打印到终端，导出单文件汇总
  · 本脚本                      → 严格以「今天往前 N 天」为窗口，
                                   导出多张表（汇总/每日/仓库明细/fork），
                                   并明确标注数据完整性

窗口定义（UTC）：
  end   = 今天（含）
  start = 今天 - (N-1) 天
  例：N=30，今天 2026-10-29 → 窗口 2026-09-30 ~ 2026-10-29

用法：
  python report_window.py                     # 30 天，输出到 reports/
  python report_window.py --days 7            # 7 天
  python report_window.py --end 2026-10-27    # 指定截止日（回溯查看历史窗口）
  python report_window.py --db path\\to.db    # 指定数据库
"""
import argparse
import csv
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(BASE, "hwc_traffic.db")
OUT_DIR = os.path.join(BASE, "reports")


def connect(path):
    if not os.path.exists(path):
        sys.exit("数据库不存在: %s" % path)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def resolve_window(conn, days, end_str):
    """确定窗口，并核对数据覆盖"""
    if end_str:
        end = datetime.strptime(end_str, "%Y-%m-%d").date()
    else:
        # 用数据库里最大日期与今天取较晚者？不 —— 严格用今天，
        # 这样窗口语义固定，缺数据会被明确暴露而非掩盖。
        end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days - 1)
    return start, end


def write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return path


def main():
    ap = argparse.ArgumentParser(description="固定 N 天滚动窗口报告")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--end", default=None, help="截止日 yyyy-mm-dd，默认今天(UTC)")
    ap.add_argument("--db", default=os.environ.get("HWC_DB") or DEFAULT_DB)
    ap.add_argument("--outdir", default=OUT_DIR)
    args = ap.parse_args()

    conn = connect(args.db)
    os.makedirs(args.outdir, exist_ok=True)
    start, end = resolve_window(conn, args.days, args.end)
    s, e = start.isoformat(), end.isoformat()
    ndays = (end - start).days + 1

    # ── 数据完整性 ─────────────────────────────────────────
    cov = conn.execute(
        """SELECT COUNT(DISTINCT date) d,
                  MIN(date) a, MAX(date) b
           FROM repo_traffic_daily WHERE date BETWEEN ? AND ?""", (s, e)
    ).fetchone()
    have = cov["d"] or 0
    missing = ndays - have

    print("=" * 68)
    print("固定 %d 天滚动窗口报告" % args.days)
    print("=" * 68)
    print("  窗口范围   : %s ~ %s   (%d 天)" % (s, e, ndays))
    print("  数据覆盖   : %d / %d 天" % (have, ndays))
    if missing > 0:
        # 列出缺哪些天
        got = {r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM repo_traffic_daily WHERE date BETWEEN ? AND ?", (s, e))}
        gaps = []
        cur = start
        while cur <= end:
            if cur.isoformat() not in got:
                gaps.append(cur.isoformat())
            cur += timedelta(days=1)
        print("  缺失日期   : %d 天  %s%s" % (
            len(gaps), ", ".join(gaps[:10]), " ..." if len(gaps) > 10 else ""))
        print("               （采集启动前/中断期，无法补回）")
        print("  ⚠ 本报告为【不完整窗口】，请以数据覆盖为准")
    else:
        print("  ✓ 窗口数据完整")
    print()

    # ── 总量 ───────────────────────────────────────────────
    t = conn.execute(
        """SELECT SUM(views) v, SUM(unique_visitors) u,
                  SUM(clones) c, SUM(unique_cloners) cu,
                  COUNT(DISTINCT repo) n
           FROM repo_traffic_daily WHERE date BETWEEN ? AND ?""", (s, e)
    ).fetchone()
    print("【总量】")
    print("  浏览量 views      : %10s   ← 精确口径，可跨天相加" % format(t["v"] or 0, ","))
    print("  访客 uniques      : %10s   ← 上限值，跨天重复会重复计" % format(t["u"] or 0, ","))
    print("  克隆量 clones     : %10s   ← 精确口径" % format(t["c"] or 0, ","))
    print("  克隆独立          : %10s   ← 上限值" % format(t["cu"] or 0, ","))
    print("  有流量仓库数      : %10s" % format(t["n"] or 0, ","))
    if have:
        print("  日均浏览          : %10.0f" % ((t["v"] or 0) / have))
        print("  日均访客(日去重)  : %10.0f" % ((t["u"] or 0) / have))

    # ── fork 变化 ──────────────────────────────────────────
    def forks_at(d):
        r = conn.execute(
            "SELECT SUM(forks) f FROM repo_snapshot WHERE date="
            "(SELECT MAX(date) FROM repo_snapshot WHERE date<=?)", (d,)
        ).fetchone()
        return r["f"]

    f0, f1 = forks_at(s), forks_at(e)
    print()
    print("【Fork】")
    if f0 is not None and f1 is not None:
        print("  窗口初 forks      : %10s" % format(f0, ","))
        print("  窗口末 forks      : %10s" % format(f1, ","))
        print("  净增              : %+10d" % (f1 - f0))
    fe = conn.execute(
        "SELECT COUNT(*) n FROM fork_events WHERE substr(created_at,1,10) BETWEEN ? AND ?",
        (s, e)).fetchone()["n"]
    print("  窗口内新增 fork   : %10s   ← 按 created_at 精确口径" % format(fe, ","))

    # ── 导出 ───────────────────────────────────────────────
    tag = "%dd_%s_%s" % (ndays, s, e)
    files = []

    # 1. 仓库汇总
    rows = conn.execute(
        """SELECT repo, SUM(views) v, SUM(unique_visitors) u, SUM(clones) c,
                  SUM(unique_cloners) cu
           FROM repo_traffic_daily WHERE date BETWEEN ? AND ?
           GROUP BY repo ORDER BY SUM(views) DESC""", (s, e)).fetchall()
    files.append(write_csv(
        os.path.join(args.outdir, "window_%s_by_repo.csv" % tag),
        ["repo", "views", "unique_visitors_daily_sum", "clones",
         "unique_cloners_daily_sum", "days_with_data"],
        [[r["repo"], r["v"], r["u"], r["c"], r["cu"],
          conn.execute("SELECT COUNT(*) FROM repo_traffic_daily WHERE repo=? AND date BETWEEN ? AND ?",
                       (r["repo"], s, e)).fetchone()[0]] for r in rows]))

    # 2. 每日汇总
    rows = conn.execute(
        """SELECT date, SUM(views) v, SUM(unique_visitors) u, SUM(clones) c
           FROM repo_traffic_daily WHERE date BETWEEN ? AND ?
           GROUP BY date ORDER BY date""", (s, e)).fetchall()
    files.append(write_csv(
        os.path.join(args.outdir, "window_%s_daily.csv" % tag),
        ["date", "views", "unique_visitors", "clones"],
        [[r["date"], r["v"], r["u"], r["c"]] for r in rows]))

    # 3. 全量明细（仓库 × 日期）
    rows = conn.execute(
        """SELECT repo, date, views, unique_visitors, clones, unique_cloners
           FROM repo_traffic_daily WHERE date BETWEEN ? AND ?
           ORDER BY repo, date""", (s, e)).fetchall()
    files.append(write_csv(
        os.path.join(args.outdir, "window_%s_detail.csv" % tag),
        ["repo", "date", "views", "unique_visitors", "clones", "unique_cloners"],
        [[r["repo"], r["date"], r["views"], r["unique_visitors"],
          r["clones"], r["unique_cloners"]] for r in rows]))

    # 4. fork 快照（窗口末）
    rows = conn.execute(
        """SELECT repo, forks, stars, open_issues FROM repo_snapshot
           WHERE date=(SELECT MAX(date) FROM repo_snapshot WHERE date<=?)
           ORDER BY forks DESC""", (e,)).fetchall()
    files.append(write_csv(
        os.path.join(args.outdir, "window_%s_forks.csv" % tag),
        ["repo", "forks", "stars", "open_issues"],
        [[r["repo"], r["forks"], r["stars"], r["open_issues"]] for r in rows]))

    # 5. fork 事件（窗口内新增）
    rows = conn.execute(
        """SELECT repo, fork_full_name, created_at FROM fork_events
           WHERE substr(created_at,1,10) BETWEEN ? AND ?
           ORDER BY created_at DESC""", (s, e)).fetchall()
    files.append(write_csv(
        os.path.join(args.outdir, "window_%s_new_forks.csv" % tag),
        ["repo", "fork_full_name", "created_at"],
        [[r["repo"], r["fork_full_name"], r["created_at"]] for r in rows]))

    print()
    print("【导出文件】")
    for f in files:
        print("  %-58s %8d bytes" % (os.path.basename(f), os.path.getsize(f)))

    # ── Top 列表（终端） ───────────────────────────────────
    print()
    print("【Top 15 仓库（按浏览量）】")
    print("  %-44s %8s %8s %8s" % ("仓库", "views", "访客", "clones"))
    for r in conn.execute(
        """SELECT repo, SUM(views) v, SUM(unique_visitors) u, SUM(clones) c
           FROM repo_traffic_daily WHERE date BETWEEN ? AND ?
           GROUP BY repo ORDER BY SUM(views) DESC LIMIT 15""", (s, e)):
        print("  %-44s %8s %8s %8s" % (
            r["repo"][:43], format(r["v"], ","), format(r["u"], ","), format(r["c"], ",")))

    conn.close()
    print()
    print("完成。")


if __name__ == "__main__":
    main()
