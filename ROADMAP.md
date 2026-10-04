# Roadmap

Working queue for bootstrap-doctor. Items graduate from the friction log and
session handoffs; done items move to `CHANGELOG.md`.

The verification loop is implemented: `scripts/verify` emits Brigade receipts and captures outcomes when Brigade is available, and the pre-push hook requires a passing receipt for every pushed tip. Documentation checks are implemented in `scripts/check_docs.py` and `.github/workflows/docs.yml`: links and supported command syntax are checked without executing snippets.

## Now

- [ ] Gateway capability probing: check the configured endpoint answers `/v1/models` and a chat-completions smoke request before `audit`/`trim` start, with a clear remediation message instead of a mid-run 404.

## Next

- [ ] Bounded retry with backoff in `judge.py` for malformed or non-string model responses before surfacing a hard failure (observed at roughly 14 percent on local gateways).
- [ ] Failure-matrix integration suite: card slug collisions, malformed verdict cache, dirty workspace on `--apply`, and gateway failures exercised together rather than per-module.

## Later

- [ ] Freeze a README information architecture so future doc edits are incremental instead of rewrites.
