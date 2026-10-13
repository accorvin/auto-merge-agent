import hashlib
import hmac
import json

import httpx
import pytest

from auto_merge_agent.service import JobQueue, create_app
from auto_merge_agent.types import PullRequestRef


def test_service_refuses_user_token(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "user-token")
    with pytest.raises(ValueError, match="must be unset"):
        create_app(tmp_path)


@pytest.mark.asyncio
async def test_in_memory_queue_deduplicates_and_requeues_changes():
    jobs = JobQueue()
    ref = PullRequestRef(owner="acme", repo="repo", number=12)
    jobs.enqueue(ref)
    jobs.enqueue(ref)
    first = await jobs.next()
    assert first == ("pr", "acme", "repo", 12)
    jobs.enqueue(ref)
    jobs.finish(first)
    assert jobs.pending == {first}
    second = await jobs.next()
    jobs.finish(second)
    assert jobs.pending == set()


@pytest.mark.asyncio
async def test_failed_job_schedules_retry_but_new_event_runs_immediately():
    jobs = JobQueue()
    ref = PullRequestRef(owner="acme", repo="repo", number=12)
    jobs.enqueue(ref)
    job = await jobs.next()
    jobs.finish(job, error=True)
    assert jobs.pending == set()
    assert job in jobs._retry_tasks
    jobs.enqueue(ref)
    assert jobs.pending == {job}
    assert job not in jobs._retry_tasks
    await jobs.close()


@pytest.mark.asyncio
async def test_webhook_verifies_signature_and_queues_status_commit(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/global.yaml").write_text("schema_version: 1\nallowed_models: []\ndefaults: {}\n")
    repo_config = tmp_path / "configs/github.com/acme/repo/config.yaml"
    repo_config.parent.mkdir(parents=True)
    repo_config.write_text("schema_version: 1\nenabled: true\nci: {expected: [{name: build}]}\nrules: [{name: docs, type: changed_paths_only, paths: [README.md]}]\n")
    (repo_config.parent / "OWNERS").write_text("approvers: [alice]\nreviewers: [alice]\n")
    disabled_dir = tmp_path / "configs/github.com/acme/disabled"
    disabled_dir.mkdir(parents=True)
    (disabled_dir / "config.yaml").write_text("schema_version: 1\nenabled: false\n")
    (disabled_dir / "OWNERS").write_text("approvers: [alice]\n")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "test-secret")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_APP_ID", "1")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "test-key-not-used-by-webhook-test")
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
    app = create_app(tmp_path)
    body = json.dumps({"repository": {"owner": {"login": "acme", "id": 42}, "name": "repo"}, "sha": "a" * 40}).encode()
    signature = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        bad = await client.post("/webhook", content=body, headers={"X-GitHub-Event": "status", "X-Hub-Signature-256": "sha256=bad"})
        assert bad.status_code == 401
        good = await client.post("/webhook", content=body, headers={"X-GitHub-Event": "status", "X-Hub-Signature-256": signature})
        assert good.status_code == 200
        outside = json.dumps({"repository": {"owner": {"login": "other-org", "id": 99}, "name": "repo"}, "sha": "b" * 40}).encode()
        outside_signature = "sha256=" + hmac.new(b"test-secret", outside, hashlib.sha256).hexdigest()
        ignored_outside = await client.post("/webhook", content=outside, headers={"X-GitHub-Event": "status", "X-Hub-Signature-256": outside_signature})
        assert ignored_outside.status_code == 200
        assert ignored_outside.json() == {"queued": 0}
        unconfigured = json.dumps({"repository": {"owner": {"login": "acme", "id": 42}, "name": "other"}, "sha": "c" * 40}).encode()
        unconfigured_signature = "sha256=" + hmac.new(b"test-secret", unconfigured, hashlib.sha256).hexdigest()
        ignored = await client.post("/webhook", content=unconfigured, headers={"X-GitHub-Event": "status", "X-Hub-Signature-256": unconfigured_signature})
        assert ignored.status_code == 200
        assert ignored.json() == {"queued": 0}
        disabled = json.dumps({"repository": {"owner": {"login": "acme", "id": 42}, "name": "disabled"}, "sha": "d" * 40}).encode()
        disabled_signature = "sha256=" + hmac.new(b"test-secret", disabled, hashlib.sha256).hexdigest()
        ignored_disabled = await client.post("/webhook", content=disabled, headers={"X-GitHub-Event": "status", "X-Hub-Signature-256": disabled_signature})
        assert ignored_disabled.status_code == 200
        assert ignored_disabled.json() == {"queued": 0}
    assert app.state.jobs.pending == {("commit", "acme", "repo", "a" * 40)}


@pytest.mark.asyncio
async def test_webhook_ignores_own_events_and_unrelated_pr_actions(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/global.yaml").write_text("schema_version: 1\nallowed_models: []\ndefaults: {}\n")
    repo_config = tmp_path / "configs/github.com/acme/repo/config.yaml"
    repo_config.parent.mkdir(parents=True)
    repo_config.write_text("schema_version: 1\nenabled: true\nci: {expected: [{name: build}]}\nrules: [{name: docs, type: changed_paths_only, paths: [README.md]}]\n")
    (repo_config.parent / "OWNERS").write_text("approvers: [alice]\n")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "test-secret")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_APP_ID", "123")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "unused")
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
    app = create_app(tmp_path)
    repository = {"owner": {"login": "acme"}, "name": "repo"}

    async def deliver(client, event, payload):
        body = json.dumps({"repository": repository, **payload}).encode()
        signature = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
        response = await client.post("/webhook", content=body, headers={"X-GitHub-Event": event, "X-Hub-Signature-256": signature})
        assert response.status_code == 200
        return response.json()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        own_comment = {"action": "created", "issue": {"number": 12, "pull_request": {}},
                       "sender": {"login": "real-app[bot]", "type": "Bot"},
                       "comment": {"body": "<!-- auto-merge-agent decision -->\nReview needed"}}
        assert await deliver(client, "issue_comment", own_comment) == {"queued": 0}
        app.state.forge._remember_comment_author({"user": {"login": "real-app[bot]"}})
        assert await deliver(client, "issue_comment", {"action": "created", "issue": {"number": 12, "pull_request": {}},
                                                       "sender": {"login": "real-app[bot]"},
                                                       "comment": {"body": "A later bot comment"}}) == {"queued": 0}
        assert await deliver(client, "pull_request", {"action": "assigned", "number": 12}) == {"queued": 0}
        assert await deliver(client, "check_run", {"action": "completed", "sender": {"login": "real-app[bot]"},
                                                    "check_run": {"name": "automerge/policy", "app": {"id": 123},
                                                                  "pull_requests": [{"number": 12}]}}) == {"queued": 0}
        assert app.state.jobs.pending == set()
        assert await deliver(client, "check_run", {"action": "completed", "sender": {"login": "ci[bot]"},
                                                    "check_run": {"name": "build", "app": {"id": 456},
                                                                  "pull_requests": [{"number": 12}]}}) == {"queued": 1}
        assert app.state.jobs.pending == {("pr", "acme", "repo", 12)}
