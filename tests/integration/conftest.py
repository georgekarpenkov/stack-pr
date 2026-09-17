"""Fixtures for the on-demand tests against a real GitHub repository.

These tests use the developer's own ``git`` (ssh) and ``gh`` set-up. The
offline harness in ``tests/conftest.py`` points ``HOME`` and the git config at
nowhere for every test, which would break both tools, so the environment for
every subprocess started here is rebuilt from a snapshot of the real process
environment taken when this module is imported (i.e. before any fixture ran).

The scratch repository is ``georgekarpenkov/pstack-pr-test`` unless
``PSTACK_PR_TEST_REPO`` says otherwise. Every run works on branches named
``itest/<runid>/<n>`` with a fresh ``runid`` and removes its pull requests and
branches again at the end of the session.
"""

from __future__ import annotations

import json
import os
import pwd
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

DEFAULT_TEST_REPO = "georgekarpenkov/pstack-pr-test"
REPO_ENV_VAR = "PSTACK_PR_TEST_REPO"

# The fields the tests look at; ``headRefOid`` is what a push must update.
PR_FIELDS = "number,url,state,isDraft,title,body,baseRefName,headRefName,headRefOid"

# Snapshot of the real environment, taken before the autouse fixture of the
# offline suite replaces HOME and the git config for the pytest process.
_REAL_ENV: dict[str, str] = dict(os.environ)

_DROP_PREFIXES = ("FAKE_GH_",)
_DROP_KEYS = frozenset(
    {
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "PSTACK_PR_CONFIG",
    }
)

COMMAND_TIMEOUT = 120  # seconds; a single git/gh call
EXPORT_TIMEOUT = 600  # seconds; one run of the tool


def _real_home() -> str:
    home = _REAL_ENV.get("HOME")
    if home and Path(home).is_dir():
        return home
    return pwd.getpwuid(os.getuid()).pw_dir


def subprocess_env() -> dict[str, str]:
    """Environment for git/gh/pstack-pr subprocesses: the real one, sanitised."""
    env = {
        key: value
        for key, value in _REAL_ENV.items()
        if key not in _DROP_KEYS and not key.startswith(_DROP_PREFIXES)
    }
    env["HOME"] = _real_home()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GH_PROMPT_DISABLED"] = "1"
    env["GH_NO_UPDATE_NOTIFIER"] = "1"
    return env


def normalize(text: str) -> str:
    """Line endings and surrounding whitespace as GitHub may rewrite them."""
    return text.replace("\r\n", "\n").strip()


@dataclass(frozen=True)
class PrInfo:
    """What GitHub currently says about one pull request."""

    number: int
    url: str
    state: str
    is_draft: bool
    title: str
    body: str
    base: str
    head: str
    head_oid: str

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> PrInfo:
        return cls(
            number=int(data["number"]),
            url=str(data["url"]),
            state=str(data["state"]),
            is_draft=bool(data["isDraft"]),
            title=str(data["title"]),
            body=str(data.get("body") or ""),
            base=str(data["baseRefName"]),
            head=str(data["headRefName"]),
            head_oid=str(data["headRefOid"]),
        )


@dataclass(frozen=True)
class ExportResult:
    rc: int
    out: str
    err: str


