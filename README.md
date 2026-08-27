# auto-workflows

Automation workflows for creating public repositories under `huaweicloud-samples`.

## Repository Creation

Open a `New repository request` issue and fill in the required fields. An active organization owner reviews the request and adds the `approved` label to trigger repository creation. Labels added by other users are removed automatically and do not create a repository.

The Team field accepts an exact team display name, slug, `@organization/slug` mention, or GitHub team URL. All requested teams are resolved and validated before a repository is created.

The Scenario field is a required dropdown of 13 predefined values (government, internet, automotive, enterprise, finance, telecom, application intelligence, data integration, distributed x infrastructure, lean governance, cloud native security, deterministic operations, multi-cloud). Detailed scenario explanations: https://3ms.huawei.com/docs/docinfo/1300901382590492672?bookstackId=866760814559879168&gid=3591759&l=zh-cn&documentkind=&attachmentIdx=5

The workflow will:

- Validate issue fields and repository name uniqueness, including the required Scenario dropdown.
- Create a public repository with generated baseline files.
- Generate `README.md`, `.github/CODEOWNERS`, and a placeholder CI workflow.
- Grant requested teams Write permission.
- Set repository topics, including `incubating`.
- Protect the `main` branch with PR review, CODEOWNER review, 3 approvals, and required CI.
- Add the new repository to `awesome-huaweicloud`.
- Comment on and close the request issue.

## Required Secret

Configure `ORG_ADMIN_TOKEN` in repository Actions secrets. The token needs organization access and permission to create repositories, write contents/workflows, set branch protection, manage topics, grant team permissions, read organization membership, and update `awesome-huaweicloud`.
