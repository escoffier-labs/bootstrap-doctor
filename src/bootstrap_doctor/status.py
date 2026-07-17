"""Read-only size and limit reporting for OpenClaw bootstrap files."""
from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path

from bootstrap_doctor.paths import (
    DEFAULT_OPTIONAL_TRACKED_FILES,
    DEFAULT_TRACKED_FILES,
    Config,
)
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

_OPENCLAW_MISSING_MARKER_FILENAMES = tuple(
    name for name in DEFAULT_TRACKED_FILES if name != "MEMORY.md"
)
_AGENTS_POLICY_DIGEST_RATIO = 0.35
_AGENTS_POLICY_HEAD_RATIO = 0.45
_AGENTS_POLICY_TAIL_RATIO = 0.15
_AGENTS_POLICY_DIGEST_MAX_LINE_CHARS = 240
_POLICY_WS_CHARS = re.escape(_ECMASCRIPT_TRIM_END_CHARS)
_POLICY_PREFIX_RE = re.compile(
    rf"^(?:#{{1,6}}|[{_POLICY_WS_CHARS}]*[-*+]"
    rf"|[{_POLICY_WS_CHARS}]*[0-9]+[.)])"
    rf"[{_POLICY_WS_CHARS}]+[^{_POLICY_WS_CHARS}]",
)
_POLICY_KEYWORDS = (
    "agents.md",
    "scoped",
    "required",
    "must",
    "never",
    "do not",
    "before subtree",
    "read scoped",
    "owner",
    "security",
    "secret",
    "credential",
    "test",
    "validation",
    "command",
    "commit",
    "push",
    "github",
    "pr",
)
_HIGH_PRIORITY_POLICY_KEYWORDS = (
    "agents.md",
    "scoped",
    "required",
    "must",
    "never",
    "do not",
    "before subtree",
    "read scoped",
    "security",
    "secret",
    "credential",
)
_POLICY_WHITESPACE_RE = re.compile(rf"[{_POLICY_WS_CHARS}]+")


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
    _injected_chars: int | None = None


@dataclass(frozen=True)
class FileSnapshot:
    """One boundary-checked immutable read of a tracked bootstrap file."""

    path: Path
    resolved_path: Path
    workspace_label: str
    name: str
    exists: bool
    bytes: int
    text: str | None
    identity: tuple[int, int, int, int, int, int] | None


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
    return _utf16_length(trimmed)


def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


def _truncate_utf16_safe(text: str, max_units: int) -> str:
    """Port OpenClaw's surrogate-safe truncateUtf16Safe helper."""
    limit = max(0, int(max_units))
    raw = text.encode("utf-16-le", errors="surrogatepass")
    units = len(raw) // 2
    if units <= limit:
        return text
    if 0 < limit < units:
        last = int.from_bytes(raw[(limit - 1) * 2 : limit * 2], "little")
        following = int.from_bytes(raw[limit * 2 : (limit + 1) * 2], "little")
        if 0xD800 <= last <= 0xDBFF and 0xDC00 <= following <= 0xDFFF:
            limit -= 1
    return raw[: limit * 2].decode("utf-16-le", errors="surrogatepass")


def _slice_utf16_units(text: str, start: int, end: int | None = None) -> str:
    """Match JavaScript String.slice indexes for rendered head/tail content."""
    raw = text.encode("utf-16-le", errors="surrogatepass")
    units = len(raw) // 2
    left = max(units + start, 0) if start < 0 else min(start, units)
    if end is None:
        right = units
    else:
        right = max(units + end, 0) if end < 0 else min(end, units)
    if right < left:
        left, right = right, left
    return raw[left * 2 : right * 2].decode("utf-16-le", errors="surrogatepass")


def _normalize_policy_digest_line(line: str) -> str:
    normalized = _POLICY_WHITESPACE_RE.sub(
        " ", line.strip(_ECMASCRIPT_TRIM_END_CHARS)
    )
    if _utf16_length(normalized) <= _AGENTS_POLICY_DIGEST_MAX_LINE_CHARS:
        return normalized
    return (
        _truncate_utf16_safe(
            normalized, _AGENTS_POLICY_DIGEST_MAX_LINE_CHARS - 1
        )
        + "…"
    )


def _javascript_iu_canonical_char(char: str) -> str:
    """Apply the simple Unicode fold relevant to JavaScript's /iu matching."""
    folded = char.casefold()
    return folded if len(folded) == 1 else char


