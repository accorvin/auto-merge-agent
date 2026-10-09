from pathlib import Path

import pytest

from auto_merge_agent.config import ConfigError, ConfigStore


def write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def setup_config(tmp_path: Path):
    write(tmp_path / "OWNERS", "approvers: [mprahl]\nreviewers: [mprahl]\n")
    write(tmp_path / "config/global.yaml", """schema_version: 1
allowed_models:
  - gpt-6-sol
defaults:
  review: {plugin: coderabbit, max_merge_risk: moderate, max_finding_severity: medium}
  ci: {expected: [], ignore: [{name: tide}], min_successful_checks: 1}
""")
    write(tmp_path / "configs/github.com/acme/config.yaml", """schema_version: 1
review: {max_merge_risk: low}
limits: {max_changed_lines: 50}
profiles: [go-deps]
trusted_authors: [org-maintainer]
""")
    write(tmp_path / "profiles/go-deps.yaml", """schema_version: 1
rules:
  - {id: go, type: changed_paths_only, paths: [go.mod, go.sum]}
""")
    write(tmp_path / "configs/github.com/acme/repo/OWNERS", "approvers: [mprahl]\nreviewers: [mprahl]\n")


def test_repo_can_override_every_org_field(tmp_path):
    setup_config(tmp_path)
    write(tmp_path / "configs/github.com/acme/repo/config.yaml", """schema_version: 1
enabled: true
review: {max_merge_risk: high}
limits: {max_changed_lines: 200}
profiles: []
trusted_authors: [repo-maintainer]
ci: {expected: [{name: build}], ignore: []}
rules:
  - {id: docs, type: changed_paths_only, paths: ["docs/**"]}
""")
    loaded = ConfigStore(tmp_path).load("github.com", "acme", "repo")
    assert loaded.policy.review.max_merge_risk.value == "high"
    assert loaded.policy.review.max_finding_severity.value == "medium"
    assert loaded.policy.limits.max_changed_lines == 200
    assert loaded.policy.profiles == []
    assert loaded.policy.ci.ignore == []
    assert [r.id for r in loaded.policy.rules] == ["docs"]
    assert loaded.trusted_authors == ["repo-maintainer"]
    assert ConfigStore(tmp_path).validate_all() == ["github.com/acme/repo"]


def test_repo_can_return_to_own_approvers_and_revision_tracks_owners(tmp_path):
    setup_config(tmp_path)
    write(tmp_path / "configs/github.com/acme/repo/config.yaml", "schema_version: 1\ntrusted_authors: null\n")
    store = ConfigStore(tmp_path)
    first = store.load("github.com", "acme", "repo")
    assert first.trusted_authors == ["mprahl"]
    write(tmp_path / "configs/github.com/acme/repo/OWNERS", "approvers: [new-maintainer]\nreviewers: [new-maintainer]\n")
    second = store.load("github.com", "acme", "repo")
    assert second.trusted_authors == ["new-maintainer"]
    assert second.revision != first.revision


def test_unallowlisted_model_rejected(tmp_path):
    setup_config(tmp_path)
    write(tmp_path / "configs/github.com/acme/repo/config.yaml", """schema_version: 1
enabled: true
ci: {expected: [{name: build}]}
rules:
  - id: docs
    type: changed_paths_only
    paths: ["docs/**"]
    llm:
      custom: {model: unknown, prompt: "Is this safe?"}
""")
    with pytest.raises(ConfigError, match="not allowlisted"):
        ConfigStore(tmp_path).validate_all()


def test_negative_llm_line_cap_rejected(tmp_path):
    setup_config(tmp_path)
    write(tmp_path / "configs/github.com/acme/repo/config.yaml", """schema_version: 1
enabled: true
ci: {expected: [{name: build}]}
rules:
  - id: docs
    type: changed_paths_only
    paths: ["docs/**"]
    llm:
      complexity: {model: gpt-6-sol, max: low, max_changed_lines: -1}
""")
    with pytest.raises(ConfigError, match="greater than or equal to 0"):
        ConfigStore(tmp_path).load("github.com", "acme", "repo")


def test_enabled_repo_requires_expected_checks(tmp_path):
    setup_config(tmp_path)
    write(tmp_path / "configs/github.com/acme/repo/config.yaml", "schema_version: 1\nenabled: true\n")
    with pytest.raises(ConfigError, match="requires expected CI"):
        ConfigStore(tmp_path).load("github.com", "acme", "repo")
