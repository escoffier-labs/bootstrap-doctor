"""Independent synthetic materialization of the frozen v1 shared corpus."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
CORPUS = json.loads((FIXTURES / "codex-context-handoff-v1.json").read_text())
TEMPLATE = json.loads((FIXTURES / "codex-context-handoff-export-v1.json").read_text())


def digest(value):
    body = {k: v for k, v in value.items() if k != "digest"}
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ).hexdigest()
    )


def identity(path):
    st = path.lstat()
    kind = next(
        name
        for pred, name in [
            (stat.S_ISDIR, "directory"),
            (stat.S_ISREG, "regular"),
            (stat.S_ISFIFO, "fifo"),
        ]
        if pred(st.st_mode)
    )
    result = {"device": st.st_dev, "inode": st.st_ino, "kind": kind}
    if kind != "directory":
        result.update(
            size_bytes=st.st_size, mtime_ns=st.st_mtime_ns, ctime_ns=st.st_ctime_ns
        )
    return result


def observation(repo, directory, name):
    path = repo / directory / name
    ident = identity(path) if path.exists() else None
    return {
        "directory": directory,
        "name": name,
        "state": ident["kind"] if ident else "absent",
        "identity": ident,
    }


def materialize(tmp_path, vector=None):
    vector = vector or CORPUS["vectors"][0]
    repo = tmp_path / "repo"
    repo.mkdir()
    for name in vector["directories"]:
        (repo / name).mkdir(parents=True, exist_ok=True)
    for name in vector["marker_directories"]:
        (repo / name / ".git").mkdir(parents=True, exist_ok=True)
    for name, raw in vector["files"].items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes.fromhex(raw))
    paths = ["."]
    if vector["cwd"] != ".":
        parts = vector["cwd"].split("/")
        paths += ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
    handoff = copy.deepcopy(TEMPLATE["expected_export"])
    handoff["producer"]["version"] = "1.2.3"
    handoff["repository"] = {
        "location_sha256": hashlib.sha256(
            b"brigade.context_handoff.repository.v1\0" + str(repo).encode()
        ).hexdigest(),
        "identity": identity(repo),
    }
    handoff["cwd"] = vector["cwd"]
    handoff["directories"] = [
        {"path": path, "identity": identity(repo / path)} for path in paths
    ]
    for key in ("project_doc_max_bytes", "fallback_filenames", "root_markers"):
        handoff["settings"][key] = {"value": vector[key], "source": "caller-supplied"}
    markers = []
    start = len(paths) - 1
    if vector["root_markers"]:
        found = False
        for start in range(len(paths) - 1, -1, -1):
            for name in vector["root_markers"]:
                item = observation(repo, paths[start], name)
                markers.append(item)
                if item["state"] != "absent":
                    found = True
                    break
            if found:
                break
        assert found
    handoff["root_marker_observations"] = markers
    selections = []
    for path in paths[start:]:
        for name in ["AGENTS.override.md", "AGENTS.md", *vector["fallback_filenames"]]:
            item = observation(repo, path, name)
            selections.append(item)
            if item["state"] == "regular":
                break
    handoff["selection_observations"] = selections
    handoff["sources"] = [
        {**row, "identity": identity(repo / row["path"])}
        for row in vector["expected_sources"]
    ]
    handoff["baseline"] = copy.deepcopy(vector["expected_baseline"])
    path = tmp_path / "handoff.json"
    save(path, handoff)
    return repo, path, handoff


def save(path, handoff):
    handoff["digest"] = digest(handoff)
    path.write_text(json.dumps(handoff, ensure_ascii=False), encoding="utf-8")


def validate(repo, path):
    from bootstrap_doctor.handoff import validate_handoff

    return validate_handoff(repo=repo, handoff=path)


def refused(result, reason=None):
    assert set(result) == {
        "schema",
        "schema_version",
        "actual_session_observed",
        "reason",
    }
    assert result["schema"] == "bootstrap_doctor.handoff_validation.error"
    assert result["schema_version"] == 1
    assert result["actual_session_observed"] is False
    if reason:
        assert result["reason"] == reason


def cli(*args):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "bootstrap_doctor.cli",
            "validate-handoff",
            *map(str, args),
        ],
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
        timeout=10,
    )


def test_cli_validates_before_any_config_resolution(tmp_path):
    repo, path, _ = materialize(tmp_path)
    result = cli("--repo", repo, "--handoff", path, "--json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "valid"
    assert result.stderr == ""


@pytest.mark.parametrize("vector", CORPUS["vectors"], ids=lambda v: v["id"])
def test_shared_accounting_vectors(tmp_path, vector):
    repo, path, _ = materialize(tmp_path, vector)
    result = validate(repo, path)
    assert result["status"] == "valid"
    assert result["actual_session_observed"] is False
    assert result["baseline"] == vector["expected_baseline"]
    assert [
        {k: v for k, v in row.items() if k != "identity"} for row in result["sources"]
    ] == vector["expected_sources"]


@pytest.mark.parametrize("vector", CORPUS["canonical_digest_vectors"])
def test_shared_canonical_vectors(vector):
    from bootstrap_doctor.handoff import canonical_bytes, integrity_digest

    assert canonical_bytes(vector["value"]).hex() == vector["canonical_utf8_hex"]
    assert integrity_digest(vector["value"]) == vector["digest"]
    assert (
        integrity_digest({**vector["value"], "digest": "ignored"}) == vector["digest"]
    )
    assert integrity_digest({"nested": {"digest": "bound"}}) != integrity_digest(
        {"nested": {"digest": "changed"}}
    )


def test_export_template_materialized_without_producer_import(tmp_path):
    vector = {
        "files": TEMPLATE["setup"]["files"],
        "directories": TEMPLATE["setup"]["directories"],
        "marker_directories": ["."],
        "cwd": "sub",
        "project_doc_max_bytes": 4,
        "fallback_filenames": [],
        "root_markers": [".git"],
        "expected_sources": [
            {k: v for k, v in row.items() if k != "identity"}
            for row in TEMPLATE["expected_export"]["sources"]
        ],
        "expected_baseline": TEMPLATE["expected_export"]["baseline"],
    }
    repo, path, handoff = materialize(tmp_path, vector)
    expected = copy.deepcopy(TEMPLATE["expected_export"])
    expected["producer"]["version"] = "1.2.3"
    expected["repository"] = handoff["repository"]
    for row in expected["directories"]:
        row["identity"] = identity(repo / row["path"])
    for key in ("root_marker_observations", "selection_observations"):
        for row in expected[key]:
            row["identity"] = observation(repo, row["directory"], row["name"])[
                "identity"
            ]
    for row in expected["sources"]:
        row["identity"] = identity(repo / row["path"])
    expected["digest"] = digest(expected)
    assert handoff == expected
    assert validate(repo, path)["status"] == "valid"
    path.write_text(json.dumps(TEMPLATE))
    refused(validate(repo, path), "invalid_handoff")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda h: h.update(extra=True),
        lambda h: h.update(schema_version=True),
        lambda h: h.update(schema_version=2),
        lambda h: h.update(actual_session_observed=True),
        lambda h: h["producer"].update(name="other"),
        lambda h: h["producer"].update(version="${producer_version}"),
        lambda h: h["consumer"].update(extra=True),
        lambda h: h["completeness"].update(source_hashes=False),
        lambda h: h["completeness"].update(selection=1),
        lambda h: h["limitations"].reverse(),
        lambda h: h["sources"][0].update(selection_order=True),
        lambda h: h["sources"][0].update(consumed_raw_bytes=4),
        lambda h: h["sources"][0].update(rendered_utf8_bytes=4),
        lambda h: h["sources"][0].update(cumulative_selected_bytes=4),
        lambda h: h["sources"][0].update(contribution="empty"),
        lambda h: h["sources"][0].update(sha256="0" * 64),
        lambda h: h["baseline"].update(selected_bytes=4),
        lambda h: h["settings"]["trust"].update(source="default-assumption"),
        lambda h: h["settings"]["project_doc_max_bytes"].update(
            source="default-assumption"
        ),
        lambda h: h["repository"]["identity"].update(extra=1),
        lambda h: h["sources"][0]["identity"].update(size_bytes=6),
        lambda h: h["sources"][0]["identity"].update(inode=True),
    ],
)
def test_recomputed_digest_cannot_authorize_inconsistent_envelope(tmp_path, mutation):
    repo, path, handoff = materialize(tmp_path)
    mutation(handoff)
    save(path, handoff)
    refused(validate(repo, path))


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("project_doc_max_bytes", 0, "inspection_disabled"),
        ("project_doc_max_bytes", None, "invalid_handoff"),
        ("project_doc_max_bytes", True, "invalid_handoff"),
        ("project_doc_max_bytes", 1048577, "resource_limit"),
        ("trust", "untrusted", "inspection_disabled"),
        ("trust", "unknown", "invalid_handoff"),
        ("read_access", "restricted", "inspection_disabled"),
        ("environment_order", ["discovered"], "invalid_handoff"),
        ("fallback_filenames", ["x"] * 65, "resource_limit"),
        ("root_markers", [".git"] * 65, "resource_limit"),
        ("fallback_filenames", [" x"], "invalid_handoff"),
        ("root_markers", ["a/b"], "invalid_handoff"),
        ("fallback_filenames", ["x\\y"], "invalid_handoff"),
    ],
)
def test_settings_are_explicit_bounded_and_enabled(tmp_path, field, value, reason):
    repo, path, handoff = materialize(tmp_path)
    handoff["settings"][field]["value"] = value
    save(path, handoff)
    refused(validate(repo, path), reason)


def test_default_provenance_and_unset_trust(tmp_path):
    vector = copy.deepcopy(CORPUS["vectors"][0])
    vector["project_doc_max_bytes"] = 32768
    repo, path, handoff = materialize(tmp_path, vector)
    for field in ("project_doc_max_bytes", "fallback_filenames", "root_markers"):
        handoff["settings"][field]["source"] = "default-assumption"
    handoff["settings"]["trust"]["value"] = "unset"
    save(path, handoff)
    result = validate(repo, path)
    assert result["status"] == "valid"
    assert result["project_doc_max_bytes"] == {
        "value": 32768,
        "source": "default-assumption",
    }


@pytest.mark.parametrize(
    "raw",
    [
        b"{}{}",
        b"\xef\xbb\xbf{}",
        b'{"a":1,"a":2}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":1.0}',
        b'{"x":-1}',
        b'{"x":18446744073709551616}',
        b'{"x":"\\ud800"}',
        b"\xff",
    ],
)
def test_strict_json_refuses_ambiguous_values(tmp_path, raw):
    repo, path, _ = materialize(tmp_path)
    path.write_bytes(raw)
    refused(validate(repo, path), "invalid_handoff")


def test_digest_required_and_transport_whitespace_irrelevant(tmp_path):
    repo, path, handoff = materialize(tmp_path)
    path.write_text(" \n" + json.dumps(handoff, indent=4) + "\n ")
    assert validate(repo, path)["status"] == "valid"
    handoff["digest"] = "sha256:" + "0" * 64
    path.write_text(json.dumps(handoff))
    refused(validate(repo, path), "invalid_handoff")


@pytest.mark.parametrize("changed", ["repo", "cwd", "worktree", "identity"])
def test_repository_and_directory_binding(tmp_path, changed):
    repo, path, handoff = materialize(tmp_path)
    if changed == "repo":
        other = tmp_path / "clone"
        other.mkdir()
        refused(validate(other, path), "inspection_changed")
    elif changed == "cwd":
        handoff["cwd"] = "sub"
        save(path, handoff)
        refused(validate(repo, path), "invalid_handoff")
    elif changed == "identity":
        handoff["repository"]["identity"]["inode"] += 1
        save(path, handoff)
        refused(validate(repo, path), "inspection_changed")
    else:
        repo.rename(tmp_path / "old")
        repo.mkdir()
        refused(validate(repo, path), "inspection_changed")


@pytest.mark.parametrize(
    "path_value",
    [
        "/escape",
        "../escape",
        "a/../b",
        "a//b",
        "a/./b",
        "a\\b",
        "a:b",
        "a\x00b",
        "a\x85b",
        "",
    ],
)
def test_unsafe_relative_paths(tmp_path, path_value):
    repo, path, handoff = materialize(tmp_path)
    handoff["sources"][0]["path"] = path_value
    save(path, handoff)
    refused(validate(repo, path), "unsafe_path")


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "extra",
        "reorder",
        "winner",
        "source",
        "marker",
        "no_marker",
        "empty_dir",
    ],
)
def test_declared_trace_must_be_complete_and_ordered(tmp_path, mutation):
    repo, path, handoff = materialize(tmp_path, CORPUS["vectors"][1])
    if mutation == "missing":
        handoff["selection_observations"].pop(0)
    elif mutation == "extra":
        handoff["selection_observations"].append(
            copy.deepcopy(handoff["selection_observations"][-1])
        )
    elif mutation == "reorder":
        handoff["selection_observations"].reverse()
    elif mutation == "winner":
        handoff["selection_observations"][1]["name"] = "other.md"
    elif mutation == "source":
        handoff["sources"].reverse()
    elif mutation == "marker":
        handoff["root_marker_observations"].reverse()
    elif mutation == "no_marker":
        handoff["root_marker_observations"].pop()
    else:
        handoff["selection_observations"] = handoff["selection_observations"][2:]
        handoff["sources"].pop(0)
    save(path, handoff)
    refused(validate(repo, path), "invalid_handoff")


@pytest.mark.parametrize(
    "mutation", ["override", "marker", "empty_winner", "source", "fifo"]
)
def test_stale_selection_and_full_source_content_refuse(tmp_path, mutation):
    repo, path, _ = materialize(tmp_path, CORPUS["vectors"][1])
    if mutation == "override":
        (repo / "AGENTS.override.md").write_bytes(b"")
    elif mutation == "marker":
        (repo / "sub/.git").mkdir()
    elif mutation == "empty_winner":
        (repo / "sub/AGENTS.md").write_bytes(b"")
    elif mutation == "fifo":
        (repo / "AGENTS.override.md").unlink(missing_ok=True)
        os.mkfifo(repo / "AGENTS.override.md")
    else:
        (repo / "sub/AGENTS.md").write_bytes(b"x" * 15)
    refused(validate(repo, path), "inspection_changed")


@pytest.mark.parametrize(
    "where",
    ["repo", "ancestor", "source", "handoff", "handoff_ancestor", "observation"],
)
def test_symlinks_are_never_followed(tmp_path, where):
    repo, path, _ = materialize(tmp_path)
    if where == "repo":
        link = tmp_path / "link"
        link.symlink_to(repo, target_is_directory=True)
        repo = link
    elif where == "ancestor":
        link = tmp_path / "link"
        link.symlink_to(tmp_path, target_is_directory=True)
        repo = link / "repo"
    elif where == "source":
        (repo / "AGENTS.md").unlink()
        (repo / "AGENTS.md").symlink_to(path)
    elif where == "observation":
        (repo / "AGENTS.override.md").symlink_to(path)
    elif where == "handoff_ancestor":
        link = tmp_path / "link"
        link.symlink_to(tmp_path, target_is_directory=True)
        path = link / "handoff.json"
    else:
        link = tmp_path / "handoff-link"
        link.symlink_to(path)
        path = link
    refused(validate(repo, path), "unsafe_path")


@pytest.mark.parametrize("kind", ["fifo", "directory"])
def test_handoff_is_bounded_regular_file(tmp_path, kind):
    repo, path, _ = materialize(tmp_path)
    path.unlink()
    if kind == "fifo":
        os.mkfifo(path)
    else:
        path.mkdir()
    refused(validate(repo, path), "unsafe_path")


def test_cli_has_no_config_judge_network_or_write_path(tmp_path, monkeypatch, capsys):
    from bootstrap_doctor import cli as cli_mod

    repo, path, _ = materialize(tmp_path)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    def forbidden(*args, **kwargs):
        raise AssertionError("config or network called")

    monkeypatch.setattr(cli_mod, "_resolve_cfg", forbidden)
    import socket

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setenv("BOOTSTRAP_DOCTOR_CONFIG", "/missing/config")
    monkeypatch.setenv("BOOTSTRAP_DOCTOR_TRACE", "1")
    assert (
        cli_mod.main(
            ["validate-handoff", "--repo", str(repo), "--handoff", str(path), "--json"]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "valid"
    assert before == {
        str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    }
    path.write_text("private invalid content")
    assert (
        cli_mod.main(
            ["validate-handoff", "--repo", str(repo), "--handoff", str(path), "--json"]
        )
        == 2
    )
    output = capsys.readouterr()
    refused(json.loads(output.out), "invalid_handoff")
    assert output.err == ""
    assert "private" not in output.out and str(tmp_path) not in output.out


@pytest.mark.parametrize(
    "failure, expected, message",
    [
        (KeyboardInterrupt, 130, "interrupted"),
        (BrokenPipeError, 3, "unexpected error in handoff validation"),
        (RuntimeError, 3, "unexpected error in handoff validation"),
    ],
)
def test_cli_handles_command_failures_without_importing_safety(
    monkeypatch, capsys, failure, expected, message
):
    import builtins

    from bootstrap_doctor import cli as cli_mod
    from bootstrap_doctor import handoff as mod

    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        assert name != "bootstrap_doctor.safety", "handoff loaded legacy safety"
        return original_import(name, *args, **kwargs)

    def fail(**kwargs):
        raise failure("private failure detail")

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(mod, "command", fail)
    monkeypatch.setenv("BOOTSTRAP_DOCTOR_TRACE", "1")
    assert (
        cli_mod.main(
            ["validate-handoff", "--repo", "/fixture", "--handoff", "/fixture.json"]
        )
        == expected
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == f"bootstrap-doctor: {message}\n"


def test_descriptor_exhaustion_refuses_opaquely_and_closes_handles(tmp_path):
    directories = ["/".join(["d"] * i) for i in range(1, 21)]
    paths = ["AGENTS.md", *(f"{p}/AGENTS.md" for p in directories)]
    vector = copy.deepcopy(CORPUS["vectors"][0])
    vector.update(
        directories=directories,
        cwd=directories[-1],
        files={p: "78" for p in paths},
        project_doc_max_bytes=len(paths),
        expected_sources=[
            {
                "path": path,
                "selection_order": i,
                "size_bytes": 1,
                "sha256": hashlib.sha256(b"x").hexdigest(),
                "cumulative_selected_bytes": i,
                "consumed_raw_bytes": 1,
                "rendered_utf8_bytes": 1,
                "contribution": "loaded",
            }
            for i, path in enumerate(paths, 1)
        ],
        expected_baseline={
            key: len(paths)
            for key in ("selected_bytes", "consumed_raw_bytes", "rendered_utf8_bytes")
        },
    )
    repo, path, _ = materialize(tmp_path, vector)
    assert validate(repo, path)["status"] == "valid"
    code = """import json, os, resource, sys
