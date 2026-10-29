# 云端定时采集 huaweicloud-samples 访问量与 Fork

每天由 GitHub Actions 自动采集，**不需要本地机器开机，也不需要本地保存 owner 凭据**。

## 它能采什么

| 数据 | 来源 | 能否回溯历史 |
|---|---|---|
| 浏览量 views / 独立访客 uniques | `/traffic/views` | ❌ 只有最近 14 天 |
| 克隆量 clones | `/traffic/clones` | ❌ 只有最近 14 天 |
| forks / stars 快照 | `/orgs/{org}/repos` | ❌ 只能从今天起累积 |
| **fork 事件时间线** | `/repos/{o}/{r}/forks` 的 `created_at` | ✅ **可完整回溯** |

> 仓库里已带一份历史数据库 `hwc_traffic.db`，含 2026-10-14 ~ 10-29 共 16 天、276 个仓库的 traffic 数据。
> 首次部署就会自动续上，不会丢。

---

## 部署步骤

### 第 0 步：确认你已登录 owner 账号

浏览器打开 https://github.com 右上角确认用户名是 **org owner 账号**。
后续所有操作都用这个账号。

---

### 第 1 步：创建 GitHub App（一次性，约 3 分钟）

> 为什么不直接用 owner PAT：PAT 是长期密钥，泄露等于交出组织全部仓库的 admin。
> App 令牌 1 小时自动过期，且可以只给 `Administration: Read`，风险低一个量级。

打开 https://github.com/settings/apps/new

**Basic information**

| 字段 | 填什么 |
|---|---|
| GitHub App name | `hwc-traffic-collector`（名字全局唯一，可加随机后缀） |
| Homepage URL | `https://github.com/huaweicloud-samples` |
| Webhook → Active | **取消勾选**（不需要） |

**Repository permissions**

| 权限 | 设置为 |
|---|---|
| **Administration** | **Read-only** ← traffic 接口需要这个 |
| Metadata | Read-only（自动） |
| 其他全部 | No access |

**Where can this GitHub App be installed?**
选 → **Any account**

点 **Create GitHub App**。

创建完成后：

1. 记下页面顶部的 **App ID**（一串数字）
2. 滚到底部 **Private keys** → **Generate a private key**
3. 会自动下载一个 `.pem` 文件，**用记事本打开，全文复制**（包含 `-----BEGIN RSA PRIVATE KEY-----` 和结尾行）

---

### 第 2 步：把 App 安装到组织

1. App 设置页左侧 → **Install App**
2. 点 `huaweicloud-samples` 右侧的 **Install**
3. Repository access 选 **All repositories**（282 个都要采）
4. 点 **Install**

> 只有 org owner 能做这一步。装完后 owner 就可以关掉页面，之后不再需要 owner 操作。

---

### 第 3 步：创建采集仓库

组织设置了 `members_can_create_repositories = false`，你在组织里建不了库，所以在**自己账号**下建。

打开 https://github.com/new

| 字段 | 填什么 |
|---|---|
| Repository name | `hwc-traffic-collector` |
| 可见性 | **Private**（推荐） |
| 勾选 | **Add a README file** |

点 **Create repository**。

---

### 第 4 步：配置 App 凭据到仓库

进入刚建的仓库 → **Settings** → 左侧 **Secrets and variables** → **Actions**

**Variables** 标签 → **New repository variable**
```
Name : APP_ID
Value: <第1步记下的 App ID>
```

**Secrets** 标签 → **New repository secret**
```
Name : APP_PRIVATE_KEY
Secret: <粘贴 .pem 文件全文>
```

> 如果嫌麻烦想用 PAT：忽略上面两步，改成在 Secrets 里加
> `ORG_OWNER_PAT = ghp_xxx`（owner 账号生成，勾 `repo`）。
> workflow 会自动回落到 PAT 分支。但仍建议用 App。

---

### 第 5 步：推送代码

**方式 A：在网页上传（不用命令行）**

1. 把 `E:\code\auto-workflows\hwc-traffic-collector\` 下所有内容准备好
2. 仓库页面点 **Add file** → **Upload files**
3. **注意**：`.github` 是隐藏目录，网页拖拽可能漏掉。用下面的方式更稳。

**方式 B：Git 命令行（推荐）**

```powershell
cd E:\code\auto-workflows\hwc-traffic-collector

git init
git add -A
git commit -m "init: traffic collector"
git branch -M main
git remote add origin https://github.com/wangliang765/hwc-traffic-collector.git
git push -u origin main
```

> 仓库里已有 `.github/workflows/collect.yml`，推送后 Actions 会自动识别。
> 若首次推送提示需要凭据，GCM 会弹出浏览器登录——登录 **owner 账号**（因为 App 是本账号创建的，
> 而 token 由 App 自己签发，推送用哪个账号都行，只要对该仓库有写权限）。

---

### 第 6 步：启用并首次运行

1. 仓库 → **Actions** 标签页
2. 若出现提示，点 **I understand my workflows, go ahead and enable them**
3. 左侧选 **Collect traffic & forks** → 右侧 **Run workflow**
4. `backfill` 选 **true**（首次会补全 fork 历史）
5. 点绿色 **Run workflow**

---

### 第 7 步：验证

等 2-5 分钟，点进那次运行看日志：

**预期日志（成功）**

```
══════════ 采集前自检 ══════════
[1/4] token 身份
     login = <owner-account>
