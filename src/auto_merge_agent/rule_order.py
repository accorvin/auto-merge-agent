"""Check rule order after repository policy inheritance and profile expansion."""

from __future__ import annotations

from .types import AllRulesConfig, ProfileEntry


LLM_TYPES = {"llm_complexity", "llm_custom"}


def rule_order_errors(rules: list[ProfileEntry], location: str) -> list[str]:
    errors = []
    first_llm = None
    for rule in rules:
        parts = rule.all if isinstance(rule, AllRulesConfig) else [rule]
        if any(part.type in LLM_TYPES for part in parts):
            first_llm = first_llm or rule.name
        elif first_llm:
            errors.append(f"{location}: static rule {rule.name!r} follows LLM rule {first_llm!r}")

        if isinstance(rule, AllRulesConfig):
            first_llm_part = None
            for part in rule.all:
                if part.type in LLM_TYPES:
                    first_llm_part = first_llm_part or part.name
                elif first_llm_part:
                    errors.append(
                        f"{location}, group {rule.name!r}: static rule {part.name!r} "
                        f"follows LLM rule {first_llm_part!r}"
                    )
    return errors
