from __future__ import annotations

import re
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag, field_validator, model_validator


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


class RuleConfig(StrictModel):
    name: str = Field(min_length=1)
    type: str
    paths: list[str] = Field(default_factory=list)
    statuses: list[Literal["modified", "added", "removed", "renamed", "copied", "changed", "unchanged"]] | None = None
    authors: list[str] | None = Field(default=None, min_length=1)
    model: str | None = None
    max: Literal["low", "medium", "high"] | None = None
    prompt: str | None = None
    max_changed_lines: int | None = Field(default=None, ge=0)
    options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("authors")
    @classmethod
    def validate_authors(cls, authors: list[str] | None) -> list[str] | None:
        if authors is not None and any(not re.fullmatch(r"[A-Za-z0-9-]+(?:\[bot\])?", author) for author in authors):
            raise ValueError("Rule authors must be GitHub logins, not teams or OWNERS aliases")
        return authors

    @model_validator(mode="after")
    def validate_builtin(self) -> RuleConfig:
        if any(not path for path in self.paths):
            raise ValueError(f"Rule {self.name}: paths must not contain empty patterns")
        if self.statuses == []:
            raise ValueError(f"Rule {self.name}: statuses must not be empty")
        if self.statuses is not None and not self.paths:
            raise ValueError(f"Rule {self.name}: statuses requires paths")
        if self.type == "changed_paths_only":
            if not self.paths or self.options or any(value is not None for value in (self.model, self.max, self.prompt, self.max_changed_lines)):
                raise ValueError(f"Rule {self.name}: changed_paths_only requires paths and no LLM fields or options")
        elif self.type in ("llm_complexity", "llm_custom"):
            required = self.max if self.type == "llm_complexity" else self.prompt
            unexpected = self.prompt if self.type == "llm_complexity" else self.max
            if not self.model or not required or (isinstance(required, str) and not required.strip()) or unexpected is not None or self.options:
                raise ValueError(f"Rule {self.name}: {self.type} requires model and its own criterion, with no other criterion or options")
        elif any(value is not None for value in (self.model, self.max, self.prompt, self.max_changed_lines)):
            raise ValueError(f"Rule {self.name}: LLM fields require an LLM rule type")
        return self


class AllRulesConfig(StrictModel):
    name: str = Field(min_length=1)
    all: list[RuleConfig] = Field(min_length=2)


class ProfileRuleConfig(StrictModel):
    profile: str = Field(pattern=r"^[A-Za-z0-9_-]+$")


def _rule_kind(value: Any) -> str:
    if isinstance(value, ProfileRuleConfig) or (isinstance(value, dict) and "profile" in value):
        return "profile"
    return "all" if isinstance(value, AllRulesConfig) or (isinstance(value, dict) and "all" in value) else "atomic"


ProfileEntry = Annotated[
    Annotated[RuleConfig, Tag("atomic")] | Annotated[AllRulesConfig, Tag("all")],
    Discriminator(_rule_kind),
]


EligibilityRule = Annotated[
    Annotated[RuleConfig, Tag("atomic")] | Annotated[AllRulesConfig, Tag("all")] | Annotated[ProfileRuleConfig, Tag("profile")],
    Discriminator(_rule_kind),
]


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
    llm_context_files: list[str] = Field(default_factory=list)
    rules: list[EligibilityRule] = Field(default_factory=list)

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
    llm_context_files: list[str] | None = None
    rules: list[EligibilityRule] | None = None


class ProfileConfig(StrictModel):
    schema_version: Literal[1]
    rules: list[ProfileEntry] = Field(min_length=1)


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
    previous_path: str | None = None
    additions: int
    deletions: int
    status: str
    patch: str | None = None
    diff_metadata: str | None = None

    @property
    def has_complete_patch(self) -> bool:
        if self.patch is None:
            return False
        lines = self.patch.split("\n")
        return (
            sum(line.startswith("+") for line in lines) == self.additions
            and sum(line.startswith("-") for line in lines) == self.deletions
        )


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
