"""Deterministic, read-only bootstrap lifecycle detector.

Compares OpenClaw agent config, workspace setup state, and bootstrap files
to report stale first-run and context drift. No LLM calls, no repairs, and
no filesystem writes.
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bootstrap_doctor.paths import DEFAULT_TRACKED_FILES, Config

DEFAULT_AGENT_ID = "main"
SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
MIN_DUPLICATE_CHARS = 200
SUBSTANTIVE_MIN_CHARS = 20

RECOGNIZED_FILES: tuple[str, ...] = tuple(DEFAULT_TRACKED_FILES)
IDENTITY_REQUIRED_FIELDS = ("Name", "Creature", "Vibe", "Emoji")
USER_REQUIRED_FIELDS = ("Name", "What to call them", "Timezone")
WORKSPACE_STATE_FILENAME = "openclaw-workspace-state.json"
WORKSPACE_MARKERS = frozenset(
    {
        *RECOGNIZED_FILES,
        WORKSPACE_STATE_FILENAME,
    }
)
IGNORED_COMPONENTS = frozenset(
    {
        ".bootstrap-backups",
        "docs",
        "documentation",
        "node_modules",
        "worktrees",
        ".git",
        "cache",
        "tmp",
    }
)

_FIELD_RE = re.compile(r"^- \*\*(?P<field>[^*]+):\*\*[ \t]*(?P<value>.*)$")
_ITALIC_HINT_RE = re.compile(r"^_\(.*\)_$")
_STOCK_VALUES = frozenset(
    {"your name", "todo", "tbd", "changeme", "xxx", "n/a"}
)


class LintError(Exception):
    """Raised when required OpenClaw config cannot be read or parsed."""


@dataclass(frozen=True)
class LintFinding:
    check_id: str
    severity: str
    message: str
    path: Path
    agent_id: str | None = None


@dataclass(frozen=True)
class LintReport:
    findings: tuple[LintFinding, ...]

    @property
    def error_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == SEVERITY_ERROR)

    @property
    def warning_count(self) -> int:
        return sum(
            1 for finding in self.findings if finding.severity == SEVERITY_WARNING
        )


def load_openclaw_config(path: Path) -> dict[str, Any]:
    """Read and parse openclaw.json. Raises LintError on any failure."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LintError(f"cannot read OpenClaw config {path}: {exc}") from exc
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise LintError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise LintError(f"{path} is not a JSON object")
    _validate_agents_config(parsed)
    return parsed


def _require_object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LintError(f"{name} must be an object")
    return value


def _require_nonempty_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LintError(f"{name} must be a non-empty string")
    return value


def _validate_allow_agents(value: Any, name: str) -> None:
    if not isinstance(value, list):
        raise LintError(f"{name} must be a list of non-empty strings")
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise LintError(f"{name}[{index}] must be a non-empty string")


def _validate_subagents(value: Any, name: str) -> None:
    block = _require_object(value, name)
    if "allowAgents" in block:
        _validate_allow_agents(block["allowAgents"], f"{name}.allowAgents")


def _validate_workspace_field(block: dict[str, Any], name: str) -> None:
    if "workspace" in block:
        _require_nonempty_string(block["workspace"], f"{name}.workspace")


def _validate_agents_config(config: dict[str, Any]) -> None:
    if "agents" not in config:
        return
    agents = _require_object(config["agents"], "agents")
    if "defaults" in agents:
        defaults = _require_object(agents["defaults"], "agents.defaults")
        _validate_workspace_field(defaults, "agents.defaults")
        if "subagents" in defaults:
            _validate_subagents(defaults["subagents"], "agents.defaults.subagents")
    if "list" not in agents:
        return
    entries = agents["list"]
    if not isinstance(entries, list):
        raise LintError("agents.list must be a list")
    for index, entry in enumerate(entries):
        item = _require_object(entry, f"agents.list[{index}]")
        _require_nonempty_string(item.get("id"), f"agents.list[{index}].id")
        _validate_workspace_field(item, f"agents.list[{index}]")
        if "default" in item and not isinstance(item["default"], bool):
            raise LintError(f"agents.list[{index}].default must be a boolean")
        if "subagents" in item:
            _validate_subagents(item["subagents"], f"agents.list[{index}].subagents")


def _agents_block(config: dict[str, Any]) -> dict[str, Any]:
    agents = config.get("agents")
    return agents if isinstance(agents, dict) else {}


def _defaults_block(config: dict[str, Any]) -> dict[str, Any]:
    defaults = _agents_block(config).get("defaults")
    return defaults if isinstance(defaults, dict) else {}


