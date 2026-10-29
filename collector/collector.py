#!/usr/bin/env python3
"""
huaweicloud-samples 组织访问量 / Fork 量长期采集器

为什么要自建：GitHub 的 traffic 接口只保留最近 14 天，且没有组织级聚合接口。
要拿到「一个月」的数据，必须自己每天落库累积。

关键设计：
  1. traffic 接口每天返回 14 天的逐日明细 —— 采集是自愈的：
     漏跑一天，第二天会自动把缺的那天补回来（只要间隔 < 14 天）。
  2. 每日只需 3 次请求就能拿到全部 276 个仓库的 forks/stars 快照
     （用 /orgs/{org}/repos 列表接口，per_page=100）。
  3. fork 历史可回溯：/repos/{o}/{r}/forks 返回每个 fork 的 created_at，
     所以即便今天才开始，也能立刻补出过去全部 fork 的时间线。
  4. 所有写入都是 UPSERT，重复运行不会产生脏数据。

用法:
    python collector.py init              # 建库
    python collector.py collect           # 每日采集（traffic + fork 快照）
    python collector.py forks-history     # 补全 fork 历史时间线
    python collector.py report --days 30  # 出报告
    python collector.py status            # 看采集状态/缺口

认证（按优先级）:
    1. 环境变量 GITHUB_TOKEN / GH_TOKEN
    2. config.json 里的 "token"
    3. git credential fill（Windows 凭据管理器）
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ORG = "huaweicloud-samples"
BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "hwc_traffic.db")
CONFIG_PATH = os.path.join(BASE, "config.json")
OUT_DIR = os.path.join(BASE, "reports")

_thread_local = threading.local()


# ---------------------------------------------------------------- auth
def get_token() -> str:
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        v = os.environ.get(var)
        if v:
            return v.strip()
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                tok = (json.load(f) or {}).get("token", "")
            if tok:
                return tok.strip()
        except Exception:
            pass
    # fallback: git credential manager
    try:
        p = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True, text=True, timeout=20,
        )
        for line in p.stdout.splitlines():
            if line.startswith("password="):
                return line[len("password="):].strip()
    except Exception:
        pass
    sys.exit("找不到 GitHub token：请设置 GITHUB_TOKEN 或写入 config.json")


def api(path: str, token: str, retries: int = 5):
    """GET api.github.com/path，返回 (data, headers)。带 403/429 退避。"""
    url = path if path.startswith("http") else "https://api.github.com" + path
    for attempt in range(1, retries + 1):
        req = Request(url, headers={
            "User-Agent": "hwc-collector/1.0",
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        try:
            with urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8")), dict(r.headers)
        except HTTPError as e:
            code = e.code
            try:
                body = json.loads(e.read().decode("utf-8"))
                msg = body.get("message", "")
            except Exception:
                msg = ""
            # 只有真正的限流才退避；权限类 403 立即失败，不浪费重试
            is_ratelimit = (code == 429) or (
                code == 403 and ("rate limit" in msg.lower() or "abuse" in msg.lower())
            )
            if is_ratelimit and attempt < retries:
                wait = min(60, 5 * attempt)
                print(f"    [{code}] 限流退避 {wait}s ...", file=sys.stderr)
                time.sleep(wait)
                continue
            if code == 404:
                return None, {}
            raise HTTPError(url, code, msg or str(e), e.headers, None)
        except URLError:
            if attempt < retries:
                time.sleep(2 * attempt)
                continue
            raise
    return None, {}


# ---------------------------------------------------------------- schema
SCHEMA = """
CREATE TABLE IF NOT EXISTS repo_traffic_daily (
    repo            TEXT NOT NULL,
    date            TEXT NOT NULL,          -- yyyy-mm-dd (UTC)
    views           INTEGER DEFAULT 0,
    unique_visitors INTEGER DEFAULT 0,
    clones          INTEGER DEFAULT 0,
    unique_cloners  INTEGER DEFAULT 0,
    collected_at    TEXT,
    PRIMARY KEY (repo, date)
);

CREATE TABLE IF NOT EXISTS repo_snapshot (
    repo        TEXT NOT NULL,
    date        TEXT NOT NULL,              -- 快照日 yyyy-mm-dd
    forks       INTEGER,
    stars       INTEGER,
    watchers    INTEGER,
    open_issues INTEGER,
    pushed_at   TEXT,
    PRIMARY KEY (repo, date)
);