@dataclass
class GhRepo:
    """A clone of the scratch repository plus helpers to talk to GitHub."""

    path: Path
    slug: str
    run_id: str
    env: dict[str, str]

    # -- naming ----------------------------------------------------------------

    @property
    def branch(self) -> str:
        """The local branch the stack lives on."""
        return f"itest-{self.run_id}"

    @property
    def branch_prefix(self) -> str:
        return f"itest/{self.run_id}/"

    @property
    def branch_template(self) -> str:
        return f"{self.branch_prefix}$ID"

    def stack_branch(self, branch_id: int) -> str:
        return f"{self.branch_prefix}{branch_id}"

    def pr_url(self, number: int) -> str:
        return f"https://github.com/{self.slug}/pull/{number}"

    # -- subprocesses ----------------------------------------------------------

    def run(
        self,
        cmd: list[str],
        *,
        check: bool = True,
        input: str | None = None,  # noqa: A002 - mirrors subprocess.run
        timeout: int = COMMAND_TIMEOUT,
    ) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            cmd,
            cwd=self.path,
            env=self.env,
            input=input,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        if check and proc.returncode != 0:
            raise AssertionError(
                f"{' '.join(cmd)} failed with exit code {proc.returncode}:\n"
                f"{proc.stdout}\n{proc.stderr}"
            )
        return proc

    def git(
        self,
        *args: str,
        check: bool = True,
        input: str | None = None,  # noqa: A002
    ) -> str:
        return self.run(["git", *args], check=check, input=input).stdout.rstrip("\n")

    def gh(self, *args: str, check: bool = True) -> str:
        return self.run(["gh", *args], check=check).stdout.rstrip("\n")

    def export(self, *args: str) -> ExportResult:
        """Run ``pstack-pr export`` in the clone, with this run's branch template."""
        cmd = [
            sys.executable,
            "-m",
            "pstack_pr",
            "export",
            "--branch-name-template",
            self.branch_template,
            *args,
        ]
        proc = self.run(cmd, check=False, timeout=EXPORT_TIMEOUT)
        print(f"$ pstack-pr export {' '.join(args)}  (rc={proc.returncode})")
        print(proc.stdout)
        if proc.stderr:
            print(proc.stderr, file=sys.stderr)
        return ExportResult(rc=proc.returncode, out=proc.stdout, err=proc.stderr)

    # -- GitHub ----------------------------------------------------------------

    def pr(self, number: int) -> PrInfo:
        out = self.gh(
            "pr", "view", str(number), "--repo", self.slug, "--json", PR_FIELDS
        )
        return PrInfo.from_json(json.loads(out))

    def wait_for_pr(
        self,
        number: int,
        *,
        head_oid: str | None = None,
        base: str | None = None,
        state: str | None = None,
        is_draft: bool | None = None,
        attempts: int = 10,
        delay: float = 1.5,
    ) -> PrInfo:
        """``pr(number)`` once GitHub reports the expected values.

        GitHub is eventually consistent for a moment after a push or an edit,
        so poll a few times before giving up.
        """
        expected: dict[str, object] = {
            k: v
            for k, v in {
                "head_oid": head_oid,
                "base": base,
                "state": state,
                "is_draft": is_draft,
            }.items()
            if v is not None
        }
        pr = self.pr(number)
        for _ in range(attempts - 1):
            if all(getattr(pr, k) == v for k, v in expected.items()):
                break
            time.sleep(delay)
            pr = self.pr(number)
        mismatched = {
            k: (getattr(pr, k), v) for k, v in expected.items() if getattr(pr, k) != v
        }
        if mismatched:
            raise AssertionError(
                f"PR #{number} did not reach the expected state; "
                f"(actual, expected) per field: {mismatched}"
            )
        return pr

    def open_prs(self) -> list[PrInfo]:
        """Open pull requests whose head branch belongs to this run."""
        out = self.gh(
            "pr", "list", "--repo", self.slug, "--state", "open",
            "--limit", "100", "--json", PR_FIELDS,
        )  # fmt: skip
        prs = [PrInfo.from_json(item) for item in json.loads(out or "[]")]
        return sorted(
            (pr for pr in prs if pr.head.startswith(self.branch_prefix)),
            key=lambda pr: pr.number,
        )

    def remote_branches(self) -> dict[str, str]:
        """Branch name -> sha for this run's branches on the remote."""
        out = self.git(
            "ls-remote", "--refs", "origin", f"refs/heads/{self.branch_prefix}*"
        )
        branches: dict[str, str] = {}
        for line in out.splitlines():
            sha, _, ref = line.partition("\t")
            branches[ref.removeprefix("refs/heads/")] = sha
        return branches

    # -- local repository state ------------------------------------------------

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

    def identity(self, rev: str = "HEAD") -> str:
        """Author and committer name, email and timestamp of ``rev``."""
        return self.git("log", "-1", "--format=%an <%ae> %at / %cn <%ce> %ct", rev)

    def status(self) -> str:
        return self.git("status", "--porcelain")

    def reflog(self, ref: str = "HEAD") -> list[str]:
        return self.git("reflog", "show", "--format=%gs", ref).splitlines()


