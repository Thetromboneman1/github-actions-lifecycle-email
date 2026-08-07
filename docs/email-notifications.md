# Lifecycle email operations

## Purpose and ownership

This repository is the canonical public runtime for GitHub Actions lifecycle
email delivery across repositories administered by `Thetromboneman1`.
Maintenance status is active.

## Runtime flow

1. A repository-local `workflow_run` observer receives `in_progress` or
   `completed` for explicitly listed source workflows.
2. The observer calls this composite action at an immutable commit SHA.
3. The official 1Password action exchanges `OP_SERVICE_ACCOUNT_TOKEN` for the
   five Microsoft Graph fields in vault `Boneman`.
4. `send_workflow_email.py` maps terminal conclusions explicitly, including
   `skipped`, renders sanitized run metadata, applies bounded
   retries, and calls Microsoft Graph `sendMail`.
5. Notification failure fails only the observer workflow and cannot change the
   source workflow conclusion.

External-fork workflow runs are excluded by the repository adapter so
untrusted forks cannot consume the service-account token or generate email.

## Configuration and secrets

GitHub stores only `OP_SERVICE_ACCOUNT_TOKEN`. Its source is the `credential`
field of 1Password item
`Local / Boneman Topology / Automation Service Account`.

Microsoft Graph fields are stored only in 1Password item
`GitHub Actions Lifecycle Email (Microsoft Graph)`:

- `tenant_id`
- `client_id`
- `client_secret`
- `sender`
- `recipient`

Never place their values in workflow YAML, documentation, issues, pull
requests, or logs.

## Validation

- Run `Validate lifecycle email action` for Python and YAML validation.
- Dispatch `Lifecycle email health check` with `dry-run: true` to validate
  rendering and vault access without sending.
- Dispatch with `dry-run: false` to validate Microsoft Graph delivery.
- Inspect both `in_progress` and `completed` observer runs after changing the adapter.

The initial production validation was GitHub run `31039016611`; the observer
start and completion validations were `31039199682` and `31039213929`.

## Recovery and rollback

1. Pin callers back to the previous known-good action commit.
2. Confirm `OP_SERVICE_ACCOUNT_TOKEN` still resolves only vault `Boneman`.
3. Validate the Microsoft Graph item’s required field names without displaying values.
4. Run the health check in dry-run mode, then perform one real delivery.
5. If the Graph client secret is rotated, retain the prior Entra credential
   until the new credential and every consumer are verified.

Last audited: 2026-08-05.
