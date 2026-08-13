"""Runtime verification of what OpenClaw actually injected (read-only).

Every other verb in this tool measures bootstrap files **on disk**. That is a
proxy, and on 2026-05-07 the proxy broke: OpenClaw began capping ``main``'s
compiled system prompt at 20,000 chars, which silently dropped AGENTS.md,
USER.md, and MEMORY.md from every session for 98 days. ``status`` reported
green the entire time, because every individual file was comfortably under its
limit and their sum was under the assumed total. The files were fine. The
prompt was not.

This module closes that gap by reading ground truth instead of inferring it.
OpenClaw writes a ``context.compiled`` event into
``<agentDir>/sessions/*.trajectory.jsonl`` containing the exact system prompt
it sent. Comparing that against the tracked bootstrap files answers the only
question that matters: did the model actually receive its instructions?

Two independent checks run here:

1. **Injection check** (ground truth). Parse the newest ``context.compiled``
   event, split the prompt on its ``### <path>`` file headings, and classify
   every tracked file as present, truncated, or absent.
2. **Cap drift** (expectation). Resolve the effective ``bootstrapMaxChars`` /
   ``bootstrapTotalMaxChars`` from ``openclaw.json`` the same way OpenClaw
   resolves them, and compare against this tool's own configured limits.

Check 2 alone would **not** have caught the outage: the key was unset, and
OpenClaw's documented default (60,000) is exactly the number this tool already
assumed. Only check 1 catches it. Check 2 exists so the reported numbers stop
drifting away from the live config.

A caveat the verb enforces rather than assumes: some harness runtimes deliver
bootstrap outside the injected system prompt. The Codex runtime hands AGENTS.md
to Codex as native project-doc ``<INSTRUCTIONS>`` and mirrors the rest into
``world_state``, so its ``context.compiled`` prompt proves nothing about those
files. Sessions on such a runtime are reported as unverifiable, not as failures.

No LLM calls, no mutations, stdlib only.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from bootstrap_doctor.paths import Config

# OpenClaw's own fallbacks, mirrored from
# src/agents/embedded-agent-helpers/bootstrap.ts. Used only when openclaw.json
# leaves the keys unset, which is exactly what OpenClaw itself does.
OPENCLAW_DEFAULT_PER_FILE_CHARS = 20_000
OPENCLAW_DEFAULT_TOTAL_CHARS = 60_000

DEFAULT_OPENCLAW_CONFIG = "~/.openclaw/openclaw.json"
DEFAULT_AGENT_ID = "main"

# Cron and heartbeat runs use OpenClaw's lightweight bootstrap context mode and
# deliberately carry no workspace files, so judging them against tracked_files
# reports a catastrophe that is really just a scheduled tick. Their session keys
# look like "agent:main:cron:<uuid>:run:<uuid>". Excluded by default; the count
# of what was skipped is always reported so the filter is never silent.
DEFAULT_LIGHTWEIGHT_SESSION_KINDS = ("cron", "heartbeat")

# Harness runtimes that deliver workspace bootstrap through their own channels
# instead of OpenClaw's injected system prompt. The Codex runtime hands AGENTS.md
# to Codex as native project-doc `<INSTRUCTIONS>` and mirrors the whole bootstrap
# set into `world_state`, so its `context.compiled` systemPrompt holds only the
# OpenClaw-side preamble. Judging those files against that field reports every
# one of them absent when they all arrived. Verified 2026-08-13 by reading a
# codex-home rollout: AGENTS.md, SOUL.md, USER.md, TOOLS.md, and MEMORY.md were
# all present as instructions, not tool output.
FOREIGN_BOOTSTRAP_RUNTIMES = ("codex",)

# The trajectory writer stores strings verbatim up to this length and replaces
# anything longer with {"truncated": true, "originalChars": N}. Mirrored from
# src/trajectory/runtime.ts so we can explain a size-only result.
TRAJECTORY_STRING_MAX_CHARS = 32_768

# Severity strings, matching status.py's vocabulary.
SEV_OK = "ok"
SEV_TRUNCATED = "truncated"
SEV_ABSENT = "absent"
SEV_UNKNOWN = "unknown"
SEV_SKIPPED = "skipped"

# A bootstrap file heading inside the compiled prompt, e.g.
# "### /home/you/.openclaw/workspace/TOOLS.md".
_HEADING_RE = re.compile(r"^###[ \t]+(?P<path>\S+\.md)[ \t]*$", re.MULTILINE)


class RuntimeError_(Exception):
    """Raised when runtime evidence cannot be read or parsed."""


@dataclass(frozen=True)
class EffectiveCaps:
    """Caps OpenClaw will actually apply, resolved the way OpenClaw does it."""

    per_file: int
    total: int
    per_file_configured: bool
    total_configured: bool
    agent_id: str
    source: Path


@dataclass(frozen=True)
class InjectedFile:
    name: str
    disk_chars: int
    injected_chars: int | None
    severity: str
    optional: bool


@dataclass(frozen=True)
class RuntimeReport:
    agent_id: str
    trace_path: Path | None
    timestamp: str | None
    model_id: str | None
    prompt_chars: int | None
    prompt_text_available: bool
    session_key: str | None = None
    skipped_sessions: int = 0
    foreign_runtime: str | None = None
    files: list[InjectedFile] = field(default_factory=list)
    caps: EffectiveCaps | None = None
    cap_drift: list[str] = field(default_factory=list)


# Effective caps ----------------------------------------------------------


def load_openclaw_config(path: Path) -> dict[str, Any]:
    """Read and parse openclaw.json. Raises RuntimeError_ on any failure."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError_(f"cannot read OpenClaw config {path}: {exc}") from exc
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError_(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError_(f"{path} is not a JSON object")
    return parsed


def _positive_int(value: Any) -> int | None:
    """OpenClaw accepts a finite positive number and floors it; mirror that."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value <= 0:
        return None
    return int(value)


def _agent_entry(cfg: dict[str, Any], agent_id: str) -> dict[str, Any]:
    agents = cfg.get("agents")
    if not isinstance(agents, dict):
        return {}
    entries = agents.get("list")
    if not isinstance(entries, list):
        return {}
    for entry in entries:
        if isinstance(entry, dict) and entry.get("id") == agent_id:
            return entry
    return {}


def resolve_effective_caps(
    cfg: dict[str, Any], agent_id: str, source: Path
) -> EffectiveCaps:
    """Resolve per-file and total caps: agent entry, then defaults, then builtin.

    Mirrors resolveBootstrapMaxChars / resolveBootstrapTotalMaxChars in
    OpenClaw. A per-agent value wins over agents.defaults, which wins over
    OpenClaw's own compiled-in fallback.
    """
    agents = cfg.get("agents")
    defaults = agents.get("defaults") if isinstance(agents, dict) else None
    defaults = defaults if isinstance(defaults, dict) else {}
    entry = _agent_entry(cfg, agent_id)

    def pick(key: str) -> int | None:
        for scope in (entry, defaults):
            value = _positive_int(scope.get(key))
            if value is not None:
                return value
        return None

    per_file = pick("bootstrapMaxChars")
    total = pick("bootstrapTotalMaxChars")
    return EffectiveCaps(
        per_file=per_file if per_file is not None else OPENCLAW_DEFAULT_PER_FILE_CHARS,
        total=total if total is not None else OPENCLAW_DEFAULT_TOTAL_CHARS,
        per_file_configured=per_file is not None,
        total_configured=total is not None,
        agent_id=agent_id,
        source=source,
    )


def resolve_model_runtime(
    cfg: dict[str, Any], model_id: str | None, agent_id: str
) -> str | None:
    """Return the agentRuntime id declared for ``model_id``, if any.

    Trajectory events carry a bare model id ("gpt-5.6-sol") while config keys
    are provider-qualified ("openai/gpt-5.6-sol"), so match on the suffix. A
    per-agent ``models`` block wins over ``agents.defaults.models``.
    """
    if not model_id:
        return None
    agents = cfg.get("agents")
    agents = agents if isinstance(agents, dict) else {}
    defaults = agents.get("defaults")
    scopes: list[dict[str, Any]] = []
    entry = _agent_entry(cfg, agent_id)
    for scope in (entry.get("models"), (defaults or {}).get("models")):
        if isinstance(scope, dict):
            scopes.append(scope)
    for scope in scopes:
        for key, value in scope.items():
            if not isinstance(value, dict):
                continue
            if key == model_id or key.rsplit("/", 1)[-1] == model_id:
                runtime = value.get("agentRuntime")
                if isinstance(runtime, dict):
                    rid = runtime.get("id")
                    if isinstance(rid, str) and rid.strip():
                        return rid.strip()
    return None


def cap_drift(caps: EffectiveCaps, cfg: Config) -> list[str]:
    """Human-readable notes where this tool's limits disagree with OpenClaw's."""
    notes: list[str] = []
    if caps.total != cfg.total_limit:
        notes.append(
            f"total budget: OpenClaw will apply {caps.total}, "
            f"bootstrap-doctor is configured for {cfg.total_limit}"
        )
    if caps.per_file != cfg.hard_limit:
        notes.append(
            f"per-file cap: OpenClaw will apply {caps.per_file}, "
            f"bootstrap-doctor hard_limit is {cfg.hard_limit}"
        )
    return notes


# Trajectory evidence -----------------------------------------------------


def agent_sessions_dir(openclaw_home: Path, agent_id: str) -> Path:
    return openclaw_home / "agents" / agent_id / "sessions"


def is_lightweight_session(
    session_key: str | None, kinds: tuple[str, ...]
) -> bool:
    """True for cron/heartbeat keys like ``agent:main:cron:<uuid>:run:<uuid>``."""
    if not session_key:
        return False
    segments = session_key.split(":")
    return any(kind in segments for kind in kinds)


def latest_compiled_event(
    sessions_dir: Path,
    *,
    lightweight_kinds: tuple[str, ...] = DEFAULT_LIGHTWEIGHT_SESSION_KINDS,
    session_filter: str | None = None,
) -> tuple[dict[str, Any], Path, int] | None:
    """Newest eligible context.compiled event, its file, and the skipped count.

    Trajectory files are append-only JSONL. A malformed line is skipped rather
    than failing the scan; a partially written trace should not blind the
    check. Lightweight (cron/heartbeat) sessions are excluded because they
    carry no bootstrap files by design, and ``session_filter`` narrows to
    session keys containing that substring.
    """
    best: tuple[str, dict[str, Any], Path] | None = None
    skipped = 0
    if not sessions_dir.is_dir():
        return None
    for path in sessions_dir.glob("*.trajectory.jsonl"):
        try:
            handle = path.open(encoding="utf-8")
        except OSError:
            continue
        with handle:
            for line in handle:
                if '"context.compiled"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") != "context.compiled":
                    continue
                key = event.get("sessionKey")
                if session_filter and session_filter not in str(key or ""):
                    continue
                if is_lightweight_session(key, lightweight_kinds):
                    skipped += 1
                    continue
                stamp = str(event.get("ts") or "")
                if best is None or stamp > best[0]:
                    best = (stamp, event, path)
    if best is None:
        return None
    return best[1], best[2], skipped


def prompt_size_and_text(event: dict[str, Any]) -> tuple[int | None, str | None]:
    """Extract (chars, text) from a context.compiled event.

    The trajectory writer never slices: it stores the string verbatim or
    swaps in a marker carrying ``originalChars``. So an oversized prompt still
    yields a trustworthy size, just no text to inspect.
    """
    data = event.get("data")
    prompt = data.get("systemPrompt") if isinstance(data, dict) else None
    if isinstance(prompt, str):
        return len(prompt), prompt
    if isinstance(prompt, dict) and prompt.get("truncated"):
        size = _positive_int(prompt.get("originalChars"))
        return size, None
    return None, None


def split_injected_files(prompt: str) -> dict[str, int]:
    """Map bootstrap file name -> injected char count, from ``### <path>`` headings.

    Content runs from the end of one heading to the start of the next heading
    (or end of prompt). Only the basename is keyed, since that is what
    ``tracked_files`` holds.
    """
    matches = list(_HEADING_RE.finditer(prompt))
    sizes: dict[str, int] = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(prompt)
        name = Path(match.group("path")).name
        # Later occurrences win; a duplicated heading means a re-injection.
        sizes[name] = len(prompt[start:end].strip("\n"))
    return sizes


def _classify_file(
    injected: int | None, disk_chars: int, *, text_available: bool
) -> str:
    if not text_available:
        return SEV_UNKNOWN
    if injected is None:
        return SEV_ABSENT
    # Injected content is stripped of surrounding newlines, so allow a small
    # slack rather than demanding an exact byte match.
    if injected + 2 < disk_chars:
        return SEV_TRUNCATED
    return SEV_OK


def analyze_files(
    prompt: str | None,
    cfg: Config,
    workspace_dir: Path,
    optional_files: frozenset[str],
) -> list[InjectedFile]:
    """Classify every tracked file against what the prompt actually contains."""
    injected = split_injected_files(prompt) if prompt is not None else {}
    rows: list[InjectedFile] = []
    for name in cfg.tracked_files:
        disk_path = workspace_dir / name
        optional = name in optional_files
        if not disk_path.exists():
            rows.append(
                InjectedFile(
                    name=name,
                    disk_chars=0,
                    injected_chars=None,
                    severity=SEV_SKIPPED,
                    optional=optional,
                )
            )
            continue
        try:
            disk_chars = len(disk_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            disk_chars = 0
        rows.append(
            InjectedFile(
                name=name,
                disk_chars=disk_chars,
                injected_chars=injected.get(name),
                severity=_classify_file(
                    injected.get(name),
                    disk_chars,
                    text_available=prompt is not None,
                ),
                optional=optional,
            )
        )
    return rows


# Report ------------------------------------------------------------------


def build_report(
    cfg: Config,
    *,
    openclaw_home: Path,
    openclaw_config: Path,
    agent_id: str,
    optional_files: frozenset[str],
    session_filter: str | None = None,
    lightweight_kinds: tuple[str, ...] = DEFAULT_LIGHTWEIGHT_SESSION_KINDS,
) -> RuntimeReport:
    """Assemble the runtime report. Raises RuntimeError_ when evidence is absent."""
    caps: EffectiveCaps | None = None
    drift: list[str] = []
    if openclaw_config.exists():
        caps = resolve_effective_caps(
            load_openclaw_config(openclaw_config), agent_id, openclaw_config
        )
        drift = cap_drift(caps, cfg)

    sessions_dir = agent_sessions_dir(openclaw_home, agent_id)
    found = latest_compiled_event(
        sessions_dir,
        lightweight_kinds=lightweight_kinds,
        session_filter=session_filter,
    )
    if found is None:
        raise RuntimeError_(
            f"no eligible context.compiled trace for agent {agent_id!r} under "
            f"{sessions_dir}; run one full agent turn first "
            "(cron and heartbeat runs are excluded)"
        )
    event, trace_path, skipped = found
    prompt_chars, prompt_text = prompt_size_and_text(event)

    # A foreign harness delivers bootstrap outside the injected system prompt,
    # so the prompt text cannot answer the question. Say so instead of calling
    # every file absent.
    foreign: str | None = None
    if openclaw_config.exists():
        runtime_id = resolve_model_runtime(
            load_openclaw_config(openclaw_config), event.get("modelId"), agent_id
        )
        if runtime_id in FOREIGN_BOOTSTRAP_RUNTIMES:
            foreign = runtime_id
            prompt_text = None

    return RuntimeReport(
        agent_id=agent_id,
        trace_path=trace_path,
        timestamp=event.get("ts"),
        model_id=event.get("modelId"),
        prompt_chars=prompt_chars,
        prompt_text_available=prompt_text is not None,
        session_key=event.get("sessionKey"),
        skipped_sessions=skipped,
        foreign_runtime=foreign,
        files=analyze_files(prompt_text, cfg, cfg.workspace_dir, optional_files),
        caps=caps,
        cap_drift=drift,
    )


def exit_code(report: RuntimeReport) -> int:
    """0 all good, 1 truncation or drift, 2 a tracked file never arrived.

    ``optional`` excuses a file that is not on disk, never one that is on disk
    and failed to reach the model. USER.md going missing from the prompt is the
    same defect as AGENTS.md going missing, and the 2026-05-07 outage dropped
    both.
    """
    if any(row.severity == SEV_ABSENT for row in report.files):
        return 2
    code = 0
    if any(row.severity == SEV_TRUNCATED for row in report.files):
        code = 1
    if report.cap_drift:
        code = 1
    return code


def render_text(report: RuntimeReport) -> str:
    lines: list[str] = [
        f"bootstrap-doctor runtime  (agent={report.agent_id})",
        "",
    ]
    if report.caps is not None:
        caps = report.caps
        per_src = "configured" if caps.per_file_configured else "OpenClaw default"
        tot_src = "configured" if caps.total_configured else "OpenClaw default"
        lines.append(f"effective caps from {caps.source}")
        lines.append(f"  per-file  {caps.per_file} ({per_src})")
        lines.append(f"  total     {caps.total} ({tot_src})")
    else:
        lines.append("effective caps: openclaw.json not found, skipping cap check")
    for note in report.cap_drift:
        lines.append(f"  DRIFT {note}")

    lines.append("")
    lines.append(f"newest compiled prompt  {report.timestamp}")
    lines.append(f"  trace   {report.trace_path}")
    if report.session_key:
        lines.append(f"  session {report.session_key}")
    if report.model_id:
        lines.append(f"  model   {report.model_id}")
    lines.append(f"  chars   {report.prompt_chars}")
    if report.skipped_sessions:
        lines.append(
            f"  skipped {report.skipped_sessions} cron/heartbeat compile(s), "
            "which carry no bootstrap files by design"
        )
    if report.foreign_runtime:
        lines.append(
            f"  note    the {report.foreign_runtime} runtime delivers workspace "
            "bootstrap through its own instruction channels, not this system "
            "prompt, so per-file presence cannot be verified from this trace"
        )
    elif not report.prompt_text_available:
        lines.append(
            f"  note    prompt exceeded the {TRAJECTORY_STRING_MAX_CHARS}-char "
            "trajectory field limit, so only its size was recorded; "
            "per-file presence cannot be verified from this trace"
        )

    lines.append("")
    name_w = max([len("file")] + [len(r.name) for r in report.files])
    lines.append(f"  {'file':<{name_w}}  {'disk':>7}  {'injected':>8}  status")
    for row in report.files:
        injected = "-" if row.injected_chars is None else str(row.injected_chars)
        disk = "-" if row.severity == SEV_SKIPPED else str(row.disk_chars)
        flag = row.severity.upper() if row.severity != SEV_OK else SEV_OK
        if row.severity == SEV_SKIPPED and row.optional:
            flag = "skipped (optional, not on disk)"
        lines.append(
            f"  {row.name:<{name_w}}  {disk:>7}  {injected:>8}  {flag}"
        )

    absent = [r for r in report.files if r.severity == SEV_ABSENT]
    truncated = [r for r in report.files if r.severity == SEV_TRUNCATED]
    lines.append("")
    lines.append(
        f"summary: {len(absent)} tracked file(s) missing from the prompt, "
        f"{len(truncated)} truncated, {len(report.cap_drift)} cap drift note(s)"
    )
    if absent:
        names = ", ".join(r.name for r in absent)
        lines.append(
            f"  {names} never reached the model. Raise "
            "agents.defaults.bootstrapTotalMaxChars or shrink earlier files."
        )
    return "\n".join(lines)


def render_json(report: RuntimeReport) -> str:
    payload: dict[str, Any] = {
        "agent_id": report.agent_id,
        "trace_path": str(report.trace_path) if report.trace_path else None,
        "timestamp": report.timestamp,
        "model_id": report.model_id,
        "prompt_chars": report.prompt_chars,
        "prompt_text_available": report.prompt_text_available,
        "session_key": report.session_key,
        "skipped_sessions": report.skipped_sessions,
        "foreign_runtime": report.foreign_runtime,
        "cap_drift": report.cap_drift,
        "caps": None,
        "files": [asdict(r) for r in report.files],
    }
    if report.caps is not None:
        caps = asdict(report.caps)
        caps["source"] = str(report.caps.source)
        payload["caps"] = caps
    return json.dumps(payload, indent=2)


def run(
    cfg: Config,
    *,
    openclaw_home: Path,
    openclaw_config: Path,
    agent_id: str,
    optional_files: frozenset[str],
    session_filter: str | None = None,
    as_json: bool = False,
) -> int:
    """Entrypoint called by cli.py. Prints the report, returns the exit code."""
    report = build_report(
        cfg,
        openclaw_home=openclaw_home,
        openclaw_config=openclaw_config,
        agent_id=agent_id,
        optional_files=optional_files,
        session_filter=session_filter,
    )
    print(render_json(report) if as_json else render_text(report))
    return exit_code(report)