def _agent_entries(config: dict[str, Any]) -> list[dict[str, Any]]:
    entries = _agents_block(config).get("list")
    if not isinstance(entries, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in entries:
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("id"), str)
            and entry["id"].strip()
        ):
            out.append(entry)
    return out


def _default_agent_id(entries: Sequence[dict[str, Any]]) -> str:
    if not entries:
        return DEFAULT_AGENT_ID
    for entry in entries:
        if entry.get("default") is True:
            return entry["id"]
    return entries[0]["id"]


def _state_home(openclaw_home: Path | None) -> Path:
    if openclaw_home is not None:
        return Path(openclaw_home).expanduser().resolve()
    return (Path.home() / ".openclaw").resolve()


def _resolve_user_path(raw: str, config_path: Path) -> Path:
    expanded = Path(os.path.expanduser(raw))
    if not expanded.is_absolute():
        expanded = config_path.parent / expanded
    return expanded.resolve()


def _primary_workspace(config: dict[str, Any], config_path: Path) -> Path | None:
    raw = _defaults_block(config).get("workspace")
    if isinstance(raw, str) and raw.strip():
        return _resolve_user_path(raw.strip(), config_path)
    return None


def resolve_agent_workspaces(
    config: dict[str, Any],
    config_path: Path,
    *,
    openclaw_home: Path | None = None,
) -> dict[str, Path]:
    """Map each configured agent id to its resolved workspace path."""
    _validate_agents_config(config)
    primary = _primary_workspace(config, config_path)
    state_home = _state_home(openclaw_home)
    entries = _agent_entries(config)
    default_id = _default_agent_id(entries)
    if not entries:
        entries = [{"id": DEFAULT_AGENT_ID}]
        default_id = DEFAULT_AGENT_ID
    resolved: dict[str, Path] = {}
    for entry in entries:
        agent_id = entry["id"]
        raw = entry.get("workspace")
        if isinstance(raw, str) and raw.strip():
            resolved[agent_id] = _resolve_user_path(raw.strip(), config_path)
            continue
        if agent_id == default_id:
            resolved[agent_id] = (
                primary if primary is not None else (state_home / "workspace")
            )
            continue
        if primary is not None:
            resolved[agent_id] = (primary / agent_id).resolve()
        else:
            resolved[agent_id] = (state_home / f"workspace-{agent_id}").resolve()
    return resolved


def _has_ignored_component(path: Path, root: Path) -> bool:
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return any(part in IGNORED_COMPONENTS for part in relative.parts)


def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _iter_dirs(path: Path) -> list[Path]:
    try:
        return [child for child in path.iterdir() if _is_dir(child)]
    except OSError:
        return []


def discover_workspace_candidates(
    primary: Path,
    configured: Iterable[Path] | None = None,
) -> list[Path]:
    """Return bounded workspace directories in deterministic path order."""
    primary = Path(primary).resolve()
    seen: dict[Path, Path] = {}

    def add(path: Path, *, force: bool = False) -> None:
        resolved = Path(path).resolve()
        if not _is_dir(resolved):
            return
        if not force and _has_ignored_component(resolved, primary):
            return
        seen[resolved] = resolved

    for path in configured or ():
        add(Path(path), force=True)
    add(primary, force=True)

    parent = primary.parent
    if _is_dir(parent):
        for sibling in _iter_dirs(parent):
            if sibling.name.startswith("workspace-"):
                add(sibling)

    if _is_dir(primary):
        for child in _iter_dirs(primary):
            if child.name in IGNORED_COMPONENTS:
                continue
            if not _is_file(child / "BOOTSTRAP.md"):
                continue
            if any(_is_file(child / marker) or _is_dir(child / marker) for marker in WORKSPACE_MARKERS):
                add(child)

    return sorted(seen, key=lambda path: str(path))


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _setup_completed(workspace: Path) -> bool:
    raw = _read_text(workspace / WORKSPACE_STATE_FILENAME)
    if raw is None:
        return False
    try:
        data = json.loads(raw)
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    value = data.get("setupCompletedAt")
    return isinstance(value, str) and bool(value.strip())


def _strip_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return text
    return text[end + 4 :].lstrip("\n")


def _is_substantive(text: str) -> bool:
    parts: list[str] = []
    for line in _strip_frontmatter(text).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts.append(stripped)
    return len(" ".join(parts)) >= SUBSTANTIVE_MIN_CHARS