from bootstrap_doctor.handoff import validate_handoff
soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (32, hard))
def opened():
    count = 0
    for fd in range(32):
        try:
            os.fstat(fd)
        except OSError:
            continue
        count += 1
    return count
before = opened()
for _ in range(3):
    result = validate_handoff(repo=sys.argv[1], handoff=sys.argv[2])
    assert result == {"schema": "bootstrap_doctor.handoff_validation.error",
        "schema_version": 1, "actual_session_observed": False,
        "reason": "identity_not_established"}, result
    assert opened() == before, "descriptor leak after refusal"
print(json.dumps(result))
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(repo), str(path)],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    refused(json.loads(result.stdout), "identity_not_established")
    assert result.stderr == ""


def test_cli_required_arguments_and_no_unrelated_options(tmp_path):
    repo, path, _ = materialize(tmp_path)
    for args in [
        ("--repo", repo),
        ("--handoff", path),
        ("--repo", repo, "--handoff", path, "--config", "bad"),
    ]:
        assert cli(*args).returncode == 2
    human = cli("--repo", repo, "--handoff", path)
    assert human.returncode == 0
    assert (
        "5 bytes" in human.stdout and "actual_session_observed: false" in human.stdout
    )


def test_same_size_change_beyond_cap_with_coarse_metadata(tmp_path, monkeypatch):
    from bootstrap_doctor import handoff as mod

    repo, path, handoff = materialize(tmp_path, CORPUS["vectors"][1])
    source = repo / "sub/AGENTS.md"
    before = source.stat()
    source.write_bytes(b"x" * 15)
    real_stat, real_fstat = os.stat, os.fstat

    def coarse_stat(*args, **kwargs):
        info = real_stat(*args, **kwargs)
        return before if info.st_ino == before.st_ino else info

    def coarse_fstat(fd):
        info = real_fstat(fd)
        return before if info.st_ino == before.st_ino else info

    monkeypatch.setattr(mod.os, "stat", coarse_stat)
    monkeypatch.setattr(mod.os, "fstat", coarse_fstat)
    refused(validate(repo, path), "inspection_changed")
    assert handoff["sources"][1]["contribution"] == "cap_exhausted"


