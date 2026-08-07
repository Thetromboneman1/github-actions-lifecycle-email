# GitHub Actions lifecycle email

This public, secret-free composite action sends standardized GitHub Actions start, success, failure, cancellation, and skipped emails through Microsoft Graph. Runtime credentials remain in the 1Password vault `Boneman`; callers provide only a narrowly scoped service-account token through the `OP_SERVICE_ACCOUNT_TOKEN` GitHub Actions secret.

## Security model

- The 1Password service account can access only `Boneman`.
- Microsoft Graph tenant, client, sender, recipient, and client-secret values are loaded at runtime and masked by the official 1Password action.
- The action never checks out source code from the observed workflow.
- Callers grant only `actions: read` and `contents: read`.
- The action and all nested third-party actions are pinned to immutable commit SHAs.
- Notification failure is visible in the observer workflow but cannot change the source workflow result.

## Observer workflow

Install one observer per repository and list all source workflow display names. Exclude the observer itself to prevent notification loops.

```yaml
name: Workflow lifecycle email

on:
  workflow_run:
    workflows: [CI, Release]
    types: [in_progress, completed]

permissions:
  actions: read
  contents: read

jobs:
  notify:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - name: Send lifecycle email
        uses: Thetromboneman1/github-actions-lifecycle-email@IMMUTABLE_COMMIT_SHA
        with:
          op-service-account-token: ${{ secrets.OP_SERVICE_ACCOUNT_TOKEN }}
          phase: ${{ github.event.action == 'in_progress' && 'started' || (github.event.workflow_run.conclusion == 'success' && 'success' || github.event.workflow_run.conclusion == 'cancelled' && 'cancelled' || github.event.workflow_run.conclusion == 'skipped' && 'skipped' || 'failure') }}
          status: ${{ github.event.action == 'in_progress' && 'started' || github.event.workflow_run.conclusion }}
          github-token: ${{ secrets.GITHUB_TOKEN }}
```

## Required secret

| GitHub secret | Source |
|---|---|
| `OP_SERVICE_ACCOUNT_TOKEN` | 1Password item `Local / Boneman Topology / Automation Service Account`, field `credential` |

Microsoft Graph values are held in the 1Password item `GitHub Actions Lifecycle Email (Microsoft Graph)`. Do not copy their values into source control or documentation.

## Validation

```bash
python3 -m py_compile send_workflow_email.py
python3 -m pytest -q
actionlint .github/workflows/ci.yml
```

Maintenance status: active. Owner: Thetromboneman1. Last audited: 2026-08-05.
