#!/usr/bin/env python3
"""Check this repo's README and docs, using only the standard library.

Links: simple Markdown inline/reference links, HTTP autolinks, and HTML href/src.
Ignore inline/fenced code; local fragments use ATX/HTML headings and explicit IDs.
This is deliberately not a full Markdown renderer (no setext headings or nested
Markdown destinations). Remote fragments are not checked. HTTP is opt-in, uses
no credentials/cookies, and never retries authentication or challenge failures.

Commands: bootstrap-doctor in bash/sh/shell/zsh fences, or $/# prompt lines in
unlabelled/text/console/terminal/shell-session transcripts. Backslash continuations
and literal quoted arguments are supported. A $ prompt also switches a shell
fence to transcript mode. Shell operators, expansions, wrappers
and placeholders are errors, not executed. Other programs and printed transcript
output are outside scope. Only build_parser().parse_args() is called, never main.
"""

from __future__ import annotations

import argparse
import io
import re
import shlex
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urldefrag, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

# Works in a fresh checkout without installing the package or its dependencies.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bootstrap_doctor.cli import build_parser  # noqa: E402

SHELL = {"bash", "sh", "shell", "zsh"}
TRANSCRIPT = {"", "text", "console", "terminal", "shell-session"}
FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})(.*)$")
PROMPT = re.compile(r"^\s*[$#]\s+(.*)$")
DOCTOR = re.compile(r"(?<![\w/.-])bootstrap-doctor(?![\w/.-])")
INLINE_CODE = re.compile(r"(`+).*?\1")
DESTINATION = r"(<[^>\n]+>|[^\s)]+)"
INLINE_LINK = re.compile(
    r"!?\[[^\]\n]*\]\(\s*" + DESTINATION + r"(?:\s+[\"\'][^\n]*?[\"\'])?\s*\)"
)
REFERENCE = re.compile(r"^ {0,3}\[([^\]]+)\]:\s*" + DESTINATION, re.MULTILINE)


def split_document(text: str) -> tuple[str, list[tuple[int, str]]]:
    """Preserve prose line numbers while selecting only runnable snippet lines."""
    prose = []
    commands = []
    fence = ""
    language = ""
    prompted = False
    pending = ""
    start = 0
    for number, line in enumerate(text.splitlines(), 1):
        marker = FENCE.match(line)
        if marker and not fence:
            fence = marker[1]
            info = marker[2].split()
            language = info[0].lower() if info else ""
            prompted = False
            prose.append("")
            continue
        if fence:
            prose.append("")
            if (
                marker
                and marker[1][0] == fence[0]
                and len(marker[1]) >= len(fence)
                and not marker[2].strip()
            ):
                if pending:
                    commands.append((start, pending + " \\"))
                    pending = ""
                fence = ""
                continue
            prompt = PROMPT.match(line)
            if pending:
                command = prompt[1] if prompt else line.strip()
            elif language in SHELL:
                if line.lstrip().startswith("#"):
                    continue
                if prompt:
                    prompted = True
                elif prompted:
                    continue
                command = prompt[1] if prompt else line.strip()
            elif language in TRANSCRIPT and prompt:
                command = prompt[1]
            else:
                continue
            if not pending:
                start = number
            continued = command.rstrip().endswith("\\")
            pending += command.rstrip()[:-1] + " " if continued else command
            if not continued:
                commands.append((start, pending))
                pending = ""
        else:
            prose.append(INLINE_CODE.sub(lambda m: " " * len(m[0]), line))
    if pending:
        commands.append((start, pending + " \\"))
    return "\n".join(prose), commands


def slug(text: str) -> str:
    text = re.sub(r"<[^>]*>", "", unescape(text))
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    return re.sub(r"[^\w\- ]", "", text.lower()).replace(" ", "-")


class HTMLLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[int, str]] = []
        self.anchors: set[str] = set()
        self.headings: list[str] = []
        self.heading: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if value is None:
                continue
            if name in {"href", "src"}:
                self.links.append((self.getpos()[0], value))
            if name == "id" or (tag == "a" and name == "name"):
                self.anchors.add(value)
        if re.fullmatch(r"h[1-6]", tag):
            self.heading = []

    def handle_data(self, data):
        if self.heading is not None:
            self.heading.append(data)

    def handle_endtag(self, tag):
        if re.fullmatch(r"h[1-6]", tag) and self.heading is not None:
            self.headings.append("".join(self.heading))
            self.heading = None


def anchors(text: str) -> set[str]:
    # Preserve inline code text in headings, while excluding fenced examples.
    prose, _ = split_document(text)
    html = HTMLLinks()
    html.feed(prose)
    headings = list(html.headings)
    # Use original inline content for slugs; prose tells us which lines are fenced.
    for original, visible in zip(text.splitlines(), prose.splitlines()):
        if visible and (match := re.match(r"^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$", original)):
            headings.append(match[1])
    result = html.anchors
    for heading in headings:
        base = slug(heading)
        name = base
        suffix = 0
        while name in result:
            suffix += 1
            name = f"{base}-{suffix}"
        result.add(name)
    return result


