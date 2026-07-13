# Auto repository creation workflow

This repository creates public repositories for `huaweicloud-samples` from approved GitHub Issues.

## Flow

1. User opens a `New repository request` issue.
2. An active organization owner reviews the request and adds the `approved` label.
3. GitHub Actions verifies that the label actor is an active organization owner; otherwise it removes the label and stops.
4. GitHub Actions validates the issue fields.
5. A public repository is created with generated baseline files.
6. Placeholders such as `{{REPO_NAME}}` are materialized in generated files.
7. `.github/CODEOWNERS` is generated from the request.
8. Team Write permissions are configured.
9. Main branch protection requires pull requests, 3 approvals, CODEOWNER review, and the `ci` status check.
10. Repository topics are set, including `incubating`.
11. `awesome-huaweicloud` is updated with an index entry.
12. The request issue is commented and closed.

## Required secret

Add `ORG_ADMIN_TOKEN` to this repository's Actions secrets.

For fine-grained tokens, use `Repository access: All repositories` and grant:

- Administration: Read and write
- Contents: Read and write
- Issues: Read and write
- Members: Read-only
- Metadata: Read-only
- Workflows: Read and write

The token must also be allowed by the organization token policy.

## Governance Audit

`Repository governance audit` runs quarterly and can also be started manually.

Modes:

- `report-only`: generate Markdown and JSON audit reports only.
- `issue`: create or update `仓库治理规范巡检未通过` issues in non-compliant repositories; close the issue automatically once the repository becomes compliant.

The audit checks repository naming, lifecycle topics, required files, README sections, license policy, contribution rules, code of conduct, deploy/app/scripts conventions, issue/PR templates, and required workflows.
