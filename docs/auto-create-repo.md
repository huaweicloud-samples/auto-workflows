# Auto repository creation workflow

This repository creates public repositories for `huaweicloud-samples` from approved GitHub Issues.

## Flow

1. User opens a `New repository request` issue.
2. Maintainer reviews the request and adds the `approved` label.
3. GitHub Actions validates the issue fields.
4. A public repository is created with generated baseline files.
5. Placeholders such as `{{REPO_NAME}}` are materialized in generated files.
6. `.github/CODEOWNERS` is generated from the request.
7. Team Write permissions are configured.
8. Main branch protection requires pull requests, 3 approvals, CODEOWNER review, and the `ci` status check.
9. Repository topics are set, including `incubating`.
10. `awesome-huaweicloud` is updated with an index entry.
11. The request issue is commented and closed.

## Required secret

Add `ORG_ADMIN_TOKEN` to this repository's Actions secrets.

For fine-grained tokens, use `Repository access: All repositories` and grant:

- Administration: Read and write
- Contents: Read and write
- Issues: Read and write
- Metadata: Read-only
- Workflows: Read and write

The token must also be allowed by the organization token policy.
