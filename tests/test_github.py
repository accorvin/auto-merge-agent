import base64
import json

import httpx
import pytest

from auto_merge_agent.github import GitHubError, GitHubForge
from auto_merge_agent.types import Decision, PullRequestRef, PullRequestSnapshot


@pytest.mark.asyncio
async def test_repository_context_is_read_from_base_commit():
    requested = []

    def handler(request):
        requested.append(request)
        if request.url.path.endswith("/AGENTS.md"):
            return httpx.Response(200, json={"type": "file", "encoding": "base64", "size": 11,
                                             "content": base64.b64encode(b"Use caution").decode()})
        return httpx.Response(404, json={"message": "Not Found"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    forge = GitHubForge(token="test", api_url="https://api.github.test", client=client)
    ref = PullRequestRef(owner="acme", repo="repo", number=12)
    pr = PullRequestSnapshot(ref=ref, head_sha="a" * 40, base_sha="b" * 40,
                             state="open", draft=False, title="Test", url="https://example.com", changed_files=1)
    try:
        assert await forge.get_repository_file(pr, "AGENTS.md") == "Use caution"
        assert await forge.get_repository_file(pr, "missing.md") is None
    finally:
        await client.aclose()
    assert requested[0].url.params["ref"] == "b" * 40


@pytest.mark.asyncio
async def test_merge_request_is_guarded_by_evaluated_head():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"merged": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    forge = GitHubForge(token="test", api_url="https://api.github.test", client=client)
    ref = PullRequestRef(owner="acme", repo="repo", number=12)
    pr = PullRequestSnapshot(ref=ref, head_sha="a" * 40, base_sha="b" * 40, state="open", draft=False, title="Test", url="https://github.test/acme/repo/pull/12", changed_files=1)
    try:
        assert await forge.merge(pr, "squash")
    finally:
        await client.aclose()
    assert requests[0].url.path == "/repos/acme/repo/pulls/12/merge"
    assert json.loads(requests[0].content) == {"sha": "a" * 40, "merge_method": "squash"}


@pytest.mark.asyncio
async def test_changed_head_rejects_merge():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(409, json={"message": "Head branch was modified"})))
    forge = GitHubForge(token="test", api_url="https://api.github.test", client=client)
    ref = PullRequestRef(owner="acme", repo="repo", number=12)
    pr = PullRequestSnapshot(ref=ref, head_sha="a" * 40, base_sha="b" * 40, state="open", draft=False, title="Test", url="https://github.test/acme/repo/pull/12", changed_files=1)
    try:
        assert not await forge.merge(pr, "squash")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_missing_app_installation_has_actionable_error(monkeypatch):
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(404, json={"message": "Not Found"})))
    forge = GitHubForge(app_id="1", private_key="test", api_url="https://api.github.test", client=client)
    monkeypatch.setattr(forge, "_app_jwt", lambda: "test-jwt")
    try:
        with pytest.raises(GitHubError, match="not installed on acme/repo") as exc:
            await forge._token(PullRequestRef(owner="acme", repo="repo", number=12))
        assert exc.value.status_code == 404
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_pr_checks_include_test_merge_commit():
    head, merge = "a" * 40, "c" * 40

    def handler(request):
        if request.url.path.endswith(f"/{merge}/check-runs"):
            return httpx.Response(200, json={"check_runs": [{
                "name": "demo-ci", "app": {"slug": "github-actions"},
                "status": "completed", "conclusion": "success", "completed_at": "2026-10-09T20:00:00Z",
            }]})
        if request.url.path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": []})
        if request.url.path.endswith("/statuses"):
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    forge = GitHubForge(token="test", api_url="https://api.github.test", client=client)
    ref = PullRequestRef(owner="acme", repo="repo", number=12)
    pr = PullRequestSnapshot(ref=ref, head_sha=head, base_sha="b" * 40, merge_commit_sha=merge,
                             state="open", draft=False, title="Test", url="https://example.com", changed_files=1)
    try:
        checks = await forge.get_checks(pr)
    finally:
        await client.aclose()
    assert [(check.name, check.source, check.sha) for check in checks] == [("demo-ci", "github-actions", merge)]


@pytest.mark.asyncio
async def test_merge_checklist_is_one_comment_updated_after_merge():
    comments = []
    methods = []

    def handler(request):
        methods.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json=comments)
        body = json.loads(request.content)["body"]
        if request.method == "POST":
            comments.append({"id": 7, "body": body, "user": {"login": "auto-merge-agent[bot]"}})
            return httpx.Response(201, json=comments[0])
        if request.method == "PATCH":
            comments[0]["body"] = body
            return httpx.Response(200, json=comments[0])
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    forge = GitHubForge(token="test", api_url="https://api.github.test", client=client)
    ref = PullRequestRef(owner="acme", repo="repo", number=12)
    pr = PullRequestSnapshot(ref=ref, head_sha="a" * 40, base_sha="b" * 40,
                             state="open", draft=False, title="Test", url="https://example.com", changed_files=1)
    decision = Decision(state="eligible", head_sha=pr.head_sha, reasons=["Eligible"],
                        config_revision="abc123", passed_checks=["CI lint passed", "CodeRabbit reviewed current HEAD"])
    try:
        await forge.publish_merge_checklist(pr, decision, "attempting")
        await forge.publish_merge_checklist(pr, decision, "merged")
        await forge.publish_merge_checklist(pr, decision, "merged")
    finally:
        await client.aclose()
    assert methods.count("POST") == 1
    assert methods.count("PATCH") == 1
    assert "- [x] CI lint passed" in comments[0]["body"]
    assert "Auto-merged after these checks passed" in comments[0]["body"]