@pytest.mark.parametrize("phase", ["read", "final_read"])
def test_persistent_mutation_during_reads_is_refused(tmp_path, monkeypatch, phase):
    from bootstrap_doctor import handoff as mod

    repo, path, _ = materialize(tmp_path, CORPUS["vectors"][1])
    source = repo / "sub/AGENTS.md"
    before = source.stat()
    real_stat, real_fstat = os.stat, os.fstat
    real_read, real_pread = os.read, os.pread
    mutated = False

    def mutate(fd):
        nonlocal mutated
        if not mutated and real_fstat(fd).st_ino == before.st_ino:
            source.write_bytes(b"x" * 15)
            mutated = True

    def read(fd, count):
        data = real_read(fd, count)
        if phase == "read":
            mutate(fd)
        return data

    def pread(fd, count, offset):
        if phase == "final_read":
            mutate(fd)
        return real_pread(fd, count, offset)

    # Simulate coarse filesystem timestamps to require actual final hashing.
    def coarse_stat(*args, **kwargs):
        info = real_stat(*args, **kwargs)
        return before if info.st_ino == before.st_ino else info

    def coarse_fstat(fd):
        info = real_fstat(fd)
        return before if info.st_ino == before.st_ino else info

    monkeypatch.setattr(mod.os, "stat", coarse_stat)
    monkeypatch.setattr(mod.os, "fstat", coarse_fstat)
    monkeypatch.setattr(mod.os, "read", read)
    monkeypatch.setattr(mod.os, "pread", pread)
    refused(validate(repo, path), "inspection_changed")
    assert mutated


