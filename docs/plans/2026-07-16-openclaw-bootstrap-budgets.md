# OpenClaw bootstrap budget sync plan

Goal: align bootstrap-doctor with OpenClaw's 20,000-character per-file and 60,000-character total defaults, then remove audit paths that can report clean after skipping unsafe input.

Architecture: budget constants and configuration stay in `budgets.py` and `paths.py`. `status.py` owns file and aggregate measurement. `cli.py` uses those measurements to preflight audit and adds hard-limit files to the existing shortlist without changing heuristic policy.

## File map

- `src/bootstrap_doctor/budgets.py`: OpenClaw-compatible standalone defaults.
- `src/bootstrap_doctor/paths.py`: `total_limit`, optional bootstrap filenames, and config layering.
- `src/bootstrap_doctor/status.py`: aggregate budget measurement and exit codes.
- `src/bootstrap_doctor/cli.py`: audit integrity preflight and CLI flags.
- `tests/test_budgets_fallback.py`, `tests/test_paths.py`, `tests/test_status.py`, `tests/test_cli.py`: regressions.
- `README.md`, `docs/bootstrap-doctor-design.md`, `CHANGELOG.md`, `AGENTS.md`: current commands, limits, loader set, and completed work.

### Task 1: budgets and status

- [x] Add failing tests for 17,000/20,000/60,000 defaults, optional `BOOTSTRAP.md` and `MEMORY.md`, total soft pressure, and total hard pressure.
- [x] Capture RED through Brigade as `20260716-170142-work-verify-11c346`.
- [x] Implement the constants, config field, optional-file handling, aggregate status output, and exit behavior.
- [x] Capture targeted GREEN through Brigade as `20260716-170414-work-verify-99eae1`.

### Task 2: audit integrity

- [x] Add failing tests for missing and unreadable required files, hard-limit forced shortlisting, and a hard-limit preamble-only file.
- [x] Capture the combined four-file RED through Brigade.
- [x] Implement status-backed audit preflight and hard-limit shortlist augmentation in `cli.py`.
- [x] Pass the combined four-file targeted suite.

### Task 3: docs and full verification

- [x] Update public docs and examples, remove the invalid `trim --dry-run` command, and record the 2026-06-01 OpenClaw limit change.
- [x] Scan changed prose for private paths, internal hostnames, banned phrases, and em dashes.
- [x] Pass the dirty-tree full suite through Brigade as `20260716-171609-work-verify-9e2b05`.
- [x] Run the patched source against the live metadata-only workspace surface; receipt `20260716-170711-work-verify-d7e22f` exited 0.
- [x] Confirm OpenClaw's native bootstrap-size doctor still exits 0; receipt `20260716-170723-work-verify-087464`.
