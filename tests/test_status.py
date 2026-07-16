"""Tests for the read-only `status` verb (size + limit reporting)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bootstrap_doctor.paths import Config, resolve_config
from bootstrap_doctor.safety import UnsafeTargetError
from bootstrap_doctor.status import (
    FileStatus,
    collect,
    collect_totals,
    render_json,
    render_text,
    run,
)

# Helpers -----------------------------------------------------------------


def make_cfg(
    tmp_path: Path,
    *,
    soft: int = 10000,
    hard: int = 11500,
    total: int = 60000,
    tracked: tuple[str, ...] = ("A.md",),
    named: tuple[str, ...] = (),
) -> Config:
    """Build a Config rooted in tmp_path with given tracked files / named workspaces."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    cards = workspace / "memory" / "cards"
    cards.mkdir(parents=True, exist_ok=True)
    cache = tmp_path / "cache"
    # Build a config.toml so we can drive tracked_files + named_workspaces
    # (those knobs are config-file-only in v1).
    toml_path = tmp_path / "config.toml"
    tracked_list = ", ".join(f'"{name}"' for name in tracked)
    named_list = ", ".join(f'"{name}"' for name in named)
    toml_path.write_text(
        f"""
workspace_dir = "{workspace}"
cards_dir = "{cards}"
soft_limit = {soft}
hard_limit = {hard}
total_limit = {total}
tracked_files = [{tracked_list}]
named_workspaces = [{named_list}]

[cache]
dir = "{cache}"
"""
    )
    return resolve_config(config_file=str(toml_path))


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


# FileStatus dataclass ----------------------------------------------------


def test_file_status_is_frozen_dataclass():
    fs = FileStatus(
        path=Path("/x/A.md"),
        workspace_label="workspace",
        name="A.md",
        exists=True,
        bytes=10,
        chars=10,
        lines=1,
        soft_remaining=90,
        hard_remaining=190,
        severity="ok",
    )
    with pytest.raises(Exception):
        fs.chars = 99  # type: ignore[misc]


# collect() ---------------------------------------------------------------


def test_collect_single_file_ok(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, soft=1000, hard=1500, tracked=("A.md",))
    _write(cfg.workspace_dir / "A.md", "hello\n")
    rows = collect(cfg)
    assert len(rows) == 1
    row = rows[0]
    assert row.name == "A.md"
    assert row.workspace_label == "workspace"
    assert row.exists is True
    assert row.bytes == 6
    assert row.chars == 5
    assert row.lines == 1
    assert row.soft_remaining == 1000 - 5
    assert row.hard_remaining == 1500 - 5
    assert row.severity == "ok"


def test_collect_uses_openclaw_trimmed_utf16_character_count(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, soft=4, hard=10, tracked=("A.md",))
    _write(cfg.workspace_dir / "A.md", "A\U0001f600B  \n")
    row = collect(cfg)[0]
    assert row.bytes == len("A\U0001f600B  \n".encode())
    assert row.chars == 4
    assert row.severity == "soft"


def test_collect_preserves_crlf_for_openclaw_character_count(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, soft=10, hard=20, tracked=("A.md",))
    (cfg.workspace_dir / "A.md").write_bytes(b"a\r\nb")
    row = collect(cfg)[0]
    assert row.bytes == 4
    assert row.chars == 4


@pytest.mark.parametrize(
    ("suffix", "expected"),
    [("\ufeff", 3), ("\u0085", 4), ("\u001c", 4)],
)
def test_collect_uses_ecmascript_trim_end_whitespace_set(
    tmp_path: Path, suffix: str, expected: int
) -> None:
    cfg = make_cfg(tmp_path, soft=10, hard=20, tracked=("A.md",))
    _write(cfg.workspace_dir / "A.md", "abc" + suffix)
    assert collect(cfg)[0].chars == expected


def test_collect_soft_range(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, soft=100, hard=200, tracked=("A.md",))
    _write(cfg.workspace_dir / "A.md", "x" * 150)
    rows = collect(cfg)
    assert rows[0].severity == "soft"
    assert rows[0].chars == 150
    assert rows[0].soft_remaining == -50
    assert rows[0].hard_remaining == 50