@pytest.mark.parametrize(
    "change", ["override", "marker", "directory", "source_binding", "artifact"]
)
def test_final_revalidation_catches_binding_races(tmp_path, monkeypatch, change):
    from bootstrap_doctor import handoff as mod

    repo, path, _ = materialize(tmp_path, CORPUS["vectors"][1])
    target = repo / "sub/AGENTS.md"
    target_inode = target.stat().st_ino
    real_pread, real_fstat = os.pread, os.fstat
    mutated = False

    def pread(fd, count, offset):
        nonlocal mutated
        data = real_pread(fd, count, offset)
        if not mutated and real_fstat(fd).st_ino == target_inode:
            mutated = True
            if change == "override":
                (repo / "sub/AGENTS.override.md").write_text("")
            elif change == "marker":
                (repo / "sub/.git").mkdir()
            elif change == "directory":
                (repo / "sub").rename(repo / "old")
                (repo / "sub").mkdir()
            elif change == "source_binding":
                target.rename(repo / "saved")
                target.write_bytes(b"replacement")
            else:
                path.write_bytes(b"changed artifact")
        return data

    monkeypatch.setattr(mod.os, "pread", pread)
    refused(validate(repo, path), "inspection_changed")
    assert mutated


def sized_vector(size, count=1):
    files, sources = {}, []
    raw = b"x" * size
    for i in range(count):
        name = "/".join(["sub"] * i + ["AGENTS.md"])
        files[name] = raw.hex()
        sources.append(
            {
                "path": name,
                "selection_order": i + 1,
                "size_bytes": size,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "cumulative_selected_bytes": size * (i + 1),
                "consumed_raw_bytes": size if i == 0 else 0,
                "rendered_utf8_bytes": size if i == 0 else 0,
                "contribution": "loaded" if i == 0 else "cap_exhausted",
            }
        )
    return {
        "files": files,
        "directories": ["/".join(["sub"] * i) for i in range(1, count)],
        "marker_directories": ["."],
        "cwd": "/".join(["sub"] * (count - 1)) or ".",
        "project_doc_max_bytes": size,
        "fallback_filenames": [],
        "root_markers": [".git"],
        "expected_sources": sources,
        "expected_baseline": {
            "selected_bytes": size * count,
            "consumed_raw_bytes": size,
            "rendered_utf8_bytes": size,
        },
    }


