from pathlib import Path

import pytest

from auto_merge_agent.config import ConfigStore
from auto_merge_agent.engine import EvaluationEngine
from auto_merge_agent.types import (
    Check, ChangedFile, GateResult, MergeRisk, PullRequestRef,
    PullRequestSnapshot, ReviewAssessment,
)


ROOT = Path(__file__).resolve().parents[1]
REF = PullRequestRef(owner="mprahl", repo="test-automerge", number=1)


class DemoForge:
    def __init__(self, files: list[ChangedFile]):
        self.files = files
        self.checks = [
            Check(name="lint", source="github-actions", status="completed", conclusion="success"),
            Check(name="CodeRabbit", source="coderabbitai[bot]", status="completed", conclusion="success"),
        ]

    async def get_pull_request(self, ref):
        return PullRequestSnapshot(
            ref=ref, author_login="mprahl", head_sha="a" * 40, base_sha="b" * 40,
            state="open", draft=False, title="Demo", url="https://example.com",
            changed_files=len(self.files),
        )

    async def get_changed_files(self, pr):
        return self.files

    async def get_checks(self, pr):
        return self.checks

    async def get_review_artifacts(self, pr):
        return []


class DemoReviewer:
    def assess(self, pr, artifacts):
        return ReviewAssessment(
            state="complete", covered_head_sha=pr.head_sha,
            merge_risk=MergeRisk.minimal, reason="Current head reviewed",
        )


class DemoJudge:
    def __init__(self, state="pass"):
        self.state = state
        self.calls = 0

    async def evaluate(self, gates, files, title, body):
        self.calls += 1
        return GateResult(state=self.state, reason="Demo LLM judgment")


def file(path, additions=1, deletions=1):
    return ChangedFile(path=path, status="modified", additions=additions, deletions=deletions, patch="example")


@pytest.mark.asyncio
async def test_dependency_bump_qualifies_without_llm():
    judge = DemoJudge()
    engine = EvaluationEngine(ConfigStore(ROOT), DemoForge([file("go.mod"), file("go.sum", 2, 2)]), DemoReviewer(), judge)
    _, result = await engine.evaluate(REF)
    assert result.state == "eligible"
    assert result.matched_rule == "go-dependencies"
    assert judge.calls == 0


@pytest.mark.asyncio
async def test_small_go_fix_uses_llm():
    judge = DemoJudge()
    engine = EvaluationEngine(ConfigStore(ROOT), DemoForge([file("main.go")]), DemoReviewer(), judge)
    _, result = await engine.evaluate(REF)
    assert result.state == "eligible"
    assert result.matched_rule == "low-risk-maintenance"
    assert judge.calls == 1


@pytest.mark.asyncio
async def test_large_server_change_stops_before_llm():
    judge = DemoJudge()
    engine = EvaluationEngine(ConfigStore(ROOT), DemoForge([file("main.go", 101, 0)]), DemoReviewer(), judge)
    _, result = await engine.evaluate(REF)
    assert result.state == "human_review"
    assert "complexity LLM limit 100 exceeded" in result.reasons[0]
    assert judge.calls == 0


@pytest.mark.asyncio
async def test_small_server_change_can_be_rejected_by_llm():
    judge = DemoJudge("reject")
    engine = EvaluationEngine(ConfigStore(ROOT), DemoForge([file("main.go", 20, 0), file("README.md")]), DemoReviewer(), judge)
    _, result = await engine.evaluate(REF)
    assert result.state == "human_review"
    assert judge.calls == 1


@pytest.mark.asyncio
async def test_coderabbit_status_does_not_satisfy_expected_ci():
    forge = DemoForge([file("go.mod"), file("go.sum")])
    forge.checks = [Check(name="CodeRabbit", source="coderabbitai[bot]", status="completed", conclusion="success")]
    judge = DemoJudge()
    engine = EvaluationEngine(ConfigStore(ROOT), forge, DemoReviewer(), judge)
    _, result = await engine.evaluate(REF)
    assert result.state == "waiting"
    assert "expected CI check: lint" in result.reasons[0]
    assert judge.calls == 0


@pytest.mark.asyncio
async def test_coderabbit_status_failure_blocks_merge_when_observed():
    forge = DemoForge([file("go.mod"), file("go.sum")])
    forge.checks[-1] = Check(name="CodeRabbit", source="coderabbitai[bot]", status="completed", conclusion="failure")
    engine = EvaluationEngine(ConfigStore(ROOT), forge, DemoReviewer(), DemoJudge())
    _, result = await engine.evaluate(REF)
    assert result.state == "human_review"
    assert "CI check CodeRabbit concluded failure" in result.reasons[0]


@pytest.mark.asyncio
async def test_llm_cap_uses_shared_generated_file_exclusion():
    judge = DemoJudge()
    engine = EvaluationEngine(
        ConfigStore(ROOT),
        DemoForge([file("main.go"), file("go.sum", 150, 0)]),
        DemoReviewer(), judge,
    )
    _, result = await engine.evaluate(REF)
    assert result.state == "eligible"
    assert judge.calls == 1


@pytest.mark.asyncio
async def test_custom_llm_section_has_independent_cap(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/global.yaml").write_text((ROOT / "config/global.yaml").read_text())
    repo_dir = tmp_path / "configs/github.com/mprahl/test-automerge"
    repo_dir.mkdir(parents=True)
    (repo_dir / "OWNERS").write_text("approvers: [mprahl]\n")
    (repo_dir / "config.yaml").write_text("""schema_version: 1
enabled: true
ci: {expected: [{name: lint, source: github-actions}], ignore: [{name: CodeRabbit}]}
rules:
  - id: custom-cap
    type: changed_paths_only
    paths: ["*.go"]
    llm:
      custom: {model: gpt-6-sol, prompt: "Is this low risk?", max_changed_lines: 5}
""")
    judge = DemoJudge()
    engine = EvaluationEngine(ConfigStore(tmp_path), DemoForge([file("main.go", 6, 0)]), DemoReviewer(), judge)
    _, result = await engine.evaluate(REF)
    assert result.state == "human_review"
    assert "custom LLM limit 5 exceeded" in result.reasons[0]
    assert judge.calls == 0