def test_collect_at_soft_boundary_is_soft(tmp_path: Path) -> None:
    # chars == soft_limit -> soft (soft is >= soft_limit)
    cfg = make_cfg(tmp_path, soft=100, hard=200, tracked=("A.md",))
    _write(cfg.workspace_dir / "A.md", "x" * 100)
    rows = collect(cfg)
    assert rows[0].severity == "soft"


def test_collect_just_under_soft_is_ok(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, soft=100, hard=200, tracked=("A.md",))
    _write(cfg.workspace_dir / "A.md", "x" * 99)
    rows = collect(cfg)
    assert rows[0].severity == "ok"


def test_collect_at_hard_boundary_is_soft(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, soft=100, hard=200, tracked=("A.md",))
    _write(cfg.workspace_dir / "A.md", "x" * 200)
    rows = collect(cfg)
    assert rows[0].severity == "soft"


def test_collect_over_hard(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, soft=100, hard=200, tracked=("A.md",))
    _write(cfg.workspace_dir / "A.md", "x" * 250)
    rows = collect(cfg)
    assert rows[0].severity == "hard"
    assert rows[0].hard_remaining == -50


def test_collect_missing_file(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, soft=100, hard=200, tracked=("A.md",))
    # No file written.
    rows = collect(cfg)
    assert len(rows) == 1
    row = rows[0]
    assert row.exists is False
    assert row.severity == "missing"
    assert row.bytes == 0
    assert row.chars == 0
    assert row.lines == 0
    # Deltas == limits exactly.
    assert row.soft_remaining == cfg.soft_limit
    assert row.hard_remaining == cfg.hard_limit


@pytest.mark.parametrize(
    "name",
    [
        "SOUL.md",
        "IDENTITY.md",
        "USER.md",
        "HEARTBEAT.md",
        "BOOTSTRAP.md",
        "MEMORY.md",
    ],
)
def test_collect_missing_openclaw_optional_file_is_not_an_error(
    tmp_path: Path, name: str
) -> None:
    cfg = make_cfg(tmp_path, soft=100, hard=200, tracked=(name,))
    row = collect(cfg)[0]
    assert row.exists is False
    assert row.severity == "optional"


def test_collect_fifo_returns_promptly_as_unreadable(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, soft=100, hard=200, tracked=("A.md",))
    os.mkfifo(cfg.workspace_dir / "A.md")
    source_root = Path(__file__).parents[1] / "src"
    script = (
        "import sys; "
        "from bootstrap_doctor.paths import resolve_config; "
        "from bootstrap_doctor.status import run; "
        "raise SystemExit(run(resolve_config(config_file=sys.argv[1])))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "config.toml")],
        capture_output=True,
        text=True,
        timeout=2,
        env={**os.environ, "PYTHONPATH": str(source_root)},
        check=False,
    )
    assert completed.returncode == 2
    assert "UNREAD" in completed.stdout


def test_collect_rejects_final_symlink_swap_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_cfg(tmp_path, soft=100, hard=200, tracked=("A.md",))
    tracked = _write(cfg.workspace_dir / "A.md", "inside")
    outside = _write(tmp_path / "outside.md", "outside secret")
    from bootstrap_doctor import status as status_mod

    original_ensure = status_mod.ensure_within
    swapped = False

    def ensure_then_swap(base: Path, candidate: Path) -> Path:
        nonlocal swapped
        resolved = original_ensure(base, candidate)
        if candidate == tracked and not swapped:
            tracked.unlink()
            tracked.symlink_to(outside)
            swapped = True
        return resolved

    monkeypatch.setattr(status_mod, "ensure_within", ensure_then_swap)
    row = collect(cfg)[0]
    assert row.severity == "unreadable"
    assert row.chars == 0


def test_collect_rejects_multichunk_overwrite_with_replayed_mtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_cfg(
        tmp_path,
        soft=3_000_000,
        hard=4_000_000,
        tracked=("A.md",),
    )
    tracked = cfg.workspace_dir / "A.md"
    chunk_size = 1024 * 1024
    tracked.write_bytes(b"A" * (chunk_size * 2))
    original = tracked.stat()
    from bootstrap_doctor import status as status_mod

    real_read = status_mod.os.read
    read_count = 0

    def overwrite_after_first_chunk(fd: int, count: int) -> bytes:
        nonlocal read_count
        chunk = real_read(fd, count)
        read_count += 1
        if read_count == 1:
            writer = os.open(tracked, os.O_WRONLY)
            try:
                os.pwrite(writer, b"B" * original.st_size, 0)
            finally:
                os.close(writer)
            os.utime(
                tracked,
                ns=(original.st_atime_ns, original.st_mtime_ns),
            )
            changed = tracked.stat()
            assert changed.st_ino == original.st_ino
            assert changed.st_size == original.st_size
            assert changed.st_mtime_ns == original.st_mtime_ns
            assert changed.st_ctime_ns != original.st_ctime_ns
        return chunk

    monkeypatch.setattr(status_mod.os, "read", overwrite_after_first_chunk)

    row = collect(cfg)[0]

    assert read_count >= 3
    assert row.severity == "unreadable"
    assert row.chars == 0


