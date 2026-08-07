#!/usr/bin/env python3
"""Send sanitized GitHub Actions lifecycle email through Microsoft Graph."""

from __future__ import annotations

import html
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone


REQUIRED_SECRET_ENV = (
    "M365_TENANT_ID",
    "M365_CLIENT_ID",
    "M365_CLIENT_SECRET",
    "M365_SENDER",
    "M365_RECIPIENT",
)

ALLOWED_PHASES = {
    "started",
    "success",
    "failure",
    "cancelled",
    "skipped",
    "notification_failure",
}
HEADER_SAFE = re.compile(r"^[^\r\n]+$")
EMAIL_SAFE = re.compile(r"^[^@\s\r\n]+@[^@\s\r\n]+\.[^@\s\r\n]+$")


class NotificationError(RuntimeError):
    pass


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def require_header_safe(label: str, value: str) -> str:
    if not value:
        return value
    if not HEADER_SAFE.match(value):
        raise NotificationError(f"{label} contains newline characters")
    return value


def require_email(label: str, value: str) -> str:
    require_header_safe(label, value)
    if not EMAIL_SAFE.match(value):
        raise NotificationError(f"{label} is not a valid email address")
    return value


def phase_from_env() -> str:
    phase = env("NOTIFY_PHASE", "notification_failure").lower()
    if phase not in ALLOWED_PHASES:
        raise NotificationError(f"invalid notification phase: {phase}")
    return phase


def load_event() -> dict:
    path = env("GITHUB_EVENT_PATH")
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        raise NotificationError(f"unable to read GitHub event payload: {exc}") from exc


def iso_duration(started_at: str, completed_at: str) -> str:
    if not started_at or not completed_at:
        return ""
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        completed = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    except ValueError:
        return ""
    total = max(0, int((completed - started).total_seconds()))
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def first_pull_request_url(run_payload: dict) -> str:
    pull_requests = run_payload.get("pull_requests") or []
    if not pull_requests or not isinstance(pull_requests[0], dict):
        return ""
    return pull_requests[0].get("html_url") or pull_requests[0].get("url") or ""


def first_line(value: str) -> str:
    lines = value.splitlines()
    return lines[0] if lines else ""


def github_context() -> dict[str, str]:
    event = load_event()
    workflow_run = event.get("workflow_run") if isinstance(event.get("workflow_run"), dict) else {}
    repository = (
        (workflow_run.get("repository") or {}).get("full_name")
        or (event.get("repository") or {}).get("full_name")
        or env("GITHUB_REPOSITORY", "unknown/unknown")
    )
    run_id = str(workflow_run.get("id") or env("GITHUB_RUN_ID"))
    server_url = env("GITHUB_SERVER_URL", "https://github.com")
    sha = workflow_run.get("head_sha") or env("GITHUB_SHA")
    ref_name = workflow_run.get("head_branch") or env("GITHUB_REF_NAME") or env("GITHUB_REF")
    run_url = workflow_run.get("html_url") or (
        f"{server_url}/{repository}/actions/runs/{run_id}" if run_id else ""
    )
    commit_url = f"{server_url}/{repository}/commit/{sha}" if sha and repository != "unknown/unknown" else ""
    head_commit = workflow_run.get("head_commit") or {}
    started_at = workflow_run.get("run_started_at") or workflow_run.get("created_at") or ""
    completed_at = workflow_run.get("updated_at") or ""
    triggering_actor = workflow_run.get("triggering_actor") or workflow_run.get("actor") or {}
    run_attempt = str(workflow_run.get("run_attempt") or env("GITHUB_RUN_ATTEMPT", "1"))
    return {
        "repository": require_header_safe("repository", repository),
        "workflow": require_header_safe(
            "workflow",
            env("NOTIFY_WORKFLOW_NAME")
            or workflow_run.get("name")
            or env("GITHUB_WORKFLOW", "Unknown workflow"),
        ),
        "workflow_file": require_header_safe("workflow file", env("NOTIFY_WORKFLOW_FILE")),
        "run_number": require_header_safe(
            "run number",
            str(workflow_run.get("run_number") or env("GITHUB_RUN_NUMBER", "unknown")),
        ),
        "run_attempt": require_header_safe("run attempt", run_attempt),
        "event": require_header_safe(
            "event",
            workflow_run.get("event") or env("GITHUB_EVENT_NAME", "unknown"),
        ),
        "actor": require_header_safe(
            "actor",
            triggering_actor.get("login") or env("GITHUB_ACTOR", "unknown"),
        ),
        "ref_name": require_header_safe("ref", ref_name),
        "sha": require_header_safe("sha", sha),
        "short_sha": sha[:12] if sha else "",
        "run_url": run_url,
        "commit_url": commit_url,
        "commit_message": require_header_safe(
            "commit message",
            first_line(head_commit.get("message") or ""),
        ),
        "pull_request_url": first_pull_request_url(workflow_run),
        "started_at": require_header_safe("start time", started_at),
        "completed_at": require_header_safe("completion time", completed_at),
        "duration": iso_duration(started_at, completed_at),
        "source_run_id": run_id,
        "correlation_id": require_header_safe(
            "correlation ID",
            env("NOTIFY_CORRELATION_ID")
            or f"{repository}:{run_id or 'unknown'}:{run_attempt}",
        ),
    }