def _normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def _field_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = _FIELD_RE.match(line)
        if match:
            values[match.group("field").strip()] = match.group("value").strip()
    return values


def _is_placeholder_value(value: str) -> bool:
    if not value:
        return True
    if _ITALIC_HINT_RE.match(value):
        return True
    return value.lower() in _STOCK_VALUES


def _has_placeholder_fields(text: str, required: Sequence[str]) -> bool:
    values = _field_values(text)
    return any(
        field in values and _is_placeholder_value(values[field])
        for field in required
    )


def _allow_agents(block: dict[str, Any]) -> list[str]:
    subagents = block.get("subagents")
    if not isinstance(subagents, dict):
        return []
    raw = subagents.get("allowAgents")
    if not isinstance(raw, list):
        return []
    return [name for name in raw if isinstance(name, str) and name]


def _agent_for(workspace: Path, workspaces: dict[str, Path]) -> str | None:
    resolved = workspace.resolve()
    for agent_id, path in workspaces.items():
        if path.resolve() == resolved:
            return agent_id
    return None


def _memory_files(workspace: Path) -> list[Path]:
    files: list[Path] = []
    root = workspace / "MEMORY.md"
    if _is_file(root):
        files.append(root)
    memory_dir = workspace / "memory"
    if not _is_dir(memory_dir):
        return files
    try:
        candidates = list(memory_dir.rglob("*.md"))
    except OSError:
        return files
    for path in candidates:
        if not _is_file(path) or _has_ignored_component(path, workspace):
            continue
        files.append(path)
    return files


def _sort_key(finding: LintFinding) -> tuple[int, str, str, str]:
    return (
        0 if finding.severity == SEVERITY_ERROR else 1,
        finding.check_id,
        str(finding.path),
        finding.agent_id or "",
    )


