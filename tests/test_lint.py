"""Tests for the read-only bootstrap lifecycle detector."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bootstrap_doctor.lint import (
    LintError,
    LintFinding,
    LintReport,
    collect_findings,
    discover_workspace_candidates,
    load_openclaw_config,
    render_json,
    render_text,
    resolve_agent_workspaces,
    run,
)
from bootstrap_doctor.paths import Config

RECOGNIZED = (
    "AGENTS.md",
    "SOUL.md",
    "TOOLS.md",
    "IDENTITY.md",
    "USER.md",
    "HEARTBEAT.md",
    "BOOTSTRAP.md",
    "MEMORY.md",
)

STOCK_IDENTITY = """# IDENTITY.md - Who Am I?

_Fill this in during your first conversation. Make it yours._

- **Name:**
  _(pick something you like)_
- **Creature:**
  _(AI? robot? familiar? ghost in the machine? something weirder?)_
- **Vibe:**
  _(how do you come across? sharp? warm? chaotic? calm?)_
- **Emoji:**
  _(your signature)_
- **Avatar:**
  _(workspace-relative path)_
"""

FILLED_IDENTITY = """# IDENTITY.md - Who Am I?

- **Name:** Lakehouse
- **Creature:** research assistant
- **Vibe:** calm
- **Emoji:** 📘
- **Avatar:** avatars/lakehouse.png
"""

STOCK_USER = """# USER.md - About Your Human

- **Name:**
- **What to call them:**
- **Pronouns:** _(optional)_
- **Timezone:**
- **Notes:**
"""

FILLED_USER = """# USER.md - About Your Human

- **Name:** Ada Example
- **What to call them:** Ada
- **Pronouns:** she/her
- **Timezone:** America/Chicago
- **Notes:** Prefers short answers.
"""

CUSTOM_IDENTITY = """# IDENTITY.md

Lakehouse is a research assistant with a calm, precise voice.
This workspace chose that identity without the stock bold field list.
"""

CUSTOM_USER = """# USER.md

