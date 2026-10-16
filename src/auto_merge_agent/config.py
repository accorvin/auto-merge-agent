from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .types import AllRulesConfig, ConfigLayer, EligibilityRule, GlobalConfig, LoadedConfig, PolicyConfig, ProfileConfig, ProfileEntry, ProfileRuleConfig
from .plugins import load_plugin
from .rules import rule_evaluator


class ConfigError(ValueError):
    pass


class _PolicyLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        if isinstance(node, yaml.MappingNode):
            seen = set()
            for key_node, _ in node.value:
                if key_node.tag == "tag:yaml.org,2002:merge":
                    continue
                key = self.construct_object(key_node, deep=deep)
                try:
                    if key in seen:
                        raise yaml.constructor.ConstructorError(None, None, f"Duplicate YAML key: {key!r}", key_node.start_mark)
                    seen.add(key)
                except TypeError as exc:
                    raise yaml.constructor.ConstructorError(None, None, "Unhashable YAML key", key_node.start_mark) from exc
        return super().construct_mapping(node, deep=deep)


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.load(path.read_text(encoding="utf-8"), Loader=_PolicyLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
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

        rules = self._resolve_rules(policy.rules, global_config.allowed_models, f"{owner}/{repo}")
        if policy.enabled and not policy.ci.expected:
            raise ConfigError(f"Enabled repo {owner}/{repo} requires expected CI checks")
        if policy.enabled and not rules:
            raise ConfigError(f"Enabled repo {owner}/{repo} requires at least one eligibility rule")
        for expected in policy.ci.expected:
            if any(expected.name == ignored.name and (ignored.source is None or ignored.source == expected.source) for ignored in policy.ci.ignore):
                raise ConfigError(f"Expected CI check {expected.name!r} is also ignored in {owner}/{repo}")
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

    def _resolve_rules(self, entries: Sequence[EligibilityRule], allowed_models: list[str], location: str) -> list[ProfileEntry]:
        rules: list[ProfileEntry] = []
        for entry in entries:
            if isinstance(entry, ProfileRuleConfig):
                profile_path = self.root / "profiles" / f"{entry.profile}.yaml"
                try:
                    rules.extend(ProfileConfig.model_validate(_read_yaml(profile_path)).rules)
                except ValidationError as exc:
                    raise ConfigError(f"Invalid profile {profile_path}: {exc}") from exc
            else:
                rules.append(entry)
        atomic_rules = [part for rule in rules for part in (rule.all if isinstance(rule, AllRulesConfig) else [rule])]
        all_names = [rule.name for rule in rules] + [part.name for rule in rules if isinstance(rule, AllRulesConfig) for part in rule.all]
        if len(set(all_names)) != len(all_names):
            raise ConfigError(f"Duplicate rule name in {location}")
        for rule in atomic_rules:
            try:
                rule_evaluator(rule.type)
            except ValueError as exc:
                raise ConfigError(str(exc)) from exc
            if rule.model and rule.model not in allowed_models:
                raise ConfigError(f"Rule {rule.name}: model {rule.model!r} is not allowlisted")
        return rules

    def validate_all(self) -> list[str]:
        if not (self.root / "OWNERS").is_file():
            raise ConfigError("Root OWNERS file is required")
        global_config = self._global()
        self._resolve_rules(global_config.defaults.rules, global_config.allowed_models, "global defaults")
        try:
            load_plugin("reviewers", global_config.defaults.review.plugin)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        for path in sorted((self.root / "profiles").glob("*.yaml")):
            try:
                profile = ProfileConfig.model_validate(_read_yaml(path))
            except ValidationError as exc:
                raise ConfigError(f"Invalid profile {path}: {exc}") from exc
            self._resolve_rules(profile.rules, global_config.allowed_models, str(path))
        for path in sorted((self.root / "configs").glob("*/*/config.yaml")):
            if path.relative_to(self.root / "configs").parts[0] != "github.com":
                raise ConfigError(f"Unsupported host in {path}")
            try:
                layer = ConfigLayer.model_validate(_read_yaml(path))
                if layer.review and layer.review.plugin is not None:
                    load_plugin("reviewers", layer.review.plugin)
            except ValueError as exc:
                raise ConfigError(f"Invalid {path}: {exc}") from exc
            self._resolve_rules(layer.rules or [], global_config.allowed_models, str(path))
        paths = sorted((self.root / "configs").glob("*/*/*/config.yaml"))
        found = []
        for path in paths:
            if not (path.parent / "OWNERS").is_file():
                raise ConfigError(f"Repository OWNERS file is required beside {path}")
            relative = path.relative_to(self.root / "configs")
            host, owner, repo, _ = relative.parts
            self.load(host, owner, repo)
            found.append(f"{host}/{owner}/{repo}")
        return found
