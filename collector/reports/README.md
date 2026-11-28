# 报告文件说明

本目录由每日 Actions 自动生成，**每次运行会先删除旧的 `window_*` 再重新生成**，
所以这里始终只有**最新一份** 30 天滚动窗口报告。

## 文件清单

| 文件 | 行数（含表头） | 内容 |
|---|---|---|
| `window_30d_<start>_<end>_by_repo.csv` | 仓库数 + 1 | 每仓库汇总：views / 访客 / clones / 克隆独立 / 有数据天数 |
| `window_30d_<start>_<end>_daily.csv` | 30 + 1 | 每日全组织汇总 |
| `window_30d_<start>_<end>_detail.csv` | 仓库数×天数 + 1 | 全量明细：仓库 × 日期 |
| `window_30d_<start>_<end>_forks.csv` | 仓库数 + 1 | 窗口末的 fork / star / issue 快照 |
| `window_30d_<start>_<end>_new_forks.csv` | 新增数 + 1 | 窗口内新增的 fork 事件（按 created_at） |

> ⚠️ 用 Excel 打开时**行数 = 数据行 + 1（表头）**。
> 例如 444 个仓库的文件，Excel 会显示 445 行。

## 窗口定义

```
end   = 运行当天（UTC）
start = end - 29 天
```

每天向前滚动一天。仓库名里的日期就是这个窗口。

## 口径说明（重要）

| 字段 | 口径 | 能否跨天相加 |
|---|---|---|
| `views` | 浏览量 | ✅ 精确 |
| `clones` | 克隆量 | ✅ 精确 |
| `unique_visitors_daily_sum` | **每日**去重后相加 | ❌ **上限值**，跨天重复访客会被多次计数（实测虚高约 16.5%） |
| `unique_cloners_daily_sum` | 同上 | ❌ 上限值 |

GitHub 的 `uniques` 只在**单次查询窗口内**去重，逐日相加必然重复。
若需要精确访客数，只能用单次 14 天窗口的顶层值。

## 完整历史在哪

报告是**滚动窗口**，旧文件会删除。**完整历史永久保存在**：

```
collector/hwc_traffic.db
```

表结构：
- `repo_traffic_daily` — 每仓库每日 views/clones（永久累积，从不删除）
- `repo_snapshot` — 每仓库每日 forks/stars 快照
- `fork_events` — 每个 fork 的创建时间（可回溯全部历史）

随时可以重新生成任意区间：

```bash
python collector.py report --days 365        # 用全库
python report_window.py --end 2026-11-01     # 回溯历史窗口
python view.py daily                         # 全部日期的趋势
```