from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path

from .config import ConfigStore
from .engine import EvaluationEngine
from .github import GitHubForge
from .llm import LangChainJudge
from .service import configure_mlflow

logger = logging.getLogger("auto_merge_agent.poll")


def local_github_token() -> str:
    token = os.getenv("GITHUB_TOKEN")
    if token:
        return token
    try:
        result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("Set GITHUB_TOKEN or sign in with `gh auth login` for polling mode") from exc
    token = result.stdout.strip()
    if not token:
        raise ValueError("Set GITHUB_TOKEN or sign in with `gh auth login` for polling mode")
    return token


async def poll_once(store: ConfigStore, forge: GitHubForge, engine: EvaluationEngine, *, active: bool) -> None:
    """Evaluate every open PR in each enabled repository config."""
    for path in sorted((store.root / "configs/github.com").glob("*/*/config.yaml")):
        owner, repo = path.parent.parent.name, path.parent.name
        try:
            config = store.load("github.com", owner, repo)
            if not config.policy.enabled:
                continue
            refs = await forge.list_open_pull_requests(owner, repo)
            logger.info("Polling %s/%s: %d open PR(s)", owner, repo, len(refs))
        except Exception:
            logger.exception("Could not poll %s/%s", owner, repo)
            continue
        for ref in refs:
            try:
                decision = await engine.act(ref, active=active)
                logger.info(
                    "PR %s#%d head=%s decision=%s rule=%s mode=%s: %s",
                    ref.slug, ref.number, decision.head_sha[:12], decision.state,
                    decision.matched_rule or "none", "active" if active else "shadow",
                    "; ".join(decision.reasons),
                )
            except Exception:
                logger.exception("Could not evaluate %s#%d", ref.slug, ref.number)


async def run_poll(root: Path, *, interval: float = 900, once: bool = False) -> None:
    token = local_github_token()
    if interval <= 0:
        raise ValueError("Polling interval must be positive")
    store = ConfigStore(root)
    forge = GitHubForge(token=token, api_url=os.getenv("GITHUB_API_URL", "https://api.github.com"))
    active = os.getenv("AUTOMERGE_ACTIVE", "false").lower() == "true"
    try:
        configure_mlflow()
        engine = EvaluationEngine(store, forge, judge=LangChainJudge(store._global().allowed_models))
        if active:
            await forge.identify_token_user()
        logger.info("Polling started in %s mode", "active" if active else "shadow")
        while True:
            await poll_once(store, forge, engine, active=active)
            if once:
                break
            await asyncio.sleep(interval)
    finally:
        if os.getenv("MLFLOW_TRACKING_URI"):
            import mlflow

            await asyncio.to_thread(mlflow.flush_trace_async_logging, terminate=True)
        await forge.client.aclose()
