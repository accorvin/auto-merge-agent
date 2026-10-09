from __future__ import annotations

from fnmatch import fnmatchcase
from importlib.metadata import entry_points
from typing import Protocol

from .types import ChangedFile, GateResult, RuleConfig


def path_matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/**"):
        return path.startswith(pattern[:-3] + "/")
    return fnmatchcase(path, pattern)


class RuleEvaluator(Protocol):
    def evaluate(self, rule: RuleConfig, files: list[ChangedFile]) -> GateResult: ...


class ChangedPathsOnly:
    def evaluate(self, rule: RuleConfig, files: list[ChangedFile]) -> GateResult:
        if files and all(
            (rule.statuses is None or file.status in rule.statuses)
            and any(path_matches(file.path, pattern) for pattern in rule.paths)
            for file in files
        ):
            return GateResult(state="pass", reason=f"Rule {rule.id} matched")
        return GateResult(state="reject", reason=f"Rule {rule.id} did not match changed paths")


BUILTIN_RULES: dict[str, type[RuleEvaluator]] = {"changed_paths_only": ChangedPathsOnly}


def rule_evaluator(rule_type: str) -> RuleEvaluator:
    if rule_type in BUILTIN_RULES:
        return BUILTIN_RULES[rule_type]()
    matches = [entry for entry in entry_points(group="auto_merge_agent.rules") if entry.name == rule_type]
    if len(matches) != 1:
        raise ValueError(f"Unknown rule plugin: {rule_type}")
    return matches[0].load()()
