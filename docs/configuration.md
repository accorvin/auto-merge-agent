# Configuration

Policies live in this repository. The agent reads them from the protected checkout used by the service, not from the pull request under evaluation.

## File layout and inheritance

```text
config/global.yaml
profiles/<profile>.yaml
configs/github.com/<owner>/config.yaml              # Optional
configs/github.com/<owner>/<repo>/config.yaml        # Required to evaluate a repo
configs/github.com/<owner>/<repo>/OWNERS             # Required
configs/github.com/<owner>/<repo>/context.md        # Optional LLM guidance
OWNERS                                               # Required for config-repo validation
```

`config/global.yaml` supplies defaults and the global `allowed_models` list. Owner and repo `config.yaml` files override those defaults in that order. Nested maps merge; scalar values and lists replace inherited values. For example, `ci.ignore: []` clears inherited ignores, and `rules: []` clears inherited rules, including profile references. Repo config can override any owner-config setting. Profile references expand in place after inheritance is resolved, and rule names must be unique in the effective policy.

Only `github.com` is implemented. Run `uv run auto-merge-agent validate-config` after every policy change; the agent-repo CI runs the same validation.

Validation includes standalone owner policies and every profile, even if no repository references them. Duplicate explicit YAML keys are rejected; normal YAML anchor/merge overrides remain supported.

JSON Schemas are available for [global config](../schemas/global.schema.json), [org and repo config](../schemas/repository.schema.json), and [profiles](../schemas/profile.schema.json). They provide editor completion and validate each file's structure. `validate-config` also checks the effective inherited policy, plugin names, duplicate rule names, model allowlisting, trusted authors, and required CI checks. The schemas are generated from the runtime models with `uv run python scripts/generate_schemas.py`; CI checks that the files stay current.

## Minimal repo policy

```yaml
schema_version: 1
enabled: true
ci:
  expected:
    - name: tests
      source: github-actions
rules:
  - name: docs-only
    type: changed_paths_only
    paths: ["README.md", "docs/**"]
```

The repo directory also needs an `OWNERS` file, for example:

```yaml
reviewers: [maintainer]
approvers: [maintainer]
```

Replace the CI check name and owner login with values from the target repo. `enabled: false` makes every evaluation ineligible and prevents the service from queueing that repo through webhooks or reconciliation.

## Policy fields

| Field | Meaning |
| --- | --- |
| `schema_version` | Required; currently `1`. |
| `enabled` | Opt a repo into evaluation for automerge. Defaults to `false`. |
| `trusted_authors` | Optional list of GitHub logins that replaces the repo directory's `OWNERS.approvers`. Omit or set `null` to use those approvers. |
| `review.plugin` | Reviewer adapter. Currently `coderabbit`. |
| `review.max_merge_risk` | Highest allowed reviewer risk: `minimal`, `low`, `moderate`, `high`, or `critical`. Default `moderate`. |
| `review.max_finding_severity` | Highest allowed finding: `info`, `low`, `medium`, `high`, or `critical`. Default `medium`. |
| `ci.expected` | Exact check selectors that must appear. An enabled repo requires at least one. |
| `ci.ignore` | Check selectors excluded from CI evaluation. |
| `ci.min_successful_checks` | Minimum actual passing CI checks; at least `1`. |
| `limits.max_changed_lines` | Repo-wide cap on additions plus deletions. Set `null` to remove this cap; default `200`. |
| `limits.exclude_from_line_count` | Path patterns omitted from line counts, such as generated lockfiles. |
| `llm_context_files` | Optional list of relative paths to LLM guidance files in the target repository. The list replaces inherited values. |
| `rules` | Eligibility rules or `profile` references; any expanded rule may qualify after all mandatory gates pass. Enabled repos need at least one effective rule. |
| `merge.method` | `merge`, `squash`, or `rebase`; default `squash`. |
| `human_review.comment` | Whether to post and update the human-review explanation comment. |
| `human_review.mention` | GitHub users or teams to mention in that comment. |

Trusted authors use direct GitHub logins. OWNERS aliases and teams are not expanded for author trust; use `trusted_authors` to name direct accounts or bots such as `dependabot[bot]`. An enabled repo needs at least one trusted author. The root `OWNERS` and per-repo `OWNERS` files let Prow review changes to this config repo when Prow is configured; their presence alone does not enforce GitHub branch rules.

## CI and review evidence

A selector has a `name` and optional `source`. For GitHub Actions check runs, `source` is the App slug, commonly `github-actions`. For commit statuses, it is the status creator's login. Prefer specifying `source` for expected checks. The agent reads check runs and commit statuses on the PR head and GitHub's test merge commit. Every observed, non-ignored check must conclude `success`; `neutral` and `skipped` do not count as success. Missing expected checks and checks still running make the decision wait. The bot's own `automerge/policy` check is excluded automatically.

An expected check cannot also be ignored. Validation rejects that contradiction before the service starts.