[2/4] 组织成员身份
     huaweicloud-samples: state=active role=admin
     ✓ org owner → 对全部仓库有 admin，traffic 应可用
[3/4] 仓库枚举
     第一页取得 100 个仓库
[4/4] traffic 接口（关键）
     ✓ 可读 traffic（iaas-mcp-server: views=xxx uniques=xxx）
══════════ 自检结束 ══════════
✅ 全部通过，开始采集
```

然后 `Collect` 步骤会打印进度，最后 `Commit data` 显示 `已提交`。

**验证数据确实进来了**：刷新仓库首页，会看到 `hwc_traffic.db` 有新的 commit；
点进 Actions → 该次运行 → 底部 **Summary** 能看到采集状态表格。

---

## 日常运维

### 查看每日结果

仓库 → **Actions** → 最近一次运行 → **Summary**

### 手动跑一次

Actions → **Collect traffic & forks** → **Run workflow**

### 出 30 天报告

在 **Actions** 里加一个手动 step，或者本地拉下数据库跑：

```powershell
git clone https://github.com/wangliang765/hwc-traffic-collector.git
cd hwc-traffic-collector
python collector.py report --days 30
```

报告会落到 `reports/` 目录。

### 换 token / 撤销

- App：组织 Settings → GitHub Apps → 配置 → 可随时 **Suspend** 或卸载
- PAT：https://github.com/settings/tokens 删除对应 token
- 两者都不影响已采集的历史数据

### 排查

| 现象 | 原因 | 处理 |
|---|---|---|
| Preflight 报 `403 Must have push access` | App 没装到组织，或没给 Administration:Read | 重新检查第 1、2 步 |
| `未配置鉴权` | `APP_ID` 变量或 `APP_PRIVATE_KEY` 没设 | 检查第 4 步 |
| Actions 不自动跑 | 仓库 60 天无活动被禁用 | 手动 Run workflow 一次即可恢复 |
| cron 没准点 | GitHub 排队抖动，正常现象 | 无影响，按日聚合 |
| Commit 步骤失败 | 仓库 Settings → Actions → Workflow permissions 不是 Read and write | 改成 **Read and write permissions** |

> 最后一条最容易踩：新仓库默认可能是只读 token，导致无法 commit。
> 路径：仓库 **Settings** → **Actions** → **General** → **Workflow permissions**
> → 选 **Read and write permissions** → **Save**

---

## 数据口径说明

### views / clones —— 可精确加总
跨天不重复计数，`SUM` 就是真实总量。

### uniques —— 是上限，会重复计数

| 字段 | 含义 |
|---|---|
| 顶层 `.uniques` | 整个 14 天窗口内去重 |
| 每日 `.uniques[i]` | 当天内去重 |

**实测证据**：276 个仓库中 33 个出现 `sum(每日) > 顶层`，243 个相等，0 个反例。
例：`iaas-mcp-server` 每日相加 134，顶层 117，差 17 = 多天重复访客被合并。

全组织：14 天去重 852 人 vs 逐日相加 1010，重复率 **15.6%**。

**结论**：跨天拼接时 unique 只能给上限。要精确总访客，只能用单次 14 天窗口。

### 访客识别靠 IP + User-Agent，不是登录身份

- 同一人换网络/设备 → 多个访客
- 公司 NAT 出口多人 → 可能合并成 1 个
- Bot / 爬虫同样计入

所以 `cloudrobo-samples` 出现 19 个访客刷 534 次浏览——那是内部 PR 评审噪声。
**`uniques` 不建议当"真实人数"汇报，看趋势和量级更合适。**

---

## 可靠性设计

| 机制 | 作用 |
|---|---|
| UPSERT 写入 | 可安全重跑，不产生脏数据 |
| 14 天窗口自愈 | 漏跑 1 天，次日自动补回（只要间隔 < 14 天） |
| Preflight 自检 | 权限不对**立即失败**，不写空数据 |
| 缺口语义分离 | `0` = 确实无人访问；`NULL` = 那天没采 |
| Artifact 备份 | 每次运行快照数据库，留 90 天 |
| 自动 commit | 数据库即长期存储，且天然产生活动避免被禁用 |

### 已知限制

- **部署前的 traffic 永久缺失**（GitHub 不提供历史），仓库里已带 10-14~10-29 的数据
- **数据库约 0.6 MB**，每天增长 < 100 KB，一年后约 30 MB，仍在 GitHub 单文件 100 MB 限制内
- **fork 数是聚合值**，偶尔滞后几分钟；精确口径用 `fork_events.created_at`
