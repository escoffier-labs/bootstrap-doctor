# Bootstrap lifecycle lint implementation plan

Goal: add a deterministic, read-only `bootstrap-doctor lint` command that catches stale workspace lifecycle and context state before Brigade surfaces it.

Architecture: `src/bootstrap_doctor/lint.py` owns config loading, workspace discovery, finding collection, rendering, and exit codes. `src/bootstrap_doctor/cli.py` only parses flags and delegates. Existing status, runtime, audit, and trim behavior remains unchanged. The implementation uses the standard library and existing `Config` values.

Agentic workers must execute each checkbox in order, keep the plan updated, and commit only after the specified green checks.

## File map

- Create `src/bootstrap_doctor/lint.py`: immutable finding type, workspace resolution, deterministic checks, JSON/text rendering, exit code.
- Create `tests/test_lint.py`: hermetic fixtures for all seven finding IDs, exclusions, rendering, and exit status.
- Modify `src/bootstrap_doctor/cli.py`: add `lint` parser flags and dispatch.
- Modify `tests/test_cli.py`: pin parser and dispatch behavior.
- Modify `docs/bootstrap-doctor-design.md`, `README.md`, and `CHANGELOG.md`: document the new read-only contract.

### Task 1: Lifecycle detector

**Files:**
- Create: `src/bootstrap_doctor/lint.py`
- Create: `tests/test_lint.py`

- [ ] Write failing tests that build a temporary OpenClaw home and assert exact findings for:

```python
assert finding_ids(report) == {
    "bootstrap-after-setup",
    "configured-placeholder",
    "memory-contradicts-fresh",
    "dangling-agent-reference",
}
```

- [ ] Write failing tests proving an unconfigured sibling or immediate-child workspace is `orphan-workspace`, while `.bootstrap-backups`, `docs`, `node_modules`, worktrees, and deeper fixture directories are ignored.
- [ ] Write failing tests proving substantive excluded bootstrap content produces `inactive-context-content`, exact normalized content of at least 200 characters across configured workspaces produces `duplicate-context`, wildcard `allowAgents = ["*"]` is valid, and findings sort by severity, ID, then path.
- [ ] Run RED: `brigade work verify run --target . --command "python3 -m pytest tests/test_lint.py -q" --capture taste`. Expect import or attribute failure because `bootstrap_doctor.lint` does not exist.
- [ ] Implement the minimum detector with `LintFinding`, `LintReport`, `load_openclaw_config`, `resolve_agent_workspaces`, `discover_workspace_candidates`, `collect_findings`, `render_json`, `render_text`, and `run`.
- [ ] Keep discovery bounded to the primary workspace, configured named workspaces, sibling `workspace-*` directories, and immediate child directories with workspace marker files. Never recurse through arbitrary workspace content.
- [ ] Run GREEN with the same Brigade command. Expect all `tests/test_lint.py` tests to pass.
- [ ] Commit: `git add src/bootstrap_doctor/lint.py tests/test_lint.py && git commit -m "feat: detect stale bootstrap lifecycle state"`

### Task 2: CLI and public contract

**Files:**
- Modify: `src/bootstrap_doctor/cli.py`
- Modify: `tests/test_cli.py`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `docs/bootstrap-doctor-design.md`

- [ ] Write failing CLI tests that assert `lint --json`, `--openclaw-home`, and `--openclaw-config` parse and dispatch to `lint.run`.
- [ ] Run RED: `brigade work verify run --target . --command "python3 -m pytest tests/test_cli.py -q" --capture taste`. Expect argparse to reject `lint`.
- [ ] Add the parser and `run_lint` delegate. Do not add repair flags.
- [ ] Document the seven stable finding IDs, JSON shape, exit codes, bounded discovery, and read-only behavior.
- [ ] Run GREEN: `brigade work verify run --target . --command "python3 -m pytest tests/test_cli.py tests/test_lint.py -q" --capture taste`.
- [ ] Run full verification: `./scripts/verify`. Expect status completed and pytest exit 0.
- [ ] Commit: `git add src/bootstrap_doctor/cli.py tests/test_cli.py README.md CHANGELOG.md docs/bootstrap-doctor-design.md docs/plans/2026-08-17-lifecycle-lint.md && git commit -m "feat: expose bootstrap lifecycle lint"`

Growth trigger: add repair behavior only after a separate approved design defines recoverable archive semantics and confirmation boundaries.