def test_source_and_total_exact_bounds_and_exhausted_file_overflow(tmp_path):
    repo, path, handoff = materialize(tmp_path, sized_vector(1048576, 8))
    assert validate(repo, path)["baseline"]["selected_bytes"] == 8388608
    last = repo / handoff["sources"][-1]["path"]
    with last.open("ab") as stream:
        stream.write(b"x")
    handoff["sources"][-1]["identity"] = identity(last)
    handoff["sources"][-1]["size_bytes"] += 1
    handoff["selection_observations"][-1]["identity"] = identity(last)
    save(path, handoff)
    refused(validate(repo, path), "resource_limit")


@pytest.mark.parametrize(
    "which", ["total", "sources", "directories", "path", "absolute_depth", "list"]
)
def test_declared_resource_ceilings_before_acquisition(tmp_path, which):
    repo, path, handoff = materialize(tmp_path)
    if which == "total":
        row = handoff["sources"][0]
        row["size_bytes"] = row["identity"]["size_bytes"] = 1048576
        handoff["sources"] = [copy.deepcopy(row) for _ in range(9)]
    elif which == "sources":
        handoff["sources"] *= 257
    elif which == "directories":
        handoff["directories"] *= 129
    elif which == "path":
        handoff["sources"][0]["path"] = "x" * 4097
    elif which == "absolute_depth":
        refused(validate("/" + "/".join(["x"] * 129), path), "resource_limit")
        return
    else:
        handoff["settings"]["fallback_filenames"]["value"] = ["missing"] * 65
    save(path, handoff)
    refused(validate(repo, path), "resource_limit")


