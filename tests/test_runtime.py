"""Tests for runtime.py: what OpenClaw actually injected, not what is on disk."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bootstrap_doctor import runtime
from bootstrap_doctor.paths import DEFAULT_OPTIONAL_TRACKED_FILES, resolve_config

TRACKED = ("AGENTS.md", "TOOLS.md", "SOUL.md", "MEMORY.md")


# Fixtures ----------------------------------------------------------------


def _write_workspace(tmp_path: Path, sizes: dict[str, int]) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "memory" / "cards").mkdir(parents=True)
    for name, size in sizes.items():
        (ws / name).write_text("x" * size, encoding="utf-8")
    return ws


def _cfg(tmp_path: Path, ws: Path, **over):
    """resolve_config takes no tracked_files kwarg, so route it through a TOML."""
    toml = tmp_path / "bd.toml"
    tracked = ", ".join(f'"{n}"' for n in TRACKED)
    toml.write_text(f"tracked_files = [{tracked}]\n", encoding="utf-8")
    return resolve_config(
        config_file=str(toml),
        workspace_dir=str(ws),
        cards_dir=str(ws / "memory" / "cards"),
        **over,
    )


def _prompt(files: dict[str, str], ws: Path) -> str:
    parts = ["preamble text that belongs to no file"]
    for name, body in files.items():
        parts.append(f"### {ws / name}\n\n{body}")
    return "\n\n".join(parts)


def _write_trace(
    tmp_path: Path, agent: str, prompt, *, ts: str = "2026-08-12T21:45:46.291Z"
) -> Path:
    sessions = tmp_path / "agents" / agent / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    path = sessions / "abc.trajectory.jsonl"
    event = {
        "type": "context.compiled",
        "ts": ts,
        "modelId": "grok-4.6",
        "data": {"systemPrompt": prompt},
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "session.started", "ts": ts}) + "\n")
        fh.write(json.dumps(event) + "\n")
    return path


# Effective caps ----------------------------------------------------------


def test_caps_fall_back_to_openclaw_defaults_when_unset():
    """The 98-day outage shape: nothing configured, so OpenClaw's own defaults."""
    caps = runtime.resolve_effective_caps({}, "main", Path("openclaw.json"))
    assert caps.per_file == runtime.OPENCLAW_DEFAULT_PER_FILE_CHARS
    assert caps.total == runtime.OPENCLAW_DEFAULT_TOTAL_CHARS
    assert caps.per_file_configured is False
    assert caps.total_configured is False


def test_caps_prefer_agent_entry_over_defaults():
    cfg = {
        "agents": {
            "defaults": {"bootstrapMaxChars": 40000, "bootstrapTotalMaxChars": 120000},
            "list": [
                {"id": "browser-operator", "bootstrapMaxChars": 3000,
                 "bootstrapTotalMaxChars": 6000},
                {"id": "main"},
            ],
        }
    }
    small = runtime.resolve_effective_caps(cfg, "browser-operator", Path("c.json"))
    assert (small.per_file, small.total) == (3000, 6000)
    assert small.per_file_configured is True

    main = runtime.resolve_effective_caps(cfg, "main", Path("c.json"))
    assert (main.per_file, main.total) == (40000, 120000)


@pytest.mark.parametrize("bad", [0, -1, "40000", None, True])
def test_caps_reject_non_positive_and_non_numeric(bad):
    cfg = {"agents": {"defaults": {"bootstrapTotalMaxChars": bad}}}
    caps = runtime.resolve_effective_caps(cfg, "main", Path("c.json"))
    assert caps.total == runtime.OPENCLAW_DEFAULT_TOTAL_CHARS
    assert caps.total_configured is False


def test_cap_drift_reports_disagreement(tmp_path):
    ws = _write_workspace(tmp_path, {"AGENTS.md": 10})
    cfg = _cfg(tmp_path, ws, total_limit=60000, hard_limit=20000, soft_limit=17000)
    caps = runtime.EffectiveCaps(
        per_file=40000, total=120000, per_file_configured=True,
        total_configured=True, agent_id="main", source=Path("c.json"),
    )
    notes = runtime.cap_drift(caps, cfg)
    assert any("120000" in n and "60000" in n for n in notes)
    assert any("40000" in n and "20000" in n for n in notes)


