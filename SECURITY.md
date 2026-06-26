# Security Policy

## Supported versions

bootstrap-doctor is pre-1.0; fixes land on the latest version. Please upgrade before reporting.

## Reporting a vulnerability

Report privately, not in a public issue:

- GitHub: **Security → Report a vulnerability** (private advisory) on this repo, or
- contact the maintainer privately via [@solomonneas](https://github.com/solomonneas)

bootstrap-doctor reads and (with `trim --apply`) rewrites local bootstrap files.
The issues that matter most are **data loss or corruption** (a trim that drops or
mangles content instead of relocating it with a breadcrumb) and any path-handling
bug that writes outside the workspace or cards directory. Include a synthetic
workspace and the exact command.

## Scope

In scope: the CLI, the size/limit checks, the trim planner and `--apply` writes,
and the breadcrumb/card relocation.

Out of scope: the upstream OpenClaw workspace and the gateway model used by `audit`.

## Notes

`status` and `audit` are read-only; `trim` is dry-run unless you pass `--apply`.
Note that `audit` sends file content to the **configured gateway model** for its
keep/move verdicts, so point it at a model you trust with that content.
