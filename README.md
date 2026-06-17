# auto-workflows

Automation workflows for creating public repositories under `huaweicloud-samples`.

## Repository Creation

Open a `New repository request` issue and fill in the required fields. A maintainer reviews the request and adds the `approved` label to trigger repository creation.

The workflow will:

- Validate issue fields and repository name uniqueness.
- Create a public repository with generated baseline files.
- Generate `README.md`, `.github/CODEOWNERS`, and a placeholder CI workflow.
- Grant requested teams Write permission.
- Set repository topics, including `incubating`.
- Protect the `main` branch with PR review, CODEOWNER review, 3 approvals, and required CI.
- Add the new repository to `awesome-huaweicloud`.
- Comment on and close the request issue.

## Required Secret

Configure `ORG_ADMIN_TOKEN` in repository Actions secrets. The token needs organization access and permission to create repositories, write contents/workflows, set branch protection, manage topics, grant team permissions, and update `awesome-huaweicloud`.
