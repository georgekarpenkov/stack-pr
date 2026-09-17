"""Git plumbing.

Everything here is built on plumbing commands (``rev-list``, ``cat-file``,
``commit-tree``, ``update-ref``, ``for-each-ref``, ``push``) so that the tool
never touches the working tree or the index. Commits are rewritten by creating
new commit objects and moving refs with compare-and-swap semantics, which means
an interrupted run leaves the repository exactly as it was.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pstack_pr import shell
from pstack_pr.errors import PstackError

ZERO_SHA = "0" * 40
SHORT_SHA_LEN = 8

_IDENTITY_RE = re.compile(r"^(?P<name>.*?) <(?P<email>[^>]*)> (?P<date>\d+ [+-]\d{4})$")


@dataclass(frozen=True)
class Identity:
    """An author or committer line: name, email and a date in git's raw format."""

    name: str
    email: str
    date: str  # e.g. "1700000000 +0100"

    @classmethod
    def parse(cls, value: str) -> Identity:
        m = _IDENTITY_RE.match(value)
        if not m:
            raise PstackError(f"cannot parse git identity line: {value!r}")
        return cls(m.group("name"), m.group("email"), m.group("date"))

    def env(self, role: str) -> dict[str, str]:
        """Environment variables that make ``commit-tree`` reuse this identity."""
        return {
            f"GIT_{role}_NAME": self.name,
            f"GIT_{role}_EMAIL": self.email,
            f"GIT_{role}_DATE": self.date,
        }


@dataclass(frozen=True)
class Commit:
    """A parsed commit object."""

    sha: str
    tree: str
    parents: tuple[str, ...]
    author: Identity
    committer: Identity
    message: str
    encoding: str | None = None  # the ``encoding`` header, if the commit has one

    @property
    def short(self) -> str:
        return self.sha[:SHORT_SHA_LEN]

    @property
    def title(self) -> str:
        return self.message.strip().split("\n", 1)[0].strip()

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1

    @classmethod
    def parse(cls, sha: str, raw: bytes) -> Commit:
        """Parse the raw bytes of a commit object as printed by ``cat-file``."""
        header_bytes, sep, message_bytes = raw.partition(b"\n\n")
        if not sep:
            raise PstackError(f"malformed commit object {sha}")
        tree = None
        parents: list[str] = []
        author = committer = encoding = None
        for line in shell.decode(header_bytes).split("\n"):
            if line.startswith(" "):
                continue  # continuation of a multi-line header such as gpgsig
            key, _, value = line.partition(" ")
            if key == "tree":
                tree = value
            elif key == "parent":
                parents.append(value)
            elif key == "author":
                author = Identity.parse(value)
            elif key == "committer":
                committer = Identity.parse(value)
            elif key == "encoding":
                encoding = value
        if tree is None or author is None or committer is None:
            raise PstackError(f"malformed commit object {sha}")
        return cls(
            sha=sha,
            tree=tree,
            parents=tuple(parents),
            author=author,
            committer=committer,
            message=shell.decode(message_bytes),
            encoding=encoding,
        )


@dataclass(frozen=True)
class RefUpdate:
    """Move ``ref`` from ``old`` to ``new``; fails if ``ref`` is not at ``old``."""

    ref: str
    new: str
    old: str


@dataclass(frozen=True)
class PushRef:
    """One refspec of a push.

    ``expect`` is the value the remote ref is required to have for the push to
    be accepted (``--force-with-lease``): a sha, ``""`` for "must not exist", or
    ``None`` to force unconditionally.
    """

    dst: str
    src: str
    expect: str | None = None

    @property
    def refspec(self) -> str:
        return f"{self.src}:{self.dst}"


