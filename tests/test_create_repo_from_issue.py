import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import authorize_repo_approval
from scripts.create_repo_from_issue import (
    authorize_approval,
    is_organization_owner,
    parse_request,
    remove_issue_label,
    resolve_request_teams,
    resolve_team_slugs,
    split_items,
    split_team_items,
)


class RecordingClient:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[tuple[str, str, object, bool]] = []

    def request(self, method: str, path: str, data: object = None, ok404: bool = False) -> object:
        self.calls.append((method, path, data, ok404))
        return self.response


class TeamResolutionTests(unittest.TestCase):
    TEAM_NAME = "Public Cloud Continuous Operation Dept"
    TEAM_SLUG = "public-cloud-continuous-operation-dept"
    ORG = "huaweicloud-samples"

    def team_client(self) -> RecordingClient:
        return RecordingClient([{"name": self.TEAM_NAME, "slug": self.TEAM_SLUG}])

    def test_team_parser_preserves_display_name_spaces(self) -> None:
        self.assertEqual(split_team_items(self.TEAM_NAME), [self.TEAM_NAME])
        self.assertEqual(
            split_team_items(f"{self.TEAM_NAME}, sample-maintainers\nother-team"),
            [self.TEAM_NAME, "sample-maintainers", "other-team"],
        )

    def test_resolves_display_name_slug_mention_and_urls(self) -> None:
        team_url = f"https://github.com/orgs/{self.ORG}/teams/{self.TEAM_SLUG}"
        references = [
            self.TEAM_NAME,
            self.TEAM_SLUG,
            f"@{self.ORG}/{self.TEAM_SLUG}",
            team_url,
            f"[{self.TEAM_NAME}]({team_url})",
        ]

        self.assertEqual(resolve_team_slugs(self.team_client(), self.ORG, references), [self.TEAM_SLUG])

    def test_resolves_issue_team_before_generating_default_codeowners(self) -> None:
        event = {
            "issue": {
                "body": "\n".join(
                    [
                        "### Repository name",
                        "example-sample",
                        "### Team",
                        self.TEAM_NAME,
                        "### Description",
                        "Example repository",
                        "### CODEOWNERS",
                        "_No response_",
                        "### Required CI status check",
                        "ci",
                    ]
                )
            }
        }

        request = parse_request(event, self.ORG)
        self.assertEqual(request.team_slugs, [self.TEAM_NAME])
        self.assertEqual(request.codeowners, [])

        resolve_request_teams(self.team_client(), request, self.ORG)

        self.assertEqual(request.team_slugs, [self.TEAM_SLUG])
        self.assertEqual(request.codeowners, [f"@{self.ORG}/{self.TEAM_SLUG}"])

    def test_rejects_unknown_or_cross_organization_teams(self) -> None:
        with self.assertRaisesRegex(ValueError, "was not found"):
            resolve_team_slugs(self.team_client(), self.ORG, ["Unknown Team"])

        with self.assertRaisesRegex(ValueError, "must belong"):
            resolve_team_slugs(self.team_client(), self.ORG, ["@another-org/other-team"])

    def test_no_response_placeholder_is_not_treated_as_an_item(self) -> None:
        self.assertEqual(split_items("_No response_"), [])
        self.assertEqual(split_team_items("_No response_"), [])


class OrganizationOwnerAuthorizationTests(unittest.TestCase):
    def test_accepts_an_active_organization_owner(self) -> None:
        client = RecordingClient({"state": "active", "role": "admin"})

        self.assertTrue(is_organization_owner(client, "huaweicloud-samples", "octocat"))
        self.assertEqual(
            client.calls,
            [("GET", "/orgs/huaweicloud-samples/memberships/octocat", None, True)],
        )

    def test_rejects_regular_members_and_inactive_owners(self) -> None:
        for membership in (
            {"state": "active", "role": "member"},
            {"state": "pending", "role": "admin"},
            None,
        ):
            with self.subTest(membership=membership):
                self.assertFalse(is_organization_owner(RecordingClient(membership), "huaweicloud-samples", "octocat"))

    def test_rejects_events_without_an_actor(self) -> None:
        client = RecordingClient({"state": "active", "role": "admin"})

        self.assertFalse(is_organization_owner(client, "huaweicloud-samples", ""))
        self.assertEqual(client.calls, [])

    def test_removes_an_unauthorized_approval_label(self) -> None:
        client = RecordingClient(None)
        event = {
            "repository": {"name": "auto-workflows", "owner": {"login": "huaweicloud-samples"}},
            "issue": {"number": 42},
        }

        remove_issue_label(client, event, "approved")

        self.assertEqual(
            client.calls,
            [
                (
                    "DELETE",
                    "/repos/huaweicloud-samples/auto-workflows/issues/42/labels/approved",
                    None,
                    True,
                )
            ],
        )

    def test_owner_approval_allows_creation_without_issue_changes(self) -> None:
        owner_client = RecordingClient({"state": "active", "role": "admin"})
        issue_client = RecordingClient(None)
        event = {
            "sender": {"login": "org-owner"},
            "repository": {"name": "auto-workflows", "owner": {"login": "huaweicloud-samples"}},
            "issue": {"number": 42},
        }

        self.assertTrue(authorize_approval(owner_client, issue_client, event, "huaweicloud-samples"))
        self.assertEqual(issue_client.calls, [])

    def test_member_approval_is_removed_and_rejected(self) -> None:
        member_client = RecordingClient({"state": "active", "role": "member"})
        issue_client = RecordingClient(None)
        event = {
            "sender": {"login": "org-member"},
            "repository": {"name": "auto-workflows", "owner": {"login": "huaweicloud-samples"}},
            "issue": {"number": 42},
        }

        self.assertFalse(authorize_approval(member_client, issue_client, event, "huaweicloud-samples"))
        self.assertEqual(issue_client.calls[0][0:2], ("DELETE", "/repos/huaweicloud-samples/auto-workflows/issues/42/labels/approved"))
        self.assertEqual(issue_client.calls[1][0:2], ("POST", "/repos/huaweicloud-samples/auto-workflows/issues/42/comments"))
        self.assertIn("Only active organization owners", issue_client.calls[1][2]["body"])


class WorkflowAuthorizationGateTests(unittest.TestCase):
    def test_create_job_requires_authorization_output(self) -> None:
        workflow = Path(".github/workflows/auto-create-repo.yml").read_text(encoding="utf-8")

        self.assertIn("python -m scripts.authorize_repo_approval", workflow)
        self.assertIn("needs: authorize_approval", workflow)
        self.assertIn("if: needs.authorize_approval.outputs.authorized == 'true'", workflow)

    def test_authorization_entrypoint_outputs_only_boolean(self) -> None:
        environment = {
            "ORG_NAME": "huaweicloud-samples",
            "GH_TOKEN": "org-token",
            "ISSUE_TOKEN": "issue-token",
            "GITHUB_EVENT_PATH": "event.json",
        }
        for authorized in (True, False):
            with self.subTest(authorized=authorized):
                output = io.StringIO()
                with (
                    patch.dict(authorize_repo_approval.os.environ, environment, clear=True),
                    patch.object(authorize_repo_approval.Path, "read_text", return_value="{}"),
                    patch.object(authorize_repo_approval, "authorize_approval", return_value=authorized),
                    redirect_stdout(output),
                ):
                    exit_code = authorize_repo_approval.main()

                self.assertEqual(exit_code, 0)
                self.assertEqual(output.getvalue(), f"{'true' if authorized else 'false'}\n")


if __name__ == "__main__":
    unittest.main()