def links(prose: str) -> list[tuple[int, str]]:
    html = HTMLLinks()
    html.feed(prose)
    found = list(html.links)
    definitions = {m[1].casefold(): m[2].strip("<>") for m in REFERENCE.finditer(prose)}
    for pattern in (INLINE_LINK, REFERENCE, re.compile(r"<(https?://[^\s>]+)>")):
        for match in pattern.finditer(prose):
            found.append(
                (
                    prose.count("\n", 0, match.start()) + 1,
                    match[match.lastindex].strip("<>"),
                )
            )
    for match in re.finditer(r"!?\[([^\]\n]+)\]\[([^\]\n]*)\]", prose):
        label = (match[2] or match[1]).casefold()
        found.append(
            (
                prose.count("\n", 0, match.start()) + 1,
                definitions.get(label, f"missing-reference:{label}"),
            )
        )
    return sorted(found)


def command_error(command: str) -> str | None:
    if not DOCTOR.search(command):
        return None
    if not command.startswith("bootstrap-doctor") or re.search(
        r"[|;&<>$`]|\.\.\.", command
    ):
        return "unsupported snippet syntax: use a literal bootstrap-doctor command without shell operators, expansions or wrappers"
    try:
        args = shlex.split(command, comments=True)
    except ValueError as exc:
        return f"unsupported snippet syntax: {exc}"
    if "\\" in args:
        return "unsupported snippet syntax: incomplete backslash continuation"
    stderr = io.StringIO()
    with redirect_stderr(stderr), redirect_stdout(io.StringIO()):
        try:
            build_parser().parse_args(args[1:])
        except SystemExit as exc:
            if exc.code != 0:
                return stderr.getvalue().strip().splitlines()[-1]
    return None


class BoundedRedirects(HTTPRedirectHandler):
    max_redirections = 3
    max_repeats = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme not in {"http", "https"}:
            raise URLError("non-HTTP redirect refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def remote_error(url: str, timeout: float) -> str | None:
    opener = build_opener(BoundedRedirects())
    for method in ("HEAD", "GET"):
        headers = {"User-Agent": "bootstrap-doctor-docs/1"}
        if method == "GET":
            headers["Range"] = "bytes=0-0"
        try:
            with opener.open(
                Request(url, method=method, headers=headers), timeout=timeout
            ) as response:
                if response.status >= 400:
                    return f"HTTP {response.status}"
            return None
        except HTTPError as exc:
            if method == "HEAD" and exc.code in {405, 501}:
                exc.close()
                continue
            exc.close()
            return f"HTTP {exc.code}: {exc.reason}"
        except (OSError, URLError, ValueError) as exc:
            return f"HTTP check failed: {exc}"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument(
        "--remote",
        action="store_true",
        help="check public HTTP links; offline by default",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="per-request socket timeout, 0 < seconds <= 10",
    )
    parser.add_argument(
        "--max-urls", type=int, default=32, help="unique remote URL budget, 1..100"
    )
    args = parser.parse_args(argv)
    if not 0 < args.timeout <= 10 or not 1 <= args.max_urls <= 100:
        parser.error("timeout must be in (0, 10]; max-urls must be in [1, 100]")
    root = args.root.resolve()
    files = [root / "README.md", *sorted((root / "docs").rglob("*.md"))]
    errors = []
    remote: dict[str, tuple[str, int]] = {}
    for path in files:
        label = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
            prose, commands = split_document(text)
            for line, command in commands:
                if error := command_error(command):
                    errors.append(f"{label}:{line}: {error}")
            for line, target in links(prose):
                location = f"{label}:{line}"
                parts = urlsplit(unescape(target))
                if parts.scheme in {"mailto", "tel"}:
                    continue
                if parts.scheme in {"http", "https"} or target.startswith("//"):
                    url = "https:" + target if target.startswith("//") else target
                    remote.setdefault(urldefrag(url)[0], (label, line))
                    continue
                if parts.scheme == "missing-reference":
                    errors.append(f"{location}: undefined reference label: {parts.path}")
                    continue
                if parts.scheme:
                    errors.append(f"{location}: unsupported link scheme: {target}")
                    continue
                local = (
                    root / unquote(parts.path).lstrip("/")
                    if parts.path.startswith("/")
                    else path.parent / unquote(parts.path)
                )
                if not parts.path:
                    local = path
                local = local.resolve()
                if not local.is_relative_to(root) or not local.exists():
                    errors.append(f"{location}: missing local target: {target}")
                elif parts.fragment:
                    if local.is_dir():
                        local = local / "README.md"
                    try:
                        target_anchors = anchors(local.read_text(encoding="utf-8"))
                    except (OSError, UnicodeError, ValueError) as exc:
                        errors.append(f"{location}: cannot check anchor: {target}: {exc}")
                        continue
                    if unquote(parts.fragment) not in target_anchors:
                        errors.append(f"{location}: missing anchor: {target}")
        except (OSError, UnicodeError, ValueError) as exc:
            errors.append(f"{label}:1: cannot check document: {exc}")
    deadline = time.monotonic() + 60
    if args.remote:
        for index, (url, (label, line)) in enumerate(remote.items()):
            remaining = deadline - time.monotonic()
            if index >= args.max_urls or remaining <= 0:
                error = "remote URL budget or 60-second time budget exceeded"
            else:
                error = remote_error(url, min(args.timeout, remaining))
            if error:
                errors.append(f"{label}:{line}: {url}: {error}")
    for error in errors:
        print(error)
    mode = "remote checked" if args.remote else "remote skipped; use --remote"
    print(
        f"docs check: {'failed' if errors else 'passed'} ({len(files)} files; {mode})"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