CREATE TABLE IF NOT EXISTS fork_events (
    repo           TEXT NOT NULL,           -- 上游仓库
    fork_full_name TEXT NOT NULL,           -- fork 的 owner/repo
    fork_owner     TEXT,
    created_at     TEXT,                    -- fork 创建时间（可回溯）
    PRIMARY KEY (repo, fork_full_name)
);

CREATE TABLE IF NOT EXISTS collect_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at     TEXT,
    mode       TEXT,
    repos_ok   INTEGER,
    repos_fail INTEGER,
    notes      TEXT
);

CREATE INDEX IF NOT EXISTS idx_traffic_date ON repo_traffic_daily(date);
CREATE INDEX IF NOT EXISTS idx_snap_date    ON repo_snapshot(date);
CREATE INDEX IF NOT EXISTS idx_fork_created ON fork_events(created_at);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


# ---------------------------------------------------------------- collect
def api_check(path: str, token: str):
    """仅用于探测：返回 (错误说明, http_code)，成功则 ('OK', 200)。"""
    url = "https://api.github.com" + path
    req = Request(url, headers={
        "User-Agent": "hwc-collector/1.0",
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urlopen(req, timeout=45) as r:
            return "OK", r.status
    except HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8")).get("message", "")
        except Exception:
            msg = ""
        return f"HTTP {e.code}: {msg}", e.code
    except Exception as e:  # noqa: BLE001
        return str(e), 0


def list_repos(token: str) -> list[dict]:
    """列举组织全部仓库（含 forks_count / stars / pushed_at）。仅需 3 次请求。"""
    out, page = [], 1
    while True:
        data, _ = api(f"/orgs/{ORG}/repos?per_page=100&page={page}&type=all", token)
        if not data:
            break
        out.extend(data)
        if len(data) < 100:
            break
        page += 1
        time.sleep(0.2)
    return out


def fetch_traffic(repo: str, token: str):
    """单个仓库的 views + clones（各含 14 天逐日明细）。"""
    v, _ = api(f"/repos/{ORG}/{repo}/traffic/views?per=day", token)
    c, _ = api(f"/repos/{ORG}/{repo}/traffic/clones?per=day", token)
    return v, c


def cmd_collect(args, token: str) -> None:
    conn = connect()
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")

    print(f"[1/3] 列举 {ORG} 仓库 ...")
    repos = list_repos(token)
    if not repos:
        sys.exit("列举仓库失败")
    print(f"      共 {len(repos)} 个仓库")

    # --- 自动探测 traffic 可用性 ---
    # traffic 需要仓库 push/admin 权限。组织把 member 降为 read 后此接口会 403，
    # 但 forks/stars 是公开数据，只要有 pull 权限（甚至匿名 + 限额）就能采。
    traffic_ok = True
    if not args.no_traffic:
        err, code = api_check(f"/repos/{ORG}/{repos[0]['name']}/traffic/views?per=day", token)
        if code != 200:
            traffic_ok = False
            print(f"      ⚠ traffic 接口不可用（{err}）——仅采集 forks/stars")
            print("        原因: traffic 需要该仓库 push/admin 权限")
            print("        对策: 如需访问量，请让 org owner 为账号授予目标仓库的 push 权限\n")

    # --- fork / star 快照（廉价：3 次请求）---
    print(f"[2/3] 写入 forks/stars 快照 ...")
    snap_rows = [
        (r["name"], today, r.get("forks_count"), r.get("stargazers_count"),
         r.get("subscribers_count"), r.get("open_issues_count"), r.get("pushed_at"))
        for r in repos
    ]
    conn.executemany(
        """INSERT INTO repo_snapshot (repo,date,forks,stars,watchers,open_issues,pushed_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(repo,date) DO UPDATE SET
             forks=excluded.forks, stars=excluded.stars, watchers=excluded.watchers,
             open_issues=excluded.open_issues, pushed_at=excluded.pushed_at""",
        snap_rows,
    )
    conn.commit()
    print(f"      {len(snap_rows)} 行快照已写入")

    if not traffic_ok:
        ts0 = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "INSERT INTO collect_log (run_at,mode,repos_ok,repos_fail,notes) VALUES (?,?,?,?,?)",
            (ts0, "collect-snapshot-only", len(repos), 0,
             "traffic 403 (need push); forks/stars only"),
        )
        conn.commit()
        print(f"[3/3] 跳过 traffic（无 push 权限）")
        print(f"\n完成: {len(repos)} 个仓库的 forks/stars 快照已更新")
        conn.close()
        return

    # --- 逐仓库 traffic（14 天窗口，UPSERT 自愈）---
    print(f"[3/3] 拉取 traffic 明细（{len(repos)} 仓库 × 2 请求）...")
    ok = fail = 0
    rows = []
    done = 0
    total = len(repos)
    lock = threading.Lock()

    def work(r):
        name = r["name"]
        try:
            v, c = fetch_traffic(name, token)
            recs = []
            if v:
                for d in v.get("views", []):
                    recs.append((name, d["timestamp"][:10], d["count"], d["uniques"], 0, 0))
            if c:
                cmap = {d["timestamp"][:10]: d for d in c.get("clones", [])}
                merged = {}
                for rec in recs:
                    merged[rec[1]] = list(rec)
                for day, d in cmap.items():
                    if day in merged:
                        merged[day][4] = d["count"]
                        merged[day][5] = d["uniques"]
                    else:
                        merged[day] = [name, day, 0, 0, d["count"], d["uniques"]]
                recs = [tuple(x) for x in merged.values()]
            return name, recs, None
        except Exception as e:  # noqa: BLE001
            return name, [], str(e)

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(work, r): r for r in repos}
        for fut in as_completed(futures):
            name, recs, err = fut.result()
            with lock:
                done += 1
                if err:
                    fail += 1
                    print(f"      ✗ {name}: {err}", file=sys.stderr)
                else:
                    ok += 1
                    rows.extend(recs)
                if done % 40 == 0:
                    print(f"      {done}/{total}  (ok={ok} fail={fail})")
            time.sleep(args.sleep)

    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.executemany(
        """INSERT INTO repo_traffic_daily
             (repo,date,views,unique_visitors,clones,unique_cloners,collected_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(repo,date) DO UPDATE SET
             views=excluded.views, unique_visitors=excluded.unique_visitors,
             clones=excluded.clones, unique_cloners=excluded.unique_cloners,
             collected_at=excluded.collected_at""",
        [tuple(list(r) + [ts]) for r in rows],
    )
    conn.execute(
        "INSERT INTO collect_log (run_at,mode,repos_ok,repos_fail,notes) VALUES (?,?,?,?,?)",
        (ts, "collect", ok, fail, f"{len(rows)} traffic rows"),
    )
    conn.commit()

    dates = [r[1] for r in rows]
    print(f"\n完成: {ok} 成功 / {fail} 失败, 写入 {len(rows)} 行 traffic")
    if dates:
        print(f"数据日期范围: {min(dates)} ~ {max(dates)}")
    conn.close()


