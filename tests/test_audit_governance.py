import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from scripts.audit_governance import GitHubClient, RepoAuditResult, audit_repositories, render_report


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


def http_error(code: int, headers: dict[str, str] | None = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.github.com/test",
        code,
        "temporary failure",
        headers or {},
        io.BytesIO(b"temporary failure"),
    )


class GitHubClientRetryTests(unittest.TestCase):
    def test_retries_transient_502_responses_until_success(self) -> None:
        sleeps: list[float] = []
        client = GitHubClient("token", sleep=sleeps.append)

        with patch(
            "scripts.audit_governance.urllib.request.urlopen",
            side_effect=[http_error(502), http_error(502), FakeResponse({"ok": True})],
        ) as urlopen:
            response = client.request("GET", "/test")

        self.assertEqual(response, {"ok": True})
        self.assertEqual(sleeps, [1.0, 2.0])
        self.assertEqual(urlopen.call_count, 3)

    def test_uses_retry_after_header(self) -> None:
        sleeps: list[float] = []
        client = GitHubClient("token", sleep=sleeps.append)

        with patch(
            "scripts.audit_governance.urllib.request.urlopen",
            side_effect=[http_error(503, {"Retry-After": "7"}), FakeResponse({"ok": True})],
        ):
            client.request("GET", "/test")

        self.assertEqual(sleeps, [7.0])

    def test_raises_after_all_retry_attempts_are_exhausted(self) -> None:
        sleeps: list[float] = []
        client = GitHubClient("token", sleep=sleeps.append)

        with patch(
            "scripts.audit_governance.urllib.request.urlopen",
            side_effect=[http_error(502) for _ in range(4)],
        ) as urlopen:
            with self.assertRaisesRegex(RuntimeError, "HTTP 502"):
                client.request("GET", "/test")

        self.assertEqual(sleeps, [1.0, 2.0, 4.0])
        self.assertEqual(urlopen.call_count, 4)

    def test_does_not_retry_non_idempotent_requests(self) -> None:
        sleeps: list[float] = []
        client = GitHubClient("token", sleep=sleeps.append)

        with patch(
            "scripts.audit_governance.urllib.request.urlopen",
            side_effect=http_error(502),
        ) as urlopen:
            with self.assertRaisesRegex(RuntimeError, "HTTP 502"):
                client.request("POST", "/test", {"title": "test"})

        self.assertEqual(sleeps, [])
        self.assertEqual(urlopen.call_count, 1)


class RepositoryAuditIsolationTests(unittest.TestCase):
    def test_one_repository_error_does_not_stop_later_repositories(self) -> None:
        repos = [
            {"name": "first", "full_name": "org/first", "html_url": "https://example/first"},
            {"name": "second", "full_name": "org/second", "html_url": "https://example/second"},
        ]
        passed = RepoAuditResult(
            name="second",
            full_name="org/second",
            html_url="https://example/second",
            status="passed",
            topics=[],
            findings=[],
        )

        with patch("scripts.audit_governance.audit_repo", side_effect=[RuntimeError("HTTP 502"), passed]) as audit:
            results = audit_repositories(object(), repos)  # type: ignore[arg-type]

        self.assertEqual([result.status for result in results], ["error", "passed"])
        self.assertEqual(audit.call_count, 2)
        report = render_report(results, "org", "report-only")
        self.assertIn("巡检异常：1", report)
        self.assertIn("org/first", report)


if __name__ == "__main__":
    unittest.main()
