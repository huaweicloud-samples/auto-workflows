#!/usr/bin/env python3
"""Audit huaweicloud-samples repositories against governance rules."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ISSUE_TITLE = "仓库治理规范巡检未通过"
ISSUE_MARKER = "<!-- auto-workflows-governance-audit -->"
LIFECYCLE_TOPICS = {"incubating", "stable", "archived"}
REPO_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)+$")


@dataclass
class Finding:
    severity: str
    area: str
    message: str


@dataclass
class RepoAuditResult:
    name: str
    full_name: str
    html_url: str
    status: str
    topics: list[str]
    findings: list[Finding]


class GitHubClient:
    def __init__(self, token: str) -> None:
        self.token = token

    def request(self, method: str, path: str, data: dict[str, Any] | None = None, ok404: bool = False) -> Any:
        url = path if path.startswith("https://") else f"https://api.github.com{path}"
        body = None
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "huaweicloud-samples-governance-audit",
        }
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                if not raw:
                    return None
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            if ok404 and exc.code == 404:
                return None
            raise RuntimeError(f"GitHub API {method} {url} failed: HTTP {exc.code} {raw}") from exc

    def paginate(self, path: str) -> list[Any]:
        separator = "&" if "?" in path else "?"
        page = 1
        items: list[Any] = []
        while True:
            chunk = self.request("GET", f"{path}{separator}per_page=100&page={page}")
            if not chunk:
                break
            items.extend(chunk)
            if len(chunk) < 100:
                break
            page += 1
        return items


def add(findings: list[Finding], severity: str, area: str, message: str) -> None:
    findings.append(Finding(severity=severity, area=area, message=message))


def get_content(client: GitHubClient, full_name: str, path: str, ref: str) -> str | None:
    encoded = urllib.parse.quote(path)
    data = client.request("GET", f"/repos/{full_name}/contents/{encoded}?ref={urllib.parse.quote(ref)}", ok404=True)
    if not data or not isinstance(data, dict) or data.get("type") != "file":
        return None
    if data.get("encoding") != "base64":
        return None
    return base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")


def path_exists(client: GitHubClient, full_name: str, path: str, ref: str) -> bool:
    encoded = urllib.parse.quote(path)
    return client.request("GET", f"/repos/{full_name}/contents/{encoded}?ref={urllib.parse.quote(ref)}", ok404=True) is not None


def list_dir(client: GitHubClient, full_name: str, path: str, ref: str) -> list[dict[str, Any]]:
    encoded = urllib.parse.quote(path)
    data = client.request("GET", f"/repos/{full_name}/contents/{encoded}?ref={urllib.parse.quote(ref)}", ok404=True)
    return data if isinstance(data, list) else []


def has_any_file(client: GitHubClient, full_name: str, ref: str, candidates: list[str]) -> bool:
    return any(path_exists(client, full_name, item, ref) for item in candidates)


def check_repo_name(repo: dict[str, Any], findings: list[Finding]) -> None:
    name = repo["name"]
    if not REPO_NAME_RE.match(name):
        add(findings, "CRITICAL", "命名规范", "仓库名必须为全小写、连字符分隔，并符合 `<领域前缀>-<场景描述>` 格式。")


def check_lifecycle(repo: dict[str, Any], topics: list[str], findings: list[Finding]) -> None:
    matched = LIFECYCLE_TOPICS.intersection(topics)
    if not matched:
        add(findings, "CRITICAL", "生命周期", "仓库 Topics 必须包含 incubating、stable 或 archived 之一。")
    if "incubating" in topics:
        pushed_at = repo.get("pushed_at")
        if pushed_at:
            last_push = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
            inactive_days = (datetime.now(timezone.utc) - last_push).days
            if inactive_days >= 180 and repo.get("open_issues_count", 0) == 0:
                add(findings, "MAJOR", "生命周期", "Incubating 仓库 6 个月无更新且无活跃 Issue，应进入归档评估。")


def check_structure(client: GitHubClient, repo: dict[str, Any], ref: str, findings: list[Finding]) -> None:
    full_name = repo["full_name"]
    required_files = {
        "README.md": ("CRITICAL", "标准套件结构", "根目录必须包含 README.md。"),
        "LICENSE": ("CRITICAL", "LICENSE", "根目录必须包含 LICENSE 文件。"),
        "CONTRIBUTING.md": ("CRITICAL", "CONTRIBUTING", "根目录必须包含 CONTRIBUTING.md。"),
        "CODE_OF_CONDUCT.md": ("CRITICAL", "CODE_OF_CONDUCT", "根目录必须包含 CODE_OF_CONDUCT.md。"),
        ".github/PULL_REQUEST_TEMPLATE.md": ("CRITICAL", ".github", "必须包含 PR 模板，覆盖关联 Issue、DCO、文档和测试检查清单。"),
    }
    for path, (severity, area, message) in required_files.items():
        if not path_exists(client, full_name, path, ref):
            add(findings, severity, area, message)

    if not path_exists(client, full_name, "deploy", ref):
        add(findings, "CRITICAL", "deploy", "必须包含 deploy 目录，存放部署脚本或 IaC 模板。")
    if not path_exists(client, full_name, ".github", ref):
        add(findings, "CRITICAL", ".github", "必须包含 .github 目录。")
    if not path_exists(client, full_name, ".github/ISSUE_TEMPLATE", ref):
        add(findings, "CRITICAL", ".github", "必须包含 .github/ISSUE_TEMPLATE 目录。")

    issue_templates = [item["name"].lower() for item in list_dir(client, full_name, ".github/ISSUE_TEMPLATE", ref)]
    if issue_templates:
        if not any("bug" in item for item in issue_templates):
            add(findings, "CRITICAL", ".github", "ISSUE_TEMPLATE 至少需要包含 Bug Report 模板。")
        if not any("feature" in item for item in issue_templates):
            add(findings, "CRITICAL", ".github", "ISSUE_TEMPLATE 至少需要包含 Feature Request 模板。")

    workflows = [item["name"].lower() for item in list_dir(client, full_name, ".github/workflows", ref)]
    if not workflows:
        add(findings, "CRITICAL", ".github", "必须包含 .github/workflows，且至少包含 DCO、Markdown Lint、密钥扫描和 IaC 验证。")
    else:
        expected = {
            "dco": "必须包含 DCO 检查 workflow。",
            "markdown": "必须包含 Markdown Lint workflow。",
            "secret": "必须包含密钥扫描 workflow。",
            "iac": "必须包含 IaC 验证 workflow。",
        }
        for key, message in expected.items():
            if not any(key in item or ("md" in item and key == "markdown") for item in workflows):
                add(findings, "CRITICAL", ".github", message)


def check_readme(client: GitHubClient, repo: dict[str, Any], ref: str, findings: list[Finding]) -> None:
    text = get_content(client, repo["full_name"], "README.md", ref)
    if text is None:
        return
    lower = text.lower()
    critical = [
        ("标题与徽章", re.search(r"^#\s+\S+", text, re.MULTILINE) and "license" in lower and "huawei" in lower),
        ("简介/概述", "简介" in text or "概述" in text or "overview" in lower),
        ("前置条件", "前置条件" in text or "prerequisite" in lower),
        ("快速开始/一键部署", "快速开始" in text or "一键部署" in text or "quick start" in lower),
        ("使用方法/验证", "验证" in text or "使用方法" in text or "verification" in lower),
        ("许可证", "许可证" in text or "license" in lower),
        ("联系方式/维护者", "联系方式" in text or "维护者" in text or "maintainer" in lower),
    ]
    for section, ok in critical:
        if not ok:
            add(findings, "CRITICAL", "README", f"README 缺少 CRITICAL 章节或要素：{section}。")

    major_keywords = [
        ("方案亮点", ["方案亮点", "亮点", "highlights"]),
        ("架构图", ["架构图", "architecture", "mermaid"]),
        ("涉及云服务与费用", ["费用", "云服务", "cost"]),
        ("分步部署", ["分步部署", "step"]),
        ("清理资源", ["清理资源", "destroy", "cleanup"]),
        ("详细说明", ["详细说明", "details"]),
        ("FAQ/故障排除", ["faq", "故障排除", "troubleshooting"]),
        ("贡献指南", ["contributing.md", "贡献指南"]),
    ]
    for section, keywords in major_keywords:
        if not any(item in lower or item in text for item in keywords):
            add(findings, "MAJOR", "README", f"README 建议补充 MAJOR 章节：{section}。")
    if len(text.splitlines()) > 80 and not ("目录" in text or "table of contents" in lower):
        add(findings, "MINOR", "README", "长 README 超过 80 行，建议包含目录。")


def check_license(client: GitHubClient, repo: dict[str, Any], ref: str, findings: list[Finding]) -> None:
    text = get_content(client, repo["full_name"], "LICENSE", ref)
    if text is None:
        return
    lower = text.lower()
    if "gpl" in lower or "agpl" in lower:
        add(findings, "CRITICAL", "LICENSE", "严禁引入 GPL、AGPL 等强传染性许可证。")
    if not ("mit no attribution" in lower or "mit-0" in lower or "apache license" in lower):
        add(findings, "CRITICAL", "LICENSE", "默认应采用 MIT-0；例外可使用 Apache 2.0。")
    if ("third-party" in lower or "第三方" in text) and not path_exists(client, repo["full_name"], "NOTICE", ref):
        add(findings, "MAJOR", "LICENSE", "包含第三方代码声明时必须提供 NOTICE 文件。")


def check_contributing(client: GitHubClient, repo: dict[str, Any], ref: str, findings: list[Finding]) -> None:
    text = get_content(client, repo["full_name"], "CONTRIBUTING.md", ref)
    if text is None:
        return
    lower = text.lower()
    checks = [
        ("必须链接至 CODE_OF_CONDUCT.md。", "code_of_conduct.md" in lower),
        ("必须说明 Fork → 分支 → 开发 → DCO → PR → Issue → 审核 → 合并流程。", all(item in lower for item in ["fork", "dco", "pull request"]) and ("issue" in lower)),
        ("必须要求所有提交包含 Signed-off-by。", "signed-off-by" in lower),
        ("必须说明维护者评审响应和批准要求。", "review" in lower or "评审" in text),
        ("必须声明贡献将在仓库许可证下分发。", "license" in lower or "许可证" in text),
    ]
    for message, ok in checks:
        if not ok:
            add(findings, "CRITICAL", "CONTRIBUTING", message)


def check_code_of_conduct(client: GitHubClient, repo: dict[str, Any], ref: str, findings: list[Finding]) -> None:
    text = get_content(client, repo["full_name"], "CODE_OF_CONDUCT.md", ref)
    if text is None:
        return
    lower = text.lower()
    if "contributor covenant" not in lower and "贡献者公约" not in text:
        add(findings, "CRITICAL", "CODE_OF_CONDUCT", "行为准则应采用标准 Contributor Covenant 模板。")
    if "@" not in text:
        add(findings, "CRITICAL", "CODE_OF_CONDUCT", "行为准则必须保留官方维护者联络方式。")
    if not any(item in lower for item in ["warning", "ban", "enforcement"]) and not any(item in text for item in ["警告", "禁止参与", "处理"]):
        add(findings, "MAJOR", "CODE_OF_CONDUCT", "行为准则应明确违规后果和投诉处理流程。")


def check_deploy(client: GitHubClient, repo: dict[str, Any], ref: str, findings: list[Finding]) -> None:
    full_name = repo["full_name"]
    if not path_exists(client, full_name, "deploy", ref):
        return
    if not any(path_exists(client, full_name, item, ref) for item in ["deploy/terraform", "deploy/aos", "deploy/scripts"]):
        add(findings, "MAJOR", "deploy", "deploy 下建议使用 terraform/aos/scripts 等子目录隔离工具。")
    if not has_any_file(client, full_name, ref, ["deploy/variables.md", "deploy/terraform/variables.tf", "deploy/aos/parameters.json"]):
        add(findings, "CRITICAL", "deploy", "deploy 必须提供变量说明。")
    readme = get_content(client, full_name, "README.md", ref) or ""
    if "destroy" not in readme.lower() and "清理资源" not in readme:
        add(findings, "CRITICAL", "deploy", "README 或部署文档必须包含资源销毁命令。")
    if not any("iac" in item["name"].lower() for item in list_dir(client, full_name, ".github/workflows", ref)):
        add(findings, "CRITICAL", "deploy", "IaC 语法验证必须集成至 CI。")


def check_app_and_scripts(client: GitHubClient, repo: dict[str, Any], ref: str, findings: list[Finding]) -> None:
    full_name = repo["full_name"]
    if path_exists(client, full_name, "app", ref):
        if not has_any_file(client, full_name, ref, ["requirements.txt", "app/requirements.txt", "pom.xml", "package-lock.json", "poetry.lock"]):
            add(findings, "CRITICAL", "app", "存在 app 代码时必须包含依赖清单并锁定版本。")
    if path_exists(client, full_name, "scripts", ref):
        scripts = [item for item in list_dir(client, full_name, "scripts", ref) if item.get("type") == "file"]
        for script in scripts[:20]:
            text = get_content(client, full_name, f"scripts/{script['name']}", ref) or ""
            head = "\n".join(text.splitlines()[:10])
            if not ("usage" in head.lower() or "用法" in head or "功能" in head):
                add(findings, "CRITICAL", "scripts", f"`scripts/{script['name']}` 文件头部应说明功能、用法及依赖环境。")


def check_github_templates(client: GitHubClient, repo: dict[str, Any], ref: str, findings: list[Finding]) -> None:
    pr = get_content(client, repo["full_name"], ".github/PULL_REQUEST_TEMPLATE.md", ref)
    if pr:
        lower = pr.lower()
        for label, ok in [
            ("关联 Issue 编号", "issue" in lower),
            ("变更说明", "变更" in pr or "description" in lower),
            ("DCO 签名确认", "dco" in lower or "signed-off-by" in lower),
            ("CI、文档、测试检查清单", ("ci" in lower and ("文档" in pr or "doc" in lower) and ("测试" in pr or "test" in lower))),
        ]:
            if not ok:
                add(findings, "CRITICAL", ".github", f"PR 模板必须包含：{label}。")


def audit_repo(client: GitHubClient, repo: dict[str, Any]) -> RepoAuditResult:
    ref = repo.get("default_branch") or "main"
    topics_data = client.request("GET", f"/repos/{repo['full_name']}/topics", ok404=True) or {}
    topics = sorted(topics_data.get("names", []))
    findings: list[Finding] = []

    check_repo_name(repo, findings)
    check_lifecycle(repo, topics, findings)
    check_structure(client, repo, ref, findings)
    check_readme(client, repo, ref, findings)
    check_license(client, repo, ref, findings)
    check_contributing(client, repo, ref, findings)
    check_code_of_conduct(client, repo, ref, findings)
    check_deploy(client, repo, ref, findings)
    check_app_and_scripts(client, repo, ref, findings)
    check_github_templates(client, repo, ref, findings)

    status = "failed" if any(item.severity in {"CRITICAL", "MAJOR"} for item in findings) else "passed"
    return RepoAuditResult(
        name=repo["name"],
        full_name=repo["full_name"],
        html_url=repo["html_url"],
        status=status,
        topics=topics,
        findings=findings,
    )


def render_report(results: list[RepoAuditResult], org: str, mode: str) -> str:
    failed = [item for item in results if item.status == "failed"]
    passed = [item for item in results if item.status == "passed"]
    lines = [
        "# 仓库治理规范巡检报告",
        "",
        f"- 组织：`{org}`",
        f"- 模式：`{mode}`",
        f"- 扫描仓库数：{len(results)}",
        f"- 通过：{len(passed)}",
        f"- 不通过：{len(failed)}",
        "",
    ]
    if failed:
        lines.extend(["## 不符合规范仓库", ""])
        for result in failed:
            lines.append(f"### [{result.full_name}]({result.html_url})")
            lines.append("")
            for severity in ["CRITICAL", "MAJOR", "MINOR"]:
                items = [item for item in result.findings if item.severity == severity]
                if not items:
                    continue
                lines.append(f"#### {severity}")
                for item in items:
                    lines.append(f"- [{item.area}] {item.message}")
                lines.append("")
    else:
        lines.extend(["## 结果", "", "全部仓库符合当前治理规范。", ""])
    return "\n".join(lines)


def find_existing_issue(client: GitHubClient, full_name: str) -> dict[str, Any] | None:
    issues = client.paginate(f"/repos/{full_name}/issues?state=open")
    for issue in issues:
        if "pull_request" in issue:
            continue
        if issue.get("title") == ISSUE_TITLE:
            return issue
    return None


def upsert_issue(client: GitHubClient, result: RepoAuditResult) -> None:
    body = render_report([result], result.full_name.split("/", 1)[0], "issue")
    body = f"{ISSUE_MARKER}\n{body}"
    issue = find_existing_issue(client, result.full_name)
    if issue:
        client.request("PATCH", f"/repos/{result.full_name}/issues/{issue['number']}", {"body": body})
    else:
        client.request("POST", f"/repos/{result.full_name}/issues", {"title": ISSUE_TITLE, "body": body})


def close_existing_issue(client: GitHubClient, result: RepoAuditResult) -> None:
    issue = find_existing_issue(client, result.full_name)
    if issue:
        client.request("PATCH", f"/repos/{result.full_name}/issues/{issue['number']}", {"state": "closed"})


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit repositories against huaweicloud-samples governance rules.")
    parser.add_argument("--org", required=True)
    parser.add_argument("--mode", choices=["report-only", "issue"], default="report-only")
    parser.add_argument("--include-archived", action="store_true")
    parser.add_argument("--include-forks", action="store_true")
    parser.add_argument("--fail-on-violations", action="store_true")
    parser.add_argument("--report", default="governance-audit-report.md")
    parser.add_argument("--json-report", default="governance-audit-report.json")
    args = parser.parse_args()

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("Missing GH_TOKEN or GITHUB_TOKEN.", file=sys.stderr)
        return 2

    client = GitHubClient(token)
    repos = client.paginate(f"/orgs/{urllib.parse.quote(args.org)}/repos?type=all&sort=full_name")
    results: list[RepoAuditResult] = []
    for repo in repos:
        if repo.get("archived") and not args.include_archived:
            continue
        if repo.get("fork") and not args.include_forks:
            continue
        result = audit_repo(client, repo)
        results.append(result)
        if args.mode == "issue":
            if result.status == "failed":
                upsert_issue(client, result)
            else:
                close_existing_issue(client, result)

    report = render_report(results, args.org, args.mode)
    Path(args.report).write_text(report, encoding="utf-8")
    Path(args.json_report).write_text(
        json.dumps(
            [
                {
                    **asdict(result),
                    "findings": [asdict(item) for item in result.findings],
                }
                for result in results
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(report)
    failed_count = sum(1 for item in results if item.status == "failed")
    return 1 if args.fail_on_violations and failed_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