def test_collect_rejects_multichunk_parent_rename_path_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_cfg(
        tmp_path,
        soft=3_000_000,
        hard=4_000_000,
        tracked=("A.md",),
    )
    tracked = cfg.workspace_dir / "A.md"
    size = 2 * 1024 * 1024
    tracked.write_bytes(b"A" * size)
    original = tracked.stat()
    moved_workspace = tmp_path / "workspace-moved"
    from bootstrap_doctor import status as status_mod

    real_read = status_mod.os.read
    read_count = 0

    def rename_parent_after_first_chunk(fd: int, count: int) -> bytes:
        nonlocal read_count
        chunk = real_read(fd, count)
        read_count += 1
        if read_count == 1:
            cfg.workspace_dir.rename(moved_workspace)
            moved = (moved_workspace / "A.md").stat()
            assert moved.st_ino == original.st_ino
            assert moved.st_size == original.st_size
            assert moved.st_mtime_ns == original.st_mtime_ns
            assert moved.st_ctime_ns == original.st_ctime_ns
        return chunk

    monkeypatch.setattr(status_mod.os, "read", rename_parent_after_first_chunk)

    row = collect(cfg)[0]

    assert read_count >= 3
    assert row.severity == "unreadable"
    assert row.chars == 0


def test_collect_accepts_stable_multichunk_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_cfg(
        tmp_path,
        soft=3_000_000,
        hard=4_000_000,
        tracked=("A.md",),
    )
    tracked = cfg.workspace_dir / "A.md"
    size = 2 * 1024 * 1024 + 17
    tracked.write_bytes(b"A" * size)
    from bootstrap_doctor import status as status_mod

    real_read = status_mod.os.read
    read_count = 0

    def count_reads(fd: int, count: int) -> bytes:
        nonlocal read_count
        read_count += 1
        return real_read(fd, count)

    monkeypatch.setattr(status_mod.os, "read", count_reads)

    row = collect(cfg)[0]

    assert read_count >= 4
    assert row.severity == "ok"
    assert row.bytes == size
    assert row.chars == size


def test_collect_unreadable_file(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, soft=100, hard=200, tracked=("A.md",))
    # Invalid UTF-8 bytes -> read_text(encoding="utf-8") will raise.
    p = cfg.workspace_dir / "A.md"
    p.write_bytes(b"\xff\xfe\xff\xfe not utf-8 \xc3\x28")
    rows = collect(cfg)
    row = rows[0]
    assert row.severity == "unreadable"
    assert row.exists is True
    assert row.bytes == p.stat().st_size
    assert row.chars == 0
    assert row.lines == 0


def test_collect_line_count_no_trailing_newline(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, soft=100, hard=200, tracked=("A.md",))
    _write(cfg.workspace_dir / "A.md", "one\ntwo\nthree")  # 3 lines, no trailing \n
    rows = collect(cfg)
    assert rows[0].lines == 3


def test_collect_line_count_with_trailing_newline(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, soft=100, hard=200, tracked=("A.md",))
    _write(cfg.workspace_dir / "A.md", "one\ntwo\nthree\n")  # 3 lines + trailing \n
    rows = collect(cfg)
    assert rows[0].lines == 3


def test_collect_empty_file_is_ok(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, soft=100, hard=200, tracked=("A.md",))
    _write(cfg.workspace_dir / "A.md", "")
    rows = collect(cfg)
    row = rows[0]
    assert row.exists is True
    assert row.chars == 0
    assert row.lines == 0
    assert row.severity == "ok"