def test_no_drift_when_limits_agree(tmp_path):
    ws = _write_workspace(tmp_path, {"AGENTS.md": 10})
    cfg = _cfg(tmp_path, ws, total_limit=120000, hard_limit=40000, soft_limit=17000)
    caps = runtime.EffectiveCaps(
        per_file=40000, total=120000, per_file_configured=True,
        total_configured=True, agent_id="main", source=Path("c.json"),
    )
    assert runtime.cap_drift(caps, cfg) == []


def test_load_openclaw_config_rejects_bad_json(tmp_path):
    bad = tmp_path / "openclaw.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(runtime.RuntimeError_, match="invalid JSON"):
        runtime.load_openclaw_config(bad)


# Prompt parsing ----------------------------------------------------------


def test_split_injected_files_keys_by_basename(tmp_path):
    ws = tmp_path / "workspace"
    prompt = _prompt({"AGENTS.md": "abcd", "TOOLS.md": "ab"}, ws)
    sizes = runtime.split_injected_files(prompt)
    assert sizes == {"AGENTS.md": 4, "TOOLS.md": 2}


def test_split_ignores_prose_that_merely_mentions_a_file(tmp_path):
    prompt = "### Heading\n\nSee AGENTS.md for rules. Also ### not/a heading.md here."
    assert runtime.split_injected_files(prompt) == {}


def test_prompt_size_and_text_handles_truncation_marker():
    event = {
        "data": {
            "systemPrompt": {
                "truncated": True,
                "reason": "trajectory-field-size-limit",
                "originalChars": 98096,
            }
        }
    }
    size, text = runtime.prompt_size_and_text(event)
    assert size == 98096
    assert text is None


def test_prompt_size_and_text_handles_plain_string():
    size, text = runtime.prompt_size_and_text({"data": {"systemPrompt": "abc"}})
    assert (size, text) == (3, "abc")


# Trace discovery ---------------------------------------------------------


