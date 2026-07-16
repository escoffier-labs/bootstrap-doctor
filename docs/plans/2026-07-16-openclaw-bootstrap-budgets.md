# OpenClaw bootstrap budget sync plan

Goal: align bootstrap-doctor with OpenClaw's 20,000-character per-file and 60,000-character total defaults, then remove audit paths that can report clean after skipping unsafe input.

Architecture: budget constants and configuration stay in `budgets.py` and `paths.py`. `status.py` owns file and aggregate measurement. `cli.py` uses those measurements to preflight audit and passes hard-limit file paths to `heuristics.py` for forced section review.

## File map

- `src/bootstrap_doctor/budgets.py`: OpenClaw-compatible standalone defaults.
- `src/bootstrap_doctor/paths.py`: `total_limit`, optional bootstrap filenames, and config layering.
- `src/bootstrap_doctor/status.py`: aggregate budget measurement and exit codes.
- `src/bootstrap_doctor/heuristics.py`: forced shortlist for hard-limit files.
- `src/bootstrap_doctor/cli.py`: audit integrity preflight and CLI flags.
- `tests/test_budgets_fallback.py`, `tests/test_paths.py`, `tests/test_status.py`, `tests/test_heuristics.py`, `tests/test_cli.py`: regressions.
- `README.md`, `docs/bootstrap-doctor-design.md`, `CHANGELOG.md`, `AGENTS.md`, `ROADMAP.md`: current commands, limits, loader set, and completed work.

### Task 1: budgets and status

- [ ] Add failing tests for 17,000/20,000/60,000 defaults, optional `BOOTSTRAP.md` and `MEMORY.md`, total soft pressure, and total hard pressure.
- [ ] Run `brigade work verify run --target . --command "python3 -m pytest tests/test_budgets_fallback.py tests/test_paths.py tests/test_status.py -q" --capture brigade-work`; expect failures for the missing constants and fields.
- [ ] Implement the constants, config field, optional-file handling, aggregate status output, and exit behavior.
- [ ] Run the same command; expect exit 0.
- [ ] Commit with `fix: align status with OpenClaw bootstrap budgets`.

### Task 2: audit integrity

- [ ] Add failing tests for missing and unreadable required files, hard-limit forced shortlisting, and a hard-limit preamble-only file.
- [ ] Run `brigade work verify run --target . --command "python3 -m pytest tests/test_cli.py tests/test_heuristics.py -q" --capture brigade-work`; expect the new tests to fail because audit currently skips these inputs.
- [ ] Implement status-backed audit preflight and `hard_limit_files` shortlisting.
- [ ] Run the same command; expect exit 0.
- [ ] Commit with `fix: fail audit when unsafe inputs are skipped`.

### Task 3: docs and full verification

- [ ] Update public docs and examples, remove the invalid `trim --dry-run` command, and record the 2026-06-01 OpenClaw limit change.
- [ ] Scan changed prose for private paths, internal hostnames, banned phrases, and em dashes.
- [ ] Run `./scripts/verify`; expect Brigade receipt status `completed` and pytest exit 0.
- [ ] Run `bootstrap-doctor status --json` from the built source against a fixture workspace that represents the live sizes; expect no per-file or total failure under the new defaults.
- [ ] Commit with `docs: update OpenClaw bootstrap budget contract`.