def test_collect_named_workspaces_iterated(tmp_path: Path) -> None:
    cfg = make_cfg(
        tmp_path,
        soft=100,
        hard=200,
        tracked=("A.md", "B.md"),
        named=("workspace-claude", "workspace-main"),
    )
    # Primary workspace
    _write(cfg.workspace_dir / "A.md", "x" * 10)
    _write(cfg.workspace_dir / "B.md", "x" * 150)
    # workspace-claude
    claude_dir = cfg.workspace_dir / "workspace-claude"
    claude_dir.mkdir()
    _write(claude_dir / "A.md", "x" * 250)
    _write(claude_dir / "B.md", "x" * 5)
    # workspace-main (B missing)
    main_dir = cfg.workspace_dir / "workspace-main"
    main_dir.mkdir()
    _write(main_dir / "A.md", "x" * 50)
    # B.md intentionally missing in workspace-main

    rows = collect(cfg)
    # 3 workspaces (primary + 2 named) x 2 files = 6 rows
    assert len(rows) == 6

    # Workspace order: primary first, then named in declared order.
    labels = [r.workspace_label for r in rows]
    assert labels == [
        "workspace", "workspace",
        "workspace-claude", "workspace-claude",
        "workspace-main", "workspace-main",
    ]

    # Within each workspace, tracked_files config order is preserved.
    for i in range(0, 6, 2):
        assert rows[i].name == "A.md"
        assert rows[i + 1].name == "B.md"

    # Severities
    by_key = {(r.workspace_label, r.name): r for r in rows}
    assert by_key[("workspace", "A.md")].severity == "ok"
    assert by_key[("workspace", "B.md")].severity == "soft"
    assert by_key[("workspace-claude", "A.md")].severity == "hard"
    assert by_key[("workspace-claude", "B.md")].severity == "ok"
    assert by_key[("workspace-main", "A.md")].severity == "ok"
    assert by_key[("workspace-main", "B.md")].severity == "missing"


def test_collect_missing_named_workspace_dir(tmp_path: Path) -> None:
    cfg = make_cfg(
        tmp_path,
        soft=100,
        hard=200,
        tracked=("A.md", "B.md"),
        named=("workspace-ghost",),
    )
    _write(cfg.workspace_dir / "A.md", "hi")
    _write(cfg.workspace_dir / "B.md", "hi")
    # NOTE: workspace-ghost directory never created.
    rows = collect(cfg)
    # 2 primary + 2 for the ghost workspace.
    assert len(rows) == 4
    ghost_rows = [r for r in rows if r.workspace_label == "workspace-ghost"]
    assert len(ghost_rows) == 2
    for r in ghost_rows:
        assert r.severity == "missing"
        assert r.exists is False


def test_collect_no_named_workspaces_only_primary(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, soft=100, hard=200, tracked=("A.md", "B.md"), named=())
    _write(cfg.workspace_dir / "A.md", "x")
    _write(cfg.workspace_dir / "B.md", "x")
    rows = collect(cfg)
    assert len(rows) == 2
    assert all(r.workspace_label == "workspace" for r in rows)


def test_collect_file_order_stable(tmp_path: Path) -> None:
    # tracked_files config order is preserved even if not lexicographic.
    cfg = make_cfg(tmp_path, soft=100, hard=200, tracked=("Z.md", "A.md", "M.md"))
    for name in ("A.md", "M.md", "Z.md"):
        _write(cfg.workspace_dir / name, "ok")
    rows = collect(cfg)
    assert [r.name for r in rows] == ["Z.md", "A.md", "M.md"]


def test_collect_path_is_absolute(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, soft=100, hard=200, tracked=("A.md",))
    _write(cfg.workspace_dir / "A.md", "x")
    rows = collect(cfg)
    assert rows[0].path.is_absolute()
    assert rows[0].path.name == "A.md"


def test_collect_rejects_tracked_file_symlink_escape(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, soft=100, hard=200, tracked=("A.md",))
    outside = tmp_path / "outside.md"
    outside.write_text("secret")
    (cfg.workspace_dir / "A.md").symlink_to(outside)

    with pytest.raises(UnsafeTargetError):
        collect(cfg)


