from types import SimpleNamespace

import pytest

from auto_merge_agent.poll import poll_once, run_poll
from auto_merge_agent.types import Decision, PullRequestRef


@pytest.mark.asyncio
async def test_poll_scans_enabled_repos_and_continues_after_pr_error(tmp_path):
    for repo in ("enabled", "disabled"):
        path = tmp_path / "configs/github.com/acme" / repo / "config.yaml"
        path.parent.mkdir(parents=True)
        path.write_text("enabled: true")
    refs = [PullRequestRef(owner="acme", repo="enabled", number=n) for n in (1, 2)]
    scanned = []
    evaluated = []

    class Store:
        root = tmp_path

        def load(self, host, owner, repo):
            return SimpleNamespace(policy=SimpleNamespace(enabled=repo == "enabled"))

    class Forge:
        async def list_open_pull_requests(self, owner, repo):
            scanned.append((owner, repo))
            return refs

    class Engine:
        async def act(self, ref, active):
            evaluated.append((ref.number, active))
            if ref.number == 1:
                raise RuntimeError("temporary failure")
            return Decision(state="waiting", head_sha="a" * 40, reasons=["CI pending"], config_revision="abc")

    await poll_once(Store(), Forge(), Engine(), active=False)
    assert scanned == [("acme", "enabled")]
    assert evaluated == [(1, False), (2, False)]


@pytest.mark.asyncio
async def test_poll_requires_token_or_gh_login(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    def unavailable(*args, **kwargs):
        raise FileNotFoundError("gh")

    monkeypatch.setattr("auto_merge_agent.poll.subprocess.run", unavailable)
    with pytest.raises(ValueError, match="Set GITHUB_TOKEN or sign in"):
        await run_poll(tmp_path, once=True)


def test_poll_uses_gh_token_when_environment_is_unset(monkeypatch):
    from auto_merge_agent.poll import local_github_token

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr("auto_merge_agent.poll.subprocess.run", lambda *args, **kwargs: SimpleNamespace(stdout="gh-token\n"))
    assert local_github_token() == "gh-token"
    monkeypatch.setenv("GITHUB_TOKEN", "environment-token")
    assert local_github_token() == "environment-token"
