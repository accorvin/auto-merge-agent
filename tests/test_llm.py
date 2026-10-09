from pathlib import Path

import pytest

from auto_merge_agent.config import ConfigStore
from auto_merge_agent.llm import ComplexityAnswer, LangChainJudge, SafetyAnswer
from auto_merge_agent.types import ChangedFile


@pytest.mark.asyncio
async def test_allowed_model_id_uses_exact_api_model_without_temperature(monkeypatch):
    config = ConfigStore(Path(__file__).resolve().parents[1]).load("github.com", "mprahl", "test-automerge")
    calls = []

    class FakeModel:
        def with_structured_output(self, schema):
            calls.append(("schema", schema))
            return self

        async def ainvoke(self, messages):
            calls.append(("messages", messages))
            return SafetyAnswer(safe_to_merge=True, reason="Small wording fix")

    def fake_init_chat_model(model, *, model_provider=None, **kwargs):
        calls.append(("model", model, model_provider, kwargs))
        return FakeModel()

    monkeypatch.setattr("langchain.chat_models.init_chat_model", fake_init_chat_model)
    answer = await LangChainJudge(config.allowed_models)._ask("gpt-6-sol", SafetyAnswer, "criterion", "diff")
    assert answer.safe_to_merge
    assert ("model", "gpt-6-sol", None, {}) in calls
    assert ("schema", SafetyAnswer) in calls


@pytest.mark.asyncio
async def test_passed_llm_sections_supply_checklist_entries(monkeypatch):
    config = ConfigStore(Path(__file__).resolve().parents[1]).load("github.com", "mprahl", "test-automerge")
    gates = next(rule.llm for rule in config.policy.rules if rule.id == "low-risk-maintenance")
    judge = LangChainJudge(config.allowed_models)

    async def answer(model, schema, system, user):
        if schema is ComplexityAnswer:
            return ComplexityAnswer(complexity="low", reason="Localized change")
        return SafetyAnswer(safe_to_merge=True, reason="No interface change")

    monkeypatch.setattr(judge, "_ask", answer)
    verdict = await judge.evaluate(
        gates,
        [ChangedFile(path="main.go", status="modified", additions=1, deletions=1, patch="@@ -1 +1 @@\n-old\n+new")],
        "Small fix", "",
    )
    assert verdict.state == "pass"
    assert len(verdict.passed_checks) == 2
    assert "complexity: low <= low" in verdict.passed_checks[0]
    assert "safe_to_merge=true" in verdict.passed_checks[1]