def subject_for(phase: str, context: dict[str, str]) -> str:
    label = {
        "started": "STARTED",
        "success": "SUCCESS",
        "failure": "FAILED",
        "cancelled": "CANCELLED",
        "skipped": "SKIPPED",
        "notification_failure": "NOTIFICATION-FAILED",
    }[phase]
    subject = f"[GitHub Actions][{label}] {context['repository']} - {context['workflow']} - Run {context['run_number']}"
    return require_header_safe("subject", subject)


def render_bodies(phase: str, status: str, context: dict[str, str]) -> tuple[str, str]:
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    summary = env("NOTIFY_WORKLOAD_SUMMARY") or "No additional job summary was provided."
    troubleshooting = env("NOTIFY_TROUBLESHOOTING") or "Open the workflow run and inspect the first failed job."
    rows = [
        ("Status", status),
        ("Repository", context["repository"]),
        ("Workflow", context["workflow"]),
        ("Workflow file", context["workflow_file"] or "not provided"),
        ("Run", f"{context['run_number']} attempt {context['run_attempt']}"),
        ("Event", context["event"]),
        ("Actor", context["actor"]),
        ("Ref", context["ref_name"]),
        ("Commit", context["short_sha"]),
        ("Commit message", context.get("commit_message") or "not available"),
        ("Start time", context.get("started_at") or "not available"),
        ("Completion time", context.get("completed_at") or "not available"),
        ("Duration", context.get("duration") or "not available"),
        ("Generated", now_utc),
        ("Correlation ID", context["correlation_id"]),
    ]
    if context["run_url"]:
        rows.append(("Run URL", context["run_url"]))
    if context["commit_url"]:
        rows.append(("Commit URL", context["commit_url"]))
    if context.get("pull_request_url"):
        rows.append(("Pull request URL", context["pull_request_url"]))
    failed_summary = (
        failed_job_summary(context)
        if phase in {"failure", "cancelled", "notification_failure"}
        else ""
    )
    if failed_summary:
        rows.append(("Failed job or step", failed_summary))
    if phase in {"failure", "cancelled", "notification_failure"}:
        rows.append(("First action", troubleshooting))

    text = "\n".join([f"{key}: {value}" for key, value in rows] + ["", "Summary:", summary])
    html_rows = "\n".join(
        f"<tr><th>{html.escape(key)}</th><td>{html.escape(value)}</td></tr>" for key, value in rows
    )
    html_body = (
        "<html><body>"
        f"<h2>GitHub Actions {html.escape(status)}</h2>"
        "<table>"
        f"{html_rows}"
        "</table>"
        f"<h3>Summary</h3><pre>{html.escape(summary)}</pre>"
        "</body></html>"
    )
    return text, html_body


def request_json(url: str, payload: dict | None, headers: dict[str, str], *, form: bool = False) -> dict:
    data = None
    if payload is not None:
        if form:
            data = urllib.parse.urlencode(payload).encode("utf-8")
        else:
            data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if body else {}


def get_json(url: str, headers: dict[str, str]) -> dict:
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if body else {}