def test_json_depth_and_raw_size_bounds(tmp_path):
    repo, path, handoff = materialize(tmp_path)
    body = json.dumps(handoff).encode()
    path.write_bytes(body + b" " * (8388608 - len(body)))
    assert validate(repo, path)["status"] == "valid"
    with path.open("ab") as stream:
        stream.write(b" ")
    refused(validate(repo, path), "resource_limit")
    path.write_bytes(b"[" * 65 + b"0" + b"]" * 65)
    refused(validate(repo, path), "resource_limit")
    path.write_bytes(b"[" * 1200 + b"0" + b"]" * 1200)
    refused(validate(repo, path), "resource_limit")


def test_canonical_boundaries_are_strict():
    from bootstrap_doctor.handoff import canonical_bytes

    for value in [1.0, -1, 2**64, "\ud800", {1: "key"}, (), object()]:
        with pytest.raises(ValueError):
            canonical_bytes(value)
    assert len(canonical_bytes("x" * (8388608 - 2))) == 8388608
    with pytest.raises(ValueError):
        canonical_bytes("x" * (8388608 - 1))
    nested = 0
    for _ in range(64):
        nested = [nested]
    canonical_bytes(nested)
    with pytest.raises(ValueError):
        canonical_bytes([nested])


def test_list_duplicates_and_nonregular_candidates_observed_without_open(tmp_path):
    repo, path, handoff = materialize(tmp_path)
    fifo = repo / "AGENTS.override.md"
    os.mkfifo(fifo)
    handoff["selection_observations"][0] = observation(repo, ".", fifo.name)
    handoff["settings"]["fallback_filenames"]["value"] = ["missing"] * 64
    handoff["settings"]["root_markers"]["value"] = ["missing", "missing", ".git"]
    handoff["root_marker_observations"] = [
        observation(repo, ".", name) for name in ["missing", "missing", ".git"]
    ]
    save(path, handoff)
    result = cli("--repo", repo, "--handoff", path, "--json")
    assert result.returncode == 0, result.stdout
    fifo.unlink()
    fifo.write_text("new winner")
    refused(validate(repo, path), "inspection_changed")