# ---------------------------------------------------------------- forks history
def cmd_forks_history(args, token: str) -> None:
    conn = connect()
    repos = list_repos(token)
    print(f"枚举 {len(repos)} 个仓库的 fork 明细（可回溯完整历史）...")

    total_new = 0
    for i, r in enumerate(repos, 1):
        name = r["name"]
        expected = r.get("forks_count") or 0
        if expected == 0:
            continue
        existing = conn.execute(
            "SELECT COUNT(*) FROM fork_events WHERE repo=?", (name,)
        ).fetchone()[0]
        if existing >= expected and not args.force:
            continue

        got = 0
        page = 1
        while page <= 20:
            data, _ = api(
                f"/repos/{ORG}/{name}/forks?per_page=100&page={page}&sort=oldest",
                token,
            )
            if not data:
                break
            for f in data:
                conn.execute(
                    """INSERT INTO fork_events (repo,fork_full_name,fork_owner,created_at)
                       VALUES (?,?,?,?)
                       ON CONFLICT(repo,fork_full_name) DO UPDATE SET
                         created_at=COALESCE(fork_events.created_at, excluded.created_at)""",
                    (name, f.get("full_name"), (f.get("owner") or {}).get("login"),
                     f.get("created_at")),
                )
                got += 1
            if len(data) < 100:
                break
            page += 1
            time.sleep(0.2)
        conn.commit()
        total_new += got
        print(f"  [{i}/{len(repos)}] {name}: {got} 个 fork")
        time.sleep(args.sleep)

    conn.execute(
        "INSERT INTO collect_log (run_at,mode,repos_ok,repos_fail,notes) VALUES (?,?,?,?,?)",
        (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "forks-history", len(repos), 0, f"{total_new} fork rows"),
    )
    conn.commit()
    grand = conn.execute("SELECT COUNT(*) FROM fork_events").fetchone()[0]
    print(f"\n完成。fork_events 累计 {grand} 行")
    conn.close()