def _javascript_iu_word_char(char: str) -> bool:
    canonical = _javascript_iu_canonical_char(char)
    return (
        "a" <= canonical <= "z"
        or "0" <= canonical <= "9"
        or canonical == "_"
    )


def _contains_javascript_iu_keyword(
    text: str, keywords: tuple[str, ...]
) -> bool:
    """Match ASCII keywords with ECMAScript /iu word-boundary semantics."""
    canonical = "".join(_javascript_iu_canonical_char(char) for char in text)
    for keyword in keywords:
        start = 0
        while (index := canonical.find(keyword, start)) >= 0:
            end = index + len(keyword)
            left_boundary = index == 0 or not _javascript_iu_word_char(
                text[index - 1]
            )
            right_boundary = end == len(text) or not _javascript_iu_word_char(
                text[end]
            )
            if left_boundary and right_boundary:
                return True
            start = index + 1
    return False


def _build_agents_policy_digest(content: str, budget: int) -> tuple[str, int]:
    if budget <= 0:
        return "", 0
    candidates = [
        (index, normalized)
        for index, line in enumerate(re.split(r"\r?\n", content))
        if (normalized := _normalize_policy_digest_line(line))
        and (
            _POLICY_PREFIX_RE.search(normalized)
            or _contains_javascript_iu_keyword(normalized, _POLICY_KEYWORDS)
        )
    ]
    selected: set[int] = set()
    used = 0

    def try_select(index: int, line: str) -> None:
        nonlocal used
        separator = 1 if selected else 0
        candidate_length = _utf16_length(line)
        if used + separator + candidate_length <= budget:
            selected.add(index)
            used += separator + candidate_length

    for index, line in candidates:
        if _contains_javascript_iu_keyword(
            line, _HIGH_PRIORITY_POLICY_KEYWORDS
        ):
            try_select(index, line)
    for index, line in candidates:
        if index not in selected:
            try_select(index, line)

    lines = [line for index, line in candidates if index in selected]
    return "\n".join(lines), max(0, len(candidates) - len(lines))


def _render_agents_truncation(
    trimmed: str,
    original_length: int,
    head_chars: int,
    tail_chars: int,
    digest_text: str,
    omitted_lines: int,
) -> str:
    parts = [
        _slice_utf16_units(trimmed, 0, head_chars),
        "[...truncated, read AGENTS.md for full content...]",
        "[Policy digest from AGENTS.md]" if digest_text else "",
        digest_text,
        f"[...{omitted_lines} more policy lines omitted...]"
        if omitted_lines > 0
        else "",
        (
            f"…(truncated AGENTS.md: kept {head_chars}+policy "
            f"{_utf16_length(digest_text)}+{tail_chars} chars of "
            f"{original_length})…"
        ),
        _slice_utf16_units(trimmed, -tail_chars) if tail_chars > 0 else "",
    ]
    return "\n".join(part for part in parts if _utf16_length(part) > 0)


def _openclaw_agents_injected_char_count(text: str, max_chars: int) -> int:
    """Port c248beb's AGENTS policy-digest truncation for aggregate pressure."""
    trimmed = text.rstrip(_ECMASCRIPT_TRIM_END_CHARS)
    original_length = _utf16_length(trimmed)
    if original_length <= max_chars:
        return original_length

    head_chars = int(max_chars * _AGENTS_POLICY_HEAD_RATIO)
    tail_chars = int(max_chars * _AGENTS_POLICY_TAIL_RATIO)
    digest_budget = int(max_chars * _AGENTS_POLICY_DIGEST_RATIO)
    digest_text, omitted_lines = _build_agents_policy_digest(
        trimmed, digest_budget
    )

    def render() -> str:
        return _render_agents_truncation(
            trimmed,
            original_length,
            head_chars,
            tail_chars,
            digest_text,
            omitted_lines,
        )

    rendered = render()
    while _utf16_length(rendered) > max_chars and (
        tail_chars > 0 or head_chars > 1 or digest_budget > 0
    ):
        overflow = _utf16_length(rendered) - max_chars
        if tail_chars > 0:
            tail_chars = max(0, tail_chars - overflow)
        elif head_chars > 1:
            head_chars = max(1, head_chars - overflow)
        else:
            digest_budget = max(0, digest_budget - overflow)
            digest_text, omitted_lines = _build_agents_policy_digest(
                trimmed, digest_budget
            )
        rendered = render()

    if _utf16_length(rendered) > max_chars:
        rendered = _truncate_utf16_safe(rendered, max_chars)
    return _utf16_length(rendered)


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