@pytest.mark.parametrize("failure", ["open", "stat", "read", "pread"])
def test_permission_and_metadata_failures_are_opaque(tmp_path, monkeypatch, failure):
    from bootstrap_doctor import handoff as mod

    repo, path, _ = materialize(tmp_path)

    def denied(*args, **kwargs):
        raise PermissionError("private path content")

    monkeypatch.setattr(mod.os, failure, denied)
    result = validate(repo, path)
    refused(result, "identity_not_established")
    assert "private" not in json.dumps(result)


def test_unsupported_platform_never_attempts_a_read(tmp_path, monkeypatch):
    from bootstrap_doctor import handoff as mod

    repo, path, _ = materialize(tmp_path)
    monkeypatch.setattr(mod, "_POSIX_SUPPORTED", False)
    refused(validate(repo, path), "unsupported_platform")


def test_json_utf8_output_ignores_ascii_stdio(tmp_path):
    vector = copy.deepcopy(CORPUS["vectors"][10])
    vector["files"] = {"é.md": "6162"}
    vector["fallback_filenames"] = ["é.md"]
    vector["expected_sources"][0]["path"] = "é.md"
    repo, path, _ = materialize(tmp_path, vector)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "bootstrap_doctor.cli",
            "validate-handoff",
            "--repo",
            str(repo),
            "--handoff",
            str(path),
            "--json",
        ],
        env={
            **os.environ,
            "PYTHONIOENCODING": "ascii",
            "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
        },
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert b"\xc3\xa9.md" in result.stdout
    assert result.stderr == b""


