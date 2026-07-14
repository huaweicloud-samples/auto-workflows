# Auto repository creation workflow

This repository creates public repositories for `huaweicloud-samples` from approved GitHub Issues.

## Flow

1. User opens a `New repository request` issue.
2. An active organization owner reviews the request and adds the `approved` label.
3. The authorization job verifies that the label actor is an active organization owner; otherwise it removes the label and stops.
4. The repository creation job is scheduled only when the authorization job returns `authorized=true`.
5. GitHub Actions validates the issue fields and resolves every requested team before creating anything.
6. A public repository is created with generated baseline files.
7. Placeholders such as `{{REPO_NAME}}` are materialized in generated files.
8. `.github/CODEOWNERS` is generated from the request.
9. Team Write permissions are configured.
10. Main branch protection requires pull requests, 3 approvals, CODEOWNER review, and the `ci` status check.
11. Repository topics are set, including `incubating`.
12. `awesome-huaweicloud` is updated with an index entry.
13. The request issue is commented and closed.

GitHub records a workflow run for every label event. For a non-owner applying `approved`, only the authorization job runs; the `create-repo` job is skipped.

## Team field

The `Team` field accepts any of these forms:

- Exact display name, for example `Public Cloud Continuous Operation Dept`
- Team slug, for example `public-cloud-continuous-operation-dept`
- Team mention, for example `@huaweicloud-samples/public-cloud-continuous-operation-dept`
- Team URL, for example `https://github.com/orgs/huaweicloud-samples/teams/public-cloud-continuous-operation-dept`

Separate multiple teams by comma or newline. The workflow resolves names and references to canonical team slugs and validates all teams before repository creation. GitHub may offer its native `@` mention suggestions in the textarea, but repository creation does not depend on those suggestions.

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
