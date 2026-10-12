from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Severity(str, Enum):
    info = "info"
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class MergeRisk(str, Enum):
    minimal = "minimal"
    low = "low"
    moderate = "moderate"
    high = "high"
    critical = "critical"


SEVERITY_ORDER = list(Severity)
RISK_ORDER = list(MergeRisk)


class CheckSelector(StrictModel):
    name: str
    source: str | None = None


class CIConfig(StrictModel):
    expected: list[CheckSelector] = Field(default_factory=list)
    ignore: list[CheckSelector] = Field(default_factory=list)
    min_successful_checks: int = Field(default=1, ge=1)


class ReviewConfig(StrictModel):
    plugin: str = "coderabbit"
    max_merge_risk: MergeRisk = MergeRisk.moderate
    max_finding_severity: Severity = Severity.medium


class LimitsConfig(StrictModel):
    max_changed_lines: int | None = Field(default=200, ge=0)
    exclude_from_line_count: list[str] = Field(default_factory=list)


class LLMComplexity(StrictModel):
    max: Literal["low", "medium", "high"]
    model: str
    max_changed_lines: int | None = Field(default=None, ge=0)


class LLMCustom(StrictModel):
    model: str
    prompt: str = Field(min_length=1)
    max_changed_lines: int | None = Field(default=None, ge=0)


class LLMGates(StrictModel):
    complexity: LLMComplexity | None = None
    custom: LLMCustom | None = None


class RuleConfig(StrictModel):
    id: str
    type: str
    paths: list[str] = Field(default_factory=list)
    statuses: list[Literal["modified", "added", "removed", "renamed", "copied", "changed", "unchanged"]] | None = None
    llm: LLMGates | None = None
    options: dict[str, Any] = Field(default_factory=dict)


class MergeConfig(StrictModel):
    method: Literal["merge", "squash", "rebase"] = "squash"


class HumanReviewConfig(StrictModel):
    comment: bool = True
    mention: list[str] = Field(default_factory=list)


class PolicyConfig(StrictModel):
    enabled: bool = False
    trusted_authors: list[str] | None = None
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    ci: CIConfig = Field(default_factory=CIConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    merge: MergeConfig = Field(default_factory=MergeConfig)
    human_review: HumanReviewConfig = Field(default_factory=HumanReviewConfig)
    profiles: list[str] = Field(default_factory=list)
    llm_context_files: list[str] = Field(default_factory=list)
    rules: list[RuleConfig] = Field(default_factory=list)

    @field_validator("llm_context_files")
    @classmethod
    def validate_llm_context_files(cls, paths: list[str]) -> list[str]:
        if len(paths) != len(set(paths)):
            raise ValueError("llm_context_files must not contain duplicates")
        for path in paths:
            if (not path or path.startswith("/") or "\\" in path
                    or any(part in ("", ".", "..") for part in path.split("/"))):
                raise ValueError(f"Invalid llm_context_files path: {path!r}; use a relative repository path")
        return paths


class GlobalConfig(StrictModel):
    schema_version: Literal[1]
    allowed_models: list[str] = Field(default_factory=list)
    defaults: PolicyConfig

    @field_validator("allowed_models")
    @classmethod
    def validate_allowed_models(cls, models: list[str]) -> list[str]:
        if len(models) != len(set(models)):
            raise ValueError("allowed_models must not contain duplicates")
        for model in models:
            if not model or any(char.isspace() for char in model):
                raise ValueError(f"Invalid model {model!r}")
            if ":" in model:
                provider, model_id = model.split(":", 1)
                if not provider or not model_id:
                    raise ValueError(f"Invalid model {model!r}; use provider:model-id")
        return models


class ReviewLayer(StrictModel):
    plugin: str | None = None
    max_merge_risk: MergeRisk | None = None
    max_finding_severity: Severity | None = None


class CILayer(StrictModel):
    expected: list[CheckSelector] | None = None
    ignore: list[CheckSelector] | None = None
    min_successful_checks: int | None = Field(default=None, ge=1)


class LimitsLayer(StrictModel):
    max_changed_lines: int | None = Field(default=None, ge=0)
    exclude_from_line_count: list[str] | None = None


class MergeLayer(StrictModel):
    method: Literal["merge", "squash", "rebase"] | None = None


class HumanReviewLayer(StrictModel):
    comment: bool | None = None
    mention: list[str] | None = None


class ConfigLayer(StrictModel):
    schema_version: Literal[1]
    enabled: bool | None = None
    trusted_authors: list[str] | None = None
    review: ReviewLayer | None = None
    ci: CILayer | None = None
    limits: LimitsLayer | None = None
    merge: MergeLayer | None = None
    human_review: HumanReviewLayer | None = None
    profiles: list[str] | None = None
    llm_context_files: list[str] | None = None
    rules: list[RuleConfig] | None = None


class ProfileConfig(StrictModel):
    schema_version: Literal[1]
    rules: list[RuleConfig]


class PullRequestRef(StrictModel):
    host: str = "github.com"
    owner: str
    repo: str
    number: int

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


class PullRequestSnapshot(StrictModel):
    ref: PullRequestRef
    author_login: str = ""
    head_sha: str
    base_sha: str
    merge_commit_sha: str | None = None
    state: str
    draft: bool
    title: str
    body: str = ""
    url: str
    changed_files: int
    mergeable: bool | None = True
    labels: list[str] = Field(default_factory=list)


class ChangedFile(StrictModel):
    path: str
    additions: int
    deletions: int
    status: str
    patch: str | None = None


class Check(StrictModel):
    name: str
    source: str
    status: str
    conclusion: str | None = None
    sha: str | None = None


class ReviewArtifact(StrictModel):
    kind: Literal["issue_comment", "review", "review_comment"]
    id: int | None = None
    review_id: int | None = None
    author: str
    body: str
    url: str
    commit_id: str | None = None
    original_commit_id: str | None = None
    updated_at: str | None = None
    state: str | None = None


class Finding(StrictModel):
    severity: Severity
    url: str
    summary: str


class ReviewAssessment(StrictModel):
    state: Literal["waiting", "complete", "unavailable", "error"]
    covered_head_sha: str | None = None
    merge_risk: MergeRisk | None = None
    findings: list[Finding] = Field(default_factory=list)
    reason: str
    evidence_url: str | None = None


class GateResult(StrictModel):
    state: Literal["pass", "wait", "reject"]
    reason: str
    passed_checks: list[str] = Field(default_factory=list)


class Decision(StrictModel):
    state: Literal["eligible", "waiting", "human_review"]
    head_sha: str
    reasons: list[str]
    matched_rule: str | None = None
    config_revision: str
    passed_checks: list[str] = Field(default_factory=list)


class LoadedConfig(StrictModel):
    policy: PolicyConfig
    allowed_models: list[str]
    trusted_authors: list[str]
    revision: str
    llm_context: str = ""
