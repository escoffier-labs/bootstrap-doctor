"""Read-only size and limit reporting for OpenClaw bootstrap files."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from bootstrap_doctor.paths import Config, DEFAULT_OPTIONAL_TRACKED_FILES
from bootstrap_doctor.safety import UnsafeTargetError, ensure_within

PRIMARY_LABEL = "workspace"

SEV_OK = "ok"
SEV_SOFT = "soft"
SEV_HARD = "hard"
SEV_MISSING = "missing"
SEV_UNREADABLE = "unreadable"
SEV_OPTIONAL = "optional"

_FLAGS = {
    SEV_OK: "ok",
    SEV_SOFT: "SOFT",
    SEV_HARD: "HARD",
    SEV_MISSING: "MISSING",
    SEV_UNREADABLE: "UNREAD",
    SEV_OPTIONAL: "OPTIONAL",
}

_ECMASCRIPT_TRIM_END_CHARS = (
    "\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000\ufeff"
)


@dataclass(frozen=True)
class FileStatus:
    path: Path
    workspace_label: str
    name: str
    exists: bool
    bytes: int
    chars: int
    lines: int
    soft_remaining: int
    hard_remaining: int
    severity: str


@dataclass(frozen=True)
class WorkspaceTotal:
    path: Path
    workspace_label: str
    chars: int
    remaining: int
    severity: str
    complete: bool


def total_soft_limit(cfg: Config) -> int:
    """Return the 85 percent near-limit boundary used by OpenClaw."""
    return (cfg.total_limit * 85 + 99) // 100


def _count_lines(text: str) -> int:
    if not text:
        return 0
    n = text.count("\n")
    if not text.endswith("\n"):
        n += 1
    return n


def _openclaw_char_count(text: str) -> int:
    """Count JavaScript UTF-16 code units after OpenClaw's trimEnd step."""
    trimmed = text.rstrip(_ECMASCRIPT_TRIM_END_CHARS)
    return len(trimmed.encode("utf-16-le")) // 2


def _classify(chars: int, cfg: Config) -> str:
    if chars > cfg.hard_limit:
        return SEV_HARD
    if chars >= cfg.soft_limit:
        return SEV_SOFT
    return SEV_OK


def _classify_total(chars: int, cfg: Config) -> str:
    if chars > cfg.total_limit:
        return SEV_HARD
    if chars >= total_soft_limit(cfg):
        return SEV_SOFT
    return SEV_OK


def _measure_file(path: Path, label: str, name: str, cfg: Config) -> FileStatus:
    if not path.exists():
        severity = (
            SEV_OPTIONAL if name in DEFAULT_OPTIONAL_TRACKED_FILES else SEV_MISSING
        )
        return FileStatus(
            path=path,
            workspace_label=label,
            name=name,
            exists=False,
            bytes=0,
            chars=0,
            lines=0,
            soft_remaining=cfg.soft_limit,
            hard_remaining=cfg.hard_limit,
            severity=severity,
        )
    try:
        size_bytes = path.stat().st_size
    except OSError:
        size_bytes = 0
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return FileStatus(
            path=path,
            workspace_label=label,
            name=name,
            exists=True,
            bytes=size_bytes,
            chars=0,
            lines=0,
            soft_remaining=cfg.soft_limit,
            hard_remaining=cfg.hard_limit,
            severity=SEV_UNREADABLE,
        )
    chars = _openclaw_char_count(text)
    return FileStatus(
        path=path,
        workspace_label=label,
        name=name,
        exists=True,
        bytes=size_bytes,
        chars=chars,
        lines=_count_lines(text),
        soft_remaining=cfg.soft_limit - chars,
        hard_remaining=cfg.hard_limit - chars,
        severity=_classify(chars, cfg),
    )


def _workspace_scopes(cfg: Config) -> list[tuple[str, Path]]:
    scopes: list[tuple[str, Path]] = [(PRIMARY_LABEL, cfg.workspace_dir)]
    for name in cfg.named_workspaces:
        try:
            resolved = ensure_within(cfg.workspace_dir, cfg.workspace_dir / name)
        except UnsafeTargetError as exc:
            raise UnsafeTargetError(
                f"named workspace {name!r} resolves outside workspace_dir: {exc}"
            ) from exc
        scopes.append((name, resolved))
    return scopes


def collect(cfg: Config) -> list[FileStatus]:
    """Measure every configured bootstrap file in every workspace."""
    rows: list[FileStatus] = []
    for label, ws_dir in _workspace_scopes(cfg):
        for name in cfg.tracked_files:
            rows.append(_measure_file(ws_dir / name, label, name, cfg))
    return rows


def collect_totals(rows: list[FileStatus], cfg: Config) -> list[WorkspaceTotal]:
    """Aggregate injected character pressure independently per workspace."""
    by_label: dict[str, list[FileStatus]] = {}
    for row in rows:
        by_label.setdefault(row.workspace_label, []).append(row)

    totals: list[WorkspaceTotal] = []
    for label, workspace_rows in by_label.items():
        chars = sum(row.chars for row in workspace_rows if row.exists)
        complete = not any(
            row.severity in {SEV_MISSING, SEV_UNREADABLE} for row in workspace_rows
        )
        totals.append(
            WorkspaceTotal(
                path=workspace_rows[0].path.parent,
                workspace_label=label,
                chars=chars,
                remaining=cfg.total_limit - chars,
                severity=_classify_total(chars, cfg),
                complete=complete,
            )
        )
    return totals


