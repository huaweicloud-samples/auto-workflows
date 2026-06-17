#!/usr/bin/env python3
"""Create huaweicloud-samples repositories from approved issue requests."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,98}[a-z0-9]$")
AUTO_SECTION_START = "<!-- AUTO-WORKFLOWS:START -->"
AUTO_SECTION_END = "<!-- AUTO-WORKFLOWS:END -->"


@dataclass
class RepoRequest:
    repo_name: str
    team_slugs: list[str]
    description: str
    codeowners: list[str]
    topics: list[str]
    ci_context: str
    awesome_category: str


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
            "User-Agent": "huaweicloud-samples-auto-workflows",
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


def run(cmd: list[str], cwd: str | Path | None = None, env: dict[str, str] | None = None) -> None:
    subprocess.run(cmd, cwd=cwd, env=env, check=True)


def capture(cmd: list[str], cwd: str | Path | None = None) -> str:
    return subprocess.check_output(cmd, cwd=cwd, text=True).strip()


def parse_issue_body(body: str) -> dict[str, str]:
    data: dict[str, str] = {}
    current_key = ""
    for raw_line in body.splitlines():
        line = raw_line.rstrip()
        match = re.match(r"^###\s+(.+?)\s*$", line)
        if match:
            current_key = match.group(1).strip()
            data[current_key] = ""
            continue
        if not current_key:
            continue
        if line.strip().startswith("<!--") or not line.strip():
            continue
        data[current_key] += line.strip() + "\n"
    return {key: value.strip() for key, value in data.items()}


def split_items(raw: str) -> list[str]:
    if not raw:
        return []
    values = re.split(r"[\n,，\s]+", raw)
    cleaned: list[str] = []
    for value in values:
        value = value.strip()
        if not value:
            continue
        if re.match(r"^_?No\s*response_?$", value, re.IGNORECASE):
            continue
        cleaned.append(value)
    return cleaned


def normalize_team_slug(value: str) -> str:
    value = value.strip().replace("@", "")
    if "/" in value:
        value = value.split("/", 1)[1]
    return value


def normalize_codeowner(value: str, org: str) -> str:
    value = value.strip()
    if not value:
        return value
    if value.startswith("@"):
        return value
    if "/" in value:
        return f"@{value}"
    return f"@{org}/{value}"


def parse_request(event: dict[str, Any], org: str) -> RepoRequest:
    issue_body = event["issue"]["body"] or ""
    fields = parse_issue_body(issue_body)

    repo_name = (fields.get("Repository name") or "").strip().lower()
    description = (fields.get("Description") or "").strip()
    team_slugs = [normalize_team_slug(item) for item in split_items(fields.get("Team", ""))]
    codeowners_raw = split_items(fields.get("CODEOWNERS", ""))
    codeowners = [normalize_codeowner(item, org) for item in codeowners_raw]
    if not codeowners:
        codeowners = [f"@{org}/{team}" for team in team_slugs]

    topics = [item.lower() for item in split_items(fields.get("Topics", ""))]
    for topic in ["huaweicloud", "sample", "incubating"]:
        if topic not in topics:
            topics.append(topic)

    ci_context = (fields.get("Required CI status check") or os.environ.get("DEFAULT_CI_CONTEXT") or "ci").strip()
    awesome_category = (fields.get("Awesome index category") or "Incubating Samples").strip()

    errors = []
    if not repo_name or not REPO_NAME_RE.match(repo_name):
        errors.append("Repository name must be lowercase and contain only a-z, 0-9, dot, underscore, or hyphen.")
    if repo_name.endswith(".git"):
        errors.append("Repository name must not end with .git.")
    if not description:
        errors.append("Description is required.")
    if not team_slugs:
        errors.append("At least one team slug is required.")
    if not ci_context:
        errors.append("Required CI status check is required.")
    if errors:
        raise ValueError("\n".join(f"- {item}" for item in errors))

    return RepoRequest(
        repo_name=repo_name,
        team_slugs=team_slugs,
        description=description,
        codeowners=codeowners,
        topics=topics,
        ci_context=ci_context,
        awesome_category=awesome_category,
    )


def ensure_repo_available(client: GitHubClient, org: str, repo_name: str) -> None:
    existing = client.request("GET", f"/repos/{org}/{repo_name}", ok404=True)
    if existing:
        raise ValueError(f"Repository already exists: https://github.com/{org}/{repo_name}")


def readme_template(request: RepoRequest, org: str) -> str:
    return f"""# {request.repo_name}

