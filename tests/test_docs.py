"""Documentation checks must catch broken examples without running them."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_docs.py"


def run_check(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def checker():
    spec = importlib.util.spec_from_file_location("check_docs", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("example", "message"),
    [
        ("[guide](docs/missing.md)", "missing local target"),
        ('<img src="docs/missing.svg">', "missing local target"),
        ("[install](#absent)", "missing anchor"),
        ("[guide][missing]", "undefined reference label: missing"),
        ("```bash\nbootstrap-doctor status --made-up\n```", "--made-up"),
        ("```bash title=x\nbootstrap-doctor imaginary\n```", "imaginary"),
        ("```console\n$ bootstrap-doctor imaginary\n```", "imaginary"),
        ("```sh\nbootstrap-doctor audit --max-input-chars nope\n```", "nope"),
    ],
)
def test_bad_docs_fail_with_file_and_line(tmp_path, example, message):
    (tmp_path / "README.md").write_text("# Install\n" + example + "\n")
    result = run_check(tmp_path)
    assert result.returncode == 1, result.stderr
    line = 3 if example.startswith("```") else 2
    assert f"README.md:{line}:" in result.stdout
    assert message in result.stdout


def test_links_and_shell_snippets_without_execution(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text(
        '# Use `status`!\n# Use `status`!\n<a id="explicit"></a>\n'
    )
    (docs / "pic.svg").write_text("<svg></svg>")
    (tmp_path / "README.md").write_text(
        "# Install\n"
        "[one](docs/guide.md#use-status) [two](docs/guide.md#use-status-1)\n"
        '<a href="docs/guide.md#explicit">guide</a>\n'
        '<img src="docs/pic.svg"> [self](#install)\n'
        "[reference][guide]\n[guide]: docs/guide.md#explicit\n"
        "`[example](absent.md)`\n"
        "```text\n[example](absent.md)\nbootstrap-doctor printed output\n```\n"
        "```console\n$ bootstrap-doctor status\n"
        "bootstrap-doctor status (soft=17000, hard=20000)\n```\n"
        "```bash\n$ bootstrap-doctor status\n"
        "bootstrap-doctor status (soft=17000, hard=20000)\n```\n"
        "```sh\nbootstrap-doctor trim --apply --workspace-dir /no/such/path\n"
        "bootstrap-doctor audit --gateway-url http://127.0.0.1:1\n"
        "bootstrap-doctor runtime \\\n  --agent 'main'\n"
        "bootstrap-doctor --help\nbootstrap-doctor --version\n```\n"
    )
    result = run_check(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "docs check: passed" in result.stdout


def test_nested_docs_are_checked(tmp_path):
    (tmp_path / "README.md").write_text("# Docs\n")
    nested = tmp_path / "docs" / "plans"
    nested.mkdir(parents=True)
    (nested / "plan.md").write_text("[bad](../missing.md)\n")
    result = run_check(tmp_path)
    assert result.returncode == 1
    assert "docs/plans/plan.md:1:" in result.stdout


def test_shell_fence_nested_in_numbered_list(tmp_path):
    (tmp_path / "README.md").write_text(
        "# Install\n1. Check the workspace:\n"
        "    ```bash\n    bootstrap-doctor imaginary\n    ```\n"
        "[later](missing.md)\n"
    )
    result = run_check(tmp_path)
    assert result.returncode == 1
    assert "README.md:4:" in result.stdout and "imaginary" in result.stdout
    assert "README.md:6: missing local target" in result.stdout


def test_unreadable_anchor_target_keeps_line_and_later_links(tmp_path):
    (tmp_path / "empty").mkdir()
    (tmp_path / "README.md").write_text(
        "# Install\n[guide](empty/#section)\n[later](missing.md)\n"
    )
    result = run_check(tmp_path)
    assert result.returncode == 1
    assert "README.md:2: cannot check anchor: empty/#section" in result.stdout
    assert "README.md:3: missing local target: missing.md" in result.stdout


@pytest.mark.parametrize(
    "command",
    [
        "bootstrap-doctor status | cat",
        "bootstrap-doctor status && echo done",
        "bootstrap-doctor status --workspace-dir $(pwd)",
        "sudo bootstrap-doctor status",
        "BOOTSTRAP_DOCTOR_TRACE=1 bootstrap-doctor status",
        "bootstrap-doctor sta...",
        "bootstrap-doctor;bogus",
        '"bootstrap-doctor" status',
        "'bootstrap-doctor' status",
    ],
)
def test_unsupported_command_syntax_is_explicit(tmp_path, command):
    (tmp_path / "README.md").write_text(f"```bash\n{command}\n```\n")
    result = run_check(tmp_path)
    assert result.returncode == 1
    assert "README.md:2:" in result.stdout
    assert "unsupported snippet syntax" in result.stdout


def test_remote_links_opt_in_deduplicated_and_offline(
    checker, tmp_path, monkeypatch, capsys
):
    (tmp_path / "README.md").write_text(
        '[one](https://example.org/a#section) <img src="https://example.org/a">\n'
    )
    requests = []

    def open_url(request, timeout):
        requests.append((request.full_url, request.method, timeout))
        raise HTTPError(request.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(
        checker, "build_opener", lambda *_: SimpleNamespace(open=open_url)
    )
    assert checker.main(["--root", str(tmp_path)]) == 0
    assert requests == []
    assert checker.main(["--root", str(tmp_path), "--remote"]) == 1
    assert requests == [("https://example.org/a", "HEAD", 5.0)]
    output = capsys.readouterr().out
    assert "README.md:1:" in output and "HTTP 404" in output


@pytest.mark.parametrize("failure", [401, 403, 429, "timeout"])
def test_remote_auth_rate_and_network_failures_are_reported(
    checker, tmp_path, monkeypatch, capsys, failure
):
    (tmp_path / "README.md").write_text("[link](https://example.org/a)\n")

    def open_url(request, timeout):
        if failure == "timeout":
            raise URLError("timed out")
        raise HTTPError(request.full_url, failure, "blocked", {}, None)

    monkeypatch.setattr(
        checker, "build_opener", lambda *_: SimpleNamespace(open=open_url)
    )
    assert checker.main(["--root", str(tmp_path), "--remote"]) == 1
    output = capsys.readouterr().out
    assert "README.md:1:" in output
    assert str(failure) in output or "timed out" in output


def test_remote_budget_and_head_fallback(checker, tmp_path, monkeypatch, capsys):
    (tmp_path / "README.md").write_text(
        "[one](https://example.org/a) [two](https://example.org/b)\n"
    )
    requests = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    def open_url(request, timeout):
        requests.append((request.method, request.get_header("Range")))
        if request.method == "HEAD":
            raise HTTPError(request.full_url, 405, "Method Not Allowed", {}, None)
        return Response()

    monkeypatch.setattr(
        checker, "build_opener", lambda *_: SimpleNamespace(open=open_url)
    )
    assert checker.main(["--root", str(tmp_path), "--remote", "--max-urls", "1"]) == 1
    assert requests == [("HEAD", None), ("GET", "bytes=0-0")]
    assert "remote URL budget" in capsys.readouterr().out
