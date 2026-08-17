# bootstrap-doctor - Design Doc

## Context

OpenClaw loads a defined set of bootstrap files from each agent workspace. Commit `6deded6698e16f3cd7e8c65f94d22660df248aa5` raised its default per-file maximum from 12,000 to 20,000 characters on 2026-06-01. Current releases also apply a 60,000-character total budget, so several individually healthy files can still create aggregate pressure.

Named workspaces can carry different bootstrap sets. Each workspace must be measured independently because per-file and total truncation are applied to that workspace's injected prefix.

Today the operator manages this by hand: noticing a file is too big, picking a section, copying it into `memory/cards/`, leaving a link behind. That doesn't scale and gets skipped.

bootstrap-doctor automates the audit-and-relocate loop, mirroring the design of the existing [memory-doctor](https://github.com/solomonneas/memory-doctor) repo (which handles MEMORY.md compaction). Goal: keep every tracked bootstrap file comfortably under threshold without losing information. Offloaded sections live in `memory/cards/` and are referenced by one-line breadcrumbs in the originals.

## Recommended approach

A Python CLI (pipx-installable, mirrors memory-doctor's project layout and command shape) with three subcommands. Dry-run by default; `--apply` required to persist any change.

### Subcommands

- **`bootstrap-doctor status`** - read-only. Reports each tracked file's char count, line count, and distance from soft/hard thresholds. No LLM calls.
- **`bootstrap-doctor lint`** - read-only and deterministic. Compares configured OpenClaw agents, workspace setup state, and bootstrap content to report lifecycle and context drift. No LLM calls and no repairs.
- **`bootstrap-doctor audit`** - read-only. Runs heuristic shortlist then LLM judge, then prints per-section verdicts (`keep` / `move` / `unsure`) with reasons and topic. Verdicts are cached by content hash so re-runs are cheap.
- **`bootstrap-doctor trim`** - applies the audit plan. Dry-run by default; `--apply` performs atomic writes. Always shows a git-style diff preview.

### Lifecycle lint contract

`bootstrap-doctor lint` reads `openclaw.json`, resolves every configured agent workspace, and inspects only the primary workspace, configured named workspaces, sibling `workspace-*` directories, and immediate child workspaces. It ignores backup, documentation, worktree, dependency, cache, and Git paths. The command never shells out to OpenClaw and never writes files.

Stable finding IDs:

- `bootstrap-after-setup` - error when `BOOTSTRAP.md` remains after `setupCompletedAt`.
- `orphan-workspace` - warning when an unconfigured workspace retains `BOOTSTRAP.md`.
- `configured-placeholder` - warning when a configured agent retains stock or blank `IDENTITY.md` or `USER.md` fields.
- `memory-contradicts-fresh` - error when `BOOTSTRAP.md` claims first-run state but the workspace already contains substantive memory.
- `inactive-context-content` - warning when a substantive recognized bootstrap file is present but excluded from the configured tracked-file set.
- `dangling-agent-reference` - error when `subagents.allowAgents` names an agent absent from `agents.list`; `*` remains valid.
- `duplicate-context` - warning for exact normalized duplicate bootstrap content of at least 200 characters across configured workspaces.

JSON output is a stable object with `ok`, `findings`, and severity counts. Each finding includes `check_id`, `severity`, `message`, and `path`, plus `agent_id` when one agent owns the finding. Exit status is 0 with no findings, 1 for warning-only findings, and 2 for any error or unreadable/invalid required input. Human output carries the same information in deterministic order.

### Tracked files (default, overridable via config)

`AGENTS.md`, `SOUL.md`, `TOOLS.md`, `IDENTITY.md`, `USER.md`, `HEARTBEAT.md`, `BOOTSTRAP.md`, and `MEMORY.md`. `SOUL.md`, `IDENTITY.md`, `USER.md`, `HEARTBEAT.md`, `BOOTSTRAP.md`, and `MEMORY.md` are optional when absent. `SAFETY_RULES.md` and `DREAMS.md` are not recognized OpenClaw bootstrap files. Each configured named workspace is scanned as its own scope.

### Thresholds

- `soft_limit` = 17,000 chars - warn in `status`
- `hard_limit` = 20,000 chars - force every reviewable H2/H3 section into `audit`
- `total_limit` = 60,000 chars - report aggregate workspace pressure

All three are configurable, and the per-file and total hard limits are independent. Total soft pressure begins at 85 percent of `total_limit`. Counts use the representation OpenClaw injects: ECMAScript trailing whitespace removed, measured as JavaScript UTF-16 code units. For standard sessions, aggregate pressure also includes OpenClaw's UTF-16 `[MISSING] Expected at: <absolute path>` marker for absent recognized defaults other than `MEMORY.md`. Content exactly at a hard boundary is retained and remains a soft warning; hard classification begins above the boundary.

### Decision pipeline

1. **Section parser** (`parsing.py`) - splits each tracked `.md` by H2/H3 headings. Emits `(file, heading_path, body, char_count, last_touched_git_mtime)`.
2. **Heuristic shortlist** (`heuristics.py`) - flags sections meeting any of:
   - Body > 400 chars
   - Contains a code block > 10 lines
   - `git log -1 --format=%cs` shows no touch in > 60 days
   - Body appears verbatim (or > 80% similar) across multiple tracked files (cross-file duplicate detection)
3. **LLM judge** (`judge.py`) - for each shortlisted section, POSTs to an OpenAI-compatible chat-completions endpoint (default `localhost:11434`, Ollama; note the original spec aimed at the OpenClaw gateway on 18789 but that port serves the Control UI, not an OpenAI-compat API) with a structured prompt asking whether the section is **must-stay-loaded** (active rules, identity, currently-relevant state) or **reference-detail** (historical, exemplar, one-off setup). Returns one of `keep` / `move` / `unsure`. Token budget capped per run; verdicts cached by SHA256 of section body.
4. **Trim plan** (`trim.py`) - for each `move` verdict:
   - Write card to `~/.openclaw/workspace/memory/cards/<slug>.md` with the existing frontmatter convention (`topic` / `category` / `tags` / `created` / `updated`). `created` is today (or git-blame first commit if available); `updated` is today.
   - Replace the original section with a one-line breadcrumb in the same H2/H3 location: `- See [<topic>](memory/cards/<slug>.md) - <one-line hook>`
   - `keep` is a no-op
   - `unsure` surfaces in audit output and is never auto-applied

### Safety

- All writes dry-run by default; `--apply` required.
- Atomic writes (write temp, rename) prevent torn files.
- Path-traversal guard: card slug must resolve inside `memory/cards/`.
- Read-only verbs allow a missing `cards_dir`; mutating trim requires it to resolve through the normal apply path.
- Refuses to run if `git status` in workspace is dirty (so any change is revertable), overridable with `--force`. If `cards_dir` is in a separate git repo, that repo is checked too.
- Card writes happen before bootstrap rewrites. A card-write failure aborts before breadcrumbs are inserted.
- LLM verdict cache stored at `~/.cache/bootstrap-doctor/verdicts.json`; bypass it for one run with `--no-cache`.

### Config

`~/.config/bootstrap-doctor/config.toml`:

```toml
workspace_dir = "~/.openclaw/workspace"
cards_dir = "~/.openclaw/workspace/memory/cards"
gateway_url = "http://localhost:11434"
gateway_model = "deepseek-v4-pro:cloud"
soft_limit = 17000
hard_limit = 20000
total_limit = 60000
tracked_files = ["AGENTS.md", "SOUL.md", "TOOLS.md", "IDENTITY.md",
                 "USER.md", "HEARTBEAT.md", "BOOTSTRAP.md", "MEMORY.md"]
named_workspaces = ["workspace-claude", "workspace-main", "workspace-researcher"]
heuristics.min_section_chars = 400
heuristics.stale_days = 60
```

Layering: defaults, then config file, then env vars, then CLI flags. Matches memory-doctor.

### Project layout (mirrors memory-doctor)

```
bootstrap-doctor/
├── pyproject.toml
├── README.md
├── src/bootstrap_doctor/
│   ├── __init__.py
│   ├── cli.py            # argparse entrypoint, subcommand dispatch
│   ├── paths.py          # config resolution + defaults
│   ├── status.py         # size/limit reporting
│   ├── parsing.py        # section splitter (heading-based)
│   ├── heuristics.py     # shortlist rules (size, age, duplicates)
│   ├── judge.py          # LLM gateway client (OpenAI-compatible) + verdict cache
│   ├── trim.py           # apply plan: write card, breadcrumb-in-place
│   └── safety.py         # atomic writes, path validation, git-clean check
└── tests/
    ├── fixtures/         # sample bootstrap files
    ├── test_parsing.py
    ├── test_heuristics.py
    ├── test_trim.py
    └── test_safety.py
```

## Reference material

This is a new standalone repo. Reference points:

- [memory-doctor](https://github.com/solomonneas/memory-doctor) - copy the project structure, config layering, dry-run/atomic-write idioms.
- `~/.openclaw/workspace/memory/cards/` - read existing cards to lock in the frontmatter shape before implementing `trim.py`.
- `~/.openclaw/workspace/AGENTS.md` and `TOOLS.md` - primary fixtures for testing (copy to `tests/fixtures/` for unit tests, never use live files in tests).

## Verification

1. **`bootstrap-doctor status`** matches OpenClaw's trimmed UTF-16 character counts and reports each workspace's aggregate pressure.
2. **`bootstrap-doctor audit`** returns 2 for missing or unreadable required files and for a hard-limit file without a reviewable H2/H3 section.
3. **`bootstrap-doctor trim --apply`** on a *copied* workspace (e.g. `/tmp/bootstrap-doctor-e2e/`):
   - At least one section moved to a new card under `memory/cards/`.
   - Breadcrumb present in the original file at the same heading location.
   - Char count of the trimmed file drops below `soft_limit`.
   - Trimmed file parses as valid markdown.
   - `git diff` is small and reviewable (no incidental whitespace churn).
4. **Idempotency** - re-running `audit` on the trimmed output produces zero new `move` verdicts.
5. **Safety** - running with dirty workspace `git status` aborts with a clear message; `--force` overrides.
6. **Multi-workspace** - running with `named_workspaces` populated processes each workspace independently; cards generated from `workspace-claude` don't end up referenced in `workspace-main` breadcrumbs.
7. **Quality gates** - `pytest -q`, `python3 -m ruff check .`, `python3 -m mypy src/bootstrap_doctor`, `python3 -m build`, and `pip-audit . --skip-editable` pass before release.

## Out of scope for v1

- Harness-agnostic file path discovery (hardcoded to OpenClaw paths; pluggable later if needed).
- Pluggable LLM backends (Ollama, OpenAI, Anthropic). Gateway-only for v1.
- TUI review mode.
- Automatic restoration of breadcrumbed sections (one-way trim only).
- Cron wrapper script. Operator can `crontab -e` manually; no installer.
- Generated hook injection and harness-specific bootstrap selection. OpenClaw's native doctor is authoritative for those runtime details.
