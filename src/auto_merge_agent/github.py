from __future__ import annotations

import asyncio
import base64
import binascii
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import jwt

from .types import Check, ChangedFile, Decision, PullRequestRef, PullRequestSnapshot, ReviewArtifact


class GitHubError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _markdown_text(value: str) -> str:
    single_line = " ".join(value.split())[:500]
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", single_line)


class GitHubForge:
    def __init__(
        self, app_id: str | None = None, private_key: str | None = None,
        token: str | None = None, bot_login: str = "auto-merge-agent[bot]",
        api_url: str = "https://api.github.com", client: httpx.AsyncClient | None = None,
    ):
        self.app_id, self.private_key, self.token, self.bot_login = app_id, private_key, token, bot_login
        self.api_url = api_url.rstrip("/")
        self.client = client or httpx.AsyncClient(timeout=30)
        self._installation_tokens: dict[int, tuple[str, float]] = {}
        self._installations: dict[str, int] = {}

    @classmethod
    def from_env(cls) -> GitHubForge:
        import os

        key = os.getenv("GITHUB_APP_PRIVATE_KEY")
        key_file = os.getenv("GITHUB_APP_PRIVATE_KEY_FILE")
        if key_file:
            key = Path(key_file).read_text()
        return cls(
            app_id=os.getenv("GITHUB_APP_ID"), private_key=key,
            token=os.getenv("GITHUB_TOKEN"),
            bot_login=os.getenv("GITHUB_BOT_LOGIN", "auto-merge-agent[bot]"),
            api_url=os.getenv("GITHUB_API_URL", "https://api.github.com"),
        )

    def _app_jwt(self) -> str:
        if not self.app_id or not self.private_key:
            raise GitHubError("GitHub App ID and private key are required")
        now = int(time.time())
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self.app_id}, self.private_key, algorithm="RS256")

    async def _request(self, method: str, path: str, *, bearer: str, json_data: dict | None = None, params: dict | None = None) -> Any:
        response = await self.client.request(
            method, f"{self.api_url}{path}", json=json_data, params=params,
            headers={
                "Accept": "application/vnd.github+json", "Authorization": f"Bearer {bearer}",
                "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "auto-merge-agent",
            },
        )
        if response.status_code >= 400:
            raise GitHubError(f"GitHub {method} {path} returned {response.status_code}: {response.text[:300]}", response.status_code)
        return response.json() if response.content else None

    async def _token(self, ref: PullRequestRef) -> str:
        if self.token:
            return self.token
        installation_id = self._installations.get(ref.slug)
        if installation_id is None:
            app_jwt = self._app_jwt()
            try:
                installation = await self._request("GET", f"/repos/{ref.slug}/installation", bearer=app_jwt)
            except GitHubError as exc:
                if exc.status_code == 404:
                    raise GitHubError(
                        f"GitHub App is not installed on {ref.slug}, or that repository was not selected during installation",
                        404,
                    ) from exc
                raise
            installation_id = installation["id"]
            self._installations[ref.slug] = installation_id
        cached = self._installation_tokens.get(installation_id)
        if cached and cached[1] > time.time() + 60:
            return cached[0]
        data = await self._request("POST", f"/app/installations/{installation_id}/access_tokens", bearer=self._app_jwt())
        expiry = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()
        self._installation_tokens[installation_id] = (data["token"], expiry)
        return data["token"]

    async def _call(self, ref: PullRequestRef, method: str, path: str, **kwargs) -> Any:
        return await self._request(method, path, bearer=await self._token(ref), **kwargs)

    async def _pages(self, ref: PullRequestRef, path: str) -> list[dict]:
        result: list[dict] = []
        for page in range(1, 101):
            data = await self._call(ref, "GET", path, params={"per_page": 100, "page": page})
            if not isinstance(data, list):
                raise GitHubError(f"Expected a list from {path}")
            result.extend(data)
            if len(data) < 100:
                return result
        raise GitHubError(f"Pagination limit exceeded for {path}")

    async def get_pull_request(self, ref: PullRequestRef) -> PullRequestSnapshot:
        for attempt in range(3):
            data = await self._call(ref, "GET", f"/repos/{ref.slug}/pulls/{ref.number}")
            if data.get("mergeable") is not None or data["state"] != "open" or attempt == 2:
                break
            await asyncio.sleep(1)
        return PullRequestSnapshot(
            ref=ref, author_login=(data.get("user") or {}).get("login") or "",
            head_sha=data["head"]["sha"], base_sha=data["base"]["sha"],
            merge_commit_sha=data.get("merge_commit_sha"),
            state=data["state"], draft=data["draft"], title=data["title"], body=data.get("body") or "",
            url=data["html_url"], changed_files=data["changed_files"], mergeable=data.get("mergeable"),
            labels=[label["name"] for label in data.get("labels") or []],
        )

    async def list_open_pull_requests(self, owner: str, repo: str) -> list[PullRequestRef]:
        ref = PullRequestRef(owner=owner, repo=repo, number=1)
        data = await self._pages(ref, f"/repos/{ref.slug}/pulls?state=open")
        return [PullRequestRef(owner=owner, repo=repo, number=item["number"]) for item in data]

    async def find_pull_requests_for_commit(self, owner: str, repo: str, sha: str) -> list[PullRequestRef]:
        ref = PullRequestRef(owner=owner, repo=repo, number=1)
        data = await self._pages(ref, f"/repos/{ref.slug}/commits/{sha}/pulls")
        return [PullRequestRef(owner=owner, repo=repo, number=item["number"]) for item in data if item.get("state") == "open"]

    async def get_changed_files(self, pr: PullRequestSnapshot) -> list[ChangedFile]:
        data = await self._pages(pr.ref, f"/repos/{pr.ref.slug}/pulls/{pr.ref.number}/files")
        return [ChangedFile(path=item["filename"], additions=item["additions"], deletions=item["deletions"], status=item["status"], patch=item.get("patch")) for item in data]

    async def get_repository_file(self, pr: PullRequestSnapshot, path: str) -> str | None:
        encoded_path = "/".join(quote(part, safe="") for part in path.split("/"))
        try:
            data = await self._call(
                pr.ref, "GET", f"/repos/{pr.ref.slug}/contents/{encoded_path}",
                params={"ref": pr.base_sha},
            )
        except GitHubError as exc:
            if exc.status_code == 404:
                return None
            raise
        if not isinstance(data, dict) or data.get("type") != "file" or data.get("encoding") != "base64":
            raise GitHubError(f"Invalid repository context file: {path}")
        if data.get("size", 0) > 20_000:
            raise GitHubError(f"Repository context file exceeds 20 KB: {path}")
        try:
            content = base64.b64decode("".join(data["content"].split()), validate=True)
            if len(content) > 20_000:
                raise GitHubError(f"Repository context file exceeds 20 KB: {path}")
            return content.decode("utf-8")
        except (KeyError, ValueError, UnicodeError, binascii.Error) as exc:
            raise GitHubError(f"Invalid UTF-8 repository context file: {path}") from exc

    async def get_checks(self, pr: PullRequestSnapshot) -> list[Check]:
        ref = pr.ref
        shas = [pr.head_sha]
        if pr.merge_commit_sha and pr.merge_commit_sha != pr.head_sha:
            shas.append(pr.merge_commit_sha)
        checks: list[Check] = []
        for sha in shas:
            runs: list[dict] = []
            for page in range(1, 101):
                data = await self._call(ref, "GET", f"/repos/{ref.slug}/commits/{sha}/check-runs", params={"per_page": 100, "page": page})
                runs.extend(data["check_runs"])
                if len(data["check_runs"]) < 100:
                    break
            else:
                raise GitHubError("Check-run pagination limit exceeded")
            latest: dict[tuple[str, str], tuple[str, Check]] = {}
            for item in runs:
                source = (item.get("app") or {}).get("slug") or "unknown"
                check = Check(name=item["name"], source=source, status=item["status"], conclusion=item.get("conclusion"), sha=sha)
                stamp = item.get("completed_at") or item.get("started_at") or ""
                key = (check.name, check.source)
                if key not in latest or stamp > latest[key][0]:
                    latest[key] = (stamp, check)
            statuses = await self._pages(ref, f"/repos/{ref.slug}/commits/{sha}/statuses")
            seen_contexts: set[str] = set()
            for item in statuses:
                if item["context"] in seen_contexts:
                    continue
                seen_contexts.add(item["context"])
                source = (item.get("creator") or {}).get("login") or "commit-status"
                check = Check(
                    name=item["context"], source=source,
                    status="completed" if item["state"] != "pending" else "in_progress",
                    conclusion="success" if item["state"] == "success" else item["state"], sha=sha,
                )
                latest[(check.name, check.source)] = (item.get("updated_at") or "", check)
            checks.extend(pair[1] for pair in latest.values())
        return checks

    async def get_review_artifacts(self, pr: PullRequestSnapshot) -> list[ReviewArtifact]:
        ref = pr.ref
        sources = [
            ("issue_comment", f"/repos/{ref.slug}/issues/{ref.number}/comments"),
            ("review", f"/repos/{ref.slug}/pulls/{ref.number}/reviews"),
            ("review_comment", f"/repos/{ref.slug}/pulls/{ref.number}/comments"),
        ]
        artifacts = []
        for kind, path in sources:
            for item in await self._pages(ref, path):
                artifacts.append(ReviewArtifact(
                    kind=kind, id=item.get("id"), review_id=item.get("pull_request_review_id"),
                    author=(item.get("user") or {}).get("login") or "",
                    body=item.get("body") or "", url=item["html_url"],
                    commit_id=item.get("commit_id"), original_commit_id=item.get("original_commit_id"),
                    updated_at=item.get("updated_at") or item.get("submitted_at") or item.get("created_at"),
                    state=item.get("state"),
                ))
        return artifacts

    async def publish_decision(self, pr: PullRequestSnapshot, decision: Decision, mention: list[str], comment: bool) -> None:
        ref = pr.ref
        conclusion = {"eligible": "success", "waiting": None, "human_review": "action_required"}[decision.state]
        summary = "\n".join(f"- {reason}" for reason in decision.reasons)
        payload: dict[str, Any] = {
            "name": "automerge/policy", "head_sha": pr.head_sha,
            "external_id": f"{ref.slug}#{ref.number}@{pr.head_sha}",
            "status": "completed" if conclusion else "in_progress",
            "details_url": pr.url,
            "output": {"title": decision.state.replace("_", " ").title(), "summary": summary[:65000]},
        }
        if conclusion:
            payload["conclusion"] = conclusion
            payload["completed_at"] = datetime.now(timezone.utc).isoformat()
        runs = await self._call(ref, "GET", f"/repos/{ref.slug}/commits/{pr.head_sha}/check-runs", params={"check_name": "automerge/policy", "per_page": 100})
        existing = next((run for run in runs["check_runs"] if run.get("external_id") == payload["external_id"] and (run.get("app") or {}).get("slug") == self.bot_login.removesuffix("[bot]")), None)
        if existing and existing.get("status") == "completed" and existing.get("conclusion") != conclusion:
            existing = None
        if existing:
            payload.pop("name")
            payload.pop("head_sha")
            await self._call(ref, "PATCH", f"/repos/{ref.slug}/check-runs/{existing['id']}", json_data=payload)
        else:
            await self._call(ref, "POST", f"/repos/{ref.slug}/check-runs", json_data=payload)
        if not comment:
            return
        marker = "<!-- auto-merge-agent decision -->"
        old = await self._pages(ref, f"/repos/{ref.slug}/issues/{ref.number}/comments")
        previous = next((item for item in old if (item.get("user") or {}).get("login") == self.bot_login and marker in (item.get("body") or "")), None)
        if decision.state == "waiting" and previous is None:
            return
        mentions = " ".join(value for value in mention if re.fullmatch(r"@[A-Za-z0-9-]+(?:/[A-Za-z0-9_.-]+)?", value))
        body = f"{marker}\n**Automerge: {decision.state.replace('_', ' ')}** for `{decision.head_sha[:12]}`.\n\n{summary}"
        if decision.state == "human_review" and mentions:
            body += f"\n\nHuman review requested: {mentions}"
        if previous:
            if previous["body"] != body:
                await self._call(ref, "PATCH", f"/repos/{ref.slug}/issues/comments/{previous['id']}", json_data={"body": body})
        elif decision.state == "human_review":
            await self._call(ref, "POST", f"/repos/{ref.slug}/issues/{ref.number}/comments", json_data={"body": body})

    async def publish_merge_checklist(self, pr: PullRequestSnapshot, decision: Decision, outcome: str) -> None:
        if decision.state != "eligible" or not decision.passed_checks:
            raise ValueError("A merge checklist requires an eligible decision with passed checks")
        headings = {
            "attempting": "All checks passed; requesting merge",
            "merged": "Auto-merged after these checks passed",
            "aborted": "Merge attempt stopped; these checks passed before the attempt",
        }
        if outcome not in headings:
            raise ValueError(f"Unknown merge checklist outcome: {outcome}")
        ref = pr.ref
        marker = "<!-- auto-merge-agent merge-checklist -->"
        checklist = [f"- [x] {_markdown_text(check)}" for check in decision.passed_checks]
        if outcome == "merged":
            checklist.append("- [x] GitHub merged the evaluated PR head SHA")
        body = (
            f"{marker}\n**{headings[outcome]}** for `{decision.head_sha[:12]}` "
            f"(policy `{decision.config_revision}`).\n\n" + "\n".join(checklist)
        )
        comments = await self._pages(ref, f"/repos/{ref.slug}/issues/{ref.number}/comments")
        previous = next((item for item in comments if (item.get("user") or {}).get("login") == self.bot_login and marker in (item.get("body") or "")), None)
        if previous:
            if previous["body"] != body:
                await self._call(ref, "PATCH", f"/repos/{ref.slug}/issues/comments/{previous['id']}", json_data={"body": body})
        else:
            await self._call(ref, "POST", f"/repos/{ref.slug}/issues/{ref.number}/comments", json_data={"body": body})

    async def merge(self, pr: PullRequestSnapshot, method: str) -> bool:
        ref = pr.ref
        try:
            data = await self._call(ref, "PUT", f"/repos/{ref.slug}/pulls/{ref.number}/merge", json_data={"sha": pr.head_sha, "merge_method": method})
        except GitHubError as exc:
            if exc.status_code == 409:
                return False
            raise
        return bool(data.get("merged"))