def test_human_utf8_output_ignores_ascii_stdio(tmp_path):
    vector = copy.deepcopy(CORPUS["vectors"][10])
    vector["files"] = {"é.md": "6162"}
    vector["fallback_filenames"] = ["é.md"]
    vector["expected_sources"][0]["path"] = "é.md"
    repo, path, _ = materialize(tmp_path, vector)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "bootstrap_doctor.cli",
            "validate-handoff",
            "--repo",
            str(repo),
            "--handoff",
            str(path),
        ],
        env={
            **os.environ,
            "PYTHONIOENCODING": "ascii",
            "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
        },
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert b"\xc3\xa9.md" in result.stdout
    assert result.stderr == b""


def test_absolute_normalization_and_relative_artifact(tmp_path, monkeypatch):
    repo, path, _ = materialize(tmp_path)
    assert validate("/" + str(repo) + "//", path)["status"] == "valid"
    monkeypatch.chdir(tmp_path)
    assert validate(repo, "handoff.json")["status"] == "valid"
    refused(validate("repo", "handoff.json"), "unsafe_path")
    for bad in (
        str(repo) + "/.",
        str(repo) + "/../repo",
        str(repo) + ":x",
        str(repo) + "\x1f",
    ):
        refused(validate(bad, path), "unsafe_path")


@pytest.mark.parametrize("field", ["version", "source_commit"])
def test_pinned_codex_contract_refuses_other_versions(tmp_path, field):
    repo, path, h = materialize(tmp_path)
    h["consumer"][field] = "unsupported"
    save(path, h)
    refused(validate(repo, path), "unsupported_version")


@pytest.mark.parametrize("delete_pread", [True, False], ids=["missing", "non-callable"])
def test_missing_pread_is_unsupported_before_file_access(tmp_path, delete_pread):
    repo, path, _ = materialize(tmp_path)
    code = "import os, json\nos.pread = None\n"
    if delete_pread:
        code += "del os.pread\n"
    code += """
from bootstrap_doctor.handoff import validate_handoff
print(json.dumps(validate_handoff(repo='/does-not-exist', handoff='/also-missing')))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
        timeout=10,
    )
    assert result.returncode == 0
    refused(json.loads(result.stdout), "unsupported_platform")
    assert result.stderr == ""


def test_transport_allows_one_framing_lf_outside_canonical_limit(tmp_path, monkeypatch):
    from bootstrap_doctor import handoff as mod

    repo, path, handoff = materialize(tmp_path)
    compact = json.dumps(
        handoff, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    monkeypatch.setattr(mod, "MAX_ENVELOPE_BYTES", len(compact))
    path.write_bytes(compact + b"\n")
    assert validate(repo, path)["status"] == "valid"
    for suffix in (b" ", b"\n ", b"\n\n"):
        path.write_bytes(compact + suffix)
        refused(validate(repo, path), "resource_limit")
    monkeypatch.setattr(mod, "MAX_ENVELOPE_BYTES", len(compact) - 1)
    path.write_bytes(compact)
    refused(validate(repo, path), "resource_limit")


def test_validation_has_no_mutation_discovery_or_optional_imports(tmp_path):
    repo, path, _ = materialize(tmp_path)
    # The hook observes product activation after interpreter import setup.
    # argparse lazily imports stdlib modules while constructing its parser.
    code = """import json, os, sys
from bootstrap_doctor.cli import build_parser, main
import bootstrap_doctor.handoff
build_parser()
mutations = {"os.mkdir", "os.remove", "os.rename", "os.rmdir", "os.symlink", "os.link", "os.chmod", "os.truncate"}
def audit(event, args):
    if event == "open":
        flags = args[2]
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            raise AssertionError("unexpected filesystem write")
    if event in mutations or event in {"os.listdir", "os.scandir"} or event.startswith("socket."):
        raise AssertionError("unexpected discovery or side effect")
    if event == "import" and args[0].startswith(("brigade", "requests", "bootstrap_doctor.paths", "bootstrap_doctor.judge", "bootstrap_doctor.trim")):
        raise AssertionError("unexpected optional import")
sys.addaudithook(audit)
raise SystemExit(main(sys.argv[1:]))
"""
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            code,
            "validate-handoff",
            "--repo",
            str(repo),
            "--handoff",
            str(path),
            "--json",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "valid"
    assert result.stderr == ""
