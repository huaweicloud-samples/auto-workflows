import io
import json
import unittest
import urllib.error
from email.message import Message
from unittest.mock import patch

from scripts.diagnose_github_api import (
    GitHubApiProbe,
    ProbeResult,
    build_interpretation,
    parse_repositories,
    select_root_blob,
)


class FakeResponse:
    def __init__(self, payload: object, headers: dict[str, str] | None = None, status: int = 200) -> None:
        self.payload = json.dumps(payload).encode("utf-8")
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value
        self.status = status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


def http_error(code: int, payload: bytes, headers: dict[str, str] | None = None) -> urllib.error.HTTPError:
    message = Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return urllib.error.HTTPError(
        "https://api.github.com/test",
        code,
        "test failure",
        message,
        io.BytesIO(payload),
    )


def result(token: str, probe: str, status: int, headers: dict[str, str] | None = None) -> ProbeResult:
    return ProbeResult(
        sequence=1,
        timestamp="2026-07-13T00:00:00+00:00",
        token_source=token,
        probe=probe,
        repository="org/repo",
        ref="main",
        path="/test",
        status=status,
        expected_statuses=[200],
        duration_ms=10,
        headers=headers or {},
    )


class GitHubApiProbeTests(unittest.TestCase):
    def test_captures_502_evidence_without_retrying(self) -> None:
        probe = GitHubApiProbe("org-admin-token", "secret-token")
        error = http_error(
            502,
            b"<html>GitHub backend failure</html>",
            {"X-GitHub-Request-Id": "REQUEST-123", "X-RateLimit-Remaining": "4800"},
        )

        with patch("scripts.diagnose_github_api.urllib.request.urlopen", side_effect=error) as urlopen:
            captured, parsed = probe.request("contents-existing", "/test", [200], "org/repo", "main")

        self.assertIsNone(parsed)
        self.assertEqual(captured.status, 502)
        self.assertEqual(captured.headers["x-github-request-id"], "REQUEST-123")
        self.assertIn("GitHub backend failure", captured.body_preview or "")
        self.assertEqual(urlopen.call_count, 1)
        self.assertNotIn("secret-token", json.dumps(captured.__dict__))

    def test_selects_a_stable_existing_root_file(self) -> None:
        tree = {
            "tree": [
                {"path": "LICENSE", "type": "blob"},
                {"path": "README.md", "type": "blob"},
                {"path": "src", "type": "tree"},
            ]
        }

        self.assertEqual(select_root_blob(tree), "README.md")


class DiagnosticClassificationTests(unittest.TestCase):
    def test_identifies_contents_only_failures_across_both_tokens(self) -> None:
        results = [
            result("org-admin-token", "contents-existing", 502, {"x-github-request-id": "A"}),
            result("workflow-token", "contents-missing", 502, {"x-github-request-id": "B"}),
            result("org-admin-token", "git-tree", 200),
            result("workflow-token", "git-tree", 200),
        ]

        interpretation = " ".join(build_interpretation(results))

        self.assertIn("isolated to the Contents API", interpretation)
        self.assertIn("both token sources", interpretation)
        self.assertIn("GitHub Request IDs were captured", interpretation)

    def test_identifies_explicit_rate_limit_signals(self) -> None:
        interpretation = " ".join(
            build_interpretation([result("org-admin-token", "contents-existing", 429, {"retry-after": "60"})])
        )

        self.assertIn("Explicit rate-limit evidence", interpretation)

    def test_identifies_nearly_exhausted_core_quota(self) -> None:
        snapshot = result("org-admin-token", "rate-limit-before", 200)
        snapshot.details = {"limit": 5000, "remaining": 80, "used": 4920}

        interpretation = " ".join(build_interpretation([snapshot]))

        self.assertIn("at or below 5% remaining", interpretation)

    def test_rejects_unsafe_or_excessive_repository_inputs(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid repository"):
            parse_repositories("safe-repo,bad/repo")
        with self.assertRaisesRegex(ValueError, "at most 10"):
            parse_repositories(",".join(f"repo-{index}" for index in range(11)))


if __name__ == "__main__":
    unittest.main()