def failed_job_summary(context: dict[str, str]) -> str:
    token = env("GITHUB_TOKEN")
    repository = context.get("repository")
    run_id = context.get("source_run_id")
    if not token or not repository or not run_id:
        return ""
    url = f"https://api.github.com/repos/{repository}/actions/runs/{run_id}/jobs?per_page=100"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        payload = with_retries("GitHub failed-job lookup", lambda: get_json(url, headers))
    except NotificationError as exc:
        print(f"::warning::{exc}")
        return ""
    for job in payload.get("jobs", []):
        if job.get("conclusion") not in {"failure", "timed_out", "cancelled", "action_required"}:
            continue
        failed_steps = [
            step.get("name", "unnamed step")
            for step in job.get("steps", [])
            if step.get("conclusion") in {"failure", "timed_out", "cancelled", "action_required"}
        ]
        summary = f"{job.get('name', 'unnamed job')}"
        if failed_steps:
            summary += ": " + ", ".join(failed_steps[:5])
        return require_header_safe("failed job summary", summary)
    return ""


def with_retries(label: str, func):
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            return func()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt == 3:
                break
            delay = min(20, (2 ** attempt) + random.uniform(0, 1.5))
            print(f"::warning::{label} attempt {attempt} failed; retrying in {delay:.1f}s")
            time.sleep(delay)
    raise NotificationError(f"{label} failed after retries: {last_error}")


def acquire_token() -> str:
    tenant = env("M365_TENANT_ID")
    client_id = env("M365_CLIENT_ID")
    client_secret = env("M365_CLIENT_SECRET")
    for name in REQUIRED_SECRET_ENV:
        if not env(name):
            raise NotificationError(f"missing required secret env {name}")
    token_url = f"https://login.microsoftonline.com/{urllib.parse.quote(tenant)}/oauth2/v2.0/token"
    payload = {
        "client_id": client_id,
        "scope": "https://graph.microsoft.com/.default",
        "client_secret": client_secret,
        "grant_type": "client_credentials",
    }
    response = with_retries(
        "Microsoft Graph token request",
        lambda: request_json(token_url, payload, {"Content-Type": "application/x-www-form-urlencoded"}, form=True),
    )
    token = response.get("access_token")
    if not token:
        raise NotificationError("Microsoft Graph token response did not include access_token")
    return token


def send_mail(token: str, subject: str, text: str, html_body: str) -> None:
    sender = require_email("M365_SENDER", env("M365_SENDER"))
    recipient = require_email("M365_RECIPIENT", env("M365_RECIPIENT"))
    url = f"https://graph.microsoft.com/v1.0/users/{urllib.parse.quote(sender)}/sendMail"
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": [{"emailAddress": {"address": recipient}}],
            "internetMessageHeaders": [
                {"name": "X-Boneman-Notification", "value": "github-actions"},
                {"name": "X-Boneman-Correlation-ID", "value": env("NOTIFY_CORRELATION_ID", "unknown")},
            ],
        },
        "saveToSentItems": True,
    }
    # Keep plain text in logs only as a summary, not as email payload duplication.
    del text
    with_retries(
        "Microsoft Graph sendMail",
        lambda: request_json(
            url,
            payload,
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        ),
    )


def append_summary(status: str, phase: str, subject: str, delivered: bool) -> None:
    path = env("GITHUB_STEP_SUMMARY")
    if not path:
        return
    safe_subject = subject.replace("|", "\\|")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n### Workflow Email Notification\n\n")
        handle.write(f"- Phase: `{phase}`\n")
        handle.write(f"- Status: `{status}`\n")
        handle.write(f"- Subject: `{safe_subject}`\n")
        handle.write(f"- Delivered: `{str(delivered).lower()}`\n")


def main() -> int:
    try:
        phase = phase_from_env()
        status = require_header_safe("status", env("NOTIFY_STATUS", phase))
        context = github_context()
        subject = subject_for(phase, context)
        text, html_body = render_bodies(phase, status, context)
        dry_run = env("NOTIFY_DRY_RUN").lower() == "true"
        if dry_run:
            print(f"::notice::Dry-run notification rendered: {subject}")
            append_summary(status, phase, subject, False)
            return 0
        token = acquire_token()
        send_mail(token, subject, text, html_body)
        append_summary(status, phase, subject, True)
        print(f"::notice::Workflow notification sent: {subject}")
        return 0
    except NotificationError as exc:
        print(f"::warning::Workflow notification failed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
