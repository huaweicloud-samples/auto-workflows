import unittest

from scripts.create_repo_from_issue import is_organization_owner, remove_issue_label


class RecordingClient:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[tuple[str, str, object, bool]] = []

    def request(self, method: str, path: str, data: object = None, ok404: bool = False) -> object:
        self.calls.append((method, path, data, ok404))
        return self.response


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


if __name__ == "__main__":
    unittest.main()
