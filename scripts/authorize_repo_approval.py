#!/usr/bin/env python3
"""Authorize an approved repository request before scheduling repository creation."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from scripts.create_repo_from_issue import GitHubClient, authorize_approval


def main() -> int:
    org = os.environ.get("ORG_NAME", "").strip()
    token = os.environ.get("GH_TOKEN", "").strip()
    issue_token = os.environ.get("ISSUE_TOKEN", "").strip()
    event_path = os.environ.get("GITHUB_EVENT_PATH", "").strip()

    if not org or not token or not issue_token or not event_path:
        print("Missing ORG_NAME, GH_TOKEN, ISSUE_TOKEN, or GITHUB_EVENT_PATH.", file=sys.stderr)
        return 2

    try:
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
        authorized = authorize_approval(
            GitHubClient(token),
            GitHubClient(issue_token),
            event,
            org,
        )
        print("true" if authorized else "false")
        return 0
    except Exception as exc:
        print(f"Approval authorization failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
