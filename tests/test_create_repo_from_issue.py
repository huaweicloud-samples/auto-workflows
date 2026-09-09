import io
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import authorize_repo_approval, create_repo_from_issue
from scripts.create_repo_from_issue import (
    DESCRIPTION_MAX,
    SCENARIO_DOC_URL,
    TRUNCATION_MARK,
    VALID_SCENARIOS,
    authorize_approval,
    awesome_index_row,
    create_base_repository,
    is_organization_owner,
    markdown_cell,
    normalize_scenario,
    parse_request,
    readme_template,
    remove_issue_label,
    resolve_request_teams,
    resolve_team_slugs,
    shorten_description,
    split_items,
    split_team_items,
)


def issue_event(description: str, scenario: str = "internet", repo_name: str = "example-sample") -> dict:
    """Build a minimal repository request event carrying the given description."""
    return {
        "issue": {
            "body": "\n".join(
                [
                    "### Repository name",
                    repo_name,
                    "### Scenario",
                    scenario,
                    "### Team",
                    "sample-maintainers",
                    "### Description",
                    description,
                    "### CODEOWNERS",
                    "_No response_",
                    "### Required CI status check",
                    "ci",
                ]
            )
        }
    }


# The description from the request that failed with
# "GraphQL: Description cannot be more than 350 characters (createRepository)".
INCIDENT_DESCRIPTION = (
    "Real-time image optimization workflow based on Huawei Cloud CDN + FunctionGraph + OBS. "
    "Original images are uploaded to OBS buckets and distributed globally via CDN; when cache misses, "
    "FunctionGraph triggers real-time compression/format conversion (WebP/AVIF/thumbnails), writes results "
    "back to OBS and caches them in CDN, balancing performance and cost."
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
                        "### Scenario",
                        "finance",
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
        self.assertEqual(request.scenario, "finance")

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


class ScenarioTests(unittest.TestCase):
    ORG = "huaweicloud-samples"

    @staticmethod
    def event_with_scenario(scenario: str) -> dict:
        return {
            "issue": {
                "body": "\n".join(
                    [
                        "### Repository name",
                        "example-sample",
                        "### Scenario",
                        scenario,
                        "### Team",
                        "sample-maintainers",
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

    def test_issue_template_scenario_options_match_allowed_list(self) -> None:
        template = Path(".github/ISSUE_TEMPLATE/new-repo-request.yml").read_text(encoding="utf-8")
        form = __import__("yaml").safe_load(template)
        scenario_field = next(item for item in form["body"] if item.get("id") == "scenario")

        self.assertEqual(scenario_field["type"], "dropdown")
        self.assertTrue(scenario_field["validations"]["required"])
        self.assertIn(SCENARIO_DOC_URL, scenario_field["attributes"]["description"])
        self.assertEqual(scenario_field["attributes"]["options"], VALID_SCENARIOS)

    def test_parses_every_dropdown_option_case_insensitively(self) -> None:
        for option in VALID_SCENARIOS:
            with self.subTest(option=option):
                request = parse_request(self.event_with_scenario(option.upper()), self.ORG)
                self.assertEqual(request.scenario, option)

    def test_rejects_missing_or_unknown_scenario(self) -> None:
        for raw in ("", "_No response_", "online gaming", "government\\nfinance"):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(ValueError, "Scenario is required"):
                    parse_request(self.event_with_scenario(raw), self.ORG)

        with self.assertRaisesRegex(ValueError, re.escape(SCENARIO_DOC_URL)):
            parse_request(self.event_with_scenario("finance,"), self.ORG)

    def test_normalize_scenarios_strips_whitespace(self) -> None:
        for raw, expected in (
            (" Government ", "government"),
            ("cloud  native   security", "cloud native security"),
            ("Multi-Cloud", "multi-cloud"),
            (VALID_SCENARIOS[8].upper(), VALID_SCENARIOS[8]),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_scenario(raw), expected)

    def test_generated_readme_contains_scenario_badge_and_explanation_link(self) -> None:
        request = parse_request(self.event_with_scenario("cloud native security"), self.ORG)
        readme = readme_template(request, self.ORG)

        self.assertIn(
            f"[![Scenario](https://img.shields.io/badge/Scenario-cloud%20native%20security-success)]({SCENARIO_DOC_URL})",
            readme,
        )
        self.assertNotIn("## Scenario", readme)


class DescriptionLimitTests(unittest.TestCase):
    """Regression tests for the 353-character description that broke `gh repo create`."""

    ORG = "huaweicloud-samples"
    LONG_DESCRIPTION = INCIDENT_DESCRIPTION

    def event_with_description(self, description: str) -> dict:
        return issue_event(description)

    def test_incident_description_really_exceeds_the_github_limit(self) -> None:
        self.assertGreater(len(self.LONG_DESCRIPTION), DESCRIPTION_MAX)

    def test_issue_template_documents_the_description_limit(self) -> None:
        template = Path(".github/ISSUE_TEMPLATE/new-repo-request.yml").read_text(encoding="utf-8")
        form = __import__("yaml").safe_load(template)
        field = next(item for item in form["body"] if item.get("id") == "description")

        self.assertTrue(field["validations"]["required"])
        self.assertIn(str(DESCRIPTION_MAX), field["attributes"]["description"])
        self.assertIn("README", field["attributes"]["description"])

    def test_shortens_at_a_word_boundary_without_inventing_text(self) -> None:
        collapsed = " ".join(self.LONG_DESCRIPTION.split())
        shortened = shorten_description(self.LONG_DESCRIPTION)
        body = shortened[: -len(TRUNCATION_MARK)]

        self.assertLessEqual(len(shortened), DESCRIPTION_MAX)
        self.assertTrue(shortened.endswith(TRUNCATION_MARK))
        self.assertTrue(collapsed.startswith(body))
        # The cut lands on a space, so the last visible word stays complete.
        self.assertEqual(collapsed[len(body)], " ")
        self.assertFalse(body.endswith((",", ";", ":", ".")))

    def test_short_descriptions_survive_and_newlines_collapse(self) -> None:
        self.assertEqual(shorten_description("Example repository"), "Example repository")
        self.assertEqual(shorten_description("Line one\n\n  line two  "), "Line one line two")
        self.assertEqual(len(shorten_description("a" * DESCRIPTION_MAX)), DESCRIPTION_MAX)

    def test_single_long_token_falls_back_to_a_hard_cut(self) -> None:
        shortened = shorten_description("x" * (DESCRIPTION_MAX + 50))

        self.assertEqual(len(shortened), DESCRIPTION_MAX)
        self.assertTrue(shortened.endswith(TRUNCATION_MARK))

    def test_parse_request_keeps_full_text_for_readme_and_shortens_about(self) -> None:
        request = parse_request(self.event_with_description(self.LONG_DESCRIPTION), self.ORG)

        self.assertEqual(request.description, self.LONG_DESCRIPTION)
        self.assertLessEqual(len(request.repo_description), DESCRIPTION_MAX)
        self.assertIn(self.LONG_DESCRIPTION, readme_template(request, self.ORG))

    def test_repository_creation_passes_the_shortened_description_to_gh(self) -> None:
        request = parse_request(self.event_with_description(self.LONG_DESCRIPTION), self.ORG)
        commands: list[list[str]] = []

        with tempfile.TemporaryDirectory() as workspace:
            with (
                patch.object(create_repo_from_issue.tempfile, "mkdtemp", return_value=workspace),
                patch.object(
                    create_repo_from_issue,
                    "run",
                    side_effect=lambda cmd, **kwargs: commands.append(list(cmd)),
                ),
                patch.object(create_repo_from_issue, "client_repo_exists", return_value=False),
                patch.object(
                    create_repo_from_issue,
                    "authenticated_repo_url",
                    return_value="https://example.invalid/repo.git",
                ),
            ):
                create_base_repository(request, self.ORG, "token")

        create_command = next(cmd for cmd in commands if cmd[:3] == ["gh", "repo", "create"])
        passed = create_command[create_command.index("--description") + 1]

        self.assertEqual(passed, request.repo_description)
        self.assertLessEqual(len(passed), DESCRIPTION_MAX)


class AwesomeIndexRowTests(unittest.TestCase):
    """The index entry is a Markdown table row, so the description must stay on one line."""

    ORG = "huaweicloud-samples"

    @staticmethod
    def cells(row: str) -> list[str]:
        return re.split(r"(?<!\\)\|", row.strip())

    def test_multi_line_description_stays_on_one_row(self) -> None:
        request = parse_request(issue_event("Line one\nLine two\n\nLine three"), self.ORG)
        row = awesome_index_row(request, self.ORG)

        self.assertEqual(row.count("\n"), 1)
        self.assertTrue(row.startswith("| [example-sample](https://github.com/huaweicloud-samples/example-sample) | "))
        self.assertTrue(row.endswith("| incubating |\n"))
        self.assertIn("Line one Line two Line three", row)
        self.assertEqual(len(self.cells(row)), 6)

    def test_pipe_characters_are_escaped_instead_of_adding_columns(self) -> None:
        request = parse_request(issue_event("CDN | OBS | FunctionGraph"), self.ORG)
        row = awesome_index_row(request, self.ORG)

        self.assertIn(r"CDN \| OBS \| FunctionGraph", row)
        self.assertEqual(len(self.cells(row)), 6)
        self.assertEqual(markdown_cell("a|b"), r"a\|b")

    def test_long_description_is_kept_in_full(self) -> None:
        request = parse_request(issue_event(INCIDENT_DESCRIPTION), self.ORG)
        row = awesome_index_row(request, self.ORG)

        self.assertIn(INCIDENT_DESCRIPTION, row)
        self.assertNotIn(TRUNCATION_MARK, row)
        self.assertEqual(len(self.cells(row)), 6)


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
