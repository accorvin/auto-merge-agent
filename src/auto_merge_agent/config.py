from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .types import ConfigLayer, GlobalConfig, LoadedConfig, PolicyConfig, ProfileConfig, RuleConfig
from .plugins import load_plugin
from .rules import rule_evaluator


class ConfigError(ValueError):
    pass


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")
    return value


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Nested maps merge; scalars and lists replace. Repo always wins over org."""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


class ConfigStore:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def _global(self) -> GlobalConfig:
        try:
            return GlobalConfig.model_validate(_read_yaml(self.root / "config/global.yaml"))
        except ValidationError as exc:
            raise ConfigError(f"Invalid global config: {exc}") from exc

    def load(self, host: str, owner: str, repo: str) -> LoadedConfig:
        if host != "github.com" or any(x in ("", ".", "..") or "/" in x for x in (owner, repo)):
            raise ConfigError("Unsupported host or invalid repository path")
        global_config = self._global()
        raw = global_config.defaults.model_dump(mode="json")
        paths = [
            self.root / "configs" / host / owner / "config.yaml",
            self.root / "configs" / host / owner / repo / "config.yaml",
        ]
        if not paths[1].is_file():
            raise ConfigError(f"No repository config: {paths[1]}")
        for path in paths:
            if not path.is_file():
                continue
            try:
                layer = ConfigLayer.model_validate(_read_yaml(path))
            except ValidationError as exc:
                raise ConfigError(f"Invalid {path}: {exc}") from exc
            raw = _merge(raw, layer.model_dump(exclude_unset=True, exclude={"schema_version"}, mode="json"))
        try:
            policy = PolicyConfig.model_validate(raw)
        except ValidationError as exc:
            raise ConfigError(f"Invalid effective config for {owner}/{repo}: {exc}") from exc

        rules: list[RuleConfig] = []
        for name in policy.profiles:
            if not name or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in name):
                raise ConfigError(f"Invalid profile name: {name!r}")
            profile_path = self.root / "profiles" / f"{name}.yaml"
            try:
                rules.extend(ProfileConfig.model_validate(_read_yaml(profile_path)).rules)
            except ValidationError as exc:
                raise ConfigError(f"Invalid profile {profile_path}: {exc}") from exc
        rules.extend(policy.rules)
        if len({rule.id for rule in rules}) != len(rules):
            raise ConfigError(f"Duplicate rule ID in {owner}/{repo}")
        for rule in rules:
            try:
                rule_evaluator(rule.type)
            except ValueError as exc:
                raise ConfigError(str(exc)) from exc
            if rule.type == "changed_paths_only" and (not rule.paths or rule.options):
                raise ConfigError(f"Rule {rule.id}: changed_paths_only needs paths and no options")
            for gate in (rule.llm.complexity, rule.llm.custom) if rule.llm else ():
                if gate and gate.model not in global_config.allowed_models:
                    raise ConfigError(f"Rule {rule.id}: model {gate.model!r} is not allowlisted")
        if policy.enabled and not policy.ci.expected:
            raise ConfigError(f"Enabled repo {owner}/{repo} requires expected CI checks")
        if policy.trusted_authors is None:
            owners_path = paths[1].parent / "OWNERS"
            approvers = _read_yaml(owners_path).get("approvers")
            if not isinstance(approvers, list):
                raise ConfigError(f"{owners_path} must contain an approvers list")
            trusted_authors = approvers
        else:
            trusted_authors = policy.trusted_authors
        if any(not isinstance(login, str) or not re.fullmatch(r"[A-Za-z0-9-]+(?:\[bot\])?", login) for login in trusted_authors):
            raise ConfigError(f"Invalid trusted author for {owner}/{repo}; use GitHub logins, not teams or OWNERS aliases")
        trusted_authors = sorted({login.lower() for login in trusted_authors})
        if policy.enabled and not trusted_authors:
            raise ConfigError(f"Enabled repo {owner}/{repo} needs at least one trusted author")
        try:
            load_plugin("reviewers", policy.review.plugin)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        context_path = paths[1].parent / "context.md"
        llm_context = ""
        if context_path.exists():
            if not context_path.is_file():
                raise ConfigError(f"{context_path} must be a file")
            try:
                llm_context = context_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise ConfigError(f"Cannot read {context_path}: {exc}") from exc
            if len(llm_context.encode("utf-8")) > 20_000:
                raise ConfigError(f"{context_path} exceeds the 20 KB LLM context limit")
        policy = policy.model_copy(update={"rules": rules})
        canonical = json.dumps({"policy": policy.model_dump(mode="json"), "allowed_models": sorted(global_config.allowed_models), "trusted_authors": trusted_authors, "llm_context": llm_context}, sort_keys=True)
        revision = hashlib.sha256(canonical.encode()).hexdigest()[:16]
        return LoadedConfig(policy=policy, allowed_models=global_config.allowed_models, trusted_authors=trusted_authors, revision=revision, llm_context=llm_context)

    def validate_all(self) -> list[str]:
        if not (self.root / "OWNERS").is_file():
            raise ConfigError("Root OWNERS file is required")
        paths = sorted((self.root / "configs").glob("*/*/*/config.yaml"))
        found = []
        for path in paths:
            if not (path.parent / "OWNERS").is_file():
                raise ConfigError(f"Repository OWNERS file is required beside {path}")
            relative = path.relative_to(self.root / "configs")
            host, owner, repo, _ = relative.parts
            self.load(host, owner, repo)
            found.append(f"{host}/{owner}/{repo}")
        self._global()
        return found