def collect_findings(
    config: dict[str, Any],
    config_path: Path,
    *,
    tracked_files: Sequence[str] | None = None,
    openclaw_home: Path | None = None,
) -> LintReport:
    """Collect lifecycle findings for one OpenClaw config."""
    _validate_agents_config(config)
    tracked = tuple(tracked_files) if tracked_files is not None else RECOGNIZED_FILES
    workspaces = resolve_agent_workspaces(
        config, config_path, openclaw_home=openclaw_home
    )
    entries = _agent_entries(config)
    primary = _primary_workspace(config, config_path)
    if primary is None:
        primary = workspaces.get(
            _default_agent_id(entries),
            _state_home(openclaw_home) / "workspace",
        )
    configured = {path.resolve() for path in workspaces.values()}
    candidates = discover_workspace_candidates(primary, configured=configured)
    findings: list[LintFinding] = []
    known_ids = {entry["id"] for entry in entries} or {DEFAULT_AGENT_ID}
    dangling_seen: set[str] = set()
    for owner, block in (
        (None, _defaults_block(config)),
        *((entry["id"], entry) for entry in entries),
    ):
        for name in _allow_agents(block):
            if name == "*" or name in known_ids or name in dangling_seen:
                continue
            dangling_seen.add(name)
            findings.append(
                LintFinding(
                    check_id="dangling-agent-reference",
                    severity=SEVERITY_ERROR,
                    message=(
                        f"subagents.allowAgents names {name!r} "
                        "which is absent from agents.list"
                    ),
                    path=config_path,
                    agent_id=owner,
                )
            )

    seen_orphans: set[Path] = set()
    for workspace in candidates:
        agent_id = _agent_for(workspace, workspaces)
        bootstrap = workspace / "BOOTSTRAP.md"
        if _is_file(bootstrap) and workspace.resolve() not in configured:
            resolved_bootstrap = bootstrap
            if resolved_bootstrap not in seen_orphans:
                seen_orphans.add(resolved_bootstrap)
                findings.append(
                    LintFinding(
                        check_id="orphan-workspace",
                        severity=SEVERITY_WARNING,
                        message="unconfigured workspace retains BOOTSTRAP.md",
                        path=bootstrap,
                        agent_id=None,
                    )
                )

        if workspace.resolve() not in configured:
            if _is_file(bootstrap):
                for memory_path in _memory_files(workspace):
                    text = _read_text(memory_path)
                    if text is not None and _is_substantive(text):
                        findings.append(
                            LintFinding(
                                check_id="memory-contradicts-fresh",
                                severity=SEVERITY_ERROR,
                                message=(
                                    "BOOTSTRAP.md claims a fresh workspace "
                                    "but memory shows prior use"
                                ),
                                path=memory_path,
                                agent_id=agent_id,
                            )
                        )
            continue

        if _is_file(bootstrap) and _setup_completed(workspace):
            findings.append(
                LintFinding(
                    check_id="bootstrap-after-setup",
                    severity=SEVERITY_ERROR,
                    message="BOOTSTRAP.md remains after setupCompletedAt",
                    path=bootstrap,
                    agent_id=agent_id,
                )
            )

        if _is_file(bootstrap):
            for memory_path in _memory_files(workspace):
                text = _read_text(memory_path)
                if text is not None and _is_substantive(text):
                    findings.append(
                        LintFinding(
                            check_id="memory-contradicts-fresh",
                            severity=SEVERITY_ERROR,
                            message=(
                                "BOOTSTRAP.md claims a fresh workspace "
                                "but memory shows prior use"
                            ),
                            path=memory_path,
                            agent_id=agent_id,
                        )
                    )

        for name, required in (
            ("IDENTITY.md", IDENTITY_REQUIRED_FIELDS),
            ("USER.md", USER_REQUIRED_FIELDS),
        ):
            path = workspace / name
            text = _read_text(path) if _is_file(path) else None
            if text is None:
                continue
            if _has_placeholder_fields(text, required):
                findings.append(
                    LintFinding(
                        check_id="configured-placeholder",
                        severity=SEVERITY_WARNING,
                        message=f"{name} retains stock or blank placeholder fields",
                        path=path,
                        agent_id=agent_id,
                    )
                )

        for name in RECOGNIZED_FILES:
            if name in tracked:
                continue
            path = workspace / name
            text = _read_text(path) if _is_file(path) else None
            if text is None or not _is_substantive(text):
                continue
            findings.append(
                LintFinding(
                    check_id="inactive-context-content",
                    severity=SEVERITY_WARNING,
                    message=(
                        "recognized bootstrap file is present but excluded "
                        "from tracked_files"
                    ),
                    path=path,
                    agent_id=agent_id,
                )
            )

    groups: dict[str, list[tuple[Path, Path, str | None]]] = {}
    for agent_id, workspace in workspaces.items():
        if not _is_dir(workspace):
            continue
        for name in RECOGNIZED_FILES:
            path = workspace / name
            text = _read_text(path) if _is_file(path) else None
            if text is None:
                continue
            normalized = _normalize(text)
            if len(normalized) < MIN_DUPLICATE_CHARS:
                continue
            groups.setdefault(normalized, []).append(
                (workspace.resolve(), path, agent_id)
            )
    for members in groups.values():
        workspace_ids = {item[0] for item in members}
        if len(workspace_ids) < 2:
            continue
        for _workspace, path, agent_id in members:
            findings.append(
                LintFinding(
                    check_id="duplicate-context",
                    severity=SEVERITY_WARNING,
                    message=(
                        "exact normalized bootstrap content is duplicated "
                        "across configured workspaces"
                    ),
                    path=path,
                    agent_id=agent_id,
                )
            )

    findings.sort(key=_sort_key)
    return LintReport(findings=tuple(findings))


def render_json(report: LintReport) -> str:
    payload = {
        "ok": not report.findings,
        "error_count": report.error_count,
        "warning_count": report.warning_count,
        "findings": [
            {
                "check_id": finding.check_id,
                "severity": finding.severity,
                "message": finding.message,
                "path": str(finding.path),
                "agent_id": finding.agent_id,
            }
            for finding in report.findings
        ],
    }
    return json.dumps(payload, indent=2)


def render_text(report: LintReport) -> str:
    lines = ["bootstrap-doctor lint"]
    for finding in report.findings:
        agent = f"  agent={finding.agent_id}" if finding.agent_id else ""
        lines.append(
            f"{finding.severity}  {finding.check_id}  {finding.path}{agent}"
        )
        lines.append(f"  {finding.message}")
    lines.extend(
        [
            "",
            f"summary: {report.error_count} error(s), {report.warning_count} warning(s)",
        ]
    )
    return "\n".join(lines)


def run(
    cfg: Config,
    *,
    openclaw_config: Path,
    openclaw_home: Path | None = None,
    as_json: bool = False,
) -> int:
    """Print a lifecycle report and return the contract exit code."""
    try:
        config = load_openclaw_config(openclaw_config)
        report = collect_findings(
            config,
            openclaw_config,
            tracked_files=cfg.tracked_files,
            openclaw_home=openclaw_home,
        )
    except LintError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(render_json(report) if as_json else render_text(report))
    if report.error_count:
        return 2
    if report.warning_count:
        return 1
    return 0
