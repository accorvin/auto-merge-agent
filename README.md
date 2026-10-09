# Auto Merge Agent

**Automatic merges for low-risk PRs, controlled by each repository's policy.**

This Python GitHub App combines static rules, CI, a current-head CodeRabbit review, and optional LLM judgments. It submits the evaluated head SHA with each merge request, so a later push prevents that merge.

> **Start in shadow mode.** The service evaluates PRs and logs decisions without posting checks, comments, or merges. Turn on writes after the decisions match your policy.

## How a PR qualifies

The agent checks every PR in this order:

1. **Mandatory gates:** The PR is open, ready, mergeable, and from a trusted author. Its file list is complete, its size is within the repo limit, and no blocking label or human change request is present. Configured CI checks pass, at least one real CI check succeeds, and CodeRabbit has reviewed the latest head SHA.
2. **One eligibility rule:** Rules are alternatives. A static rule can allow a Go dependency bump without an LLM. Another rule can require both a complexity judgment and a custom safety judgment. If a static rule passes, the LLM is skipped.
3. **Final recheck:** Before merging, the agent reruns the policy and confirms the head and base have not changed. Missing evidence, an unrecognized review, or a model error prevents an automatic merge.

In active mode, the bot shows its decision in an `automerge/policy` check:

| Result | Check status | What happens |
| --- | --- | --- |
| Waiting for evidence | In progress | Reevaluate when CI or review arrives. |
| Eligible | Success | Post a checklist of passed gates and attempt the SHA-guarded merge. |
| Human review needed | Action required | Explain why; optionally mention configured maintainers. |

Keep `automerge/policy` informational so PRs needing human review can still use the repository's normal path.

## Try a read-only evaluation

You need Python 3.11+, [uv](https://docs.astral.sh/uv/), a repo policy in this checkout, and a GitHub token with read access. If you use the GitHub CLI:

```bash
uv sync --extra test
uv run auto-merge-agent validate-config
uv run pytest -q
GITHUB_TOKEN="$(gh auth token)" uv run auto-merge-agent evaluate OWNER REPO PR_NUMBER
```

The CLI prints the decision as JSON. `--execute` enables writes and can merge the PR; use the GitHub App service for normal operation.

## Configure a repository

Policies live in this agent's checkout:

```text
config/global.yaml                         # Defaults and allowed model IDs
configs/github.com/<owner>/config.yaml     # Optional owner defaults
configs/github.com/<owner>/<repo>/config.yaml
configs/github.com/<owner>/<repo>/OWNERS   # Default trusted PR authors
OWNERS                                     # Ownership of this agent repo
```

Repo settings override owner settings; nested maps merge and lists replace inherited lists. The service evaluates only repositories with an enabled, valid config.

### Example: dependency bumps and small fixes

**1. Allow a model** in `config/global.yaml`:

```yaml
schema_version: 1
allowed_models: [gpt-6-sol]
defaults: {}
```

**2. Configure the repository.** For `github.com/acme/widget`, put `approvers: [maintainer]` in `configs/github.com/acme/widget/OWNERS`, then create `configs/github.com/acme/widget/config.yaml`:

```yaml
schema_version: 1
enabled: true
ci:
  expected:
    - name: tests
      source: github-actions
rules:
  - id: go-dependencies
    type: changed_paths_only
    paths: [go.mod, go.sum]
    statuses: [modified]
  - id: small-maintenance
    type: changed_paths_only
    paths: ["*.go", go.mod, go.sum, README.md, "docs/**"]
    llm:
      complexity:
        model: gpt-6-sol
        max: low
        max_changed_lines: 100
      custom:
        model: gpt-6-sol
        prompt: >-
          Require human review for public API changes, new services, or
          unclear security effects. If uncertain, return false.
```

Replace `maintainer` and `tests` with the repository's GitHub login and CI check. Both rules require a trusted author, passing CI, and a current-head CodeRabbit review. If CodeRabbit also posts a status, that status must pass. A PR changing only `go.mod` and `go.sum` uses the static rule and skips the LLM, even though `small-maintenance` also matches. Other matching changes must pass both LLM criteria.

See [configuration](docs/configuration.md) for inheritance, every policy field, and decision semantics. The checked-in policy under `configs/` is another example.

JSON Schemas for [global](schemas/global.schema.json) and [org/repo](schemas/repository.schema.json) YAML provide editor completion. `validate-config` checks cross-file requirements such as model allowlisting and expected CI checks. Regenerate schemas after changing config models with `uv run python scripts/generate_schemas.py`.

## Run the GitHub App

1. Register a GitHub App and install it on selected repositories.
2. Configure its credentials and webhook secret, then run one service process.
3. Inspect decisions with `AUTOMERGE_ACTIVE=false` (the default). Set it to `true` when ready to let the App post checks, comment, and merge.

The service accepts webhooks at `/webhook`, exposes `/health`, and scans enabled repos at startup and every 15 minutes to recover work missed during downtime.

See [deployment](docs/deployment.md) for permissions, events, environment variables, local webhook forwarding, and rollout steps. See [architecture](docs/architecture.md) for the evaluation flow, extension points, and MLflow tracing.

## Project scope

The current implementation supports GitHub and CodeRabbit. GitLab and additional reviewer integrations are extension points. The service uses one worker and an in-memory queue with retries; pending events do not survive a restart. GitHub branch rules still decide which merges are permitted, so configure required CI and any App review bypass separately from this policy.