[![Status](https://img.shields.io/badge/Status-Incubating-blue)]()
[![Huawei Cloud](https://img.shields.io/badge/Huawei%20Cloud-Samples-red)]()

{request.description}

## Overview

This repository is created from the huaweicloud-samples automated repository request workflow.

## Getting Started

Add setup, deployment, and verification steps here.

## Contributing

Please use pull requests and follow the repository review rules.

## License

This project is licensed under the MIT-0 license.

## Maintainers

CODEOWNERS: {" ".join(request.codeowners)}

## Feedback

Please use GitHub Issues: https://github.com/{org}/{request.repo_name}/issues
"""


def create_base_repository(request: RepoRequest, org: str, token: str) -> str:
    temp_dir = tempfile.mkdtemp(prefix="sample-repo-")
    repo_dir = Path(temp_dir)
    (repo_dir / ".github" / "workflows").mkdir(parents=True, exist_ok=True)

    (repo_dir / "README.md").write_text(readme_template(request, org), encoding="utf-8")
    (repo_dir / ".github" / "CODEOWNERS").write_text(f"* {' '.join(request.codeowners)}\n", encoding="utf-8")
    (repo_dir / ".github" / "workflows" / "ci.yml").write_text(
        """name: CI

on:
  pull_request:
  push:
    branches:
      - main

jobs:
  ci:
    name: ci
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4
      - name: Placeholder check
        run: echo "CI placeholder passed"
""",
        encoding="utf-8",
    )

    run(["git", "init", "-b", "main"], cwd=repo_dir)
    run(["git", "config", "user.name", "github-actions[bot]"], cwd=repo_dir)
    run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], cwd=repo_dir)
    run(["git", "add", "README.md", ".github/CODEOWNERS", ".github/workflows/ci.yml"], cwd=repo_dir)
    run(["git", "commit", "-m", "chore: initialize sample repository"], cwd=repo_dir)

    env = os.environ.copy()
    env["GH_TOKEN"] = token
    existing = client_repo_exists(token, org, request.repo_name)
    if existing and repo_has_branches(token, org, request.repo_name):
        print(f"Repository already exists and has branches, skip initialization: https://github.com/{org}/{request.repo_name}")
        return f"https://github.com/{org}/{request.repo_name}"
    if not existing:
        run(
            [
                "gh",
                "repo",
                "create",
                f"{org}/{request.repo_name}",
                "--public",
                "--description",
                request.description,
            ],
            cwd=repo_dir,
            env=env,
        )

    run(["git", "remote", "add", "origin", authenticated_repo_url(token, org, request.repo_name)], cwd=repo_dir)
    run(["git", "push", "-u", "origin", "main"], cwd=repo_dir)
    return f"https://github.com/{org}/{request.repo_name}"


def client_repo_exists(token: str, org: str, repo_name: str) -> bool:
    client = GitHubClient(token)
    return client.request("GET", f"/repos/{org}/{repo_name}", ok404=True) is not None


def repo_has_branches(token: str, org: str, repo_name: str) -> bool:
    client = GitHubClient(token)
    try:
        branches = client.request("GET", f"/repos/{org}/{repo_name}/branches")
    except RuntimeError as exc:
        if "HTTP 409" in str(exc) or "Git Repository is empty" in str(exc):
            return False
        raise
    return bool(branches)


def add_team_permissions(client: GitHubClient, org: str, repo_name: str, team_slugs: list[str]) -> None:
    for team in team_slugs:
        team_data = client.request("GET", f"/orgs/{org}/teams/{urllib.parse.quote(team)}", ok404=True)
        if not team_data:
            raise ValueError(
                f"Team `{team}` was not found or ORG_ADMIN_TOKEN cannot see it. "
                "Use the GitHub team slug, and ensure the token has Members organization read permission."
            )
        client.request(
            "PUT",
            f"/orgs/{org}/teams/{urllib.parse.quote(team)}/repos/{org}/{repo_name}",
            {"permission": "push"},
        )


def set_topics(client: GitHubClient, org: str, repo_name: str, topics: list[str]) -> None:
    clean_topics = []
    for topic in topics:
        topic = re.sub(r"[^a-z0-9-]", "-", topic.lower()).strip("-")
        if topic and topic not in clean_topics:
            clean_topics.append(topic)
    client.request("PUT", f"/repos/{org}/{repo_name}/topics", {"names": clean_topics[:20]})


def set_branch_protection(client: GitHubClient, org: str, request: RepoRequest) -> None:
    approvals = int(os.environ.get("APPROVALS_REQUIRED", "3"))
    body = {
        "required_status_checks": {
            "strict": True,
            "contexts": [request.ci_context],
        },
        "enforce_admins": True,
        "required_pull_request_reviews": {
            "dismiss_stale_reviews": True,
            "require_code_owner_reviews": True,
            "required_approving_review_count": approvals,
        },
        "restrictions": None,
        "required_linear_history": False,
        "allow_force_pushes": False,
        "allow_deletions": False,
    }
    client.request("PUT", f"/repos/{org}/{request.repo_name}/branches/main/protection", body)


def authenticated_repo_url(token: str, org: str, repo: str) -> str:
    return f"https://x-access-token:{token}@github.com/{org}/{repo}.git"


def update_awesome_index(request: RepoRequest, org: str, token: str, index_repo: str) -> str:
    temp_dir = Path(tempfile.mkdtemp(prefix="awesome-index-"))
    run(["git", "clone", authenticated_repo_url(token, org, index_repo), str(temp_dir)])

    try:
        capture(["git", "rev-parse", "--verify", "HEAD"], cwd=temp_dir)
    except subprocess.CalledProcessError:
        run(["git", "checkout", "-b", "main"], cwd=temp_dir)

    readme_path = temp_dir / "README.md"
    if readme_path.exists():
        content = readme_path.read_text(encoding="utf-8")
    else:
        content = "# Awesome HuaweiCloud\n\n"

    row = f"| [{request.repo_name}](https://github.com/{org}/{request.repo_name}) | {request.description} | {', '.join(request.team_slugs)} | incubating |\n"
    section_header = f"## {request.awesome_category}\n\n"
    table_header = "| Repository | Description | Team | Status |\n|------------|-------------|------|--------|\n"

    if AUTO_SECTION_START in content and AUTO_SECTION_END in content:
        before, rest = content.split(AUTO_SECTION_START, 1)
        current, after = rest.split(AUTO_SECTION_END, 1)
        rows = [line for line in current.splitlines() if line.startswith("| [") and f"/{request.repo_name})" not in line]
        rows.append(row.rstrip())
        block = AUTO_SECTION_START + "\n" + table_header + "\n".join(sorted(rows)) + "\n" + AUTO_SECTION_END
        content = before + block + after
    else:
        if not content.endswith("\n"):
            content += "\n"
        content += "\n" + section_header + AUTO_SECTION_START + "\n" + table_header + row + AUTO_SECTION_END + "\n"

    readme_path.write_text(content, encoding="utf-8")
    run(["git", "config", "user.name", "github-actions[bot]"], cwd=temp_dir)
    run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], cwd=temp_dir)
    run(["git", "add", "README.md"], cwd=temp_dir)
    if capture(["git", "status", "--short"], cwd=temp_dir):
        run(["git", "commit", "-m", f"docs: add {request.repo_name} to index"], cwd=temp_dir)
        run(["git", "push", "origin", "HEAD:main"], cwd=temp_dir)
    return f"https://github.com/{org}/{index_repo}"


def comment_and_close(issue_client: GitHubClient, event: dict[str, Any], body: str, close: bool) -> None:
    repo = event["repository"]["name"]
    owner = event["repository"]["owner"]["login"]
    issue_number = event["issue"]["number"]
    issue_client.request("POST", f"/repos/{owner}/{repo}/issues/{issue_number}/comments", {"body": body})
    if close:
        issue_client.request("PATCH", f"/repos/{owner}/{repo}/issues/{issue_number}", {"state": "closed"})


def main() -> int:
    org = os.environ.get("ORG_NAME", "").strip()
    token = os.environ.get("GH_TOKEN", "").strip()
    issue_token = os.environ.get("ISSUE_TOKEN", "").strip()
    index_repo = os.environ.get("INDEX_REPO", "awesome-huaweicloud").strip()
    event_path = os.environ.get("GITHUB_EVENT_PATH", "").strip()

    if not org or not token or not issue_token or not event_path:
        print("Missing ORG_NAME, GH_TOKEN, ISSUE_TOKEN, or GITHUB_EVENT_PATH.", file=sys.stderr)
        return 2

    event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    api = GitHubClient(token)
    issue_api = GitHubClient(issue_token)

    try:
        request = parse_request(event, org)
        repo_url = create_base_repository(request, org, token)
        add_team_permissions(api, org, request.repo_name, request.team_slugs)
        set_topics(api, org, request.repo_name, request.topics)
        set_branch_protection(api, org, request)
        index_url = update_awesome_index(request, org, token, index_repo)
        comment_and_close(
            issue_api,
            event,
            "\n".join(
                [
                    "Repository created successfully.",
                    "",
                    f"- Repository: {repo_url}",
                    f"- Awesome index: {index_url}",
                    f"- Status topic: `incubating`",
                    f"- Required approvals: {os.environ.get('APPROVALS_REQUIRED', '3')}",
                    f"- Required CI check: `{request.ci_context}`",
                ]
            ),
            close=True,
        )
        print(f"Created {repo_url}")
        return 0
    except Exception as exc:
        message = f"Repository creation failed:\n\n```text\n{exc}\n```"
        try:
            comment_and_close(issue_api, event, message, close=False)
        except Exception as comment_exc:
            print(f"Failed to comment on issue: {comment_exc}", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
