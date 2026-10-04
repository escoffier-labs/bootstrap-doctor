# Contributing

bootstrap-doctor audits and trims the OpenClaw bootstrap files that load into
every session prefix. The bar is "keeps the prefix short without ever losing
content."

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e ".[dev]"
./scripts/verify       # pytest, with Brigade receipt/capture when available
python3 -m ruff check .
python3 -m mypy src/bootstrap_doctor
python3 scripts/check_docs.py
```

`./scripts/verify` runs only pytest, with Brigade verification and outcome capture when Brigade is on PATH. Ruff and mypy use the configuration in `pyproject.toml` and run separately after the venv dev install.

`scripts/check_docs.py` checks `README.md` and Markdown under `docs/` for local links and anchors. Its command checks cover only supported `bootstrap-doctor` shell snippets and prompt transcripts, parsed against the CLI parser without execution. It does not validate other programs, gateway reachability, response correctness, printed output, or trim safety. Add `--remote` for bounded HTTP link checks, as `.github/workflows/docs.yml` does. Remote fragments are not checked.

## What lands easily

- New read-only checks (extending `status`) with tests
- Bug fixes with a test that fails before and passes after
- Documentation

## What needs a conversation first

Open an issue before a PR for:

- Changes to how `trim` relocates content (the breadcrumb/card mechanism) -
  data safety comes first, which is why it is dry-run by default
- Changes to the size limits/heuristics or the `audit` verdict prompt

## Rules

- `trim` stays **dry-run by default**; `--apply` is required to write.
- **No real bootstrap content** in tests or fixtures; use small synthetic files.
- Conventional commits, no AI co-authorship trailers.
