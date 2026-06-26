# Contributing

bootstrap-doctor audits and trims the OpenClaw bootstrap files that load into
every session prefix. The bar is "keeps the prefix short without ever losing
content."

## Local setup

```bash
python3 -m pip install -e ".[dev]"
scripts/verify          # lint, type-check, and tests
```

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
