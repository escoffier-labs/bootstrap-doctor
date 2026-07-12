<!-- Keep this short; delete sections that do not apply. See CONTRIBUTING.md. -->

## What and why

<!-- One or two sentences on the change and the problem it solves. -->

Closes #

## Type of change

- [ ] New read-only check (status)
- [ ] Bug fix
- [ ] Change to `trim` behavior, limits/heuristics, or the audit prompt (opened an issue first)
- [ ] Docs

## Checklist

- [ ] `scripts/verify` passes (full test suite; ruff, mypy, build, and pip-audit run in CI)
- [ ] Added or updated tests covering the change
- [ ] `trim` stays dry-run by default; `--apply` is still required to write
- [ ] No real bootstrap content in tests or fixtures (synthetic samples only)
- [ ] No PII, secrets, home paths, hostnames, or real private IPs in the diff (run content-guard if available)
- [ ] Updated the `Unreleased` section of `CHANGELOG.md` for any user-visible effect
- [ ] Conventional commit messages, no AI co-authorship trailers