def test_cron_and_heartbeat_sessions_are_excluded(tmp_path):
    """Cron runs use lightweight bootstrap mode; judging them is a false alarm."""
    sessions = tmp_path / "agents" / "main" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "a.trajectory.jsonl").write_text(
        json.dumps(
            {
                "type": "context.compiled",
                "ts": "2026-08-13T13:10:01Z",
                "sessionKey": "agent:main:cron:abc:run:def",
                "data": {"systemPrompt": "lightweight, no bootstrap files"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "context.compiled",
                "ts": "2026-08-12T21:45:46Z",
                "sessionKey": "agent:main:telegram:direct:123",
                "data": {"systemPrompt": "the real one"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    event, _path, skipped = runtime.latest_compiled_event(sessions)
    # The cron event is newer but must lose to the real session.
    assert event["data"]["systemPrompt"] == "the real one"
    assert skipped == 1


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("agent:main:cron:abc:run:def", True),
        ("agent:main:heartbeat:xyz", True),
        ("agent:main:telegram:direct:123", False),
        ("agent:main:bootstrapfix-verify", False),
        (None, False),
        # "cron" as a substring of a real key segment must not match.
        ("agent:main:cronjob-notes", False),
    ],
)
def test_is_lightweight_session(key, expected):
    assert (
        runtime.is_lightweight_session(
            key, runtime.DEFAULT_LIGHTWEIGHT_SESSION_KINDS
        )
        is expected
    )


def test_session_filter_narrows_to_matching_key(tmp_path):
    sessions = tmp_path / "agents" / "main" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "a.trajectory.jsonl").write_text(
        json.dumps(
            {
                "type": "context.compiled",
                "ts": "2026-08-13T00:00:00Z",
                "sessionKey": "agent:main:telegram:direct:123",
                "data": {"systemPrompt": "telegram"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "context.compiled",
                "ts": "2026-08-12T00:00:00Z",
                "sessionKey": "agent:main:verify-run",
                "data": {"systemPrompt": "verify"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    event, _path, _skipped = runtime.latest_compiled_event(
        sessions, session_filter="verify-run"
    )
    assert event["data"]["systemPrompt"] == "verify"


def test_optional_file_on_disk_but_absent_from_prompt_is_hard(tmp_path):
    """USER.md vanishing from the prompt is the same defect as AGENTS.md."""
    ws = _write_workspace(tmp_path, {"AGENTS.md": 100, "MEMORY.md": 9000})
    prompt = _prompt({"AGENTS.md": "x" * 100}, ws)
    _write_trace(tmp_path, "main", prompt)
    cfg = _cfg(tmp_path, ws)

    report = runtime.build_report(
        cfg,
        openclaw_home=tmp_path,
        openclaw_config=tmp_path / "absent.json",
        agent_id="main",
        optional_files=frozenset({"MEMORY.md"}),
    )
    row = {r.name: r for r in report.files}["MEMORY.md"]
    assert row.severity == runtime.SEV_ABSENT
    assert row.optional is True
    assert runtime.exit_code(report) == 2


def test_latest_compiled_event_picks_newest_and_skips_bad_lines(tmp_path):
    sessions = tmp_path / "agents" / "main" / "sessions"
    sessions.mkdir(parents=True)
    old = sessions / "old.trajectory.jsonl"
    old.write_text(
        json.dumps(
            {"type": "context.compiled", "ts": "2026-08-01T00:00:00Z",
             "data": {"systemPrompt": "old"}}
        )
        + "\n",
        encoding="utf-8",
    )
    new = sessions / "new.trajectory.jsonl"
    new.write_text(
        '{"type": "context.compiled", BROKEN\n'
        + json.dumps(
            {"type": "context.compiled", "ts": "2026-08-12T00:00:00Z",
             "data": {"systemPrompt": "new"}}
        )
        + "\n",
        encoding="utf-8",
    )
    event, path, _skipped = runtime.latest_compiled_event(sessions)
    assert event["data"]["systemPrompt"] == "new"
    assert path == new


def test_latest_compiled_event_returns_none_without_traces(tmp_path):
    assert runtime.latest_compiled_event(tmp_path / "nope") is None


# End-to-end classification ----------------------------------------------


def test_absent_required_file_is_caught_and_exits_2(tmp_path):
    """The regression under test: AGENTS.md on disk, absent from the prompt."""
    ws = _write_workspace(
        tmp_path, {"AGENTS.md": 14000, "TOOLS.md": 12000, "SOUL.md": 5000}
    )
    prompt = _prompt({"TOOLS.md": "x" * 12000, "SOUL.md": "x" * 5000}, ws)
    _write_trace(tmp_path, "main", prompt)
    cfg = _cfg(tmp_path, ws)

    report = runtime.build_report(
        cfg,
        openclaw_home=tmp_path,
        openclaw_config=tmp_path / "absent.json",
        agent_id="main",
        optional_files=frozenset(),
    )
    by_name = {r.name: r for r in report.files}
    assert by_name["AGENTS.md"].severity == runtime.SEV_ABSENT
    assert by_name["TOOLS.md"].severity == runtime.SEV_OK
    assert runtime.exit_code(report) == 2
    assert "AGENTS.md" in runtime.render_text(report)


def test_optional_only_excuses_a_file_that_is_not_on_disk(tmp_path):
    ws = _write_workspace(tmp_path, {"AGENTS.md": 100})
    prompt = _prompt({"AGENTS.md": "x" * 100}, ws)
    _write_trace(tmp_path, "main", prompt)
    cfg = _cfg(tmp_path, ws)

    report = runtime.build_report(
        cfg,
        openclaw_home=tmp_path,
        openclaw_config=tmp_path / "absent.json",
        agent_id="main",
        optional_files=frozenset({"MEMORY.md", "SOUL.md", "TOOLS.md"}),
    )
    by_name = {r.name: r for r in report.files}
    assert by_name["MEMORY.md"].severity == runtime.SEV_SKIPPED
    assert runtime.exit_code(report) == 0


def test_truncated_file_is_flagged(tmp_path):
    ws = _write_workspace(tmp_path, {"AGENTS.md": 14000})
    prompt = _prompt({"AGENTS.md": "x" * 9000}, ws)
    _write_trace(tmp_path, "main", prompt)
    cfg = _cfg(tmp_path, ws)

    report = runtime.build_report(
        cfg,
        openclaw_home=tmp_path,
        openclaw_config=tmp_path / "absent.json",
        agent_id="main",
        optional_files=frozenset(),
    )
    row = {r.name: r for r in report.files}["AGENTS.md"]
    assert row.severity == runtime.SEV_TRUNCATED
    assert row.injected_chars == 9000
    assert runtime.exit_code(report) == 1


def test_all_present_exits_zero(tmp_path):
    ws = _write_workspace(tmp_path, {"AGENTS.md": 100, "TOOLS.md": 200})
    prompt = _prompt({"AGENTS.md": "x" * 100, "TOOLS.md": "x" * 200}, ws)
    _write_trace(tmp_path, "main", prompt)
    cfg = _cfg(tmp_path, ws)

    report = runtime.build_report(
        cfg,
        openclaw_home=tmp_path,
        openclaw_config=tmp_path / "absent.json",
        agent_id="main",
        optional_files=frozenset(),
    )
    assert runtime.exit_code(report) == 0


def test_oversized_prompt_reports_size_but_not_presence(tmp_path):
    """A >32k prompt is stored as a marker, so presence is unknown, not absent."""
    ws = _write_workspace(tmp_path, {"AGENTS.md": 14000})
    _write_trace(
        tmp_path,
        "main",
        {"truncated": True, "reason": "trajectory-field-size-limit",
         "originalChars": 98096},
    )
    cfg = _cfg(tmp_path, ws)

    report = runtime.build_report(
        cfg,
        openclaw_home=tmp_path,
        openclaw_config=tmp_path / "absent.json",
        agent_id="main",
        optional_files=frozenset(),
    )
    assert report.prompt_chars == 98096
    assert report.prompt_text_available is False
    by_name = {r.name: r for r in report.files}
    # On disk but unverifiable from a size-only trace.
    assert by_name["AGENTS.md"].severity == runtime.SEV_UNKNOWN
    # Not on disk at all, so there is nothing to verify either way.
    assert by_name["TOOLS.md"].severity == runtime.SEV_SKIPPED
    assert runtime.exit_code(report) == 0
    assert "cannot be verified" in runtime.render_text(report)


def test_missing_trace_raises(tmp_path):
    ws = _write_workspace(tmp_path, {"AGENTS.md": 10})
    cfg = _cfg(tmp_path, ws)
    with pytest.raises(runtime.RuntimeError_, match="no eligible context.compiled"):
        runtime.build_report(
            cfg,
            openclaw_home=tmp_path,
            openclaw_config=tmp_path / "absent.json",
            agent_id="main",
            optional_files=frozenset(),
        )


def test_report_includes_caps_when_openclaw_config_present(tmp_path):
    ws = _write_workspace(tmp_path, {"AGENTS.md": 100})
    prompt = _prompt({"AGENTS.md": "x" * 100}, ws)
    _write_trace(tmp_path, "main", prompt)
    oc = tmp_path / "openclaw.json"
    oc.write_text(
        json.dumps({"agents": {"defaults": {"bootstrapTotalMaxChars": 120000}}}),
        encoding="utf-8",
    )
    cfg = _cfg(tmp_path, ws, total_limit=60000)

    report = runtime.build_report(
        cfg,
        openclaw_home=tmp_path,
        openclaw_config=oc,
        agent_id="main",
        optional_files=frozenset(),
    )
    assert report.caps is not None
    assert report.caps.total == 120000
    assert report.cap_drift
    assert runtime.exit_code(report) == 1
    assert "DRIFT" in runtime.render_text(report)


def test_render_json_is_parseable(tmp_path):
    ws = _write_workspace(tmp_path, {"AGENTS.md": 100})
    prompt = _prompt({"AGENTS.md": "x" * 100}, ws)
    _write_trace(tmp_path, "main", prompt)
    cfg = _cfg(tmp_path, ws)

    report = runtime.build_report(
        cfg,
        openclaw_home=tmp_path,
        openclaw_config=tmp_path / "absent.json",
        agent_id="main",
        optional_files=frozenset(),
    )
    payload = json.loads(runtime.render_json(report))
    assert payload["agent_id"] == "main"
    assert payload["files"][0]["name"] == "AGENTS.md"
    assert payload["caps"] is None


def test_default_optional_set_is_reusable():
    """The CLI passes paths.DEFAULT_OPTIONAL_TRACKED_FILES; keep it a frozenset."""
    assert isinstance(DEFAULT_OPTIONAL_TRACKED_FILES, frozenset)
    assert "AGENTS.md" not in DEFAULT_OPTIONAL_TRACKED_FILES
