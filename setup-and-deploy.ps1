# 本地一次性配置 GitHub Actions 的 Variables / Secrets，然后推送触发云端定时采集
#
# 用途：把「网页上手动点 N 次」变成「本地跑一次脚本」。
#       配置完成后推送到远程仓库，Actions 会按 cron 每天自动跑。
#
# 前提：装好 gh CLI 并完成 gh auth login（owner 账号）
#
# 用法：
#   .\setup-and-deploy.ps1 -AppId 5522782 -PemPath "C:\path\to\key.pem"
#   .\setup-and-deploy.ps1 -AppId 5522782 -PemPath "..." -Push
#   .\setup-and-deploy.ps1 -Status                 # 只看当前配置状态
#   .\setup-and-deploy.ps1 -TriggerNow             # 手动触发一次 workflow
#
# 参数说明：
#   -AppId       GitHub App 的 App ID（数字）
#   -PemPath     App 私钥 .pem 文件路径
#   -Pat         也可改用 owner PAT（不给 AppId/PemPath 时使用）
#   -Push        配置完成后自动 git push
#   -TriggerNow  配置完成后立即触发一次 workflow_run（不等待，验证用）

[CmdletBinding()]
param(
    [string]$AppId,
    [string]$PemPath,
    [string]$Pat,
    [switch]$Status,
    [switch]$Push,
    [switch]$TriggerNow,
    [string]$Repo  = "huaweicloud-samples/auto-workflows",
    [string]$LocalRepo = "E:\code\auto-workflows",
    [string]$WorkflowFile = "collect-traffic.yml"
)

$ErrorActionPreference = 'Stop'

function Info($m) { Write-Host "    $m" -ForegroundColor Gray }
function Ok($m)   { Write-Host "    ✓ $m" -ForegroundColor Green }
function Warn($m) { Write-Host "    ⚠ $m" -ForegroundColor Yellow }
function Step($n, $m) { Write-Host "`n[$n] $m" -ForegroundColor Cyan }

# ── gh 可用性 ────────────────────────────────────────────────
$ghCmd = Get-Command gh -ErrorAction SilentlyContinue
if (-not $ghCmd) {
    $candidates = @("C:\Program Files\GitHub CLI\gh.exe", "$env:LOCALAPPDATA\GitHubCLI\gh.exe")
    $found = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($found) { $ghCmd = Get-Item $found } else {
        Write-Host "✗ 未找到 gh CLI。请先安装：winget install --id GitHub.cli --exact" -ForegroundColor Red
        exit 1
    }
}
$GH = $ghCmd.Source
Info "gh: $GH"

# ── 认证状态 ─────────────────────────────────────────────────
Step "1/4" "检查 gh 认证状态"
$authOut = & $GH auth status 2>&1 | Out-String
if ($authOut -match 'Logged in to github\.com account (\S+)') {
    $currentUser = $Matches[1]
    Ok "已登录: $currentUser"
} else {
    Warn "未登录 gh"
    Info "请执行： gh auth login"
    Info "  · 选择 GitHub.com → HTTPS → Login with a web browser"
    Info "  · 用 owner 账号登录"
    exit 1
}

# ── 只查状态 ─────────────────────────────────────────────────
if ($Status) {
    Step "状态" "当前仓库配置"
    Write-Host "`n  Variables:" -ForegroundColor Yellow
    & $GH variable list --repo $Repo 2>&1 | ForEach-Object { "    $_" }
    Write-Host "`n  Secrets:" -ForegroundColor Yellow
    & $GH secret list --repo $Repo 2>&1 | ForEach-Object { "    $_" }
    Write-Host "`n  Workflows:" -ForegroundColor Yellow
    & $GH workflow list --repo $Repo --all 2>&1 | ForEach-Object { "    $_" }
    Write-Host ""
    exit 0
}