CodeRabbit may report a commit status as well as a review. If present and not ignored, its status must pass like any other observed check; it can also count toward `ci.min_successful_checks`. Name a real CI job in `ci.expected` so the reviewer status cannot stand in for CI. The reviewer adapter separately requires CodeRabbit's completed assessment to cover the current PR head SHA, even if its status passed. A new push invalidates the old review. CodeRabbit `Major` findings map to `high` severity and `Minor` to `medium`; overall merge risk is a separate gate. Unrecognized review output requires human review or waits for a new assessment.

## Eligibility rules

The built-in `changed_paths_only` rule requires every changed file to match one of its `paths` patterns. For renames, both the old and new paths must match; missing old-path metadata fails the path rule. Both paths must also match `limits.exclude_from_line_count` for a rename to be excluded. These path checks also apply to scoped LLM rules. Optional `statuses` restricts file operations such as `modified` or `added`. Patterns use shell-style matching; `docs/**` matches files under `docs/`. The included `go-dependencies` profile allows modified `go.mod` and `go.sum` paths. It checks file paths and statuses, not dependency-version semantics; the mandatory CI and reviewer gates still apply.

Each rule has one criterion: `changed_paths_only`, `llm_complexity`, or `llm_custom`. Give every rule and `all` group a unique `name`; it appears in decisions and checklists. Entries in `rules` are alternatives; any one may qualify a PR. To reuse a policy, add `- profile: go-dependencies` as an entry. The agent replaces that entry with the rules in `profiles/go-dependencies.yaml`; each expanded rule is an alternative. Profile files contain rules or `all` groups and cannot reference other profiles. Put multiple separate rules in an `all` group when every criterion must pass. This makes the required relationship explicit. A passing static alternative skips LLM calls. For example:

```yaml
rules:
  - profile: go-dependencies
  - name: small-maintenance
    all:
      - name: maintenance-paths
        type: changed_paths_only
        paths: ["*.go", "docs/**"]
      - name: maintenance-complexity
        type: llm_complexity
        model: gpt-6-sol
        max: low
        max_changed_lines: 100
      - name: maintenance-safety
        type: llm_custom
        model: gpt-6-sol
        max_changed_lines: 100
        prompt: >-
          Require human review for public API or interface-contract changes.
```

LLM rules may use optional `paths` and `statuses` to limit which PRs they match. With no `paths`, they consider every PR that passes mandatory gates. Each LLM rule's `max_changed_lines` is independent. The agent applies `limits.exclude_from_line_count` to these caps and checks them before any model call. Exceeding a cap sends the PR to human review unless another top-level rule qualifies it. Complexity is `low`, `medium`, or `high`; the custom step must return a structured `safe_to_merge` boolean and reason. The custom prompt can add a requirement but cannot override mandatory gates. Unavailable full-text patches or an LLM input above the built-in size limit also prevent automerge. Every rule name, including names inside `all`, must be unique.

Before an LLM call, patch additions and deletions must match GitHub's file totals. If a patch is missing or truncated, the agent retrieves a full comparison pinned to the evaluated base and head SHAs and verifies those totals again. Recovered diff metadata, including file-mode changes, is included in the model input. Retrieval failures make the decision wait; binary or still-incomplete diffs require human review. Static-only eligibility does not trigger this extra request.

For LLM rules, an optional `context.md` beside the repo's policy `config.yaml` is included automatically. The agent also reads the target repository's root `AGENTS.md`; if absent, it reads root `CLAUDE.md`. To include other files, set a list of paths in the policy:

```yaml
llm_context_files:
  - docs/merge-policy.md
  - .github/maintainer-guidance.md
```

Target-repository files are read at the PR's base commit. This keeps a PR from changing the guidance used to evaluate itself. Explicitly listed files must exist at that commit; a missing file requires human review, while an API read failure makes the decision wait. Missing `AGENTS.md` and `CLAUDE.md` are allowed. Each context file must be UTF-8 and at most 20 KB; the combined LLM input limit is 100,000 characters. Context guides both complexity and custom judgments but cannot override mandatory gates. A change to the policy checkout's `context.md` changes the config revision.

Model IDs are listed directly in `config/global.yaml:allowed_models`. A repo may select only an allowed ID. LangChain infers providers for recognized model names; use the same `provider:model-id` string in both places when provider inference is ambiguous. OpenAI models require `OPENAI_API_KEY` at runtime. Do not store API keys in YAML.

## Decision states

| State | Example cause | Active-mode GitHub output |
| --- | --- | --- |
| `eligible` | All mandatory gates and one rule pass. | Successful policy check, then a merge checklist and SHA-guarded merge attempt. |
| `waiting` | Expected CI or current-head review is missing; model call is unavailable. | In-progress policy check. |
| `human_review` | Rule mismatch, failed CI, risk above threshold, or size cap exceeded. | Action-required policy check and optional tagged explanation. |

The agent re-fetches the PR and reruns the policy before sending a merge request. The effective policy, approved model list, and resolved trusted authors contribute to the decision revision, so a policy change also invalidates an in-flight decision.