# ---------------------------------------------------------------- report
def cmd_report(args, token: str | None) -> None:
    conn = connect()
    os.makedirs(OUT_DIR, exist_ok=True)
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=args.days - 1)
    s, e = start.isoformat(), end.isoformat()

    print(f"===== 报告区间 {s} ~ {e}（{args.days} 天，UTC）=====\n")

    # 数据完整性
    have = conn.execute(
        "SELECT COUNT(DISTINCT date), MIN(date), MAX(date) FROM repo_traffic_daily"
        " WHERE date BETWEEN ? AND ?", (s, e)
    ).fetchone()
    span_days = (end - start).days + 1
    print(f"数据覆盖: {have[0]}/{span_days} 天  ({have[1]} ~ {have[2]})")
    if have[0] < span_days:
        print("  ⚠ 存在日期缺口（采集启动前/中断期）——那些天永久缺失，无法补回\n")
    else:
        print()

    r = conn.execute(
        """SELECT SUM(views), SUM(unique_visitors), SUM(clones), SUM(unique_cloners),
                  COUNT(DISTINCT repo), COUNT(DISTINCT date)
           FROM repo_traffic_daily WHERE date BETWEEN ? AND ?""", (s, e)
    ).fetchone()
    total_views, sum_day_uniques, total_clones, sum_day_cloners = (r[0] or 0), (r[1] or 0), (r[2] or 0), (r[3] or 0)
    n_repo, n_day = r[4], r[5]

    print("【总量】")
    print(f"  浏览量 views      : {total_views:>8,}   (精确可加总)")
    print(f"  访客 uniques(上限) : {sum_day_uniques:>8,}   (逐日相加，重复访客会被多次计数)")
    print(f"  克隆量 clones     : {total_clones:>8,}   (精确可加总)")
    print(f"  克隆独立(上限)     : {sum_day_cloners:>8,}")
    print(f"  有数据仓库数       : {n_repo:>8,}")
    print(f"  有数据天数         : {n_day:>8,}")

    # fork 变化
    def forks_at(day):
        row = conn.execute(
            "SELECT SUM(forks) FROM repo_snapshot WHERE date="
            "(SELECT MAX(date) FROM repo_snapshot WHERE date<=?)", (day,)
        ).fetchone()
        return row[0]

    f_start, f_end = forks_at(start.isoformat()), forks_at(e)
    if f_start is not None and f_end is not None:
        print("\n【Fork】")
        print(f"  期初 forks : {f_start:,}")
        print(f"  期末 forks : {f_end:,}")
        print(f"  区间净增   : {f_end - f_start:+,}")

    fe = conn.execute(
        "SELECT COUNT(*) FROM fork_events WHERE substr(created_at,1,10) BETWEEN ? AND ?",
        (s, e)
    ).fetchone()[0]
    if fe:
        print(f"  区间新增 fork(按 created_at 精确口径): {fe}")

    print("\n【Top 20 仓库（按浏览量）】")
    print(f"  {'仓库':<44}{'views':>8}{'uniq(日)':>10}{'clones':>9}")
    top = conn.execute(
        """SELECT repo, SUM(views), SUM(unique_visitors), SUM(clones)
           FROM repo_traffic_daily WHERE date BETWEEN ? AND ?
           GROUP BY repo ORDER BY SUM(views) DESC LIMIT 20""", (s, e)
    ).fetchall()
    for name, v, u, c in top:
        print(f"  {name[:43]:<44}{v:>8,}{u:>10,}{c:>9,}")

    print("\n【Top 15 fork 增长】")
    fg = conn.execute(
        """SELECT a.repo, b.forks - a.forks AS delta, b.forks
           FROM repo_snapshot a JOIN repo_snapshot b
             ON a.repo=b.repo
           WHERE a.date=(SELECT MIN(date) FROM repo_snapshot WHERE date BETWEEN ? AND ?)
             AND b.date=(SELECT MAX(date) FROM repo_snapshot WHERE date BETWEEN ? AND ?)
             AND b.forks > a.forks
           ORDER BY delta DESC LIMIT 15""", (s, e, s, e)
    ).fetchall()
    if fg:
        for name, delta, cur in fg:
            print(f"  {name[:43]:<44} +{delta:<4} (现 {cur})")
    else:
        print("  （区间内暂无 fork 增长，或快照天数不足）")

    # 导出
    out_csv = os.path.join(OUT_DIR, f"report_{s}_{e}.csv")
    with open(out_csv, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["repo", "views", "unique_visitors_daily_sum", "clones", "unique_cloners_daily_sum"])
        w.writerows(conn.execute(
            """SELECT repo, SUM(views), SUM(unique_visitors), SUM(clones), SUM(unique_cloners)
               FROM repo_traffic_daily WHERE date BETWEEN ? AND ?
               GROUP BY repo ORDER BY SUM(views) DESC""", (s, e)
        ).fetchall())
    print(f"\n明细已导出: {out_csv}")
    conn.close()


