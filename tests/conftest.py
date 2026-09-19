"""Shared fixtures: a local bare "GitHub" remote, a clone, and a fake ``gh``."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from pstack_pr.cli import main
from pstack_pr.stack import STACK_COMMENT_MARKER

TESTS_DIR = Path(__file__).parent
FAKE_GH = TESTS_DIR / "fake_gh.py"

# Deterministic identities so that commit shas are reproducible within a test.
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Ada Author",
    "GIT_AUTHOR_EMAIL": "ada@example.com",
    "GIT_COMMITTER_NAME": "Cy Committer",
    "GIT_COMMITTER_EMAIL": "cy@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "HOME": "/nonexistent",
}


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--integration",
        action="store_true",
        default=False,
        help="run tests that talk to a real GitHub repository",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if config.getoption("--integration") or os.environ.get("PSTACK_PR_INTEGRATION"):
        return
    skip = pytest.mark.skip(reason="needs --integration (or PSTACK_PR_INTEGRATION=1)")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


def git(
    *args: str,
    cwd: Path,
    check: bool = True,
    input: str | None = None,  # noqa: A002 - mirrors subprocess.run
) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**os.environ, **GIT_ENV},
        capture_output=True,
        text=True,
        check=False,
        input=input,
    )
    if check and proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed:\n{proc.stdout}\n{proc.stderr}"
        )
    return proc.stdout.rstrip("\n")


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)


@dataclass
class Remote:
    """A bare repository standing in for GitHub."""

    path: Path
    slug: str = "octo/widgets"

    def branches(self) -> dict[str, str]:
        out = git(
            "for-each-ref",
            "refs/heads/",
            "--format=%(refname:short) %(objectname)",
            cwd=self.path,
        )
        return dict(line.split() for line in out.splitlines())

    def sha(self, branch: str) -> str | None:
        return self.branches().get(branch)

    def message(self, sha: str) -> str:
        return git("log", "-1", "--format=%B", sha, cwd=self.path)


PR_WRITES = (["pr", "create"], ["pr", "edit"], ["pr", "ready"], ["pr", "close"])
# How a gh call that creates or edits a stack comment starts (REST via gh api).
COMMENT_CALL = ["api", "--hostname"]


def stack_comment(numbers: list[int], current: int) -> str:
    """The comment the tool keeps on PR ``current`` (``numbers`` bottom first)."""
    lines = ["Stacked PRs:"]
    lines += [f" * {'__->__' if n == current else ''}#{n}" for n in reversed(numbers)]
    return STACK_COMMENT_MARKER + "\n" + "\n".join(lines)


def is_write_call(call: list[str]) -> bool:
    if call[:2] in PR_WRITES:
        return True
    if call[:1] == ["api"] and "--method" in call:
        return call[call.index("--method") + 1] in ("POST", "PATCH", "DELETE")
    return False


@dataclass
class FakeGitHub:
    """Handle on the fake ``gh`` state."""

    state_path: Path

    def state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"next_number": 1, "prs": {}, "calls": []}
        return json.loads(self.state_path.read_text())  # type: ignore[no-any-return]

    def prs(self) -> dict[int, dict[str, Any]]:
        return {int(k): v for k, v in self.state()["prs"].items()}

    def pr(self, number: int) -> dict[str, Any]:
        return self.prs()[number]

    def calls(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.state()["calls"] if c[: len(prefix)] == list(prefix)]

    def write_calls(self) -> list[list[str]]:
        """Every gh invocation that changes something on the fake GitHub."""
        return [c for c in self.state()["calls"] if is_write_call(c)]

    def comments(self, number: int) -> list[dict[str, Any]]:
        return list(self.pr(number).get("comments", []))

    def stack_comments(self, number: int) -> list[str]:
        """Bodies of the tool's comments on PR ``number``; at most one expected."""
        return [
            c["body"]
            for c in self.comments(number)
            if c["body"].startswith(STACK_COMMENT_MARKER)
        ]

    def remove_comments(self, number: int) -> None:
        data = self.state()
        data["prs"][str(number)]["comments"] = []
        self.state_path.write_text(json.dumps(data))

    def set_comment(self, comment_id: int, body: str) -> None:
        data = self.state()
        for pr in data["prs"].values():
            for comment in pr.get("comments", []):
                if comment["databaseId"] == comment_id:
                    comment["body"] = body
        self.state_path.write_text(json.dumps(data))

    def set_body(self, number: int, body: str) -> None:
        data = self.state()
        data["prs"][str(number)]["body"] = body
        self.state_path.write_text(json.dumps(data))

    def close(self, number: int) -> None:
        data = self.state()
        data["prs"][str(number)]["state"] = "CLOSED"
        self.state_path.write_text(json.dumps(data))


