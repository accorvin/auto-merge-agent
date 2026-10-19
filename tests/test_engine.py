from pathlib import Path

import pytest

from auto_merge_agent.config import ConfigStore
from auto_merge_agent.engine import EvaluationEngine, _counted_lines, _human_change_request
from auto_merge_agent.rules import rule_evaluator
from auto_merge_agent.types import (
    Check, ChangedFile, MergeRisk, PullRequestRef, PullRequestSnapshot, ReviewArtifact, ReviewAssessment,
    RuleConfig,
)


@pytest.mark.parametrize("previous_path, expected", [("src/app.py", "reject"), (None, "reject"), ("docs/old.md", "pass")])
@pytest.mark.parametrize("rule_type", ["changed_paths_only", "llm_custom"])
def test_renamed_file_requires_both_paths_in_scope(previous_path, expected, rule_type):
    file = ChangedFile(path="docs/new.md", previous_path=previous_path, status="renamed", additions=10, deletions=5)
    options = {"model": "test", "prompt": "Safe?"} if rule_type == "llm_custom" else {}
    rule = RuleConfig(name="docs", type=rule_type, paths=["docs/**"], **options)
    assert rule_evaluator(rule_type).evaluate(rule, [file]).state == expected
    assert _counted_lines([file], ["docs/**"]) == (0 if expected == "pass" else 15)


@pytest.mark.parametrize("later_state, blocked", [("COMMENTED", True), ("PENDING", True), ("APPROVED", False)])
def test_only_substantive_reviews_clear_change_requests(later_state, blocked):
    artifacts = [
        ReviewArtifact(kind="review", author="alice", body="", url="", state="CHANGES_REQUESTED", updated_at="2026-01-01T00:00:00Z"),
        ReviewArtifact(kind="review", author="alice", body="", url="", state=later_state, updated_at="2026-01-02T00:00:00Z"),
    ]
    assert _human_change_request(artifacts) is blocked


