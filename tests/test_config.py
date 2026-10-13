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
rules: [{profile: go-deps}]
trusted_authors: [org-maintainer]
""")
    write(tmp_path / "profiles/go-deps.yaml", """schema_version: 1
rules:
  - {name: go, type: changed_paths_only, paths: [go.mod, go.sum]}
""")
    write(tmp_path / "configs/github.com/acme/repo/OWNERS", "approvers: [mprahl]\nreviewers: [mprahl]\n")


def test_repo_can_override_every_org_field(tmp_path):
    setup_config(tmp_path)
    write(tmp_path / "configs/github.com/acme/repo/config.yaml", """schema_version: 1
enabled: true
review: {max_merge_risk: high}
limits: {max_changed_lines: 200}
trusted_authors: [repo-maintainer]
ci: {expected: [{name: build}], ignore: []}
rules:
  - {name: docs, type: changed_paths_only, paths: ["docs/**"]}
""")
    loaded = ConfigStore(tmp_path).load("github.com", "acme", "repo")
    assert loaded.policy.review.max_merge_risk.value == "high"
    assert loaded.policy.review.max_finding_severity.value == "medium"
    assert loaded.policy.limits.max_changed_lines == 200
    assert loaded.policy.ci.ignore == []
    assert [r.name for r in loaded.policy.rules] == ["docs"]
    assert loaded.trusted_authors == ["repo-maintainer"]
    assert ConfigStore(tmp_path).validate_all() == ["github.com/acme/repo"]


def test_profile_reference_expands_in_rule_order(tmp_path):
    setup_config(tmp_path)
    write(tmp_path / "profiles/go-deps.yaml", """schema_version: 1
rules:
  - {name: go-mod, type: changed_paths_only, paths: [go.mod]}
  - {name: go-sum, type: changed_paths_only, paths: [go.sum]}
""")
    write(tmp_path / "configs/github.com/acme/repo/config.yaml", """schema_version: 1
rules:
  - {name: before, type: changed_paths_only, paths: [README.md]}
  - {profile: go-deps}
  - {name: after, type: changed_paths_only, paths: ["docs/**"]}
""")
    loaded = ConfigStore(tmp_path).load("github.com", "acme", "repo")
    assert [rule.name for rule in loaded.policy.rules] == ["before", "go-mod", "go-sum", "after"]


def test_profile_reference_rejects_invalid_names_and_nested_profiles(tmp_path):
    setup_config(tmp_path)
    path = tmp_path / "configs/github.com/acme/repo/config.yaml"
    write(path, "schema_version: 1\nrules: [{profile: ../secrets}]\n")
    with pytest.raises(ConfigError, match="string_pattern_mismatch"):
        ConfigStore(tmp_path).load("github.com", "acme", "repo")
    write(path, "schema_version: 1\nrules: [{profile: go-deps}]\n")
    write(tmp_path / "profiles/go-deps.yaml", "schema_version: 1\nrules: [{profile: other}]\n")
    with pytest.raises(ConfigError, match="Invalid profile"):
        ConfigStore(tmp_path).load("github.com", "acme", "repo")


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
  - name: docs
    type: llm_custom
    paths: ["docs/**"]
    model: unknown
    prompt: "Is this safe?"
""")
    with pytest.raises(ConfigError, match="not allowlisted"):
        ConfigStore(tmp_path).validate_all()


def test_negative_llm_line_cap_rejected(tmp_path):
    setup_config(tmp_path)
    write(tmp_path / "configs/github.com/acme/repo/config.yaml", """schema_version: 1
enabled: true
ci: {expected: [{name: build}]}
rules:
  - name: docs
    type: llm_complexity
    paths: ["docs/**"]
    model: gpt-6-sol
    max: low
    max_changed_lines: -1
""")
    with pytest.raises(ConfigError, match="greater than or equal to 0"):
        ConfigStore(tmp_path).load("github.com", "acme", "repo")


def test_rule_cannot_mix_criteria_and_group_names_must_be_unique(tmp_path):
    setup_config(tmp_path)
    path = tmp_path / "configs/github.com/acme/repo/config.yaml"
    write(path, """schema_version: 1
rules:
  - name: mixed
    type: llm_complexity
    model: gpt-6-sol
    max: low
    prompt: "Also check the API"
""")
    with pytest.raises(ConfigError, match="no other criterion"):
        ConfigStore(tmp_path).load("github.com", "acme", "repo")
    write(path, """schema_version: 1
rules:
  - name: grouped
    all:
      - {name: same, type: changed_paths_only, paths: [go.mod]}
      - {name: same, type: llm_custom, model: gpt-6-sol, prompt: "Safe?"}
""")
    with pytest.raises(ConfigError, match="Duplicate rule name"):
        ConfigStore(tmp_path).load("github.com", "acme", "repo")


def test_rule_name_is_required(tmp_path):
    setup_config(tmp_path)
    write(tmp_path / "configs/github.com/acme/repo/config.yaml", """schema_version: 1
rules:
  - {id: legacy, type: changed_paths_only, paths: [go.mod]}
""")
    with pytest.raises(ConfigError, match="name"):
        ConfigStore(tmp_path).load("github.com", "acme", "repo")


def test_enabled_repo_requires_expected_checks(tmp_path):
    setup_config(tmp_path)
    write(tmp_path / "configs/github.com/acme/repo/config.yaml", "schema_version: 1\nenabled: true\n")
    with pytest.raises(ConfigError, match="requires expected CI"):
        ConfigStore(tmp_path).load("github.com", "acme", "repo")


def test_enabled_repo_requires_rules_and_cannot_ignore_expected_check(tmp_path):
    setup_config(tmp_path)
    path = tmp_path / "configs/github.com/acme/repo/config.yaml"
    write(path, "schema_version: 1\nenabled: true\nrules: []\nci: {expected: [{name: build}]}\n")
    with pytest.raises(ConfigError, match="requires at least one eligibility rule"):
        ConfigStore(tmp_path).load("github.com", "acme", "repo")
    write(path, """schema_version: 1
enabled: true
ci: {expected: [{name: build, source: github-actions}], ignore: [{name: build}]}
""")
    with pytest.raises(ConfigError, match="also ignored"):
        ConfigStore(tmp_path).load("github.com", "acme", "repo")


def test_context_file_changes_revision_and_explicit_paths_are_inherited(tmp_path):
    setup_config(tmp_path)
    write(tmp_path / "configs/github.com/acme/config.yaml", "schema_version: 1\nllm_context_files: [docs/merge-policy.md]\n")
    write(tmp_path / "configs/github.com/acme/repo/config.yaml", "schema_version: 1\n")
    store = ConfigStore(tmp_path)
    first = store.load("github.com", "acme", "repo")
    assert first.policy.llm_context_files == ["docs/merge-policy.md"]
    context = tmp_path / "configs/github.com/acme/repo/context.md"
    write(context, "Follow the release checklist.")
    second = store.load("github.com", "acme", "repo")
    assert second.llm_context == "Follow the release checklist."
    assert second.revision != first.revision


@pytest.mark.parametrize("path", ["../secrets", "/absolute", "a//b", "a/./b", "a\\b"])
def test_context_file_paths_must_be_relative(tmp_path, path):
    setup_config(tmp_path)
    write(tmp_path / "configs/github.com/acme/repo/config.yaml", f"schema_version: 1\nllm_context_files: [{path!r}]\n")
    with pytest.raises(ConfigError, match="Invalid llm_context_files path"):
        ConfigStore(tmp_path).load("github.com", "acme", "repo")