# ── 收集凭据 ─────────────────────────────────────────────────
Step "2/4" "准备凭据"
if ($AppId -and $PemPath) {
    if (-not (Test-Path $PemPath)) { Write-Host "✗ .pem 不存在: $PemPath" -ForegroundColor Red; exit 1 }
    $pem = Get-Content $PemPath -Raw
    if ($pem -notmatch '-----BEGIN .*PRIVATE KEY-----') {
        Write-Host "✗ .pem 内容不合法（缺少 BEGIN 行）" -ForegroundColor Red; exit 1
    }
    Ok "App ID : $AppId"
    Ok "私钥    : $PemPath ($([math]::Round((Get-Item $PemPath).Length/1KB,1)) KB)"
    $mode = 'app'
} elseif ($Pat) {
    Ok "使用 PAT（长度 $($Pat.Length)）"
    $mode = 'pat'
} else {
    Write-Host "✗ 请提供 -AppId + -PemPath（推荐），或 -Pat" -ForegroundColor Red
    exit 1
}

# ── 写入 Variables / Secrets ────────────────────────────────
Step "3/4" "写入 GitHub 仓库配置（$Repo）"

if ($mode -eq 'app') {
    # App ID 是公开值，用 Variable
    & $GH variable set APP_ID --body "$AppId" --repo $Repo 2>&1 | ForEach-Object { Info $_ }
    Ok "Variable APP_ID = $AppId"

    # 私钥是敏感值，用 Secret（PowerShell 不支持 < 重定向，改用管道 + --body）
    $pemContent = Get-Content $PemPath -Raw
    & $GH secret set APP_PRIVATE_KEY --body $pemContent --repo $Repo 2>&1 | ForEach-Object { Info $_ }
    Ok "Secret APP_PRIVATE_KEY 已写入"
} else {
    & $GH secret set ORG_OWNER_PAT --body "$Pat" --repo $Repo 2>&1 | ForEach-Object { Info $_ }
    Ok "Secret ORG_OWNER_PAT 已写入"
}

Write-Host ""
Info "当前配置："
& $GH variable list --repo $Repo 2>&1 | ForEach-Object { "      [var] $_" }
& $GH secret list --repo $Repo 2>&1 | ForEach-Object { "      [sec] $_" }

# ── 推送 ─────────────────────────────────────────────────────
Step "4/4" "推送代码到远程"
Push-Location $LocalRepo
try {
    $branch = git branch --show-current 2>&1
    $ahead  = git rev-list --count "origin/$branch..HEAD" 2>&1
    Info "分支 $branch，待推送 $ahead 个提交"

    if ($Push) {
        git push origin $branch 2>&1 | ForEach-Object { Info $_ }
        if ($LASTEXITCODE -eq 0) { Ok "推送成功" } else { Warn "推送失败，请检查写权限" }
    } else {
        Warn "未加 -Push，跳过推送。确认无误后执行："
        Info "  cd $LocalRepo; git push origin $branch"
    }
} finally {
    Pop-Location
}

# ── 可选：立即触发 ──────────────────────────────────────────
if ($TriggerNow) {
    Write-Host ""
    Write-Host "触发 workflow ..." -ForegroundColor Cyan
    & $GH workflow run $WorkflowFile --repo $Repo --ref main 2>&1 | ForEach-Object { Info $_ }
    Start-Sleep -Seconds 5
    & $GH run list --repo $Repo --workflow $WorkflowFile --limit 3 2>&1 | ForEach-Object { "    $_" }
    Write-Host ""
    Info "查看日志： gh run watch --repo $Repo"
}

Write-Host ""
Write-Host "══════ 完成 ══════" -ForegroundColor Green
Write-Host "校验配置： .\setup-and-deploy.ps1 -Status" -ForegroundColor Gray
Write-Host "手动触发： gh workflow run $WorkflowFile --repo $Repo" -ForegroundColor Gray
Write-Host "看运行：   gh run list --repo $Repo --workflow $WorkflowFile" -ForegroundColor Gray
