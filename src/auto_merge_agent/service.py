from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request

from .config import ConfigError, ConfigStore
from .engine import EvaluationEngine
from .llm import LangChainJudge
from .plugins import load_plugin
from .types import PullRequestRef

# Uvicorn configures this logger at INFO; the root logger may only show warnings.
logger = logging.getLogger("uvicorn.error")
Job = tuple[str, str, str, int | str]


class JobQueue:
    """One-process queue that deduplicates pending jobs and reruns changed jobs."""

    def __init__(self):
        self._pending: asyncio.Queue[Job] = asyncio.Queue()
        self._queued: set[Job] = set()
        self._running: set[Job] = set()
        self._rerun: set[Job] = set()
        self._attempts: dict[Job, int] = {}
        self._retry_tasks: dict[Job, asyncio.Task[None]] = {}

    def _enqueue(self, job: Job) -> None:
        retry = self._retry_tasks.pop(job, None)
        if retry:
            retry.cancel()
        if job in self._running:
            self._rerun.add(job)
        elif job not in self._queued:
            self._queued.add(job)
            self._pending.put_nowait(job)

    def enqueue(self, ref: PullRequestRef) -> None:
        self._enqueue(("pr", ref.owner, ref.repo, ref.number))

    def enqueue_commit(self, owner: str, repo: str, sha: str) -> None:
        self._enqueue(("commit", owner, repo, sha))

    async def next(self) -> Job:
        job = await self._pending.get()
        self._queued.remove(job)
        self._running.add(job)
        return job

    async def _retry_after(self, job: Job, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        self._retry_tasks.pop(job, None)
        self._enqueue(job)

    def finish(self, job: Job, *, error: bool = False) -> None:
        self._running.remove(job)
        self._pending.task_done()
        if job in self._rerun:
            self._rerun.remove(job)
            self._enqueue(job)
        elif error:
            attempts = self._attempts.get(job, 0) + 1
            self._attempts[job] = attempts
            delay = min(3600, 30 * (1 << min(attempts - 1, 7)))
            self._retry_tasks[job] = asyncio.create_task(self._retry_after(job, delay))
        else:
            self._attempts.pop(job, None)

    async def close(self) -> None:
        tasks = list(self._retry_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._retry_tasks.clear()

    @property
    def pending(self) -> set[Job]:
        return set(self._queued)


def configure_mlflow() -> None:
    uri = os.getenv("MLFLOW_TRACKING_URI")
    if not uri:
        return
    import mlflow

    mlflow.set_tracking_uri(uri)
    mlflow.set_experiment(os.getenv("MLFLOW_EXPERIMENT", "auto-merge-agent"))
    mlflow.langchain.autolog()


def create_app(root: Path | None = None) -> FastAPI:
    if os.getenv("GITHUB_TOKEN"):
        raise ValueError("GITHUB_TOKEN must be unset for the webhook service; use GitHub App credentials")
    if not os.getenv("GITHUB_APP_ID") or not (os.getenv("GITHUB_APP_PRIVATE_KEY") or os.getenv("GITHUB_APP_PRIVATE_KEY_FILE")):
        raise ValueError("GITHUB_APP_ID and a GitHub App private key are required for the webhook service")
    root = root or Path(os.getenv("AUTOMERGE_CONFIG_ROOT", Path.cwd()))
    store = ConfigStore(root)
    queue = JobQueue()
    forge = load_plugin("forges", "github").from_env()
    configure_mlflow()
    engine = EvaluationEngine(store, forge, judge=LangChainJudge(store._global().allowed_models))
    active = os.getenv("AUTOMERGE_ACTIVE", "false").lower() == "true"

    async def worker():
        while True:
            job = await queue.next()
            kind, owner, repo, value = job
            failed = False
            try:
                if kind == "commit":
                    refs = await forge.find_pull_requests_for_commit(owner, repo, str(value))
                    logger.info("Commit %s in %s/%s maps to %d open PR(s)", str(value)[:12], owner, repo, len(refs))
                    for ref in refs:
                        queue.enqueue(ref)
                else:
                    logger.info("Evaluating PR %s/%s#%s in %s mode", owner, repo, value, "active" if active else "shadow")
                    decision = await engine.act(PullRequestRef(owner=owner, repo=repo, number=int(value)), active=active)
                    logger.info(
                        "PR %s/%s#%s head=%s decision=%s rule=%s mode=%s: %s",
                        owner, repo, value, decision.head_sha[:12], decision.state,
                        decision.matched_rule or "none", "active" if active else "shadow",
                        "; ".join(decision.reasons),
                    )
            except ConfigError as exc:
                logger.warning("Skipping %s/%s job: %s", owner, repo, exc)
            except Exception:
                logger.exception("Job failed for %s/%s: %s", owner, repo, value)
                failed = True
            finally:
                queue.finish(job, error=failed)

    async def reconcile():
        while True:
            try:
                for path in (root / "configs/github.com").glob("*/*/config.yaml"):
                    owner, repo = path.parent.parent.name, path.parent.name
                    try:
                        config = store.load("github.com", owner, repo)
                        if not config.policy.enabled:
                            continue
                        logger.info("Reconciling %s/%s", owner, repo)
                        refs = await forge.list_open_pull_requests(owner, repo)
                        logger.info("Reconciled %s/%s: %d open PR(s) queued", owner, repo, len(refs))
                        for ref in refs:
                            queue.enqueue(ref)
                    except Exception:
                        logger.exception("Reconciliation failed for %s/%s", owner, repo)
            except Exception:
                logger.exception("Reconciliation scan failed")
            await asyncio.sleep(900)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("Auto-merge service started in %s mode", "active" if active else "shadow")
        tasks = [asyncio.create_task(worker()), asyncio.create_task(reconcile())]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await queue.close()
            if os.getenv("MLFLOW_TRACKING_URI"):
                import mlflow
                await asyncio.to_thread(mlflow.flush_trace_async_logging, terminate=True)
            await forge.client.aclose()

    app = FastAPI(lifespan=lifespan)
    app.state.jobs = queue
    app.state.forge = forge

    @app.get("/health")
    async def health():
        return {"ok": True, "active": active}

    @app.post("/webhook")
    async def webhook(request: Request, x_hub_signature_256: str | None = Header(default=None), x_github_event: str | None = Header(default=None)):
        secret = os.getenv("GITHUB_WEBHOOK_SECRET")
        if not secret:
            raise HTTPException(503, "Webhook secret is not configured")
        body = await request.body()
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not x_hub_signature_256 or not hmac.compare_digest(expected, x_hub_signature_256):
            raise HTTPException(401, "Invalid webhook signature")
        data = json.loads(body)
        repository = data.get("repository") or {}
        owner = (repository.get("owner") or {}).get("login")
        repo = repository.get("name")
        if not owner or not repo:
            return {"queued": 0}
        try:
            config = store.load("github.com", owner, repo)
        except ConfigError:
            return {"queued": 0}
        if not config.policy.enabled:
            return {"queued": 0}
        numbers: set[int] = set()
        action = data.get("action")
        sender = data.get("sender") or {}
        own_sender = bool(forge.bot_login and sender.get("login") == forge.bot_login)
        if x_github_event == "pull_request" and action in {
            "opened", "reopened", "synchronize", "ready_for_review", "converted_to_draft",
            "edited", "labeled", "unlabeled",
        }:
            numbers.add(data.get("number"))
        elif x_github_event == "pull_request_review" and action in {"submitted", "dismissed", "edited"}:
            numbers.add((data.get("pull_request") or {}).get("number"))
        elif x_github_event == "pull_request_review_comment" and action in {"created", "edited", "deleted"}:
            numbers.add((data.get("pull_request") or {}).get("number"))
        elif x_github_event == "issue_comment" and action in {"created", "edited", "deleted"} and (data.get("issue") or {}).get("pull_request"):
            comment = data.get("comment") or {}
            app = comment.get("performed_via_github_app") or {}
            own_comment = (
                own_sender or
                (forge.app_id is not None and str(app.get("id")) == str(forge.app_id)) or
                ((sender.get("type") == "Bot" or (comment.get("user") or {}).get("type") == "Bot")
                 and "<!-- auto-merge-agent " in (comment.get("body") or ""))
            )
            if own_comment:
                return {"queued": 0}
            numbers.add((data.get("issue") or {}).get("number"))
        elif x_github_event in ("check_run", "check_suite") and action in {"created", "completed", "rerequested", "requested", "in_progress"}:
            check = data.get(x_github_event) or {}
            check_app = check.get("app") or {}
            if own_sender or check.get("name") == "automerge/policy" or (forge.app_id is not None and str(check_app.get("id")) == str(forge.app_id)):
                return {"queued": 0}
            for pr in check.get("pull_requests") or []:
                numbers.add(pr.get("number"))
        elif x_github_event == "status" and isinstance(data.get("sha"), str):
            if own_sender or data.get("context") == "automerge/policy":
                return {"queued": 0}
            queue.enqueue_commit(owner, repo, data["sha"])
        for number in numbers:
            if isinstance(number, int) and number > 0:
                queue.enqueue(PullRequestRef(owner=owner, repo=repo, number=number))
        return {"queued": len([number for number in numbers if isinstance(number, int) and number > 0])}

    return app
