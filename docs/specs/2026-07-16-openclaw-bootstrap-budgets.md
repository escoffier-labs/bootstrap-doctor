# OpenClaw bootstrap budget sync

## Goal

Make bootstrap-doctor evaluate the bootstrap files and limits used by current OpenClaw releases while keeping the CLI useful without OpenClaw installed.

## Basis

OpenClaw commit `6deded6698e16f3cd7e8c65f94d22660df248aa5`, authored on 2026-06-01, changed the default per-file limit from 12,000 to 20,000 characters. Current OpenClaw also enforces a 60,000-character total budget and recognizes `BOOTSTRAP.md`, but does not recognize `SAFETY_RULES.md` as a bootstrap file.

## Design

- Use standalone defaults that match current OpenClaw: 17,000 soft, 20,000 hard, and 60,000 total characters. The soft limit matches OpenClaw's 85 percent near-limit threshold.
- Keep configuration overrides. Add `total_limit` to TOML, environment, CLI, status JSON, and validation.
- Change the default tracked set to OpenClaw's recognized files: `AGENTS.md`, `SOUL.md`, `TOOLS.md`, `IDENTITY.md`, `USER.md`, `HEARTBEAT.md`, `BOOTSTRAP.md`, and optional `MEMORY.md`.
- Treat missing `BOOTSTRAP.md` and `MEMORY.md` as optional. Other configured tracked files remain required.
- Report total raw characters and total-budget status. Return 1 near or exactly at the total limit and 2 above it, matching OpenClaw's truncation boundary.
- Audit the same measured inputs as status. Missing or unreadable required files return 2. A hard-limit file forces all of its H2/H3 sections into the judge shortlist. A hard-limit file with no movable H2/H3 section returns 2 instead of a false clean result.
- Keep the section-level judge and trim workflow. OpenClaw's native lint remains the authority for runtime-specific injection behavior in live operations.

## Failure handling

Invalid non-positive limits and per-file soft limits at or above the hard limit fail configuration before file reads. Per-file and total hard limits are otherwise independent, matching OpenClaw. Audit never converts skipped input into exit 0. Total-budget pressure is report-only because section moves remain per-file operations.

## Verification

Regression tests cover defaults, optional files, total pressure, forced hard-limit shortlisting, missing and unreadable audit inputs, and the oversized preamble-only case. The full repository verification runs through Brigade.