def cleanup(repo: GhRepo) -> None:
    """Close this run's pull requests and delete its remote branches.

    Individual failures are reported but do not abort the cleanup.
    """
    problems: list[str] = []

    def attempt(cmd: list[str]) -> None:
        try:
            proc = repo.run(cmd, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            problems.append(f"{' '.join(cmd)}: {e}")
            return
        if proc.returncode != 0:
            problems.append(
                f"{' '.join(cmd)} -> exit {proc.returncode}: {proc.stderr.strip()}"
            )

    try:
        prs = repo.open_prs()
    except (AssertionError, OSError, subprocess.TimeoutExpired) as e:
        problems.append(f"could not list open pull requests: {e}")
        prs = []
    for pr in prs:
        attempt(["gh", "pr", "close", str(pr.number), "--repo", repo.slug])

    try:
        branches = sorted(repo.remote_branches())
    except (AssertionError, OSError, subprocess.TimeoutExpired) as e:
        problems.append(f"could not list remote branches: {e}")
        branches = []
    if branches:
        proc = repo.run(
            ["git", "push", "--quiet", "origin", "--delete", *branches], check=False
        )
        if proc.returncode != 0:
            # Fall back to one branch at a time so one bad ref cannot block the rest.
            for branch in branches:
                attempt(["git", "push", "--quiet", "origin", "--delete", branch])

    print(
        f"\n[integration cleanup] run {repo.run_id}: closed {len(prs)} pull "
        f"request(s) {[pr.number for pr in prs]}, deleted {len(branches)} "
        f"branch(es) {branches}"
    )
    for problem in problems:
        print(f"[integration cleanup] problem: {problem}")
    try:
        left_prs = [pr.number for pr in repo.open_prs()]
        left_branches = sorted(repo.remote_branches())
    except (AssertionError, OSError, subprocess.TimeoutExpired) as e:
        print(f"[integration cleanup] could not verify: {e}")
        return
    print(
        f"[integration cleanup] remaining open PRs: {left_prs}, "
        f"remaining branches: {left_branches}"
    )


@pytest.fixture(scope="session")
def gh_repo(tmp_path_factory: pytest.TempPathFactory) -> Iterator[GhRepo]:
    """A fresh clone of the scratch repository on a new branch off origin/main."""
    slug = _REAL_ENV.get(REPO_ENV_VAR) or DEFAULT_TEST_REPO
    run_id = uuid.uuid4().hex[:8]
    root = tmp_path_factory.mktemp("gh")
    path = root / "clone"
    env = subprocess_env()
    assert not any(k.startswith("FAKE_GH_") for k in env)
    assert "GIT_CONFIG_GLOBAL" not in env
    assert Path(env["HOME"]).is_dir(), env["HOME"]

    proc = subprocess.run(
        ["git", "clone", "--quiet", f"git@github.com:{slug}.git", str(path)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=COMMAND_TIMEOUT,
    )
    if proc.returncode != 0:
        pytest.fail(f"cannot clone git@github.com:{slug}.git:\n{proc.stderr}")

    repo = GhRepo(path=path, slug=slug, run_id=run_id, env=env)
    # The try/finally starts before anything reaches the remote, so the cleanup
    # runs even when the rest of the set-up or any test fails.
    try:
        repo.git("config", "user.name", "pstack-pr integration test")
        repo.git("config", "user.email", "pstack-pr-itest@example.com")
        repo.git("checkout", "--quiet", "-b", repo.branch, "origin/main")
        assert repo.remote_branches() == {}, "stale branches from another run"
        print(f"\n[integration] repo {slug}, run {run_id}, clone at {path}")
        yield repo
    finally:
        cleanup(repo)
