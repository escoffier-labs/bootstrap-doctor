"""Validate explicit v1 local Codex accounting artifacts without discovery.

Only declared observations are checked. All source bytes, including exhausted
rows, are hashed twice through held descriptors. No session or atomic snapshot
claim follows from a successful validation.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from contextlib import ExitStack
from hashlib import sha256
from pathlib import Path
from typing import Any

MAX_SOURCE_BYTES = 1_048_576
MAX_TOTAL_BYTES = 8_388_608
MAX_SOURCES = 256
MAX_DIRECTORIES = 128
MAX_LIST = 64
MAX_PATH_BYTES = 4096
MAX_ABSOLUTE_DEPTH = 128
MAX_ENVELOPE_BYTES = 8_388_608
MAX_CANONICAL_DEPTH = 64
SOURCE_COMMIT = "a956835d020762cb2b570053af06f643a11c0ecc"
RUST_WHITESPACE = "\t\n\v\f\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
LIMITATIONS = [
    "fresh_disk_only",
    "no_atomic_snapshot",
    "timestamp_granularity_limits_change_detection",
    "access_times_may_change",
    "thread_and_session_state_unknown",
    "global_not_requested",
    "profile_project_system_managed_launch_overrides_unresolved",
    "integrity_digest_not_authorization",
    "under_cap_not_behavior_guarantee",
]
_NATIVE_OPEN, _NATIVE_STAT = os.open, os.stat
_POSIX_SUPPORTED = (
    os.name == "posix"
    and {_NATIVE_OPEN, _NATIVE_STAT} <= os.supports_dir_fd
    and _NATIVE_STAT in os.supports_follow_symlinks
    and all(hasattr(os, flag) for flag in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK"))
    and callable(getattr(os, "pread", None))
)
_JSON = dict[str, Any]


class _Refusal(Exception):
    def __init__(self, reason: str):
        self.reason = reason


def _require(condition: bool, reason: str = "invalid_handoff") -> None:
    if not condition:
        raise _Refusal(reason)


def _strict_value(value: Any, depth: int = 0) -> None:
    if depth > MAX_CANONICAL_DEPTH:
        raise _Refusal("resource_limit")
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        _require(0 <= value <= 2**64 - 1)
    elif isinstance(value, str):
        value.encode("utf-8", errors="strict")
    elif isinstance(value, list):
        for item in value:
            _strict_value(item, depth + 1)
    elif isinstance(value, dict):
        for key, item in value.items():
            _require(isinstance(key, str))
            _strict_value(key, depth + 1)
            _strict_value(item, depth + 1)
    else:
        raise _Refusal("invalid_handoff")


def canonical_bytes(value: Any) -> bytes:
    """Return strict v1 canonical JSON. Invalid values raise ValueError."""
    try:
        _strict_value(value)
        raw = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        _require(len(raw) <= MAX_ENVELOPE_BYTES, "resource_limit")
        return raw
    except (_Refusal, UnicodeError, RecursionError) as exc:
        raise ValueError("invalid canonical JSON") from exc


def integrity_digest(value: _JSON) -> str:
    """Bind all fields except the top-level digest. This is not authorization."""
    return (
        "sha256:"
        + sha256(
            canonical_bytes({k: v for k, v in value.items() if k != "digest"})
        ).hexdigest()
    )


def _keys(value: Any, names: str) -> _JSON:
    _require(isinstance(value, dict) and set(value) == set(names.split()))
    return value


def _uint(value: Any) -> int:
    _require(type(value) is int and 0 <= value <= 2**64 - 1)
    return value


def _exact(value: Any, expected: Any) -> None:
    # JSON equality distinguishes false/0 and true/1, unlike Python equality.
    _require(canonical_bytes(value) == canonical_bytes(expected))


def _hex(value: Any) -> None:
    _require(
        isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None
    )


def _path_text(value: Any, *, basename: bool = False) -> str:
    reason = "invalid_handoff" if basename else "unsafe_path"
    _require(isinstance(value, str), reason)
    raw = value.encode("utf-8", errors="strict")
    _require(len(raw) <= MAX_PATH_BYTES, "resource_limit")
    _require(
        not any(ch in "\\:" or ord(ch) < 32 or 127 <= ord(ch) <= 159 for ch in value),
        reason,
    )
    if basename:
        _require(
            bool(value)
            and value not in (".", "..")
            and "/" not in value
            and value == value.strip(RUST_WHITESPACE),
            reason,
        )
    return value


def _relative(value: Any, *, directory: bool = False) -> str:
    text = _path_text(value)
    if directory and text == ".":
        return text
    _require(
        all(part not in ("", ".", "..") for part in text.split("/")), "unsafe_path"
    )
    return text


def _absolute(value: str | Path, *, required: bool = True) -> str:
    text = os.fspath(value)
    _path_text(text)
    _require(not required or text.startswith("/"), "unsafe_path")
    _require(all(part not in (".", "..") for part in text.split("/")), "unsafe_path")
    if not text.startswith("/"):
        _require(bool(text), "unsafe_path")
        text = os.getcwd().rstrip("/") + "/" + text
    text = "/" + "/".join(part for part in text.split("/") if part)
    _path_text(text)
    _require(
        len([p for p in text.split("/") if p]) <= MAX_ABSOLUTE_DEPTH, "resource_limit"
    )
    return text


def _identity_schema(value: Any, kind: str) -> None:
    _require(kind in {"regular", "directory", "fifo", "socket", "character", "block"})
    names = "device inode kind" + (
        " size_bytes mtime_ns ctime_ns" if kind != "directory" else ""
    )
    obj = _keys(value, names)
    _require(obj["kind"] == kind)
    for key in obj:
        if key != "kind":
            _uint(obj[key])


def _settings(settings: Any) -> _JSON:
    obj = _keys(
        settings,
        "project_doc_max_bytes fallback_filenames root_markers trust read_access environment_order",
    )
    defaults = {
        "project_doc_max_bytes": 32768,
        "fallback_filenames": [],
        "root_markers": [".git"],
    }
    for name, default in defaults.items():
        item = _keys(obj[name], "value source")
        _require(item["source"] in ("caller-supplied", "default-assumption"))
        if item["source"] == "default-assumption":
            _exact(item["value"], default)
        value = item["value"]
        if name == "project_doc_max_bytes":
            _uint(value)
            _require(value <= MAX_SOURCE_BYTES, "resource_limit")
        else:
            _require(isinstance(value, list))
            _require(len(value) <= MAX_LIST, "resource_limit")
            for filename in value:
                _path_text(filename, basename=True)
    for name in ("trust", "read_access", "environment_order"):
        _keys(obj[name], "value source")
        _require(obj[name]["source"] == "caller-supplied")
    _require(obj["trust"]["value"] in ("trusted", "unset", "untrusted"))
    _require(obj["read_access"]["value"] in ("full", "restricted"))
    _exact(obj["environment_order"]["value"], ["explicit-cwd"])
    _require(
        obj["project_doc_max_bytes"]["value"] != 0
        and obj["trust"]["value"] != "untrusted"
        and obj["read_access"]["value"] != "restricted",
        "inspection_disabled",
    )
    return obj


def _observations_schema(value: Any, max_count: int) -> None:
    _require(isinstance(value, list))
    _require(len(value) <= max_count, "resource_limit")
    for row in value:
        _keys(row, "directory name state identity")
        _relative(row["directory"], directory=True)
        _path_text(row["name"], basename=True)
        state = row["state"]
        if state == "absent":
            _require(row["identity"] is None)
        else:
            _require(isinstance(state, str))
            _identity_schema(row["identity"], state)


def _schema(value: Any) -> _JSON:
    h = _keys(
        value,
        "schema schema_version producer consumer actual_session_observed repository cwd settings directories root_marker_observations selection_observations sources baseline completeness limitations digest",
    )
    _exact(h["schema"], "brigade.context_handoff")
    _exact(h["schema_version"], 1)
    _exact(h["actual_session_observed"], False)
    producer = _keys(h["producer"], "name version")
    _require(producer["name"] == "brigade")
    version = producer["version"]
    _require(
        isinstance(version, str)
        and len(version) <= 128
        and re.fullmatch(
            r"[0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+|\.post[0-9]+|\.dev[0-9]+)?(?:\+[a-zA-Z0-9]+(?:[.-][a-zA-Z0-9]+)*)?",
            version,
        )
        is not None
    )
    consumer = _keys(h["consumer"], "name version version_source source_commit")
    _require(
        consumer["version"] == "0.160.0" and consumer["source_commit"] == SOURCE_COMMIT,
        "unsupported_version",
    )
    _exact(
        consumer,
        {
            "name": "codex",
            "version": "0.160.0",
            "version_source": "caller-supplied",
            "source_commit": SOURCE_COMMIT,
        },
    )
    repository = _keys(h["repository"], "location_sha256 identity")
    _hex(repository["location_sha256"])
    _identity_schema(repository["identity"], "directory")
    cwd = _relative(h["cwd"], directory=True)
    _settings(h["settings"])
    dirs = h["directories"]
    _require(isinstance(dirs, list))
    _require(len(dirs) <= MAX_DIRECTORIES, "resource_limit")
    parts = [] if cwd == "." else cwd.split("/")
    expected = [".", *["/".join(parts[:i]) for i in range(1, len(parts) + 1)]]
    _require(len(expected) <= MAX_DIRECTORIES, "resource_limit")
    for row in dirs:
        _keys(row, "path identity")
        _relative(row["path"], directory=True)
        _identity_schema(row["identity"], "directory")
    _require([row["path"] for row in dirs] == expected)
    _observations_schema(h["root_marker_observations"], MAX_DIRECTORIES * MAX_LIST)
    _observations_schema(h["selection_observations"], MAX_DIRECTORIES * (MAX_LIST + 2))
    sources = h["sources"]
    _require(isinstance(sources, list))
    _require(len(sources) <= MAX_SOURCES, "resource_limit")
    total = 0
    for row in sources:
        _keys(
            row,
            "path selection_order identity size_bytes sha256 cumulative_selected_bytes consumed_raw_bytes rendered_utf8_bytes contribution",
        )
        _relative(row["path"])
        _identity_schema(row["identity"], "regular")
        _hex(row["sha256"])
        for key in (
            "selection_order",
            "size_bytes",
            "cumulative_selected_bytes",
            "consumed_raw_bytes",
            "rendered_utf8_bytes",
        ):
            _uint(row[key])
        total += row["size_bytes"]
        _require(
            row["size_bytes"] <= MAX_SOURCE_BYTES and total <= MAX_TOTAL_BYTES,
            "resource_limit",
        )
        _require(row["size_bytes"] == row["identity"]["size_bytes"])
        _require(
            row["contribution"] in ("empty", "loaded", "truncated", "cap_exhausted")
        )
    baseline = _keys(
        h["baseline"], "selected_bytes consumed_raw_bytes rendered_utf8_bytes"
    )
    for value in baseline.values():
        _uint(value)
    _exact(
        h["completeness"],
        {
            "inspection": "complete",
            "source_hashes": True,
            "selection": True,
            "directories": True,
            "root_markers": True,
        },
    )
    _exact(h["limitations"], LIMITATIONS)
    _require(
        isinstance(h["digest"], str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", h["digest"]) is not None
    )
    _require(h["digest"] == integrity_digest(h))
    return h


def _trace(h: _JSON) -> list[tuple[str, str, _JSON]]:
    """Validate declared ordering without discovering alternative candidates."""
    directories = [row["path"] for row in h["directories"]]
    markers = h["settings"]["root_markers"]["value"]
    observations = iter(h["root_marker_observations"])
    boundary = len(directories) - 1
    if markers:
        found = False
        for index in range(len(directories) - 1, -1, -1):
            for name in markers:
                row = next(observations, None)
                if row is None:
                    raise _Refusal("invalid_handoff")
                _require((row["directory"], row["name"]) == (directories[index], name))
                if row["state"] != "absent":
                    boundary, found = index, True
                    break
            if found:
                break
        _require(found)
    _require(next(observations, None) is None)
    candidates = [
        "AGENTS.override.md",
        "AGENTS.md",
        *h["settings"]["fallback_filenames"]["value"],
    ]
    observations = iter(h["selection_observations"])
    winners = []
    for directory in directories[boundary:]:
        for name in candidates:
            row = next(observations, None)
            if row is None:
                raise _Refusal("invalid_handoff")
            _require((row["directory"], row["name"]) == (directory, name))
            if row["state"] == "regular":
                winners.append((directory, name, row["identity"]))
                break
    _require(next(observations, None) is None)
    _require(len(winners) == len(h["sources"]))
    for i, ((directory, name, ident), source) in enumerate(
        zip(winners, h["sources"], strict=True), 1
    ):
        path = name if directory == "." else f"{directory}/{name}"
        _require(
            source["path"] == path
            and source["identity"] == ident
            and source["selection_order"] == i
        )
    return winners


def _identity(info: os.stat_result) -> _JSON:
    kind = next(
        (
            name
            for predicate, name in (
                (stat.S_ISREG, "regular"),
                (stat.S_ISDIR, "directory"),
                (stat.S_ISFIFO, "fifo"),
                (stat.S_ISSOCK, "socket"),
                (stat.S_ISCHR, "character"),
                (stat.S_ISBLK, "block"),
            )
            if predicate(info.st_mode)
        ),
        None,
    )
    _require(kind is not None, "unsafe_path")
    result = {"device": info.st_dev, "inode": info.st_ino, "kind": kind}
    if kind != "directory":
        result.update(
            size_bytes=info.st_size,
            mtime_ns=info.st_mtime_ns,
            ctime_ns=info.st_ctime_ns,
        )
    _require(
        all(
            type(v) is int and 0 <= v <= 2**64 - 1
            for k, v in result.items()
            if k != "kind"
        ),
        "identity_not_established",
    )
    return result


def _metadata(parent: int | None, name: str) -> _JSON | None:
    try:
        return _identity(os.stat(name, dir_fd=parent, follow_symlinks=False))
    except FileNotFoundError:
        return None


class _Files:
    """Own bounded descriptors and their lexical bindings for one validation."""

    def __init__(self, stack: ExitStack):
        self.stack = stack
        self.bindings: list[tuple[int | None, str, int, _JSON]] = []
        self.observations: list[tuple[int, str, _JSON | None]] = []
        self.sources: list[tuple[int, int, str, _JSON, str]] = []

    def directory(self, parent: int | None, name: str) -> int:
        before = _metadata(parent, name)
        if before is None:
            raise _Refusal("identity_not_established")
        _require(before["kind"] == "directory", "identity_not_established")
        fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent,
        )
        self.stack.callback(os.close, fd)
        _require(_identity(os.fstat(fd)) == before, "inspection_changed")
        self.bindings.append((parent, name, fd, before))
        return fd

    def absolute_directory(self, path: str) -> int:
        fd = self.directory(None, "/")
        for name in path.split("/")[1:]:
            if name:
                fd = self.directory(fd, name)
        return fd

    def file(
        self, parent: int, name: str, expected: _JSON | None = None
    ) -> tuple[int, _JSON]:
        before = _metadata(parent, name)
        if before is None:
            raise _Refusal("identity_not_established")
        _require(before["kind"] == "regular", "unsafe_path")
        if expected is not None:
            _require(before == expected, "inspection_changed")
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        self.stack.callback(os.close, fd)
        _require(_identity(os.fstat(fd)) == before, "inspection_changed")
        return fd, before

    def check_file(self, fd: int, parent: int, name: str, identity: _JSON) -> None:
        _require(
            _identity(os.fstat(fd)) == identity and _metadata(parent, name) == identity,
            "inspection_changed",
        )

    def observe(self, parent: int, name: str, expected: _JSON | None) -> None:
        _require(_metadata(parent, name) == expected, "inspection_changed")
        self.observations.append((parent, name, expected))

    def revalidate(self) -> None:
        for parent, name, fd, before in self.bindings:
            _require(
                _identity(os.fstat(fd)) == before and _metadata(parent, name) == before,
                "inspection_changed",
            )
        for parent, name, observed in self.observations:
            _require(_metadata(parent, name) == observed, "inspection_changed")
        for fd, parent, name, before, _ in self.sources:
            self.check_file(fd, parent, name, before)

    def finish(self) -> None:
        self.revalidate()
        for fd, parent, name, before, expected_hash in self.sources:
            raw = _read(fd, before["size_bytes"], positional=True)
            _require(
                len(raw) == before["size_bytes"]
                and sha256(raw).hexdigest() == expected_hash,
                "inspection_changed",
            )
            self.check_file(fd, parent, name, before)
        self.revalidate()


def _read(fd: int, limit: int, *, positional: bool = False) -> bytes:
    chunks = []
    offset = 0
    while offset <= limit:
        count = min(65536, limit + 1 - offset)
        chunk = os.pread(fd, count, offset) if positional else os.read(fd, count)
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
    return b"".join(chunks)


def _unique_object(pairs: list[tuple[str, Any]]) -> _JSON:
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _invalid_number(value: str) -> Any:
    raise _Refusal("invalid_handoff")


def _load(files: _Files, path: str) -> _JSON:
    parent = files.absolute_directory(os.path.dirname(path))
    name = os.path.basename(path)
    fd, before = files.file(parent, name)
    transport_limit = MAX_ENVELOPE_BYTES + 1
    _require(before["size_bytes"] <= transport_limit, "resource_limit")
    raw = _read(fd, transport_limit)
    _require(len(raw) <= transport_limit, "resource_limit")
    _require(len(raw) <= MAX_ENVELOPE_BYTES or raw.endswith(b"\n"), "resource_limit")
    files.check_file(fd, parent, name, before)
    _require(len(raw) == before["size_bytes"], "inspection_changed")
    # JSON transport is bounded separately from canonical UTF-8 serialization.
    value = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_invalid_number,
        parse_float=_invalid_number,
    )
    _strict_value(value)
    try:
        canonical_bytes(value)
    except ValueError as exc:
        if isinstance(exc.__cause__, _Refusal):
            raise exc.__cause__ from None
        raise
    files.sources.append((fd, parent, name, before, sha256(raw).hexdigest()))
    return _schema(value)


def _account(
    h: _JSON,
    files: _Files,
    directories: dict[str, int],
    winners: list[tuple[str, str, _JSON]],
) -> None:
    remaining = h["settings"]["project_doc_max_bytes"]["value"]
    cumulative = raw_total = rendered_total = 0
    for source, (directory, name, identity) in zip(h["sources"], winners, strict=True):
        parent = directories[directory]
        fd, before = files.file(parent, name, identity)
        limit = min(MAX_SOURCE_BYTES, MAX_TOTAL_BYTES - cumulative)
        raw = _read(fd, limit)
        _require(len(raw) <= limit, "resource_limit")
        files.check_file(fd, parent, name, before)
        _require(len(raw) == before["size_bytes"], "inspection_changed")
        _require(sha256(raw).hexdigest() == source["sha256"], "inspection_changed")
        files.sources.append((fd, parent, name, before, source["sha256"]))
        cumulative += len(raw)
        consumed = rendered = 0
        if remaining == 0:
            contribution = "cap_exhausted"
        else:
            prefix = raw[:remaining]
            text = prefix.decode("utf-8", errors="replace")
            if not text.strip(RUST_WHITESPACE):
                contribution = "empty"
            else:
                consumed, rendered = len(prefix), len(text.encode("utf-8"))
                contribution = "truncated" if len(raw) > remaining else "loaded"
        _require(
            (
                source["cumulative_selected_bytes"],
                source["consumed_raw_bytes"],
                source["rendered_utf8_bytes"],
                source["contribution"],
            )
            == (cumulative, consumed, rendered, contribution)
        )
        remaining -= consumed
        raw_total += consumed
        rendered_total += rendered
    _require(
        h["baseline"]
        == {
            "selected_bytes": cumulative,
            "consumed_raw_bytes": raw_total,
            "rendered_utf8_bytes": rendered_total,
        }
    )


def _error(reason: str) -> _JSON:
    return {
        "schema": "bootstrap_doctor.handoff_validation.error",
        "schema_version": 1,
        "actual_session_observed": False,
        "reason": reason,
    }


def validate_handoff(*, repo: str | Path, handoff: str | Path) -> _JSON:
    """Validate an explicit local v1 artifact or return an opaque refusal.

    Both paths are required. ``repo`` must be absolute. No config is resolved,
    no alternative chain is selected, and no writes or network calls occur.
    """
    try:
        _require(_POSIX_SUPPORTED, "unsupported_platform")
        root, artifact = _absolute(repo), _absolute(handoff, required=False)
        with ExitStack() as stack:
            files = _Files(stack)
            h = _load(files, artifact)
            winners = _trace(h)
            working = root.rstrip("/") + ("/" + h["cwd"] if h["cwd"] != "." else "")
            _absolute(working or "/")
            fd = files.absolute_directory(root)
            _require(
                sha256(
                    b"brigade.context_handoff.repository.v1\0" + root.encode("utf-8")
                ).hexdigest()
                == h["repository"]["location_sha256"],
                "inspection_changed",
            )
            _require(
                _identity(os.fstat(fd)) == h["repository"]["identity"],
                "inspection_changed",
            )
            directories = {".": fd}
            for row in h["directories"]:
                path = row["path"]
                if path != ".":
                    parent, _, name = path.rpartition("/")
                    fd = files.directory(directories[parent or "."], name)
                    directories[path] = fd
                _require(
                    _identity(os.fstat(fd)) == row["identity"], "inspection_changed"
                )
            for row in [*h["root_marker_observations"], *h["selection_observations"]]:
                files.observe(
                    directories[row["directory"]], row["name"], row["identity"]
                )
            _account(h, files, directories, winners)
            files.finish()
            return {
                "schema": "bootstrap_doctor.handoff_validation",
                "schema_version": 1,
                "status": "valid",
                "actual_session_observed": False,
                "consumer": h["consumer"],
                "producer": h["producer"],
                "project_doc_max_bytes": h["settings"]["project_doc_max_bytes"],
                "baseline": h["baseline"],
                "sources": h["sources"],
                "limitations": h["limitations"],
            }
    except _Refusal as exc:
        return _error(exc.reason)
    except RecursionError:
        return _error("resource_limit")
    except (UnicodeError, ValueError, TypeError, OverflowError):
        return _error("invalid_handoff")
    except (NotImplementedError, AttributeError):
        return _error("unsupported_platform")
    except OSError:
        return _error("identity_not_established")


def command(*, repo: str, handoff: str, as_json: bool) -> int:
    """Print only validated accounting or an opaque refusal, never raw errors."""
    result = validate_handoff(repo=repo, handoff=handoff)
    if as_json:
        text = canonical_bytes(result).decode("utf-8")
    elif "reason" in result:
        text = f"handoff refused: {result['reason']}"
    else:
        lines = ["Handoff accounting validated (fresh disk only)"]
        cap = result["project_doc_max_bytes"]
        lines.append(
            f"project_doc_max_bytes: {cap['value']} bytes ({cap['value'] / 1024:.3f} KiB), {cap['source']}"
        )
        for name, count in result["baseline"].items():
            lines.append(f"{name}: {count} bytes ({count / 1024:.3f} KiB)")
        for source in result["sources"]:
            lines.append(
                f"{source['path']}: {source['size_bytes']} selected bytes, {source['consumed_raw_bytes']} consumed raw bytes, {source['rendered_utf8_bytes']} rendered UTF-8 bytes, {source['contribution']}"
            )
        lines.extend(
            [
                "actual_session_observed: false",
                "No atomic snapshot, session equivalence or behavior guarantee.",
            ]
        )
        text = "\n".join(lines)
    # Avoid locale transcoding of already validated UTF-8, in both output modes.
    if hasattr(sys.stdout, "buffer"):
        sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    else:
        sys.stdout.write(text + "\n")
    return 2 if "reason" in result else 0
