# Validate a local Codex accounting handoff

`validate-handoff` independently checks a Brigade `brigade.context_handoff`
schema version 1 artifact against fresh local filesystem bytes. The pinned
Codex accounting version is `0.160.0`, source commit
`a956835d020762cb2b570053af06f643a11c0ecc`.

```bash
bootstrap-doctor validate-handoff --repo /absolute/repository --handoff handoff.json --json
```

`--repo` must be absolute. `--handoff` names a regular local JSON file and may
be relative to the process working directory. The handoff records the intended
repository-relative cwd. Both flags are mandatory. Omit `--json` for a human
report with exact byte counts and secondary KiB values. JSON output is canonical
UTF-8 with one final newline. Validation refusals have exactly `schema`,
`schema_version`, `actual_session_observed` and an opaque `reason`, without
source bodies, raw exceptions or absolute paths. Argument syntax errors use
normal argparse diagnostics.

The stdlib-only API is:

```python
from bootstrap_doctor.handoff import validate_handoff

result = validate_handoff(repo="/absolute/repository", handoff="handoff.json")
```

Success returns `schema: "bootstrap_doctor.handoff_validation"`,
`schema_version: 1`, `status: "valid"`, producer and pinned consumer metadata,
`project_doc_max_bytes` with its provenance, verified `sources`, `baseline`
byte totals, the protocol limitations, and `actual_session_observed: false`.
Failure returns `schema: "bootstrap_doctor.handoff_validation.error"` with
`schema_version: 1`, `actual_session_observed: false`, and `reason`.

Refusal reasons are `invalid_handoff`, `unsupported_version`,
`inspection_disabled`, `unsupported_platform`, `resource_limit`, `unsafe_path`,
`identity_not_established`, or `inspection_changed`. The API returns the same
refusal object as JSON mode. Missing or unknown Python keyword arguments raise
`TypeError` before validation.

A valid handoff exits `0`, including modeled truncation or exhausted cap rows.
Refusal exits `2`. The existing OpenClaw commands retain their character units,
classifications and exit behavior. Validation resolves no configuration,
selects no replacement instruction chain, invokes no model, performs no network
access, installs nothing, and writes no instructions, cards, caches or settings.

The validator requires POSIX nofollow, nonblocking, dirfd and positional-read
support. It binds every lexical absolute ancestor, the repository's location
hash and directory identity, and the complete declared directory chain. It
checks the ordered marker and candidate trace against the supplied settings,
then checks only those declared observations. It never searches for another
winner. A new override, changed marker or rebound directory requires a new
producer inspection.

All selected sources are read and hashed in full, including empty files and
files after the cap is exhausted. Accounting comes from those same bytes:
raw prefixes, UTF-8 replacement, Rust whitespace, CRLF preservation and empty
override precedence follow the pinned contract. Source descriptors remain open
for a final bounded positional rehash and metadata, observation and directory
revalidation.

Resource limits are independent of the modeled cap: 1,048,576 bytes per source,
8,388,608 bytes across sources, 256 sources, 128 repository-to-cwd directories,
64 entries per filename list, 4,096 UTF-8 bytes per path, 128 components per
absolute path and canonical JSON depth 64. Canonical envelopes have an
8,388,608-byte ceiling. Raw JSON transport permits that ceiling plus one final
LF for producer output framing. Reads use at most one additional overflow
probe byte. The cap must be positive and no greater than 1,048,576 bytes.

Handoffs bind one local worktree and location. A clone, moved checkout or
replacement directory requires a new handoff. Artifacts contain filenames,
local identities and hashes, so treat them as local artifacts. Hashes can
confirm guessed paths or short content. The integrity digest supplies no
authorization, confidentiality or signature.

Validation establishes fresh disk accounting. It supplies no atomic snapshot,
observed session, instruction-loading equivalence or behavior guarantee.
Timestamp granularity can limit change detection, access times may change,
and transient changes restored between reads can escape detection. A valid
truncated row does not establish that its full instructions reached a session.