def test_collect_totals_keeps_same_label_workspaces_separate(tmp_path: Path) -> None:
    cfg = make_cfg(
        tmp_path,
        soft=100,
        hard=200,
        total=500,
        tracked=("A.md",),
        named=("workspace",),
    )
    _write(cfg.workspace_dir / "A.md", "x" * 10)
    nested = cfg.workspace_dir / "workspace"
    nested.mkdir()
    _write(nested / "A.md", "x" * 20)

    totals = collect_totals(collect(cfg), cfg)
    assert [(total.path, total.chars) for total in totals] == [
        (cfg.workspace_dir, 10),
        (nested, 20),
    ]


def test_collect_totals_caps_each_file_at_openclaw_limit(tmp_path: Path) -> None:
    cfg = make_cfg(
        tmp_path,
        soft=50,
        hard=100,
        total=150,
        tracked=("A.md", "B.md"),
    )
    _write(cfg.workspace_dir / "A.md", "x" * 200)
    _write(cfg.workspace_dir / "B.md", "x" * 10)

    total = collect_totals(collect(cfg), cfg)[0]
    assert total.chars == 110
    assert total.severity == "ok"


def test_collect_totals_uses_openclaw_agents_policy_digest_length(
    tmp_path: Path,
) -> None:
    cfg = make_cfg(
        tmp_path,
        soft=17000,
        hard=20000,
        total=60000,
        tracked=("AGENTS.md",),
    )
    _write(cfg.workspace_dir / "AGENTS.md", "x" * 30000)

    rows = collect(cfg)
    total = collect_totals(rows, cfg)[0]

    assert rows[0].chars == 30000
    assert rows[0].severity == "hard"
    assert total.chars == 12116