def cmd_status(args, token=None) -> None:
    conn = connect()
    print("===== 采集状态 =====\n")
    for tbl, label in (("repo_traffic_daily", "traffic"), ("repo_snapshot", "snapshot"),
                       ("fork_events", "fork_events")):
        n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        print(f"  {label:<14} {n:>8,} 行")

    d = conn.execute(
        "SELECT MIN(date), MAX(date), COUNT(DISTINCT date) FROM repo_traffic_daily"
    ).fetchone()
    print(f"\n  traffic 日期范围 : {d[0]} ~ {d[1]}  ({d[2]} 天)")

    print("\n  最近采集记录:")
    for row in conn.execute(
        "SELECT run_at,mode,repos_ok,repos_fail,notes FROM collect_log ORDER BY id DESC LIMIT 8"
    ):
        print(f"    {row[0]}  {row[1]:<14} ok={row[2]:<4} fail={row[3]:<3} {row[4]}")

    # 缺口检测
    rows = conn.execute(
        "SELECT DISTINCT date FROM repo_traffic_daily ORDER BY date"
    ).fetchall()
    if rows:
        dates = [datetime.strptime(r[0], "%Y-%m-%d").date() for r in rows]
        today = datetime.now(timezone.utc).date()
        gaps, cur = [], dates[0]
        while cur <= today:
            if cur not in set(dates):
                gaps.append(cur.isoformat())
            cur += timedelta(days=1)
        if gaps:
            print(f"\n  ⚠ 缺失日期 {len(gaps)} 天: {', '.join(gaps[:14])}"
                  + (" ..." if len(gaps) > 14 else ""))
            print("    （若缺口 < 14 天，再跑一次 collect 会自动补回；否则永久缺失）")
        else:
            print("\n  ✓ 无日期缺口")
    conn.close()


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description="huaweicloud-samples 访问量与 fork 长期采集器")
    sub = ap.add_subparsers(dest="mode", required=True)

    sub.add_parser("init", help="初始化数据库")

    p = sub.add_parser("collect", help="每日采集 traffic + fork 快照")
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--sleep", type=float, default=0.50)
    p.add_argument("--no-traffic", action="store_true", help="只采 forks/stars，跳过 traffic")

    p = sub.add_parser("forks-history", help="补全 fork 历史时间线（可回溯）")
    p.add_argument("--force", action="store_true", help="强制重扫已有仓库")
    p.add_argument("--sleep", type=float, default=0.25)

    p = sub.add_parser("report", help="生成区间报告")
    p.add_argument("--days", type=int, default=30)

    sub.add_parser("status", help="查看采集状态与缺口")

    args = ap.parse_args()

    if args.mode == "init":
        connect().close()
        print(f"数据库已就绪: {DB_PATH}")
        return
    if args.mode == "status":
        cmd_status(args)
        return

    token = get_token()
    if args.mode == "collect":
        cmd_collect(args, token)
    elif args.mode == "forks-history":
        cmd_forks_history(args, token)
    elif args.mode == "report":
        cmd_report(args, token)


if __name__ == "__main__":
    main()
