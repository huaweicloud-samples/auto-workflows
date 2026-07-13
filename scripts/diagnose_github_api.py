#!/usr/bin/env python3
"""Collect evidence for intermittent GitHub Contents API 502 responses."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


API_BASE = "https://api.github.com"
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9._-]+$")
CAPTURED_HEADERS = (
    "content-type",
    "retry-after",
    "server",
    "via",
    "x-accepted-oauth-scopes",
    "x-github-request-id",
    "x-oauth-scopes",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
    "x-ratelimit-resource",
    "x-ratelimit-used",
)


@dataclass
class ProbeResult:
    sequence: int
    timestamp: str
    token_source: str
    probe: str
    repository: str | None
    ref: str | None
    path: str
    status: int | None
    expected_statuses: list[int]
    duration_ms: int
    headers: dict[str, str]
    error: str | None = None
    body_preview: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def expected(self) -> bool:
        return self.status in self.expected_statuses


class GitHubApiProbe:
    def __init__(self, token_source: str, token: str) -> None:
        self.token_source = token_source
        self.token = token
        self.sequence = 0

    def request(
        self,
        probe: str,
        path: str,
        expected_statuses: list[int],
        repository: str | None = None,
        ref: str | None = None,
    ) -> tuple[ProbeResult, Any]:
        self.sequence += 1
        url = path if path.startswith("https://") else f"{API_BASE}{path}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "huaweicloud-samples-api-diagnostic",
            },
            method="GET",
        )
        started = time.perf_counter()
        status: int | None = None
        response_headers: dict[str, str] = {}
        raw = b""
        error: str | None = None

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status = response.status
                response_headers = capture_headers(response.headers)
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            response_headers = capture_headers(exc.headers)
            raw = exc.read()
            error = f"HTTP {exc.code} {exc.reason}"
        except (urllib.error.URLError, TimeoutError) as exc:
            error = str(exc)

        duration_ms = round((time.perf_counter() - started) * 1000)
        parsed = parse_json(raw)
        unexpected = status not in expected_statuses
        body_preview = preview_body(raw) if unexpected else None
        result = ProbeResult(
            sequence=self.sequence,
            timestamp=datetime.now(timezone.utc).isoformat(),
            token_source=self.token_source,
            probe=probe,
            repository=repository,
            ref=ref,
            path=path,
            status=status,
            expected_statuses=expected_statuses,
            duration_ms=duration_ms,
            headers=response_headers,
            error=error,
            body_preview=body_preview,
        )
        remaining = response_headers.get("x-ratelimit-remaining", "unknown")
        request_id = response_headers.get("x-github-request-id", "missing")
        print(
            f"[{self.token_source}] {probe} repo={repository or '-'} "
            f"status={status if status is not None else 'network-error'} duration_ms={duration_ms} "
            f"remaining={remaining} request_id={request_id}"
        )
        return result, parsed


def capture_headers(headers: Any) -> dict[str, str]:
    if not headers:
        return {}
    normalized = {str(key).lower(): str(value) for key, value in headers.items()}
    return {name: normalized[name] for name in CAPTURED_HEADERS if name in normalized}


def parse_json(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def preview_body(raw: bytes, limit: int = 500) -> str | None:
    if not raw:
        return None
    text = " ".join(raw.decode("utf-8", errors="replace").split())
    return text[:limit] + ("... [truncated]" if len(text) > limit else "")


def parse_repositories(value: str) -> list[str]:
    repositories = list(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))
    if not repositories:
        raise ValueError("at least one repository is required")
    if len(repositories) > 10:
        raise ValueError("at most 10 repositories may be probed in one run")
    invalid = [item for item in repositories if not REPOSITORY_RE.fullmatch(item)]
    if invalid:
        raise ValueError(f"invalid repository names: {', '.join(invalid)}")
    return repositories


def select_root_blob(tree_data: Any) -> str | None:
    if not isinstance(tree_data, dict) or not isinstance(tree_data.get("tree"), list):
        return None
    blobs = [
        item.get("path")
        for item in tree_data["tree"]
        if isinstance(item, dict) and item.get("type") == "blob" and isinstance(item.get("path"), str)
    ]
    for preferred in ("README.md", "README", ".gitignore", "LICENSE"):
        if preferred in blobs:
            return preferred
    return blobs[0] if blobs else None


def wait(delay_seconds: float) -> None:
    if delay_seconds > 0:
        time.sleep(delay_seconds)


def probe_token(
    token_source: str,
    token: str,
    org: str,
    repositories: list[str],
    iterations: int,
    delay_seconds: float,
) -> tuple[list[ProbeResult], list[str]]:
    client = GitHubApiProbe(token_source, token)
    results: list[ProbeResult] = []
    notes: list[str] = []

    before, before_data = client.request("rate-limit-before", "/rate_limit", [200])
    if isinstance(before_data, dict):
        before.details = before_data.get("resources", {}).get("core", {})
    results.append(before)
    wait(delay_seconds)

    for repository in repositories:
        full_name = f"{org}/{repository}"
        metadata, metadata_data = client.request(
            "repository-metadata",
            f"/repos/{full_name}",
            [200],
            repository=full_name,
        )
        results.append(metadata)
        if metadata.status != 200 or not isinstance(metadata_data, dict):
            notes.append(f"{token_source}: skipped {full_name}; repository metadata was unavailable")
            wait(delay_seconds)
            continue

        ref = metadata_data.get("default_branch")
        metadata.details = {"default_branch": ref, "size": metadata_data.get("size")}
        if not isinstance(ref, str) or not ref:
            notes.append(f"{token_source}: skipped {full_name}; repository has no default branch")
            wait(delay_seconds)
            continue

        encoded_ref = urllib.parse.quote(ref, safe="")
        tree_path = f"/repos/{full_name}/git/trees/{encoded_ref}"
        tree_result, tree_data = client.request(
            "git-tree-discovery",
            tree_path,
            [200],
            repository=full_name,
            ref=ref,
        )
        results.append(tree_result)
        existing_path = select_root_blob(tree_data)
        tree_result.details = {
            "entry_count": len(tree_data.get("tree", [])) if isinstance(tree_data, dict) else None,
            "selected_path": existing_path,
            "truncated": tree_data.get("truncated") if isinstance(tree_data, dict) else None,
        }
        if not existing_path:
            notes.append(f"{token_source}: {full_name} has no root file for the existing-content probe")
        wait(delay_seconds)

        for iteration in range(1, iterations + 1):
            if existing_path:
                encoded_path = urllib.parse.quote(existing_path, safe="/")
                result, _ = client.request(
                    "contents-existing",
                    f"/repos/{full_name}/contents/{encoded_path}?ref={encoded_ref}",
                    [200],
                    repository=full_name,
                    ref=ref,
                )
                result.details = {"iteration": iteration, "file": existing_path}
                results.append(result)
                wait(delay_seconds)

            missing_path = ".github/__governance_diagnostic_missing__"
            encoded_missing = urllib.parse.quote(missing_path, safe="/")
            result, _ = client.request(
                "contents-missing",
                f"/repos/{full_name}/contents/{encoded_missing}?ref={encoded_ref}",
                [404],
                repository=full_name,
                ref=ref,
            )
            result.details = {"iteration": iteration, "file": missing_path}
            results.append(result)
            wait(delay_seconds)

            result, _ = client.request(
                "git-tree",
                tree_path,
                [200],
                repository=full_name,
                ref=ref,
            )
            result.details = {"iteration": iteration}
            results.append(result)
            wait(delay_seconds)

    after, after_data = client.request("rate-limit-after", "/rate_limit", [200])
    if isinstance(after_data, dict):
        after.details = after_data.get("resources", {}).get("core", {})
    results.append(after)
    return results, notes


def build_interpretation(results: list[ProbeResult]) -> list[str]:
    five_xx = [item for item in results if item.status is not None and 500 <= item.status <= 599]
    explicit_rate_limits = [
        item
        for item in results
        if item.status in {403, 429} or "retry-after" in item.headers
    ]
    lines: list[str] = []
    rate_snapshots = [item for item in results if item.probe.startswith("rate-limit-") and item.details]
    low_quota = []
    for item in rate_snapshots:
        try:
            limit = int(item.details.get("limit"))
            remaining = int(item.details.get("remaining"))
        except (TypeError, ValueError):
            continue
        if remaining <= max(100, round(limit * 0.05)):
            low_quota.append(item)

    if explicit_rate_limits:
        lines.append("Explicit rate-limit evidence was detected (HTTP 403/429 or Retry-After).")
    elif low_quota:
        lines.append("The core API quota was at or below 5% remaining during the diagnostic.")
    else:
        lines.append("No explicit rate-limit signal or near-exhausted core API quota was detected.")

    if not five_xx:
        lines.append("No 5xx response was reproduced during this controlled sample.")
        return lines

    contents_5xx = [item for item in five_xx if item.probe.startswith("contents-")]
    tree_5xx = [item for item in five_xx if item.probe.startswith("git-tree")]
    token_sources = sorted({item.token_source for item in five_xx})
    if contents_5xx and not tree_5xx:
        lines.append("5xx responses were isolated to the Contents API; equivalent Git Trees probes did not return 5xx.")
    elif tree_5xx:
        lines.append("5xx responses affected both Contents and Git Trees probes, indicating a broader API or network failure.")
    else:
        lines.append("5xx responses occurred outside the repeated Contents and Git Trees probes; inspect the samples below.")

    if len(token_sources) > 1:
        lines.append("5xx responses affected both token sources, so the failure is not isolated to ORG_ADMIN_TOKEN.")
    else:
        lines.append(f"5xx responses were observed only with token source `{token_sources[0]}`.")

    request_ids = [item for item in five_xx if item.headers.get("x-github-request-id")]
    if request_ids:
        lines.append("GitHub Request IDs were captured and can be supplied to GitHub Support.")
    return lines


def status_rows(results: list[ProbeResult]) -> list[tuple[str, str, str, int]]:
    counts = Counter(
        (
            item.token_source,
            item.probe,
            str(item.status) if item.status is not None else "network-error",
        )
        for item in results
    )
    return [(*key, count) for key, count in sorted(counts.items())]


def rate_limit_rows(results: list[ProbeResult]) -> list[ProbeResult]:
    return [item for item in results if item.probe.startswith("rate-limit-") and item.details]


def render_markdown(
    org: str,
    repositories: list[str],
    iterations: int,
    delay_ms: int,
    results: list[ProbeResult],
    notes: list[str],
) -> str:
    five_xx = [item for item in results if item.status is not None and 500 <= item.status <= 599]
    unexpected = [item for item in results if not item.expected]
    lines = [
        "# GitHub API 502 diagnostic report",
        "",
        f"- Organization: `{org}`",
        f"- Repositories: {', '.join(f'`{item}`' for item in repositories)}",
        f"- Iterations per endpoint: {iterations}",
        f"- Delay between requests: {delay_ms} ms",
        f"- Total requests: {len(results)}",
        f"- Unexpected responses: {len(unexpected)}",
        f"- 5xx responses: {len(five_xx)}",
        "",
        "## Automated interpretation",
        "",
    ]
    lines.extend(f"- {item}" for item in build_interpretation(results))
    lines.extend(
        [
            "",
            "## Status summary",
            "",
            "| Token source | Probe | Status | Count |",
            "| --- | --- | ---: | ---: |",
        ]
    )
    lines.extend(f"| {token} | {probe} | {status} | {count} |" for token, probe, status, count in status_rows(results))

    lines.extend(
        [
            "",
            "## Core API quota snapshots",
            "",
            "| Token source | Snapshot | Limit | Remaining | Used | Reset |",
            "| --- | --- | ---: | ---: | ---: | ---: |",
        ]
    )
    snapshots = rate_limit_rows(results)
    if snapshots:
        for item in snapshots:
            lines.append(
                f"| {item.token_source} | {item.probe} | {item.details.get('limit', '-')} | "
                f"{item.details.get('remaining', '-')} | {item.details.get('used', '-')} | "
                f"{item.details.get('reset', '-')} |"
            )
    else:
        lines.append("| - | unavailable | - | - | - | - |")

    lines.extend(["", "## 5xx samples", ""])
    if not five_xx:
        lines.append("No 5xx responses were captured.")
    else:
        lines.extend(
            [
                "| Time (UTC) | Token | Repository | Probe | Status | Duration | Remaining | Request ID |",
                "| --- | --- | --- | --- | ---: | ---: | ---: | --- |",
            ]
        )
        for item in five_xx:
            lines.append(
                f"| {item.timestamp} | {item.token_source} | {item.repository or '-'} | {item.probe} | "
                f"{item.status} | {item.duration_ms} ms | {item.headers.get('x-ratelimit-remaining', '-')} | "
                f"{item.headers.get('x-github-request-id', '-')} |"
            )

    lines.extend(["", "## Notes", ""])
    if notes:
        lines.extend(f"- {item}" for item in notes)
    else:
        lines.append("No repositories or token sources were skipped.")
    lines.append("")
    return "\n".join(lines)


def validate_args(args: argparse.Namespace) -> None:
    if not 1 <= args.iterations <= 20:
        raise ValueError("iterations must be between 1 and 20")
    if not 0 <= args.delay_ms <= 2000:
        raise ValueError("delay-ms must be between 0 and 2000")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", required=True)
    parser.add_argument("--repositories", required=True, help="Comma-separated repository names")
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--delay-ms", type=int, default=250)
    parser.add_argument("--json-report", default="github-api-diagnostic.json")
    parser.add_argument("--markdown-report", default="github-api-diagnostic.md")
    args = parser.parse_args()

    try:
        validate_args(args)
        repositories = parse_repositories(args.repositories)
    except ValueError as exc:
        print(f"Invalid diagnostic configuration: {exc}", file=sys.stderr)
        return 2

    tokens = [
        ("org-admin-token", os.environ.get("ORG_ADMIN_TOKEN")),
        ("workflow-token", os.environ.get("WORKFLOW_TOKEN")),
    ]
    results: list[ProbeResult] = []
    notes: list[str] = []
    for token_source, token in tokens:
        if not token:
            notes.append(f"Skipped {token_source}; token was not configured")
            continue
        token_results, token_notes = probe_token(
            token_source,
            token,
            args.org,
            repositories,
            args.iterations,
            args.delay_ms / 1000,
        )
        results.extend(token_results)
        notes.extend(token_notes)

    if not results:
        print("No token was available for the diagnostic.", file=sys.stderr)
        return 2

    report = render_markdown(args.org, repositories, args.iterations, args.delay_ms, results, notes)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "organization": args.org,
        "repositories": repositories,
        "iterations": args.iterations,
        "delay_ms": args.delay_ms,
        "interpretation": build_interpretation(results),
        "notes": notes,
        "results": [{**asdict(item), "expected": item.expected} for item in results],
    }
    Path(args.markdown_report).write_text(report, encoding="utf-8")
    Path(args.json_report).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