def _file_identity(
    stat_result: os.stat_result,
) -> tuple[int, int, int, int, int, int]:
    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_mode,
        stat_result.st_size,
        stat_result.st_mtime_ns,
        stat_result.st_ctime_ns,
    )


def _opened_fd_path(fd: int) -> Path:
    """Resolve the kernel's path for an open descriptor."""
    for fd_root in (Path("/proc/self/fd"), Path("/dev/fd")):
        try:
            return (fd_root / str(fd)).resolve(strict=True)
        except OSError:
            continue
    raise OSError("could not verify opened file descriptor path")


def _unreadable_snapshot(
    path: Path,
    resolved: Path,
    label: str,
    name: str,
    *,
    size_bytes: int = 0,
) -> FileSnapshot:
    return FileSnapshot(path, resolved, label, name, True, size_bytes, None, None)


def read_file_snapshot(
    workspace_dir: Path, path: Path, label: str, name: str
) -> FileSnapshot:
    """Open one contained regular file without following a swapped symlink."""
    resolved = ensure_within(workspace_dir, path)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    if not nofollow or not nonblock:
        return _unreadable_snapshot(path, resolved, label, name)
    flags = os.O_RDONLY | nofollow | nonblock | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(resolved, flags)
    except FileNotFoundError:
        return FileSnapshot(path, resolved, label, name, False, 0, None, None)
    except OSError:
        return _unreadable_snapshot(path, resolved, label, name)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            return _unreadable_snapshot(
                path, resolved, label, name, size_bytes=before.st_size
            )
        opened_path = _opened_fd_path(fd)
        if opened_path != resolved:
            return _unreadable_snapshot(
                path, resolved, label, name, size_bytes=before.st_size
            )
        chunks: list[bytes] = []
        while chunk := os.read(fd, 1024 * 1024):
            chunks.append(chunk)
        raw = b"".join(chunks)
        after = os.fstat(fd)
        if _opened_fd_path(fd) != resolved:
            return _unreadable_snapshot(
                path, resolved, label, name, size_bytes=after.st_size
            )
        if _file_identity(before) != _file_identity(after):
            return _unreadable_snapshot(
                path, opened_path, label, name, size_bytes=after.st_size
            )
    except UnsafeTargetError:
        raise
    except OSError:
        return _unreadable_snapshot(path, resolved, label, name)
    finally:
        os.close(fd)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    return FileSnapshot(
        path=path,
        resolved_path=opened_path,
        workspace_label=label,
        name=name,
        exists=True,
        bytes=len(raw),
        text=text,
        identity=_file_identity(after),
    )


def validate_snapshot(cfg: Config, snapshot: FileSnapshot) -> None:
    """Fail if a tracked path no longer names the exact bytes that were measured."""
    current = read_file_snapshot(
        cfg.workspace_dir,
        snapshot.path,
        snapshot.workspace_label,
        snapshot.name,
    )
    if current.resolved_path != snapshot.resolved_path:
        raise OSError(f"tracked bootstrap path changed target: {snapshot.path}")
    if not snapshot.exists:
        if current.exists:
            raise OSError(f"tracked bootstrap file appeared: {snapshot.path}")
        return
    if not current.exists:
        raise OSError(f"tracked bootstrap file disappeared: {snapshot.path}")
    if current.identity != snapshot.identity:
        raise OSError(f"tracked bootstrap file identity changed: {snapshot.path}")
    if current.text != snapshot.text:
        raise OSError(f"tracked bootstrap file content changed: {snapshot.path}")


def _measure_snapshot(snapshot: FileSnapshot, cfg: Config) -> FileStatus:
    if not snapshot.exists:
        severity = (
            SEV_OPTIONAL
            if snapshot.name in DEFAULT_OPTIONAL_TRACKED_FILES
            else SEV_MISSING
        )
        return FileStatus(
            path=snapshot.path,
            workspace_label=snapshot.workspace_label,
            name=snapshot.name,
            exists=False,
            bytes=0,
            chars=0,
            lines=0,
            soft_remaining=cfg.soft_limit,
            hard_remaining=cfg.hard_limit,
            severity=severity,
        )
    if snapshot.text is None:
        return FileStatus(
            path=snapshot.path,
            workspace_label=snapshot.workspace_label,
            name=snapshot.name,
            exists=True,
            bytes=snapshot.bytes,
            chars=0,
            lines=0,
            soft_remaining=cfg.soft_limit,
            hard_remaining=cfg.hard_limit,
            severity=SEV_UNREADABLE,
        )
    chars = _openclaw_char_count(snapshot.text)
    injected_chars = (
        _openclaw_agents_injected_char_count(snapshot.text, cfg.hard_limit)
        if snapshot.name.lower() == "agents.md"
        else min(chars, cfg.hard_limit)
    )
    return FileStatus(
        path=snapshot.path,
        workspace_label=snapshot.workspace_label,
        name=snapshot.name,
        exists=True,
        bytes=snapshot.bytes,
        chars=chars,
        lines=_count_lines(snapshot.text),
        soft_remaining=cfg.soft_limit - chars,
        hard_remaining=cfg.hard_limit - chars,
        severity=_classify(chars, cfg),
        _injected_chars=injected_chars,
    )


