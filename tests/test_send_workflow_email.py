import importlib.util
import json
import os
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "send_workflow_email.py"
SPEC = importlib.util.spec_from_file_location("send_workflow_email", SCRIPT)
module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(module)


def test_subject_blocks_header_injection(monkeypatch):
    monkeypatch.setenv("NOTIFY_PHASE", "started")
    context = {
        "repository": "owner/repo",
        "workflow": "CI\nBcc: attacker@example.com",
        "run_number": "42",
    }
    with pytest.raises(module.NotificationError):
        module.subject_for("started", context)


def test_email_validation_rejects_newlines():
    with pytest.raises(module.NotificationError):
        module.require_email("recipient", "user@example.com\nBcc: attacker@example.com")


def test_render_bodies_escape_html(monkeypatch):
    monkeypatch.setenv("NOTIFY_WORKLOAD_SUMMARY", "<script>alert(1)</script>")
    context = {
        "repository": "owner/repo",
        "workflow": "CI",
        "workflow_file": ".github/workflows/ci.yml",
        "run_number": "7",
        "run_attempt": "1",
        "event": "workflow_dispatch",
        "actor": "octocat",
        "ref_name": "main",
        "short_sha": "abc123",
        "run_url": "https://github.com/owner/repo/actions/runs/1",
        "commit_url": "https://github.com/owner/repo/commit/abc123",
        "correlation_id": "owner/repo:1:1",
    }
    text, html_body = module.render_bodies("failure", "failure", context)
    assert "<script>" in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_body
    assert "<script>" not in html_body


def test_dry_run_main_does_not_require_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("NOTIFY_DRY_RUN", "true")
    monkeypatch.setenv("NOTIFY_PHASE", "started")
    monkeypatch.setenv("NOTIFY_STATUS", "started")
    monkeypatch.setenv("NOTIFY_WORKFLOW_NAME", "CI")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_RUN_NUMBER", "1")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    for key in module.REQUIRED_SECRET_ENV:
        monkeypatch.delenv(key, raising=False)
    assert module.main() == 0
    assert "Workflow Email Notification" in (tmp_path / "summary.md").read_text()


def test_workflow_run_context_uses_original_run_metadata(monkeypatch, tmp_path):
    payload = {
        "repository": {"full_name": "owner/repo"},
        "workflow_run": {
            "id": 123,
            "name": "CI",
            "run_number": 8,
            "run_attempt": 2,
            "event": "pull_request",
            "head_branch": "feature",
            "head_sha": "a" * 40,
            "html_url": "https://github.com/owner/repo/actions/runs/123",
            "created_at": "2026-07-26T00:00:00Z",
            "run_started_at": "2026-07-26T00:00:05Z",
            "updated_at": "2026-07-26T00:02:10Z",
            "triggering_actor": {"login": "octocat"},
            "head_commit": {"message": "fix: preserve config\n\nDetails"},
            "pull_requests": [
                {"html_url": "https://github.com/owner/repo/pull/9"},
            ],
        },
    }
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(payload))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_run")
    monkeypatch.delenv("NOTIFY_WORKFLOW_NAME", raising=False)

    context = module.github_context()

    assert context["workflow"] == "CI"
    assert context["run_number"] == "8"
    assert context["run_attempt"] == "2"
    assert context["event"] == "pull_request"
    assert context["actor"] == "octocat"
    assert context["ref_name"] == "feature"
    assert context["commit_message"] == "fix: preserve config"
    assert context["duration"] == "2m 5s"
    assert context["pull_request_url"].endswith("/pull/9")
    assert context["correlation_id"] == "owner/repo:123:2"


def test_failed_job_summary_uses_first_failed_step(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(
        module,
        "get_json",
        lambda *_args, **_kwargs: {
            "jobs": [
                {
                    "name": "build",
                    "conclusion": "failure",
                    "steps": [
                        {"name": "compile", "conclusion": "success"},
                        {"name": "test", "conclusion": "failure"},
                    ],
                }
            ]
        },
    )

    assert (
        module.failed_job_summary(
            {"repository": "owner/repo", "source_run_id": "123"}
        )
        == "build: test"
    )


def test_success_render_does_not_query_failed_jobs(monkeypatch):
    monkeypatch.setattr(
        module,
        "failed_job_summary",
        lambda _context: (_ for _ in ()).throw(AssertionError("unexpected lookup")),
    )
    context = {
        "repository": "owner/repo",
        "workflow": "CI",
        "workflow_file": ".github/workflows/ci.yml",
        "run_number": "7",
        "run_attempt": "1",
        "event": "push",
        "actor": "octocat",
        "ref_name": "main",
        "short_sha": "abc123",
        "run_url": "https://github.com/owner/repo/actions/runs/1",
        "commit_url": "https://github.com/owner/repo/commit/abc123",
        "correlation_id": "owner/repo:1:1",
    }

    text, _html = module.render_bodies("success", "success", context)

    assert "Status: success" in text


def test_skipped_phase_has_an_explicit_nonfailure_subject():
    context = {
        "repository": "owner/repo",
        "workflow": "Deploy",
        "run_number": "9",
    }

    assert module.subject_for("skipped", context) == (
        "[GitHub Actions][SKIPPED] owner/repo - Deploy - Run 9"
    )
