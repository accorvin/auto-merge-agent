"""Generate JSON Schemas for the YAML config files from their runtime models."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from auto_merge_agent.types import ConfigLayer, GlobalConfig, ProfileConfig


ROOT = Path(__file__).resolve().parents[1]
MODELS = {
    "global.schema.json": GlobalConfig,
    "repository.schema.json": ConfigLayer,  # Also used for org defaults.
    "profile.schema.json": ProfileConfig,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if checked-in schemas are out of date")
    args = parser.parse_args()
    stale = []
    for filename, model in MODELS.items():
        path = ROOT / "schemas" / filename
        schema = model.model_json_schema(mode="validation")
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        content = json.dumps(schema, indent=2, sort_keys=True) + "\n"
        if args.check:
            if not path.is_file() or path.read_text() != content:
                stale.append(str(path.relative_to(ROOT)))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    if stale:
        parser.exit(1, "Outdated schemas: " + ", ".join(stale) + "\nRun: uv run python scripts/generate_schemas.py\n")
    if not args.check:
        print(f"Generated {len(MODELS)} schemas")


if __name__ == "__main__":
    main()
