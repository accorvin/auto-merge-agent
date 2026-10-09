# Configuring a repository

1. Add `configs/github.com/<owner>/<repo>/config.yaml` and an `OWNERS` file beside it with `approvers` set to trusted GitHub logins.
2. Set `enabled: true`, name at least one expected CI check, and choose static or LLM rules. Reuse `profiles/` where possible. LLM models must appear in `config/global.yaml:allowed_models`.
3. Run `uv run auto-merge-agent validate-config`. Keep the service in shadow mode until its logged decisions match the intended policy.

See [the README](README.md) for examples and [configuration](docs/configuration.md) for the full schema and inheritance rules.