def _workspace_scopes(cfg: Config) -> list[tuple[str, Path]]:
    scopes: list[tuple[str, Path]] = [(PRIMARY_LABEL, cfg.workspace_dir)]
    seen = {cfg.workspace_dir.resolve()}
    for name in cfg.named_workspaces:
        try:
            resolved = ensure_within(cfg.workspace_dir, cfg.workspace_dir / name)
        except UnsafeTargetError as exc:
            raise UnsafeTargetError(
                f"named workspace {name!r} resolves outside workspace_dir: {exc}"
            ) from exc
        if resolved in seen:
            raise UnsafeTargetError(
                f"named workspace {name!r} duplicates workspace path {resolved}"
            )
        seen.add(resolved)
        scopes.append((name, resolved))
    return scopes


def collect_with_snapshots(
    cfg: Config,
) -> tuple[list[FileStatus], tuple[FileSnapshot, ...]]:
    """Measure every tracked file and return the exact snapshots used."""
    rows: list[FileStatus] = []
    snapshots: list[FileSnapshot] = []
    for label, ws_dir in _workspace_scopes(cfg):
        for name in cfg.tracked_files:
            snapshot = read_file_snapshot(
                cfg.workspace_dir, ws_dir / name, label, name
            )
            snapshots.append(snapshot)
            rows.append(_measure_snapshot(snapshot, cfg))
    return rows, tuple(snapshots)


def collect(cfg: Config) -> list[FileStatus]:
    """Measure every configured bootstrap file in every workspace."""
    rows, _snapshots = collect_with_snapshots(cfg)
    return rows


def _group_rows(
    rows: list[FileStatus],
) -> dict[tuple[str, Path], list[FileStatus]]:
    grouped: dict[tuple[str, Path], list[FileStatus]] = {}
    for row in rows:
        key = (row.workspace_label, row.path.parent)
        grouped.setdefault(key, []).append(row)
    return grouped


def collect_totals(rows: list[FileStatus], cfg: Config) -> list[WorkspaceTotal]:
    """Aggregate injected character pressure independently per workspace."""
    totals: list[WorkspaceTotal] = []
    for (label, workspace_path), workspace_rows in _group_rows(rows).items():
        chars = 0
        for row in workspace_rows:
            if row.exists:
                chars += (
                    row._injected_chars
                    if row._injected_chars is not None
                    else min(row.chars, cfg.hard_limit)
                )
            elif row.name in _OPENCLAW_MISSING_MARKER_FILENAMES:
                marker = f"[MISSING] Expected at: {row.path}"
                chars += _utf16_length(marker)
        complete = not any(
            row.severity in {SEV_MISSING, SEV_UNREADABLE} for row in workspace_rows
        )
        totals.append(
            WorkspaceTotal(
                path=workspace_path,
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

    grouped = _group_rows(rows)

    name_w = max([len("file"), *(len(row.name) for row in rows)])
    chars_w = max([len("chars"), *(len(str(row.chars)) for row in rows)])
    lines_w = max([len("lines"), *(len(str(row.lines)) for row in rows)])
    soft_w = max([len("soft"), *(len(_fmt_delta(row.soft_remaining)) for row in rows)])
    hard_w = max([len("hard"), *(len(_fmt_delta(row.hard_remaining)) for row in rows)])
    header = (
        f"  {'file':<{name_w}}  {'chars':>{chars_w}}  {'lines':>{lines_w}}  "
        f"{'soft':>{soft_w}}  {'hard':>{hard_w}}  sev"
    )
    totals_by_scope = {
        (total.workspace_label, total.path): total for total in totals
    }

    for (label, workspace_path), workspace_rows in grouped.items():
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
        total = totals_by_scope[(label, workspace_path)]
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
        data.pop("_injected_chars", None)
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