def _missing_named_workspace_dirs(cfg: Config) -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    for name in cfg.named_workspaces:
        candidate = cfg.workspace_dir / name
        try:
            resolved = ensure_within(cfg.workspace_dir, candidate)
        except UnsafeTargetError:
            continue
        if not resolved.is_dir():
            out.append((name, resolved))
    return out


def _fmt_delta(n: int) -> str:
    return f"{n:+d}"


def render_text(rows: list[FileStatus], cfg: Config) -> str:
    totals = collect_totals(rows, cfg)
    lines: list[str] = [
        "bootstrap-doctor status  "
        f"(soft={cfg.soft_limit}, hard={cfg.hard_limit}, total={cfg.total_limit})"
    ]
    for name, path in _missing_named_workspace_dirs(cfg):
        lines.append(f"warning: named workspace {name!r} does not exist: {path}")

    by_label: dict[str, list[FileStatus]] = {}
    for row in rows:
        by_label.setdefault(row.workspace_label, []).append(row)

    name_w = max([len("file"), *(len(row.name) for row in rows)])
    chars_w = max([len("chars"), *(len(str(row.chars)) for row in rows)])
    lines_w = max([len("lines"), *(len(str(row.lines)) for row in rows)])
    soft_w = max([len("soft"), *(len(_fmt_delta(row.soft_remaining)) for row in rows)])
    hard_w = max([len("hard"), *(len(_fmt_delta(row.hard_remaining)) for row in rows)])
    header = (
        f"  {'file':<{name_w}}  {'chars':>{chars_w}}  {'lines':>{lines_w}}  "
        f"{'soft':>{soft_w}}  {'hard':>{hard_w}}  sev"
    )
    totals_by_label = {total.workspace_label: total for total in totals}

    for label, workspace_rows in by_label.items():
        lines.extend(["", f"{label}  {workspace_rows[0].path.parent}", header])
        for row in workspace_rows:
            absent = row.severity in {SEV_MISSING, SEV_OPTIONAL}
            chars_cell = "-" if absent else str(row.chars)
            lines_cell = "-" if absent else str(row.lines)
            if row.severity in {SEV_MISSING, SEV_UNREADABLE, SEV_OPTIONAL}:
                soft_cell = hard_cell = "-"
            else:
                soft_cell = _fmt_delta(row.soft_remaining)
                hard_cell = _fmt_delta(row.hard_remaining)
            lines.append(
                f"  {row.name:<{name_w}}  {chars_cell:>{chars_w}}  "
                f"{lines_cell:>{lines_w}}  {soft_cell:>{soft_w}}  "
                f"{hard_cell:>{hard_w}}  {_FLAGS[row.severity]}"
            )
        total = totals_by_label[label]
        completeness = "complete" if total.complete else "incomplete"
        lines.append(
            f"  total: {total.chars}/{cfg.total_limit} "
            f"({_fmt_delta(total.remaining)} remaining, {total.severity}, {completeness})"
        )

    counts = {
        severity: sum(1 for row in rows if row.severity == severity)
        for severity in _FLAGS
    }
    lines.extend(
        [
            "",
            f"summary: {len(rows)} files, {counts[SEV_HARD]} over hard, "
            f"{counts[SEV_SOFT]} over soft, {counts[SEV_MISSING]} missing, "
            f"{counts[SEV_OPTIONAL]} optional absent"
            + (
                f", {counts[SEV_UNREADABLE]} unreadable"
                if counts[SEV_UNREADABLE]
                else ""
            ),
        ]
    )
    return "\n".join(lines)


def render_json(rows: list[FileStatus], cfg: Config) -> str:
    out_rows: list[dict] = []
    for row in rows:
        data = asdict(row)
        data["path"] = str(row.path)
        out_rows.append(data)
    out_totals: list[dict] = []
    for total in collect_totals(rows, cfg):
        data = asdict(total)
        data["path"] = str(total.path)
        out_totals.append(data)
    return json.dumps(
        {
            "soft_limit": cfg.soft_limit,
            "hard_limit": cfg.hard_limit,
            "total_soft_limit": total_soft_limit(cfg),
            "total_limit": cfg.total_limit,
            "totals": out_totals,
            "rows": out_rows,
        },
        indent=2,
    )


def _exit_code(rows: list[FileStatus], totals: list[WorkspaceTotal]) -> int:
    if any(
        row.severity in {SEV_HARD, SEV_MISSING, SEV_UNREADABLE} for row in rows
    ) or any(total.severity == SEV_HARD for total in totals):
        return 2
    if any(row.severity == SEV_SOFT for row in rows) or any(
        total.severity == SEV_SOFT for total in totals
    ):
        return 1
    return 0


def run(cfg: Config, *, as_json: bool = False) -> int:
    rows = collect(cfg)
    totals = collect_totals(rows, cfg)
    print(render_json(rows, cfg) if as_json else render_text(rows, cfg))
    return _exit_code(rows, totals)