class Git:
    """Runs git commands in one repository."""

    def __init__(self, cwd: Path | None = None) -> None:
        self.cwd = cwd

    # -- low level -----------------------------------------------------------

    def run(
        self,
        *args: str,
        input: bytes | str | None = None,  # noqa: A002
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        return shell.run(
            ["git", *args], input=input, check=check, cwd=self.cwd, env=env
        )

    def output(
        self,
        *args: str,
        input: bytes | str | None = None,  # noqa: A002
        env: dict[str, str] | None = None,
    ) -> str:
        return shell.output(["git", *args], input=input, cwd=self.cwd, env=env)

    # -- repository state ------------------------------------------------------

    def root(self) -> Path:
        proc = self.run("rev-parse", "--show-toplevel", check=False)
        if proc.returncode != 0:
            raise PstackError("not inside a git repository")
        return Path(shell.decode(proc.stdout).strip())

    def git_dir(self) -> Path:
        return Path(self.output("rev-parse", "--absolute-git-dir"))

    def rebase_in_progress(self) -> bool:
        git_dir = self.git_dir()
        return (git_dir / "rebase-merge").exists() or (
            git_dir / "rebase-apply"
        ).exists()

    def remote_url(self, remote: str) -> str:
        """The configured URL of ``remote`` (before any ``insteadOf`` rewriting)."""
        proc = self.run("config", "--get", f"remote.{remote}.url", check=False)
        if proc.returncode != 0:
            raise PstackError(f"remote '{remote}' does not exist")
        return shell.decode(proc.stdout).strip()

    # -- revisions -------------------------------------------------------------

    def try_rev_parse(self, rev: str) -> str | None:
        proc = self.run(
            "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}", check=False
        )
        if proc.returncode != 0:
            return None
        return shell.decode(proc.stdout).strip()

    def rev_parse(self, rev: str) -> str:
        sha = self.try_rev_parse(rev)
        if sha is None:
            raise PstackError(f"'{rev}' is not a valid commit")
        return sha

    def symbolic_full_name(self, rev: str) -> str | None:
        """Full ref name behind ``rev`` (``refs/heads/x``, ``HEAD``) or None."""
        proc = self.run("rev-parse", "--symbolic-full-name", rev, check=False)
        if proc.returncode != 0:
            return None
        name = shell.decode(proc.stdout).strip()
        return name or None

    def current_branch_ref(self) -> str | None:
        """``refs/heads/<branch>`` for the checked out branch, None if detached."""
        proc = self.run("symbolic-ref", "--quiet", "HEAD", check=False)
        if proc.returncode != 0:
            return None
        return shell.decode(proc.stdout).strip()

    def merge_base(self, a: str, b: str) -> str:
        proc = self.run("merge-base", a, b, check=False)
        if proc.returncode != 0:
            raise PstackError(f"'{a}' and '{b}' have no common ancestor")
        return shell.decode(proc.stdout).strip()

    def is_ancestor(self, a: str, b: str) -> bool:
        proc = self.run("merge-base", "--is-ancestor", a, b, check=False)
        if proc.returncode not in (0, 1):
            raise shell.CommandError(
                ["git", "merge-base", "--is-ancestor", a, b],
                proc.returncode,
                shell.decode(proc.stdout),
                shell.decode(proc.stderr),
            )
        return proc.returncode == 0

    def rev_list(self, base: str, head: str) -> list[str]:
        """Commits in ``base..head``, oldest first."""
        out = self.output("rev-list", "--reverse", f"^{base}", head)
        return out.split() if out else []

    # -- objects ---------------------------------------------------------------

    def read_commits(self, shas: Sequence[str]) -> list[Commit]:
        if not shas:
            return []
        proc = self.run("cat-file", "--batch", input="\n".join(shas) + "\n")
        data = proc.stdout
        commits: list[Commit] = []
        pos = 0
        for sha in shas:
            nl = data.index(b"\n", pos)
            header = data[pos:nl].decode("ascii").split()
            pos = nl + 1
            if len(header) != 3 or header[1] != "commit":  # noqa: PLR2004
                raise PstackError(f"{sha} is not a commit object")
            size = int(header[2])
            commits.append(Commit.parse(header[0], data[pos : pos + size]))
            pos += size + 1  # skip the trailing newline
        return commits

    def read_commit(self, sha: str) -> Commit:
        return self.read_commits([sha])[0]

    def commit_tree(
        self,
        *,
        tree: str,
        parents: Sequence[str],
        author: Identity,
        committer: Identity,
        message: str,
        encoding: str | None = None,
    ) -> str:
        """Create a commit object; nothing points at it until a ref is updated.

        ``encoding`` reproduces a commit's ``encoding`` header; without it git
        would re-encode a non-UTF-8 message.
        """
        args = []
        if encoding:
            args += ["-c", f"i18n.commitEncoding={encoding}"]
        args += ["commit-tree", tree]
        for parent in parents:
            args += ["-p", parent]
        args += ["-F", "-"]
        env = {**author.env("AUTHOR"), **committer.env("COMMITTER")}
        return self.output(*args, input=message, env=env)

    def rewrite(self, commit: Commit, *, parents: Sequence[str], message: str) -> str:
        """Return a commit like ``commit`` but with new parents and message.

        The sha of ``commit`` itself is returned when nothing would change, so
        the operation is idempotent.
        """
        if tuple(parents) == commit.parents and message == commit.message:
            return commit.sha
        return self.commit_tree(
            tree=commit.tree,
            parents=parents,
            author=commit.author,
            committer=commit.committer,
            message=message,
            encoding=commit.encoding,
        )

    # -- refs ------------------------------------------------------------------

    def for_each_ref(self, *patterns: str) -> dict[str, str]:
        """Map of full ref name to sha for refs matching ``patterns``."""
        out = self.output(
            "for-each-ref", "--format=%(refname) %(objectname)", *patterns
        )
        refs: dict[str, str] = {}
        for line in out.splitlines():
            name, _, sha = line.partition(" ")
            refs[name] = sha
        return refs

    def update_refs(self, updates: Sequence[RefUpdate], *, message: str) -> None:
        """Apply all ``updates`` in one transaction, or none of them.

        Each update requires the ref to still have its expected old value.
        """
        if not updates:
            return
        script = ["start"]
        script += [f"update {u.ref} {u.new} {u.old}" for u in updates]
        script += ["prepare", "commit", ""]
        self.run("update-ref", "-m", message, "--stdin", input="\n".join(script))

    # -- remotes ---------------------------------------------------------------

    def ls_remote(self, remote: str, patterns: Sequence[str]) -> dict[str, str]:
        """Map of full ref name to sha for branches of ``remote`` matching ``patterns``.

        Asks the remote directly and transfers no objects. ``--heads`` makes the
        server advertise branches only (not tags or ``refs/pull/*``), which is
        what keeps this cheap on busy repositories. Patterns are globs matched
        against the tail of the ref name (``refs/heads/x`` or ``refs/heads/x/*``).
        """
        if not patterns:
            return {}
        out = self.output("ls-remote", "--heads", "--refs", remote, *patterns)
        refs: dict[str, str] = {}
        for line in out.splitlines():
            sha, _, name = line.partition("\t")
            if name:
                refs[name] = sha
        return refs

    def fetch_branch(self, remote: str, branch: str) -> bool:
        """Fetch one branch of ``remote`` into its remote-tracking ref.

        The explicit refspec makes git ask the server for that single ref, so
        this costs one round trip however many branches the remote has, and an
        empty pack when the branch is already up to date locally. Returns False
        if the remote has no such branch.
        """
        args = [
            "fetch",
            "--quiet",
            "--no-tags",
            remote,
            f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}",
        ]
        proc = self.run(*args, check=False)
        if proc.returncode == 0:
            return True
        stderr = shell.decode(proc.stderr)
        if "couldn't find remote ref" in stderr:
            return False
        raise shell.CommandError(
            ["git", *args], proc.returncode, shell.decode(proc.stdout), stderr
        )

    def push(
        self, remote: str, refs: Sequence[PushRef], *, atomic: bool = True
    ) -> None:
        if not refs:
            return
        args = ["push", "--quiet"]
        if atomic:
            args.append("--atomic")
        refspecs = []
        for ref in refs:
            if ref.expect is None:
                # A leading '+' forces just this refspec; a global --force would
                # also disable the leases of the other refs in the same push.
                refspecs.append(f"+{ref.refspec}")
            else:
                args.append(f"--force-with-lease={ref.dst}:{ref.expect}")
                refspecs.append(ref.refspec)
        self.run(*args, remote, *refspecs)