Ada Example prefers Ada, works from America/Chicago, and wants short answers.
This file never used the stock bold field template.
"""

BOOTSTRAP_BODY = "# BOOTSTRAP.md - Hello, World\n\nThere is no memory yet.\n"
SUBSTANTIVE_MEMORY = (
    "# MEMORY.md\n\n"
    "Ada already finished onboarding last month and stored the lakehouse "
    "runbook plus the prior operator handoff.\n"
)
DUPLICATE_BODY = ("shared bootstrap policy for the lakehouse agents. " * 8).strip() + "\n"
LONG_STOCK_USER = STOCK_USER + (
    "\nThis stock USER.md template still has blank required fields. " * 6
)
LONG_STOCK_BOOTSTRAP = (
    "# BOOTSTRAP.md - Hello, World\n\n"
    + ("There is no memory yet. Complete first-run setup only. " * 8)
)
LONG_FILLED_IDENTITY = FILLED_IDENTITY + (
    "\nLakehouse keeps one shared identity paragraph across agents. " * 8
)
IGNORED_COMPONENTS = (
    ".bootstrap-backups",
    "docs",
    "documentation",
    "node_modules",
    "worktrees",
    ".git",
    "cache",
    "tmp",
)


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _cfg(
    tmp_path: Path, *, tracked: tuple[str, ...] = RECOGNIZED
) -> Config:
    workspace = tmp_path / "bd-workspace"
    workspace.mkdir(exist_ok=True)
    cards = workspace / "memory" / "cards"
    cards.mkdir(parents=True, exist_ok=True)
    return Config(
        workspace_dir=workspace,
        cards_dir=cards,
        gateway_url="http://127.0.0.1:9",
        gateway_model="fixture",
        soft_limit=17000,
        hard_limit=20000,
        total_limit=60000,
        tracked_files=tracked,
        named_workspaces=(),
        min_section_chars=400,
        stale_days=60,
        cache_dir=tmp_path / "cache",
    )


def _openclaw_home(
    tmp_path: Path,
    *,
    agents: list[dict],
    defaults_workspace: str | None = None,
    allow_agents: list[str] | None = None,
    defaults_allow_agents: list[str] | None = None,
) -> tuple[Path, Path, Path]:
    """Build a fake OpenClaw home. Returns (home, config_path, primary)."""
    home = tmp_path / "openclaw-home"
    primary = home / "workspace"
    primary.mkdir(parents=True)
    config_path = home / "openclaw.json"
    defaults: dict = {}
    defaults["workspace"] = (
        defaults_workspace if defaults_workspace is not None else str(primary)
    )
    if defaults_allow_agents is not None:
        defaults["subagents"] = {"allowAgents": defaults_allow_agents}
    payload: dict = {"agents": {"defaults": defaults, "list": agents}}
    if allow_agents is not None and agents:
        agents[0].setdefault("subagents", {})["allowAgents"] = allow_agents
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    return home, config_path, primary


def _seed_workspace(
    workspace: Path,
    *,
    bootstrap: bool = False,
    setup_completed: bool = False,
    identity: str | None = FILLED_IDENTITY,
    user: str | None = FILLED_USER,
    memory: str | None = None,
    extra: dict[str, str] | None = None,
) -> None:
    if bootstrap:
        _write(workspace / "BOOTSTRAP.md", BOOTSTRAP_BODY)
    if setup_completed:
        _write(
            workspace / "openclaw-workspace-state.json",
            json.dumps({"version": 1, "setupCompletedAt": "2026-02-17T10:34:20.551Z"}),
        )
    if identity is not None:
        _write(workspace / "IDENTITY.md", identity)
    if user is not None:
        _write(workspace / "USER.md", user)
    if memory is not None:
        _write(workspace / "MEMORY.md", memory)
    for name, body in (extra or {}).items():
        _write(workspace / name, body)


def _finding_ids(report: LintReport) -> set[str]:
    return {finding.check_id for finding in report.findings}


def _ids_in_order(report: LintReport) -> list[str]:
    return [finding.check_id for finding in report.findings]


# Types -------------------------------------------------------------------


def test_finding_and_report_are_frozen() -> None:
    finding = LintFinding(
        check_id="orphan-workspace",
        severity="warning",
        message="unconfigured workspace retains BOOTSTRAP.md",
        path=Path("/tmp/workspace-ghost/BOOTSTRAP.md"),
        agent_id=None,
    )
    report = LintReport(findings=(finding,))
    with pytest.raises(Exception):
        finding.message = "nope"  # type: ignore[misc]
    with pytest.raises(Exception):
        report.findings = ()  # type: ignore[misc]
    assert report.error_count == 0
    assert report.warning_count == 1


# Combined dirty configured workspace -------------------------------------


def test_collect_reports_stale_configured_lifecycle(tmp_path: Path) -> None:
    _home, config_path, primary = _openclaw_home(
        tmp_path,
        agents=[{"id": "main"}],
        allow_agents=["missing-helper"],
    )
    _seed_workspace(
        primary,
        bootstrap=True,
        setup_completed=True,
        identity=STOCK_IDENTITY,
        user=FILLED_USER,
        memory=SUBSTANTIVE_MEMORY,
    )
    config = load_openclaw_config(config_path)
    report = collect_findings(config, config_path)
    assert _finding_ids(report) == {
        "bootstrap-after-setup",
        "configured-placeholder",
        "memory-contradicts-fresh",
        "dangling-agent-reference",
    }
    assert report.error_count == 3
    assert report.warning_count == 1


# Orphans and exclusions --------------------------------------------------


def test_orphan_sibling_workspace_is_warned(tmp_path: Path) -> None:
    _home, config_path, primary = _openclaw_home(
        tmp_path, agents=[{"id": "main"}]
    )
    _seed_workspace(primary, setup_completed=True)
    orphan = primary.parent / "workspace-ghost"
    _seed_workspace(orphan, bootstrap=True, identity=None, user=None)
    report = collect_findings(load_openclaw_config(config_path), config_path)
    orphans = [f for f in report.findings if f.check_id == "orphan-workspace"]
    assert len(orphans) == 1
    assert orphans[0].severity == "warning"
    assert orphans[0].path == orphan / "BOOTSTRAP.md"
    assert orphans[0].agent_id is None


def test_orphan_immediate_child_with_markers_is_warned(tmp_path: Path) -> None:
    _home, config_path, primary = _openclaw_home(
        tmp_path, agents=[{"id": "main"}]
    )
    _seed_workspace(primary, setup_completed=True)
    child = primary / "nested-extra"
    _seed_workspace(
        child,
        bootstrap=True,
        identity=None,
        user=None,
        extra={"AGENTS.md": "# AGENTS.md\n\nFollow the lakehouse rules.\n"},
    )
    report = collect_findings(load_openclaw_config(config_path), config_path)
    orphans = [f for f in report.findings if f.check_id == "orphan-workspace"]
    assert [f.path for f in orphans] == [child / "BOOTSTRAP.md"]


@pytest.mark.parametrize("ignored", IGNORED_COMPONENTS)
def test_ignored_path_components_are_not_orphan_candidates(
    tmp_path: Path, ignored: str
) -> None:
    _home, config_path, primary = _openclaw_home(
        tmp_path, agents=[{"id": "main"}]
    )
    _seed_workspace(primary, setup_completed=True)
    decoy = primary / ignored / "stale-copy"
    _seed_workspace(
        decoy,
        bootstrap=True,
        identity=None,
        user=None,
        extra={"AGENTS.md": "# AGENTS.md\n\nIgnore this decoy workspace.\n"},
    )
    configured = resolve_agent_workspaces(
        load_openclaw_config(config_path), config_path
    )
    candidates = discover_workspace_candidates(
        primary, configured=configured.values()
    )
    assert decoy.resolve() not in {path.resolve() for path in candidates}
    report = collect_findings(load_openclaw_config(config_path), config_path)
    assert "orphan-workspace" not in _finding_ids(report)


def test_deeper_descendants_are_ignored(tmp_path: Path) -> None:
    _home, config_path, primary = _openclaw_home(
        tmp_path, agents=[{"id": "main"}]
    )
    _seed_workspace(primary, setup_completed=True)
    deep = primary / "team" / "project"
    _seed_workspace(
        deep,
        bootstrap=True,
        identity=None,
        user=None,
        extra={"AGENTS.md": "# AGENTS.md\n\nDeep fixture workspace.\n"},
    )
    report = collect_findings(load_openclaw_config(config_path), config_path)
    assert "orphan-workspace" not in _finding_ids(report)


# Remaining finding IDs ---------------------------------------------------


def test_inactive_context_content_for_excluded_bootstrap_file(
    tmp_path: Path,
) -> None:
    _home, config_path, primary = _openclaw_home(
        tmp_path, agents=[{"id": "main"}]
    )
    _seed_workspace(
        primary,
        setup_completed=True,
        extra={
            "SOUL.md": (
                "# SOUL.md\n\nBe precise, keep secrets off the wire, and "
                "prefer the lakehouse runbook over improvising.\n"
            )
        },
    )
    report = collect_findings(
        load_openclaw_config(config_path),
        config_path,
        tracked_files=("AGENTS.md", "TOOLS.md", "IDENTITY.md", "USER.md"),
    )
    inactive = [f for f in report.findings if f.check_id == "inactive-context-content"]
    assert len(inactive) == 1
    assert inactive[0].severity == "warning"
    assert inactive[0].path == primary / "SOUL.md"


def test_duplicate_context_across_configured_workspaces(tmp_path: Path) -> None:
    home, config_path, primary = _openclaw_home(
        tmp_path,
        agents=[
            {"id": "main"},
            {"id": "beta", "workspace": str(tmp_path / "openclaw-home" / "workspace-beta")},
        ],
    )
    beta = home / "workspace-beta"
    _seed_workspace(
        primary,
        setup_completed=True,
        extra={"AGENTS.md": DUPLICATE_BODY},
    )
    _seed_workspace(
        beta,
        setup_completed=True,
        extra={"AGENTS.md": DUPLICATE_BODY},
    )
    report = collect_findings(load_openclaw_config(config_path), config_path)
    dupes = [f for f in report.findings if f.check_id == "duplicate-context"]
    assert dupes
    assert all(f.severity == "warning" for f in dupes)
    assert {f.path for f in dupes} <= {primary / "AGENTS.md", beta / "AGENTS.md"}


def test_stock_identity_templates_are_not_duplicate_context(
    tmp_path: Path,
) -> None:
    home, config_path, primary = _openclaw_home(
        tmp_path,
        agents=[
            {"id": "main"},
            {
                "id": "beta",
                "workspace": str(tmp_path / "openclaw-home" / "workspace-beta"),
            },
        ],
    )
    _seed_workspace(
        primary,
        setup_completed=True,
        identity=STOCK_IDENTITY,
        user=FILLED_USER,
    )
    _seed_workspace(
        home / "workspace-beta",
        setup_completed=True,
        identity=STOCK_IDENTITY,
        user=FILLED_USER,
    )
    report = collect_findings(load_openclaw_config(config_path), config_path)
    assert "duplicate-context" not in _finding_ids(report)
    placeholders = [
        f for f in report.findings if f.check_id == "configured-placeholder"
    ]
    assert {f.path.name for f in placeholders} == {"IDENTITY.md"}
    assert len(placeholders) == 2


def test_stock_user_templates_are_not_duplicate_context(tmp_path: Path) -> None:
    home, config_path, primary = _openclaw_home(
        tmp_path,
        agents=[
            {"id": "main"},
            {
                "id": "beta",
                "workspace": str(tmp_path / "openclaw-home" / "workspace-beta"),
            },
        ],
    )
    _seed_workspace(
        primary,
        setup_completed=True,
        identity=FILLED_IDENTITY,
        user=LONG_STOCK_USER,
    )
    _seed_workspace(
        home / "workspace-beta",
        setup_completed=True,
        identity=FILLED_IDENTITY,
        user=LONG_STOCK_USER,
    )
    report = collect_findings(load_openclaw_config(config_path), config_path)
    assert "duplicate-context" not in _finding_ids(report)
    placeholders = [
        f for f in report.findings if f.check_id == "configured-placeholder"
    ]
    assert {f.path.name for f in placeholders} == {"USER.md"}
    assert len(placeholders) == 2


def test_bootstrap_is_excluded_from_duplicate_context(tmp_path: Path) -> None:
    home, config_path, primary = _openclaw_home(
        tmp_path,
        agents=[
            {"id": "main"},
            {
                "id": "beta",
                "workspace": str(tmp_path / "openclaw-home" / "workspace-beta"),
            },
        ],
    )
    _seed_workspace(
        primary,
        identity=FILLED_IDENTITY,
        user=FILLED_USER,
        extra={"BOOTSTRAP.md": LONG_STOCK_BOOTSTRAP},
    )
    _seed_workspace(
        home / "workspace-beta",
        identity=FILLED_IDENTITY,
        user=FILLED_USER,
        extra={"BOOTSTRAP.md": LONG_STOCK_BOOTSTRAP},
    )
    report = collect_findings(load_openclaw_config(config_path), config_path)
    assert "duplicate-context" not in _finding_ids(report)


def test_filled_identity_duplicate_is_still_reported(tmp_path: Path) -> None:
    home, config_path, primary = _openclaw_home(
        tmp_path,
        agents=[
            {"id": "main"},
            {
                "id": "beta",
                "workspace": str(tmp_path / "openclaw-home" / "workspace-beta"),
            },
        ],
    )
    _seed_workspace(
        primary,
        setup_completed=True,
        identity=LONG_FILLED_IDENTITY,
        user=FILLED_USER,
    )
    _seed_workspace(
        home / "workspace-beta",
        setup_completed=True,
        identity=LONG_FILLED_IDENTITY,
        user=FILLED_USER,
    )
    report = collect_findings(load_openclaw_config(config_path), config_path)
    dupes = [f for f in report.findings if f.check_id == "duplicate-context"]
    assert {f.path for f in dupes} == {
        primary / "IDENTITY.md",
        home / "workspace-beta" / "IDENTITY.md",
    }
    assert "configured-placeholder" not in _finding_ids(report)


def test_short_duplicate_is_ignored(tmp_path: Path) -> None:
    home, config_path, primary = _openclaw_home(
        tmp_path,
        agents=[
            {"id": "main"},
            {"id": "beta", "workspace": str(tmp_path / "openclaw-home" / "workspace-beta")},
        ],
    )
    short = "too short to count as duplicate context\n"
    _seed_workspace(primary, setup_completed=True, extra={"AGENTS.md": short})
    _seed_workspace(home / "workspace-beta", setup_completed=True, extra={"AGENTS.md": short})
    report = collect_findings(load_openclaw_config(config_path), config_path)
    assert "duplicate-context" not in _finding_ids(report)


def test_wildcard_allow_agents_is_valid(tmp_path: Path) -> None:
    _home, config_path, primary = _openclaw_home(
        tmp_path,
        agents=[{"id": "main"}],
        allow_agents=["*"],
    )
    _seed_workspace(primary, setup_completed=True)
    report = collect_findings(load_openclaw_config(config_path), config_path)
    assert "dangling-agent-reference" not in _finding_ids(report)
    assert report.findings == ()


def test_blank_user_fields_are_configured_placeholder(tmp_path: Path) -> None:
    _home, config_path, primary = _openclaw_home(
        tmp_path, agents=[{"id": "main"}]
    )
    _seed_workspace(
        primary,
        setup_completed=True,
        identity=FILLED_IDENTITY,
        user=STOCK_USER,
    )
    report = collect_findings(load_openclaw_config(config_path), config_path)
    placeholders = [
        f for f in report.findings if f.check_id == "configured-placeholder"
    ]
    assert [f.path for f in placeholders] == [primary / "USER.md"]
    assert placeholders[0].agent_id == "main"


def test_untouched_stock_identity_is_configured_placeholder(tmp_path: Path) -> None:
    _home, config_path, primary = _openclaw_home(
        tmp_path, agents=[{"id": "main"}]
    )
    _seed_workspace(
        primary,
        setup_completed=True,
        identity=STOCK_IDENTITY,
        user=FILLED_USER,
    )
    report = collect_findings(load_openclaw_config(config_path), config_path)
    placeholders = [
        f for f in report.findings if f.check_id == "configured-placeholder"
    ]
    assert [f.path for f in placeholders] == [primary / "IDENTITY.md"]
    assert placeholders[0].agent_id == "main"


def test_custom_format_identity_is_not_configured_placeholder(
    tmp_path: Path,
) -> None:
    _home, config_path, primary = _openclaw_home(
        tmp_path, agents=[{"id": "main"}]
    )
    _seed_workspace(
        primary,
        setup_completed=True,
        identity=CUSTOM_IDENTITY,
        user=FILLED_USER,
    )
    report = collect_findings(load_openclaw_config(config_path), config_path)
    assert "configured-placeholder" not in _finding_ids(report)
    assert report.findings == ()


def test_custom_format_user_is_not_configured_placeholder(tmp_path: Path) -> None:
    _home, config_path, primary = _openclaw_home(
        tmp_path, agents=[{"id": "main"}]
    )
    _seed_workspace(
        primary,
        setup_completed=True,
        identity=FILLED_IDENTITY,
        user=CUSTOM_USER,
    )
    report = collect_findings(load_openclaw_config(config_path), config_path)
    assert "configured-placeholder" not in _finding_ids(report)
    assert report.findings == ()


def test_memory_card_proves_prior_use(tmp_path: Path) -> None:
    _home, config_path, primary = _openclaw_home(
        tmp_path, agents=[{"id": "main"}]
    )
    _seed_workspace(primary, bootstrap=True, identity=FILLED_IDENTITY)
    _write(
        primary / "memory" / "cards" / "lakehouse-runbook.md",
        "# lakehouse-runbook\n\nPrior operator already documented the nightly sync.\n",
    )
    report = collect_findings(load_openclaw_config(config_path), config_path)
    memory = [f for f in report.findings if f.check_id == "memory-contradicts-fresh"]
    assert memory
    assert memory[0].severity == "error"
    assert memory[0].path == primary / "memory" / "cards" / "lakehouse-runbook.md"


# Workspace resolution ----------------------------------------------------


def test_resolve_agent_workspaces_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    config_path = cfg_dir / "openclaw.json"
    config = {
        "agents": {
            "defaults": {"workspace": "~/primary-ws"},
            "list": [
                {"id": "main"},
                {"id": "helper"},
                {"id": "named", "workspace": "rel-named"},
            ],
        }
    }
    resolved = resolve_agent_workspaces(config, config_path)
    assert resolved["main"] == (home / "primary-ws").resolve()
    assert resolved["helper"] == (home / "primary-ws" / "helper").resolve()
    assert resolved["named"] == (cfg_dir / "rel-named").resolve()


def test_per_agent_workspace_wins_over_defaults(tmp_path: Path) -> None:
    config_path = tmp_path / "openclaw.json"
    primary = tmp_path / "workspace"
    other = tmp_path / "other-ws"
    config = {
        "agents": {
            "defaults": {"workspace": str(primary)},
            "list": [{"id": "main", "workspace": str(other)}],
        }
    }
    resolved = resolve_agent_workspaces(config, config_path)
    assert resolved["main"] == other.resolve()


def test_defaults_only_config_resolves_implicit_main_to_state_workspace(
    tmp_path: Path,
) -> None:
    home = tmp_path / "openclaw-home"
    config_path = tmp_path / "elsewhere" / "openclaw.json"
    config_path.parent.mkdir()
    config: dict = {"agents": {"defaults": {}}}
    resolved = resolve_agent_workspaces(config, config_path, openclaw_home=home)
    assert resolved == {"main": (home / "workspace").resolve()}


def test_defaults_only_config_uses_defaults_workspace_for_implicit_main(
    tmp_path: Path,
) -> None:
    home = tmp_path / "openclaw-home"
    primary = tmp_path / "named-primary"
    config_path = tmp_path / "openclaw.json"
    config = {"agents": {"defaults": {"workspace": str(primary)}}}
    resolved = resolve_agent_workspaces(config, config_path, openclaw_home=home)
    assert resolved == {"main": primary.resolve()}


def test_non_main_default_agent_inherits_defaults_workspace(
    tmp_path: Path,
) -> None:
    home = tmp_path / "openclaw-home"
    primary = tmp_path / "named-primary"
    config_path = tmp_path / "openclaw.json"
    config = {
        "agents": {
            "defaults": {"workspace": str(primary)},
            "list": [
                {"id": "coder", "default": True},
                {"id": "helper"},
            ],
        }
    }
    resolved = resolve_agent_workspaces(config, config_path, openclaw_home=home)
    assert resolved["coder"] == primary.resolve()
    assert resolved["helper"] == (primary / "helper").resolve()


def test_first_listed_agent_inherits_defaults_workspace_as_default(
    tmp_path: Path,
) -> None:
    home = tmp_path / "openclaw-home"
    primary = tmp_path / "named-primary"
    config_path = tmp_path / "openclaw.json"
    config = {
        "agents": {
            "defaults": {"workspace": str(primary)},
            "list": [{"id": "coder"}, {"id": "helper"}],
        }
    }
    resolved = resolve_agent_workspaces(config, config_path, openclaw_home=home)
    assert resolved["coder"] == primary.resolve()
    assert resolved["helper"] == (primary / "helper").resolve()


def test_non_default_agent_without_defaults_workspace_uses_state_home(
    tmp_path: Path,
) -> None:
    home = tmp_path / "openclaw-home"
    config_path = tmp_path / "elsewhere" / "openclaw.json"
    config_path.parent.mkdir()
    config = {"agents": {"list": [{"id": "main"}, {"id": "helper"}]}}
    resolved = resolve_agent_workspaces(config, config_path, openclaw_home=home)
    assert resolved["main"] == (home / "workspace").resolve()
    assert resolved["helper"] == (home / "workspace-helper").resolve()


def test_explicit_workspace_wins_for_default_and_non_default(
    tmp_path: Path,
) -> None:
    home = tmp_path / "openclaw-home"
    primary = tmp_path / "named-primary"
    coder_ws = tmp_path / "coder-ws"
    helper_ws = tmp_path / "helper-ws"
    config_path = tmp_path / "openclaw.json"
    config = {
        "agents": {
            "defaults": {"workspace": str(primary)},
            "list": [
                {"id": "coder", "default": True, "workspace": str(coder_ws)},
                {"id": "helper", "workspace": str(helper_ws)},
            ],
        }
    }
    resolved = resolve_agent_workspaces(config, config_path, openclaw_home=home)
    assert resolved["coder"] == coder_ws.resolve()
    assert resolved["helper"] == helper_ws.resolve()


def test_normalize_uppercase_agent_id(tmp_path: Path) -> None:
    home = tmp_path / "openclaw-home"
    primary = tmp_path / "named-primary"
    config_path = tmp_path / "openclaw.json"
    config = {
        "agents": {
            "defaults": {"workspace": str(primary)},
            "list": [
                {"id": "MAIN"},
                {"id": "Helper"},
            ],
        }
    }
    resolved = resolve_agent_workspaces(config, config_path, openclaw_home=home)
    assert resolved == {
        "main": primary.resolve(),
        "helper": (primary / "helper").resolve(),
    }


def test_normalize_surrounding_whitespace_agent_id(tmp_path: Path) -> None:
    home = tmp_path / "openclaw-home"
    primary = tmp_path / "named-primary"
    config_path = tmp_path / "openclaw.json"
    config = {
        "agents": {
            "defaults": {"workspace": str(primary)},
            "list": [
                {"id": "  coder  ", "default": True},
                {"id": "\thelper\n"},
            ],
        }
    }
    resolved = resolve_agent_workspaces(config, config_path, openclaw_home=home)
    assert resolved == {
        "coder": primary.resolve(),
        "helper": (primary / "helper").resolve(),
    }


def test_normalize_invalid_characters_are_hyphenated(tmp_path: Path) -> None:
    home = tmp_path / "openclaw-home"
    config_path = tmp_path / "elsewhere" / "openclaw.json"
    config_path.parent.mkdir()
    config = {
        "agents": {
            "list": [
                {"id": "main"},
                {"id": "ops/team"},
                {"id": "my agent"},
            ]
        }
    }
    resolved = resolve_agent_workspaces(config, config_path, openclaw_home=home)
    assert resolved["main"] == (home / "workspace").resolve()
    assert resolved["ops-team"] == (home / "workspace-ops-team").resolve()
    assert resolved["my-agent"] == (home / "workspace-my-agent").resolve()
    assert set(resolved) == {"main", "ops-team", "my-agent"}


def test_normalize_collisions_use_first_entry_workspace(tmp_path: Path) -> None:
    home = tmp_path / "openclaw-home"
    first_ws = tmp_path / "first-ws"
    second_ws = tmp_path / "second-ws"
    config_path = tmp_path / "openclaw.json"
    config = {
        "agents": {
            "list": [
                {"id": "Coder", "workspace": str(first_ws)},
                {"id": "coder", "workspace": str(second_ws)},
            ]
        }
    }
    resolved = resolve_agent_workspaces(config, config_path, openclaw_home=home)
    assert resolved == {"coder": first_ws.resolve()}


def test_normalize_later_colliding_default_keeps_first_entry_workspace(
    tmp_path: Path,
) -> None:
    home = tmp_path / "openclaw-home"
    primary = tmp_path / "named-primary"
    first_ws = tmp_path / "first-ws"
    second_ws = tmp_path / "second-ws"
    config_path = tmp_path / "openclaw.json"
    config = {
        "agents": {
            "defaults": {"workspace": str(primary)},
            "list": [
                {"id": "Coder", "workspace": str(first_ws)},
                {"id": "coder", "default": True, "workspace": str(second_ws)},
            ],
        }
    }
    resolved = resolve_agent_workspaces(config, config_path, openclaw_home=home)
    assert resolved == {"coder": first_ws.resolve()}


def test_normalize_default_selection(tmp_path: Path) -> None:
    home = tmp_path / "openclaw-home"
    primary = tmp_path / "named-primary"
    config_path = tmp_path / "openclaw.json"
    config = {
        "agents": {
            "defaults": {"workspace": str(primary)},
            "list": [
                {"id": "Helper"},
                {"id": "CODER", "default": True},
            ],
        }
    }
    resolved = resolve_agent_workspaces(config, config_path, openclaw_home=home)
    assert resolved["coder"] == primary.resolve()
    assert resolved["helper"] == (primary / "helper").resolve()


def test_normalize_allow_agents_matches_case_and_sanitization(
    tmp_path: Path,
) -> None:
    _home, config_path, primary = _openclaw_home(
        tmp_path,
        agents=[{"id": "Coder"}, {"id": "ops-team"}],
        allow_agents=["CODER", "  ops/team  "],
        defaults_allow_agents=["coder"],
    )
    _seed_workspace(primary, setup_completed=True)
    report = collect_findings(load_openclaw_config(config_path), config_path)
    assert "dangling-agent-reference" not in _finding_ids(report)


def test_normalize_truly_absent_reference_is_dangling(tmp_path: Path) -> None:
    _home, config_path, primary = _openclaw_home(
        tmp_path,
        agents=[{"id": "MAIN"}],
        allow_agents=["Ghost!", "ghost", "missing-helper"],
    )
    _seed_workspace(primary, setup_completed=True)
    report = collect_findings(load_openclaw_config(config_path), config_path)
    dangling = [f for f in report.findings if f.check_id == "dangling-agent-reference"]
    assert {f.agent_id for f in dangling} == {"main"}
    assert {f.message for f in dangling} == {
        "subagents.allowAgents names 'Ghost!' which is absent from agents.list",
        "subagents.allowAgents names 'missing-helper' which is absent from agents.list",
    }


def test_normalize_agent_id_truncated_to_64_chars(tmp_path: Path) -> None:
    home = tmp_path / "openclaw-home"
    config_path = tmp_path / "elsewhere" / "openclaw.json"
    config_path.parent.mkdir()
    long_id = "a" * 70
    truncated = "a" * 64
    config = {"agents": {"list": [{"id": "main"}, {"id": long_id}]}}
    resolved = resolve_agent_workspaces(config, config_path, openclaw_home=home)
    assert truncated in resolved
    assert resolved[truncated] == (home / f"workspace-{truncated}").resolve()
    assert long_id not in resolved


def test_normalize_all_invalid_agent_id_falls_back_to_main(tmp_path: Path) -> None:
    home = tmp_path / "openclaw-home"
    primary = tmp_path / "named-primary"
    config_path = tmp_path / "openclaw.json"
    config = {
        "agents": {
            "defaults": {"workspace": str(primary)},
            "list": [{"id": "@@@"}],
        }
    }
    resolved = resolve_agent_workspaces(config, config_path, openclaw_home=home)
    assert resolved == {"main": primary.resolve()}


def test_normalize_unicode_casefold_outliers_match_javascript_ascii_regex(
    tmp_path: Path,
) -> None:
    home = tmp_path / "openclaw-home"
    config_path = tmp_path / "openclaw.json"
    config = {
        "agents": {
            "list": [
                {"id": "main"},
                {"id": "İ"},
                {"id": "ı"},
                {"id": "ſ"},
                {"id": "K"},
            ]
        }
    }
    resolved = resolve_agent_workspaces(config, config_path, openclaw_home=home)
    assert set(resolved) == {"main", "i", "k"}
    assert resolved["i"] == (home / "workspace-i").resolve()
    assert resolved["k"] == (home / "workspace-k").resolve()


def test_normalize_unicode_collision_keeps_first_ascii_equivalent(
    tmp_path: Path,
) -> None:
    first_ws = tmp_path / "first-ws"
    second_ws = tmp_path / "second-ws"
    config = {
        "agents": {
            "list": [
                {"id": "i", "workspace": str(first_ws)},
                {"id": "İ", "workspace": str(second_ws)},
            ]
        }
    }
    resolved = resolve_agent_workspaces(
        config, tmp_path / "openclaw.json", openclaw_home=tmp_path / "home"
    )
    assert resolved == {"i": first_ws.resolve()}


def test_normalize_unicode_allow_agents_matches_runtime_ids(tmp_path: Path) -> None:
    _home, config_path, primary = _openclaw_home(
        tmp_path,
        agents=[{"id": "i"}, {"id": "main"}],
        allow_agents=["İ", "ı", "ſ"],
    )
    _seed_workspace(primary, setup_completed=True)
    report = collect_findings(load_openclaw_config(config_path), config_path)
    dangling = [f for f in report.findings if f.check_id == "dangling-agent-reference"]
    assert dangling == []


def test_normalize_finding_agent_values(tmp_path: Path) -> None:
    first_ws = tmp_path / "first-ws"
    second_ws = tmp_path / "second-ws"
    _home, config_path, _primary = _openclaw_home(
        tmp_path,
        agents=[
            {"id": "Coder", "workspace": str(first_ws)},
            {"id": "coder", "default": True, "workspace": str(second_ws)},
        ],
    )
    _seed_workspace(first_ws, setup_completed=True, identity=STOCK_IDENTITY)
    _seed_workspace(second_ws, setup_completed=True, identity=FILLED_IDENTITY)
    report = collect_findings(load_openclaw_config(config_path), config_path)
    placeholders = [
        f for f in report.findings if f.check_id == "configured-placeholder"
    ]
    assert [f.path for f in placeholders] == [first_ws / "IDENTITY.md"]
    assert placeholders[0].agent_id == "coder"


def test_orphan_sibling_roots_at_later_explicit_default_workspace(
    tmp_path: Path,
) -> None:
    home = tmp_path / "openclaw-home"
    home.mkdir()
    default_ws = tmp_path / "elsewhere" / "coder-ws"
    orphan = default_ws.parent / "workspace-ghost"
    config_path = home / "openclaw.json"
    config = {
        "agents": {
            "list": [
                {"id": "helper"},
                {
                    "id": "coder",
                    "default": True,
                    "workspace": str(default_ws),
                },
            ]
        }
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")
    _seed_workspace(home / "workspace-helper", setup_completed=True)
    _seed_workspace(default_ws, setup_completed=True)
    _seed_workspace(orphan, bootstrap=True, identity=None, user=None)
    report = collect_findings(
        load_openclaw_config(config_path),
        config_path,
        openclaw_home=home,
    )
    orphans = [f for f in report.findings if f.check_id == "orphan-workspace"]
    assert [f.path for f in orphans] == [orphan / "BOOTSTRAP.md"]


# Structural validation ---------------------------------------------------


INVALID_AGENT_SHAPES = (
    {"agents": "nope"},
    {"agents": {"defaults": []}},
    {"agents": {"list": {}}},
    {"agents": {"list": ["main"]}},
    {"agents": {"list": [{}]}},
    {"agents": {"list": [{"id": ""}]}},
    {"agents": {"list": [{"id": 1}]}},
    {"agents": {"defaults": {"workspace": 123}}},
    {"agents": {"defaults": {"workspace": ""}}},
    {"agents": {"list": [{"id": "main", "workspace": ""}]}},
    {"agents": {"list": [{"id": "main", "workspace": 1}]}},
    {"agents": {"list": [{"id": "main", "default": "yes"}]}},
    {"agents": {"defaults": {"subagents": []}}},
    {"agents": {"list": [{"id": "main", "subagents": "nope"}]}},
    {"agents": {"defaults": {"subagents": {"allowAgents": "main"}}}},
    {"agents": {"list": [{"id": "main", "subagents": {"allowAgents": [""]}}]}},
    {"agents": {"list": [{"id": "main", "subagents": {"allowAgents": ["   "]}}]}},
    {"agents": {"list": [{"id": "main", "subagents": {"allowAgents": [1]}}]}},
)

VALID_ABSENT_AGENT_BLOCKS = (
    {},
    {"agents": {}},
    {"agents": {"defaults": {}}},
    {"agents": {"list": []}},
    {"agents": {"defaults": {}, "list": []}},
    {"agents": {"list": [{"id": "main"}]}},
    {"agents": {"defaults": {"workspace": "ws"}, "list": [{"id": "main"}]}},
    {
        "agents": {
            "defaults": {"subagents": {"allowAgents": ["*"]}},
            "list": [{"id": "main", "default": False}],
        }
    },
)


@pytest.mark.parametrize("payload", INVALID_AGENT_SHAPES)
def test_invalid_agent_shapes_raise_lint_error(
    tmp_path: Path, payload: dict, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "openclaw.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(LintError):
        load_openclaw_config(path)
    with pytest.raises(LintError):
        collect_findings(payload, path)
    assert run(_cfg(tmp_path), openclaw_config=path) == 2
    captured = capsys.readouterr()
    assert captured.out or captured.err


@pytest.mark.parametrize("payload", VALID_ABSENT_AGENT_BLOCKS)
def test_valid_absent_optional_agent_blocks_are_accepted(
    tmp_path: Path, payload: dict
) -> None:
    path = tmp_path / "openclaw.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    config = load_openclaw_config(path)
    report = collect_findings(config, path, openclaw_home=tmp_path / "openclaw-home")
    assert isinstance(report, LintReport)


def test_collect_findings_does_not_hide_malformed_agents(tmp_path: Path) -> None:
    path = tmp_path / "openclaw.json"
    payload = {"agents": {"list": [{"workspace": "/tmp/ws"}]}}
    with pytest.raises(LintError):
        collect_findings(payload, path)
    with pytest.raises(LintError):
        resolve_agent_workspaces(payload, path)


# Ordering, rendering, exits ----------------------------------------------


def test_findings_sort_error_then_id_then_path_then_agent(tmp_path: Path) -> None:
    home, config_path, primary = _openclaw_home(
        tmp_path,
        agents=[
            {"id": "main"},
            {"id": "beta", "workspace": str(tmp_path / "openclaw-home" / "workspace-beta")},
        ],
        allow_agents=["missing-helper"],
    )
    _seed_workspace(
        primary,
        bootstrap=True,
        setup_completed=True,
        identity=STOCK_IDENTITY,
        extra={"SOUL.md": "# SOUL.md\n\nKeep the lakehouse secrets off chat logs.\n"},
    )
    _seed_workspace(home / "workspace-beta", setup_completed=True)
    orphan = primary.parent / "workspace-ghost"
    _seed_workspace(orphan, bootstrap=True, identity=None, user=None)
    report = collect_findings(
        load_openclaw_config(config_path),
        config_path,
        tracked_files=("AGENTS.md", "IDENTITY.md", "USER.md"),
    )
    keys = [
        (
            0 if f.severity == "error" else 1,
            f.check_id,
            str(f.path),
            f.agent_id or "",
        )
        for f in report.findings
    ]
    assert keys == sorted(keys)
    assert _ids_in_order(report)[0] == "bootstrap-after-setup"
    assert "orphan-workspace" in _ids_in_order(report)
    assert keys[0][0] == 0
    assert keys[-1][0] == 1


def test_render_json_shape_and_paths(tmp_path: Path) -> None:
    _home, config_path, primary = _openclaw_home(
        tmp_path,
        agents=[{"id": "main"}],
        allow_agents=["missing-helper"],
    )
    _seed_workspace(
        primary,
        bootstrap=True,
        setup_completed=True,
        identity=STOCK_IDENTITY,
    )
    orphan = primary.parent / "workspace-ghost"
    _seed_workspace(orphan, bootstrap=True, identity=None, user=None)
    report = collect_findings(load_openclaw_config(config_path), config_path)
    data = json.loads(render_json(report))
    assert set(data) >= {"ok", "findings", "error_count", "warning_count"}
    assert data["ok"] is False
    assert data["error_count"] == report.error_count
    assert data["warning_count"] == report.warning_count
    assert data["error_count"] >= 1
    required = {"check_id", "severity", "message", "path", "agent_id"}
    assert data["findings"]
    for row, finding in zip(data["findings"], report.findings, strict=True):
        assert required <= set(row)
        assert isinstance(row["path"], str)
        assert row["check_id"] == finding.check_id
        assert Path(row["path"]) == finding.path
        assert row["agent_id"] == finding.agent_id
        assert row["agent_id"] is None or isinstance(row["agent_id"], str)
    assert any(row["agent_id"] is None for row in data["findings"])
    assert any(isinstance(row["agent_id"], str) for row in data["findings"])


def test_render_text_includes_ids_paths_and_summary(tmp_path: Path) -> None:
    _home, config_path, primary = _openclaw_home(
        tmp_path,
        agents=[{"id": "main"}],
        allow_agents=["missing-helper"],
    )
    _seed_workspace(
        primary,
        bootstrap=True,
        setup_completed=True,
        identity=STOCK_IDENTITY,
    )
    report = collect_findings(load_openclaw_config(config_path), config_path)
    text = render_text(report)
    for finding in report.findings:
        assert finding.check_id in text
        assert finding.severity in text
        assert str(finding.path) in text
    assert str(report.error_count) in text
    assert str(report.warning_count) in text
    first = report.findings[0].check_id
    last = report.findings[-1].check_id
    assert text.index(first) < text.index(last)


def test_run_exit_codes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _home, config_path, primary = _openclaw_home(
        tmp_path, agents=[{"id": "main"}]
    )
    _seed_workspace(primary, setup_completed=True)
    cfg = _cfg(tmp_path)
    assert run(cfg, openclaw_config=config_path) == 0
    capsys.readouterr()

    _write(primary / "SOUL.md", "# SOUL.md\n\nBe careful with lakehouse credentials.\n")
    warning_code = run(
        _cfg(tmp_path, tracked=("AGENTS.md", "IDENTITY.md", "USER.md")),
        openclaw_config=config_path,
        as_json=True,
    )
    warning_out = json.loads(capsys.readouterr().out)
    assert warning_code == 1
    assert warning_out["warning_count"] >= 1
    assert warning_out["error_count"] == 0

    _write(primary / "BOOTSTRAP.md", BOOTSTRAP_BODY)
    _write(
        primary / "openclaw-workspace-state.json",
        json.dumps({"setupCompletedAt": "2026-02-17T10:34:20.551Z"}),
    )
    error_code = run(cfg, openclaw_config=config_path, as_json=True)
    error_out = json.loads(capsys.readouterr().out)
    assert error_code == 2
    assert error_out["error_count"] >= 1


def test_run_unreadable_config_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _cfg(tmp_path)
    missing = tmp_path / "missing-openclaw.json"
    assert run(cfg, openclaw_config=missing) == 2

    bad = tmp_path / "openclaw.json"
    bad.write_text("{not json", encoding="utf-8")
    assert run(cfg, openclaw_config=bad) == 2
    captured = capsys.readouterr()
    assert captured.out or captured.err


def test_load_openclaw_config_rejects_non_object(tmp_path: Path) -> None:
    path = tmp_path / "openclaw.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(Exception, match="JSON object"):
        load_openclaw_config(path)