@dataclass
class Work:
    """A clone of :class:`Remote` where the tests make commits."""

    path: Path
    remote: Remote

    def git(
        self,
        *args: str,
        check: bool = True,
        input: str | None = None,  # noqa: A002 - mirrors subprocess.run
    ) -> str:
        return git(*args, cwd=self.path, check=check, input=input)

    def commit(
        self, name: str, message: str | None = None, content: str | None = None
    ) -> str:
        """Add/overwrite file ``name`` and commit; returns the new sha."""
        (self.path / name).write_text(content if content is not None else f"{name}\n")
        self.git("add", name)
        self.git("commit", "-q", "-m", message or f"Add {name}")
        return self.head()

    def head(self, rev: str = "HEAD") -> str:
        return self.git("rev-parse", rev)

    def message(self, rev: str = "HEAD") -> str:
        return self.git("log", "-1", "--format=%B", rev)

    def messages(self, rng: str = "origin/main..HEAD") -> list[str]:
        out = self.git("log", "--reverse", "--format=%B%x00", rng)
        return [m.strip("\n") for m in out.split("\x00") if m.strip()]

    def shas(self, rng: str = "origin/main..HEAD") -> list[str]:
        return self.git("rev-list", "--reverse", rng).split()

    def tree(self, rev: str = "HEAD") -> str:
        return self.git("rev-parse", f"{rev}^{{tree}}")

    def status(self) -> str:
        return self.git("status", "--porcelain")

    def reflog(self, ref: str = "HEAD") -> list[str]:
        return self.git("reflog", "show", "--format=%gs", ref).splitlines()


@pytest.fixture
def remote(tmp_path: Path) -> Remote:
    path = tmp_path / "remote.git"
    git("init", "-q", "--bare", "--initial-branch=main", str(path), cwd=tmp_path)
    seed = tmp_path / "seed"
    git("clone", "-q", str(path), str(seed), cwd=tmp_path)
    (seed / "README.md").write_text("# widgets\n")
    git("add", "README.md", cwd=seed)
    git("commit", "-q", "-m", "Initial commit", cwd=seed)
    git("push", "-q", "origin", "HEAD:main", cwd=seed)
    return Remote(path=path)


@pytest.fixture
def fake_gh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote: Remote
) -> FakeGitHub:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "gh"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_GH}" "$@"\n')
    shim.chmod(0o755)
    state = tmp_path / "gh-state.json"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_STATE", str(state))
    monkeypatch.setenv("FAKE_GH_REMOTE", str(remote.path))
    monkeypatch.setenv("FAKE_GH_USER", "testbot")
    monkeypatch.delenv("FAKE_GH_FAIL_ON", raising=False)
    hooks = remote.path / "hooks"
    hooks.mkdir(exist_ok=True)
    hook = hooks / "post-receive"
    hook.write_text(
        "#!/bin/sh\n"
        f'FAKE_GH_STATE="{state}" FAKE_GH_REMOTE="{remote.path}" '
        f'exec "{sys.executable}" "{FAKE_GH}" post-receive\n'
    )
    hook.chmod(0o755)
    return FakeGitHub(state_path=state)


@pytest.fixture
def work(tmp_path: Path, remote: Remote, monkeypatch: pytest.MonkeyPatch) -> Work:
    path = tmp_path / "work"
    git("clone", "-q", str(remote.path), str(path), cwd=tmp_path)
    # The remote URL looks like GitHub (so the tool can work out the repository
    # slug) but git resolves it to the local bare repository via insteadOf.
    github_url = f"git@github.com:{remote.slug}.git"
    git("remote", "set-url", "origin", github_url, cwd=path)
    git("config", f"url.{remote.path}.insteadOf", github_url, cwd=path)
    git("checkout", "-q", "-b", "feature", cwd=path)
    monkeypatch.chdir(path)
    return Work(path=path, remote=remote)


RunExport = Callable[..., tuple[int, str, str]]


@pytest.fixture
def run_export(
    work: Work, fake_gh: FakeGitHub, capsys: pytest.CaptureFixture[str]
) -> RunExport:
    """Run ``pstack-pr export`` in-process inside ``work``; returns (rc, out, err)."""

    def run(*args: str) -> tuple[int, str, str]:
        capsys.readouterr()
        rc = main(["export", *args])
        captured = capsys.readouterr()
        return rc, captured.out, captured.err

    return run


@pytest.fixture
def stack3(work: Work) -> list[str]:
    """Three commits on ``feature`` above ``main``."""
    return [
        work.commit("a.txt", "Add a"),
        work.commit("b.txt", "Add b"),
        work.commit("c.txt", "Add c"),
    ]


def pytest_configure(config: pytest.Config) -> None:
    # Keep pytest from importing fake_gh as a test module.
    config.addinivalue_line("python_files", "test_*.py")


__all__ = [
    "COMMENT_CALL",
    "FakeGitHub",
    "Iterator",
    "Remote",
    "RunExport",
    "Work",
    "git",
    "stack_comment",
]