def make_store(tmp_path: Path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/global.yaml").write_text("""schema_version: 1
allowed_models: []
defaults:
  enabled: false
  review: {plugin: coderabbit}
  ci: {expected: [], ignore: [{name: tide}]}
""")
    path = tmp_path / "configs/github.com/acme/repo/config.yaml"
    path.parent.mkdir(parents=True)
    (path.parent / "OWNERS").write_text("approvers: [alice]\nreviewers: [alice]\n")
    path.write_text("""schema_version: 1
enabled: true
ci: {expected: [{name: build}]}
rules:
  - {name: go, type: changed_paths_only, paths: [go.mod, go.sum]}
""")
    return ConfigStore(tmp_path)


class Reviewer:
    def assess(self, pr, artifacts):
        return ReviewAssessment(state="complete", covered_head_sha=pr.head_sha, merge_risk=MergeRisk.low, reason="Reviewed")


class Forge:
    def __init__(self):
        self.heads = ["a" * 40]
        self.author = "alice"
        self.checks = [Check(name="build", source="github-actions", status="completed", conclusion="success")]
        self.merges = []
        self.checklists = []
        self.events = []
        self.merge_result = True

    async def get_pull_request(self, ref):
        head = self.heads.pop(0) if len(self.heads) > 1 else self.heads[0]
        return PullRequestSnapshot(ref=ref, author_login=self.author, head_sha=head, base_sha="b" * 40, state="open", draft=False, title="deps", url="https://example.com", changed_files=2)

    async def get_changed_files(self, pr):
        return [ChangedFile(path="go.mod", additions=2, deletions=2, status="modified", patch="x"), ChangedFile(path="go.sum", additions=3, deletions=3, status="modified", patch="x")]

    async def get_checks(self, pr):
        return self.checks

    async def get_review_artifacts(self, pr):
        return []

    async def publish_decision(self, pr, decision, mention, comment):
        pass

    async def publish_merge_checklist(self, pr, decision, outcome):
        self.checklists.append((outcome, decision.passed_checks))
        self.events.append(f"checklist:{outcome}")

    async def merge(self, pr, method):
        self.merges.append((pr.head_sha, method))
        self.events.append("merge")
        return self.merge_result


@pytest.mark.asyncio
async def test_missing_expected_check_waits(tmp_path):
    forge = Forge()
    forge.checks = [Check(name="unrelated", source="github-actions", status="completed", conclusion="success")]
    engine = EvaluationEngine(make_store(tmp_path), forge, Reviewer())
    _, decision = await engine.evaluate(PullRequestRef(owner="acme", repo="repo", number=1))
    assert decision.state == "waiting"
    assert "expected CI" in decision.reasons[0]


@pytest.mark.asyncio
async def test_rechecks_head_before_merge(tmp_path):
    forge = Forge()
    forge.heads = ["a" * 40, "c" * 40]
    engine = EvaluationEngine(make_store(tmp_path), forge, Reviewer())
    result = await engine.act(PullRequestRef(owner="acme", repo="repo", number=1), active=True)
    assert result.head_sha == "c" * 40
    assert forge.merges == []
    assert forge.checklists == []


@pytest.mark.asyncio
async def test_eligible_uses_direct_merge(tmp_path):
    forge = Forge()
    engine = EvaluationEngine(make_store(tmp_path), forge, Reviewer())
    result = await engine.act(PullRequestRef(owner="acme", repo="repo", number=1), active=True)
    assert result.state == "eligible"
    assert forge.merges == [("a" * 40, "squash")]
    assert [outcome for outcome, _ in forge.checklists] == ["attempting", "merged"]
    assert forge.events == ["checklist:attempting", "merge", "checklist:merged"]
    assert any("CI build" in check for check in result.passed_checks)
    assert any("Static eligibility rule go" in check for check in result.passed_checks)
    assert any("Final recheck" in check for check in result.passed_checks)


@pytest.mark.asyncio
async def test_failed_merge_updates_checklist_without_claiming_merge(tmp_path):
    forge = Forge()
    forge.merge_result = False
    engine = EvaluationEngine(make_store(tmp_path), forge, Reviewer())
    result = await engine.act(PullRequestRef(owner="acme", repo="repo", number=1), active=True)
    assert result.state == "waiting"
    assert [outcome for outcome, _ in forge.checklists] == ["attempting", "aborted"]


@pytest.mark.asyncio
async def test_drive_by_author_cannot_merge_even_when_other_gates_pass(tmp_path):
    forge = Forge()
    forge.author = "drive-by-contributor"
    engine = EvaluationEngine(make_store(tmp_path), forge, Reviewer())
    result = await engine.act(PullRequestRef(owner="acme", repo="repo", number=1), active=True)
    assert result.state == "human_review"
    assert "not a trusted author" in result.reasons[0]
    assert forge.merges == []


@pytest.mark.asyncio
@pytest.mark.parametrize("author, expected", [
    ("FILE-SYNC[bot]", "eligible"),
    ("alice", "human_review"),
])
async def test_rule_authors_override_repository_trusted_authors(tmp_path, author, expected):
    store = make_store(tmp_path)
    repo = tmp_path / "configs/github.com/acme/repo"
    (repo / "OWNERS").write_text("approvers: [alice]\n")
    (repo / "config.yaml").write_text("""schema_version: 1
enabled: true
ci: {expected: [{name: build}]}
rules:
  - name: bot-go
    type: changed_paths_only
    paths: [go.mod, go.sum]
    authors: ['file-sync[bot]', other-bot]
""")
    forge = Forge()
    forge.author = author
    _, decision = await EvaluationEngine(store, forge, Reviewer()).evaluate(PullRequestRef(owner="acme", repo="repo", number=1))
    assert decision.state == expected
    assert decision.matched_rule == ("bot-go" if expected == "eligible" else None)


@pytest.mark.asyncio
async def test_other_rule_can_match_when_author_restricted_rule_does_not(tmp_path):
    store = make_store(tmp_path)
    repo = tmp_path / "configs/github.com/acme/repo/config.yaml"
    repo.write_text("""schema_version: 1
enabled: true
ci: {expected: [{name: build}]}
rules:
  - {name: bot-go, type: changed_paths_only, paths: [go.mod, go.sum], authors: [sync-bot]}
  - {name: maintainer-go, type: changed_paths_only, paths: [go.mod, go.sum]}
""")
    _, decision = await EvaluationEngine(store, Forge(), Reviewer()).evaluate(PullRequestRef(owner="acme", repo="repo", number=1))
    assert decision.state == "eligible"
    assert decision.matched_rule == "maintainer-go"
