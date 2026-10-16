from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path

from .config import ConfigStore
from .engine import EvaluationEngine
from .llm import LangChainJudge
from .plugins import load_plugin
from .service import configure_mlflow
from .types import PullRequestRef


async def _evaluate(args):
    store = ConfigStore(Path(args.config_root))
    forge = load_plugin("forges", "github").from_env()
    try:
        configure_mlflow()
        engine = EvaluationEngine(store, forge, judge=LangChainJudge(store._global().allowed_models))
        decision = await engine.act(PullRequestRef(owner=args.owner, repo=args.repo, number=args.number), active=args.execute)
        print(json.dumps(decision.model_dump(mode="json"), indent=2))
    finally:
        if os.getenv("MLFLOW_TRACKING_URI"):
            import mlflow
            mlflow.flush_trace_async_logging(terminate=True)
        await forge.client.aclose()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-root", default=os.getenv("AUTOMERGE_CONFIG_ROOT", "."))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate-config")
    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("owner")
    evaluate.add_argument("repo")
    evaluate.add_argument("number", type=int)
    evaluate.add_argument("--execute", action="store_true", help="Publish status and merge when eligible")
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    poll = sub.add_parser("poll", help="Poll configured repositories using GITHUB_TOKEN or gh auth")
    poll.add_argument("--interval", type=float, default=900, help="Seconds between scans (default: 900)")
    poll.add_argument("--once", action="store_true", help="Scan once and exit")
    args = parser.parse_args()
    if args.command == "validate-config":
        repos = ConfigStore(Path(args.config_root)).validate_all()
        print(f"Validated {len(repos)} repository configs")
    elif args.command == "evaluate":
        asyncio.run(_evaluate(args))
    elif args.command == "poll":
        from .poll import run_poll

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        asyncio.run(run_poll(Path(args.config_root), interval=args.interval, once=args.once))
    else:
        import uvicorn
        from .service import create_app

        uvicorn.run(create_app(Path(args.config_root)), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