@pytest.mark.parametrize(
    ("line", "size", "expected_injected_chars"),
    [
        ("érequiredé\n", 36500, 19185),
        ("中required中\n", 36500, 19185),
        ("securİty\n", 33500, 12116),
        ("securıty\n", 33500, 12116),
        ("ſrequiredſ\n", 36500, 12116),
        ("KrequiredK\n", 36500, 12116),
    ],
)
def test_collect_totals_matches_javascript_iu_policy_boundaries(
    tmp_path: Path, line: str, size: int, expected_injected_chars: int
) -> None:
    cfg = make_cfg(
        tmp_path,
        soft=17000,
        hard=20000,
        total=60000,
        tracked=("AGENTS.md",),
    )
    content = (line * (size // len(line) + 1))[:size]
    _write(cfg.workspace_dir / "AGENTS.md", content)

    total = collect_totals(collect(cfg), cfg)[0]

    assert total.chars == expected_injected_chars


def test_collect_totals_matches_javascript_ascii_ordered_list_prefix(
    tmp_path: Path,
) -> None:
    cfg = make_cfg(
        tmp_path,
        soft=17000,
        hard=20000,
        total=60000,
        tracked=("AGENTS.md",),
    )
    line = "١. note\n"
    size = 30000
    content = (line * (size // len(line) + 1))[:size]
    _write(cfg.workspace_dir / "AGENTS.md", content)

    total = collect_totals(collect(cfg), cfg)[0]

    assert total.chars == 12116


@pytest.mark.parametrize(
    ("name", "size", "expected_injected_chars"),
    [
        ("AGENTS.md", 19999, 19999),
        ("TOOLS.md", 30000, 20000),
    ],
)
def test_collect_totals_preserves_agents_boundary_and_tools_cap(
    tmp_path: Path, name: str, size: int, expected_injected_chars: int
) -> None:
    cfg = make_cfg(
        tmp_path,
        soft=17000,
        hard=20000,
        total=60000,
        tracked=(name,),
    )
    _write(cfg.workspace_dir / name, "x" * size)

    total = collect_totals(collect(cfg), cfg)[0]

    assert total.chars == expected_injected_chars


def test_collect_totals_counts_missing_marker_across_total_limit(
    tmp_path: Path,
) -> None:
    cfg = make_cfg(
        tmp_path,
        soft=17000,
        hard=20000,
        total=60000,
        tracked=("A.md",),
    )
    workspace = Path("/workspace/abcdefghijkl")
    rows = [
        FileStatus(
            path=workspace / name,
            workspace_label="workspace",
            name=name,
            exists=True,
            bytes=chars,
            chars=chars,
            lines=1,
            soft_remaining=17000 - chars,
            hard_remaining=20000 - chars,
            severity="soft",
        )
        for name, chars in (
            ("AGENTS.md", 20000),
            ("TOOLS.md", 20000),
            ("IDENTITY.md", 19950),
        )
    ]
    rows.insert(
        1,
        FileStatus(
            path=workspace / "SOUL.md",
            workspace_label="workspace",
            name="SOUL.md",
            exists=False,
            bytes=0,
            chars=0,
            lines=0,
            soft_remaining=17000,
            hard_remaining=20000,
            severity="optional",
        ),
    )
    marker = f"[MISSING] Expected at: {workspace / 'SOUL.md'}"
    assert len(marker.encode("utf-16-le")) // 2 == 54

    total = collect_totals(rows, cfg)[0]

    assert total.chars == 60004
    assert total.severity == "hard"


def test_collect_totals_counts_surrogateescaped_missing_marker(
    tmp_path: Path,
) -> None:
    cfg = make_cfg(
        tmp_path,
        soft=17000,
        hard=20000,
        total=60000,
        tracked=("SOUL.md",),
    )
    path = Path("/workspace/invalid-\udcff/SOUL.md")
    row = FileStatus(
        path=path,
        workspace_label="workspace",
        name="SOUL.md",
        exists=False,
        bytes=0,
        chars=0,
        lines=0,
        soft_remaining=cfg.soft_limit,
        hard_remaining=cfg.hard_limit,
        severity="optional",
    )

    total = collect_totals([row], cfg)[0]

    marker = f"[MISSING] Expected at: {path}"
    assert total.chars == len(marker)


@pytest.mark.parametrize(
    ("name", "retained_when_missing"),
    [
        ("AGENTS.md", True),
        ("SOUL.md", True),
        ("TOOLS.md", True),
        ("IDENTITY.md", True),
        ("USER.md", True),
        ("HEARTBEAT.md", True),
        ("BOOTSTRAP.md", True),
        ("MEMORY.md", False),
        ("CUSTOM.md", False),
    ],
)
def test_collect_totals_matches_openclaw_missing_marker_set_and_utf16(
    tmp_path: Path, name: str, retained_when_missing: bool
) -> None:
    cfg = make_cfg(
        tmp_path,
        soft=17000,
        hard=20000,
        total=60000,
        tracked=("A.md",),
    )
    path = Path("/workspace/\U0001f600") / name
    row = FileStatus(
        path=path,
        workspace_label="workspace",
        name=name,
        exists=False,
        bytes=0,
        chars=0,
        lines=0,
        soft_remaining=17000,
        hard_remaining=20000,
        severity="optional",
    )
    marker = f"[MISSING] Expected at: {path}"
    expected = len(marker.encode("utf-16-le")) // 2 if retained_when_missing else 0

    assert collect_totals([row], cfg)[0].chars == expected


# render_text() -----------------------------------------------------------


def test_render_text_includes_workspace_header(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, soft=100, hard=200, tracked=("A.md",))
    _write(cfg.workspace_dir / "A.md", "x")
    rows = collect(cfg)
    out = render_text(rows, cfg)
    assert "workspace" in out
    assert str(cfg.workspace_dir) in out


def test_render_text_lists_each_tracked_file(tmp_path: Path) -> None:
    cfg = make_cfg(
        tmp_path, soft=100, hard=200, tracked=("A.md", "B.md", "C.md")
    )
    for n in ("A.md", "B.md", "C.md"):
        _write(cfg.workspace_dir / n, "x")
    rows = collect(cfg)
    out = render_text(rows, cfg)
    assert "A.md" in out
    assert "B.md" in out
    assert "C.md" in out


def test_render_text_severity_flags(tmp_path: Path) -> None:
    cfg = make_cfg(
        tmp_path,
        soft=100,
        hard=200,
        tracked=("ok.md", "soft.md", "hard.md", "miss.md", "bad.md"),
    )
    _write(cfg.workspace_dir / "ok.md", "x" * 10)
    _write(cfg.workspace_dir / "soft.md", "x" * 150)
    _write(cfg.workspace_dir / "hard.md", "x" * 250)
    # miss.md intentionally absent
    (cfg.workspace_dir / "bad.md").write_bytes(b"\xff\xfe\xc3\x28")
    rows = collect(cfg)
    out = render_text(rows, cfg)
    assert "ok" in out
    assert "SOFT" in out
    assert "HARD" in out
    assert "MISSING" in out
    assert "UNREAD" in out


def test_render_text_summary_footer_counts(tmp_path: Path) -> None:
    cfg = make_cfg(
        tmp_path, soft=100, hard=200, tracked=("a.md", "b.md", "c.md", "d.md")
    )
    _write(cfg.workspace_dir / "a.md", "x" * 10)     # ok
    _write(cfg.workspace_dir / "b.md", "x" * 150)    # soft
    _write(cfg.workspace_dir / "c.md", "x" * 250)    # hard
    # d.md missing
    rows = collect(cfg)
    out = render_text(rows, cfg)
    # Footer mentions counts
    assert "4 files" in out
    assert "1 over hard" in out
    assert "1 over soft" in out
    assert "1 missing" in out


def test_render_text_warns_on_missing_named_workspace(tmp_path: Path) -> None:
    cfg = make_cfg(
        tmp_path, soft=100, hard=200, tracked=("A.md",), named=("ghost",)
    )
    _write(cfg.workspace_dir / "A.md", "x")
    rows = collect(cfg)
    out = render_text(rows, cfg)
    # Some kind of warning that ghost is missing.
    assert "ghost" in out
    # Lowercase 'warning' or 'missing' phrase, just make sure the directory
    # absence is surfaced.
    assert ("warning" in out.lower()) or ("does not exist" in out.lower())


def test_render_text_header_mentions_limits(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, soft=123, hard=456, tracked=("A.md",))
    _write(cfg.workspace_dir / "A.md", "x")
    rows = collect(cfg)
    out = render_text(rows, cfg)
    assert "123" in out
    assert "456" in out


# render_json() -----------------------------------------------------------


def test_render_json_top_level_keys(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, soft=100, hard=200, tracked=("A.md",))
    _write(cfg.workspace_dir / "A.md", "hi")
    rows = collect(cfg)
    out = render_json(rows, cfg)
    data = json.loads(out)
    assert set(data.keys()) >= {
        "soft_limit",
        "hard_limit",
        "total_limit",
        "total_soft_limit",
        "totals",
        "rows",
    }
    assert data["soft_limit"] == 100
    assert data["hard_limit"] == 200
    assert data["total_limit"] == 60000


def test_run_returns_one_at_total_soft_pressure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = make_cfg(
        tmp_path,
        soft=100,
        hard=200,
        total=200,
        tracked=("A.md", "B.md"),
    )
    _write(cfg.workspace_dir / "A.md", "x" * 90)
    _write(cfg.workspace_dir / "B.md", "x" * 80)
    code = run(cfg, as_json=True)
    data = json.loads(capsys.readouterr().out)
    assert code == 1
    assert data["total_soft_limit"] == 170
    assert data["totals"][0]["chars"] == 170
    assert data["totals"][0]["severity"] == "soft"


def test_run_returns_two_at_total_hard_pressure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = make_cfg(
        tmp_path,
        soft=100,
        hard=200,
        total=200,
        tracked=("A.md", "B.md"),
    )
    _write(cfg.workspace_dir / "A.md", "x" * 101)
    _write(cfg.workspace_dir / "B.md", "x" * 100)
    code = run(cfg, as_json=True)
    data = json.loads(capsys.readouterr().out)
    assert code == 2
    assert data["totals"][0]["chars"] == 201
    assert data["totals"][0]["severity"] == "hard"


def test_run_returns_zero_for_missing_optional_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = make_cfg(tmp_path, soft=100, hard=200, tracked=("BOOTSTRAP.md",))
    assert run(cfg) == 0
    assert "OPTIONAL" in capsys.readouterr().out


def test_render_json_row_count(tmp_path: Path) -> None:
    cfg = make_cfg(
        tmp_path,
        soft=100,
        hard=200,
        tracked=("A.md", "B.md"),
        named=("workspace-claude",),
    )
    _write(cfg.workspace_dir / "A.md", "x")
    _write(cfg.workspace_dir / "B.md", "x")
    (cfg.workspace_dir / "workspace-claude").mkdir()
    _write(cfg.workspace_dir / "workspace-claude" / "A.md", "x")
    _write(cfg.workspace_dir / "workspace-claude" / "B.md", "x")
    rows = collect(cfg)
    data = json.loads(render_json(rows, cfg))
    assert len(data["rows"]) == len(rows)
    assert len(data["rows"]) == 4


def test_render_json_paths_are_strings(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, soft=100, hard=200, tracked=("A.md",))
    _write(cfg.workspace_dir / "A.md", "x")
    rows = collect(cfg)
    data = json.loads(render_json(rows, cfg))
    for r in data["rows"]:
        assert isinstance(r["path"], str)
        assert r["path"].endswith("A.md")


def test_render_json_includes_all_fields(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, soft=100, hard=200, tracked=("A.md",))
    _write(cfg.workspace_dir / "A.md", "hi")
    rows = collect(cfg)
    data = json.loads(render_json(rows, cfg))
    row = data["rows"][0]
    expected_keys = {
        "path",
        "workspace_label",
        "name",
        "exists",
        "bytes",
        "chars",
        "lines",
        "soft_remaining",
        "hard_remaining",
        "severity",
    }
    assert set(row.keys()) == expected_keys


def test_render_json_is_parseable(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, soft=100, hard=200, tracked=("A.md",))
    _write(cfg.workspace_dir / "A.md", "x" * 50)
    rows = collect(cfg)
    out = render_json(rows, cfg)
    # Should not raise.
    json.loads(out)


# run() -------------------------------------------------------------------


def test_run_returns_zero_when_all_ok(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = make_cfg(tmp_path, soft=100, hard=200, tracked=("A.md", "B.md"))
    _write(cfg.workspace_dir / "A.md", "x" * 10)
    _write(cfg.workspace_dir / "B.md", "x" * 20)
    code = run(cfg)
    captured = capsys.readouterr()
    assert code == 0
    assert "A.md" in captured.out
    assert "B.md" in captured.out


def test_run_returns_one_when_any_soft(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = make_cfg(tmp_path, soft=100, hard=200, tracked=("A.md", "B.md"))
    _write(cfg.workspace_dir / "A.md", "x" * 10)       # ok
    _write(cfg.workspace_dir / "B.md", "x" * 150)      # soft
    code = run(cfg)
    capsys.readouterr()
    assert code == 1


def test_run_returns_two_when_any_hard(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = make_cfg(tmp_path, soft=100, hard=200, tracked=("A.md", "B.md"))
    _write(cfg.workspace_dir / "A.md", "x" * 10)
    _write(cfg.workspace_dir / "B.md", "x" * 250)
    code = run(cfg)
    capsys.readouterr()
    assert code == 2


def test_run_returns_two_when_any_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = make_cfg(tmp_path, soft=100, hard=200, tracked=("A.md", "B.md"))
    _write(cfg.workspace_dir / "A.md", "x" * 10)
    # B.md missing
    code = run(cfg)
    capsys.readouterr()
    assert code == 2


def test_run_returns_two_when_any_unreadable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = make_cfg(tmp_path, soft=100, hard=200, tracked=("A.md", "B.md"))
    _write(cfg.workspace_dir / "A.md", "x" * 10)
    (cfg.workspace_dir / "B.md").write_bytes(b"\xff\xfe\xc3\x28")
    code = run(cfg)
    capsys.readouterr()
    assert code == 2


def test_run_hard_beats_soft_in_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = make_cfg(
        tmp_path,
        soft=100,
        hard=200,
        tracked=("ok.md", "soft.md", "hard.md"),
    )
    _write(cfg.workspace_dir / "ok.md", "x" * 10)
    _write(cfg.workspace_dir / "soft.md", "x" * 150)
    _write(cfg.workspace_dir / "hard.md", "x" * 250)
    code = run(cfg)
    capsys.readouterr()
    assert code == 2


def test_run_as_json_prints_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = make_cfg(tmp_path, soft=100, hard=200, tracked=("A.md",))
    _write(cfg.workspace_dir / "A.md", "x")
    code = run(cfg, as_json=True)
    captured = capsys.readouterr()
    assert code == 0
    data = json.loads(captured.out)
    assert "rows" in data
    assert data["soft_limit"] == 100
    assert data["hard_limit"] == 200


def test_run_text_output_no_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = make_cfg(tmp_path, soft=100, hard=200, tracked=("A.md",))
    _write(cfg.workspace_dir / "A.md", "x")
    run(cfg, as_json=False)
    captured = capsys.readouterr()
    # Text output shouldn't be valid JSON (it's a table).
    with pytest.raises(json.JSONDecodeError):
        json.loads(captured.out)
