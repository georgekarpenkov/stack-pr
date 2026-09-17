"""Tests for :mod:`pstack_pr.git` against real temporary repositories.

No ``gh`` is involved. ``remote`` is a bare repository standing in for GitHub
and ``work`` is a clone of it whose ``origin`` URL looks like GitHub but is
rewritten to the bare repository through an ``insteadOf`` setting.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from pstack_pr import shell
from pstack_pr.errors import PstackError
from pstack_pr.git import ZERO_SHA, Commit, Git, Identity, PushRef, RefUpdate
from pstack_pr.shell import CommandError
from tests.conftest import Remote, Work, git

# --------------------------------------------------------------------------- #
# Hand-written commit objects
# --------------------------------------------------------------------------- #
TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
P1 = "1111111111111111111111111111111111111111"
P2 = "2222222222222222222222222222222222222222"
SHA = "abcdef0123456789abcdef0123456789abcdef01"
AUTHOR_LINE = "Ada Author <ada@example.com> 1700000000 +0100"
COMMITTER_LINE = "Cy Committer <cy@example.com> 1700000001 -0500"
ADA = Identity("Ada Author", "ada@example.com", "1700000000 +0100")
CY = Identity("Cy Committer", "cy@example.com", "1700000001 -0500")

# A multi-line header: every line after the first starts with a space. The
# continuation lines deliberately look like ``parent``/``tree`` headers.
GPGSIG = (
    "gpgsig -----BEGIN PGP SIGNATURE-----",
    " ",
    " iQIzBAABCAAdFiEEexampleexampleexampleexample",
    " parent 3333333333333333333333333333333333333333",
    " tree 4444444444444444444444444444444444444444",
    " =AbCd",
    " -----END PGP SIGNATURE-----",
)

BR_X = "refs/heads/testbot/stack/1"
BR_Y = "refs/heads/testbot/stack/2"
STACK_INFO = (
    "stack-info: PR: https://github.com/octo/widgets/pull/1, branch: testbot/stack/1"
)


def raw_commit(
    *,
    tree: str = TREE,
    parents: Sequence[str] = (P1,),
    headers: Sequence[str] = (),
    message: bytes = b"Title\n",
) -> bytes:
    lines = [
        f"tree {tree}",
        *(f"parent {p}" for p in parents),
        f"author {AUTHOR_LINE}",
        f"committer {COMMITTER_LINE}",
        *headers,
    ]
    return "\n".join(lines).encode() + b"\n\n" + message


# --------------------------------------------------------------------------- #
# Repository helpers
# --------------------------------------------------------------------------- #
@pytest.fixture
def g(work: Work) -> Git:
    return Git(cwd=work.path)


def make_chain(work: Work, count: int) -> list[str]:
    """``count`` commits on top of HEAD made with plumbing; no ref is moved."""
    tree = work.tree()
    parent = work.head()
    shas = []
    for i in range(count):
        parent = work.git(
            "commit-tree", tree, "-p", parent, "-F", "-", input=f"Commit {i}\n"
        )
        shas.append(parent)
    return shas


def commit_raw(work: Work, message: bytes) -> str:
    """A commit on top of HEAD whose message is exactly ``message`` (any bytes).

    ``git commit-tree`` rewrites bytes that are not valid UTF-8, so the object
    is assembled by hand and stored with ``hash-object``.
    """
    raw = raw_commit(tree=work.tree(), parents=(work.head(),), message=message)
    proc = subprocess.run(
        ["git", "hash-object", "-t", "commit", "-w", "--stdin"],
        cwd=work.path,
        input=raw,
        capture_output=True,
        check=True,
    )
    return proc.stdout.decode("ascii").strip()


def raw_object(work: Work, sha: str) -> bytes:
    proc = subprocess.run(
        ["git", "cat-file", "commit", sha],
        cwd=work.path,
        capture_output=True,
        check=True,
    )
    return proc.stdout


def object_count(work: Work) -> int:
    out = work.git("cat-file", "--batch-all-objects", "--batch-check")
    return len(out.splitlines())


def has_commit(work: Work, sha: str) -> bool:
    """Whether the commit object ``sha`` is in ``work``'s object store.

    Decided by the exit status of ``git cat-file -e``, which prints nothing on
    success, so the answer cannot be confused with an empty stdout.
    """
    proc = subprocess.run(
        ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
        cwd=work.path,
        capture_output=True,
        check=False,
    )
    return proc.returncode == 0


def remote_tags(remote: Remote) -> set[str]:
    """Full names of the tags that exist on the bare remote."""
    out = git("for-each-ref", "refs/tags/", "--format=%(refname)", cwd=remote.path)
    return set(out.splitlines())


def push_from_other_clone(remote: Remote, tmp_path: Path, branch: str) -> str:
    """Commit on ``branch`` in a second clone and push it; returns the new sha."""
    other = tmp_path / "other"
    if not other.exists():
        git("clone", "-q", str(remote.path), str(other), cwd=tmp_path)
    git("checkout", "-q", "-B", branch, "origin/main", cwd=other)
    name = branch.replace("/", "_") + ".txt"
    (other / name).write_text(f"{branch}\n")
    git("add", name, cwd=other)
    git("commit", "-q", "-m", f"Add {branch}", cwd=other)
    git("push", "-q", "origin", f"HEAD:refs/heads/{branch}", cwd=other)
    return git("rev-parse", "HEAD", cwd=other)


RunCall = tuple[list[str], "bytes | str | None"]


def record_runs(monkeypatch: pytest.MonkeyPatch) -> list[RunCall]:
    """Replace ``Git.run`` with a recorder; returns the list of (args, input)."""
    calls: list[RunCall] = []

    def fake_run(
        _self: Git, *args: str, **kwargs: Any
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append((list(args), kwargs.get("input")))
        return subprocess.CompletedProcess(["git", *args], 0, b"", b"")

    monkeypatch.setattr(Git, "run", fake_run)
    return calls


def record_commands(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Replace ``Git.run`` and ``Git.output`` with recorders; returns the args.

    Unlike :func:`record_runs` this also catches commands issued through
    ``Git.output`` (``ls-remote``, ``for-each-ref``, ...).
    """
    calls: list[list[str]] = []

    def fake_run(
        _self: Git, *args: str, **kwargs: Any
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append(list(args))
        return subprocess.CompletedProcess(["git", *args], 0, b"", b"")

    def fake_output(_self: Git, *args: str, **kwargs: Any) -> str:
        calls.append(list(args))
        return ""

    monkeypatch.setattr(Git, "run", fake_run)
    monkeypatch.setattr(Git, "output", fake_output)
    return calls


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #
def test_identity_parse_valid() -> None:
    ident = Identity.parse(AUTHOR_LINE)
    assert ident == ADA
    assert (ident.name, ident.email, ident.date) == (
        "Ada Author",
        "ada@example.com",
        "1700000000 +0100",
    )


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            "Jean-Luc O'Brien, Jr. <jl@x.io> 1 +0000",
            Identity("Jean-Luc O'Brien, Jr.", "jl@x.io", "1 +0000"),
        ),
        ("bot <> 1700000000 -1200", Identity("bot", "", "1700000000 -1200")),
        (
            "Zoë <z@x.io> 1700000000 +0530",
            Identity("Zoë", "z@x.io", "1700000000 +0530"),
        ),
        (
            "a <with spaces@x.io> 1700000000 +0000",
            Identity("a", "with spaces@x.io", "1700000000 +0000"),
        ),
    ],
)
def test_identity_parse_unusual_but_valid(line: str, expected: Identity) -> None:
    assert Identity.parse(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        "",
        "Ada Author",
        "Ada Author <ada@example.com>",
        "Ada Author <ada@example.com> 1700000000",
        "Ada Author <ada@example.com> 1700000000 +01",
        "Ada Author <ada@example.com> 1700000000 +01000",
        "Ada Author <ada@example.com> 1700000000 0100",
        "Ada Author <ada@example.com> yesterday +0100",
        "Ada Author ada@example.com 1700000000 +0100",
        "Ada Author <ada@example.com>1700000000 +0100",
    ],
)
def test_identity_parse_invalid_raises(line: str) -> None:
    with pytest.raises(PstackError, match="cannot parse git identity line"):
        Identity.parse(line)


def test_identity_parse_error_message_quotes_the_line() -> None:
    with pytest.raises(PstackError, match=re.escape("line: 'Ada Author'")):
        Identity.parse("Ada Author")


def test_identity_env_author() -> None:
    assert ADA.env("AUTHOR") == {
        "GIT_AUTHOR_NAME": "Ada Author",
        "GIT_AUTHOR_EMAIL": "ada@example.com",
        "GIT_AUTHOR_DATE": "1700000000 +0100",
    }


def test_identity_env_committer() -> None:
    assert CY.env("COMMITTER") == {
        "GIT_COMMITTER_NAME": "Cy Committer",
        "GIT_COMMITTER_EMAIL": "cy@example.com",
        "GIT_COMMITTER_DATE": "1700000001 -0500",
    }


# --------------------------------------------------------------------------- #
# Commit.parse
# --------------------------------------------------------------------------- #
def test_commit_parse_single_parent() -> None:
    commit = Commit.parse(SHA, raw_commit())
    assert commit.sha == SHA
    assert commit.tree == TREE
    assert commit.parents == (P1,)
    assert commit.author == ADA
    assert commit.committer == CY
    assert commit.message == "Title\n"
    assert not commit.is_merge


def test_commit_parse_two_parents_is_merge() -> None:
    commit = Commit.parse(SHA, raw_commit(parents=(P1, P2)))
    assert commit.parents == (P1, P2)
    assert commit.is_merge


def test_commit_parse_root_commit_has_no_parents() -> None:
    commit = Commit.parse(SHA, raw_commit(parents=()))
    assert commit.parents == ()
    assert not commit.is_merge


def test_commit_parse_skips_gpgsig_continuation_lines() -> None:
    raw = raw_commit(headers=GPGSIG, message=b"Signed\n\nBody\n")
    commit = Commit.parse(SHA, raw)
    assert commit.tree == TREE
    assert commit.parents == (P1,)
    assert commit.author == ADA
    assert commit.committer == CY
    assert commit.message == "Signed\n\nBody\n"


def test_commit_parse_ignores_unknown_headers() -> None:
    commit = Commit.parse(SHA, raw_commit(headers=("encoding ISO-8859-1",)))
    assert commit.tree == TREE
    assert commit.message == "Title\n"


@pytest.mark.parametrize(
    "message",
    [
        b"Title\n\nBody paragraph.\n",
        b"Title\n\nBody\n\n\n",
        b"Title without trailing newline",
        b"",
        b"\n\nLeading blank lines\n",
        b"Title\n\nline one\n  indented line\n\n\nline after two blanks\n",
        b"Title\n\n\n\nBody after several blank lines\n",
    ],
    ids=[
        "blank-line-and-newline",
        "many-trailing-newlines",
        "no-trailing-newline",
        "empty",
        "leading-blank-lines",
        "indentation",
        "several-blank-lines",
    ],
)
def test_commit_parse_preserves_message_verbatim(message: bytes) -> None:
    commit = Commit.parse(SHA, raw_commit(message=message))
    assert commit.message == message.decode()


def test_commit_parse_non_utf8_message_survives_round_trip() -> None:
    message = b"Caf\xe9 title\n\nbody \xff\xfe\n"
    commit = Commit.parse(SHA, raw_commit(message=message))
    assert shell.encode(commit.message) == message
    assert commit.title == "Caf\udce9 title"


def test_commit_parse_non_utf8_author_survives_round_trip() -> None:
    raw = (
        f"tree {TREE}\n".encode()
        + b"author J\xf6rg <j@x.io> 1700000000 +0000\n"
        + f"committer {COMMITTER_LINE}\n\nx\n".encode()
    )
    commit = Commit.parse(SHA, raw)
    assert shell.encode(commit.author.name) == b"J\xf6rg"
    assert commit.author.email == "j@x.io"


@pytest.mark.parametrize(
    ("message", "title"),
    [
        ("Title\n\nBody\n", "Title"),
        ("  Padded title  \n\nBody", "Padded title"),
        ("\n\nAfter blank lines\n", "After blank lines"),
        ("single line", "single line"),
        ("", ""),
        ("   \n\n", ""),
        ("Title\nSecond line without blank\n", "Title"),
    ],
)
def test_commit_title(message: str, title: str) -> None:
    assert Commit(SHA, TREE, (P1,), ADA, CY, message).title == title


def test_commit_short_is_eight_characters() -> None:
    commit = Commit.parse(SHA, raw_commit())
    assert len(commit.short) == 8
    assert commit.short == "abcdef01"


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        f"tree {TREE}\nauthor {AUTHOR_LINE}\ncommitter {COMMITTER_LINE}\n".encode(),
        f"parent {P1}\nauthor {AUTHOR_LINE}\ncommitter {COMMITTER_LINE}\n\nx\n".encode(),
        f"tree {TREE}\ncommitter {COMMITTER_LINE}\n\nx\n".encode(),
        f"tree {TREE}\nauthor {AUTHOR_LINE}\n\nx\n".encode(),
        b"\n\nmessage only\n",
    ],
    ids=["empty", "no-separator", "no-tree", "no-author", "no-committer", "no-header"],
)
def test_commit_parse_malformed_raises(raw: bytes) -> None:
    with pytest.raises(PstackError, match=f"malformed commit object {SHA}"):
        Commit.parse(SHA, raw)


def test_commit_parse_bad_identity_line_raises() -> None:
    raw = f"tree {TREE}\nauthor nobody\ncommitter {COMMITTER_LINE}\n\nx\n".encode()
    with pytest.raises(PstackError, match="cannot parse git identity line: 'nobody'"):
        Commit.parse(SHA, raw)


# --------------------------------------------------------------------------- #
# Low-level wrappers
# --------------------------------------------------------------------------- #
def test_output_strips_trailing_newlines(g: Git, stack3: list[str]) -> None:
    assert g.output("rev-parse", "HEAD") == stack3[2]
    assert g.output("cat-file", "-p", "HEAD:a.txt") == "a.txt"
    assert g.output("log", "-1", "--format=%B%n%n", "HEAD") == "Add c"


def test_output_passes_input_to_stdin(g: Git) -> None:
    # The blob id of "hello\n" is a well-known constant.
    assert (
        g.output("hash-object", "--stdin", input="hello\n")
        == "ce013625030ba8dba906f756967f9e9ca394464a"
    )
    assert (
        g.output("hash-object", "--stdin", input=b"hello\n")
        == "ce013625030ba8dba906f756967f9e9ca394464a"
    )


def test_output_extra_env_is_visible_to_git(g: Git) -> None:
    ident = Identity("Env Person", "e@x.io", "1700000000 +0000")
    assert g.output("var", "GIT_AUTHOR_IDENT", env=ident.env("AUTHOR")) == (
        "Env Person <e@x.io> 1700000000 +0000"
    )


def test_run_check_false_returns_failed_process(g: Git) -> None:
    proc = g.run("rev-parse", "--verify", "--quiet", "refs/heads/nope", check=False)
    assert proc.returncode == 1
    assert proc.stdout == b""


def test_run_raises_command_error_with_full_command_line(g: Git) -> None:
    missing = "deadbeef" * 5
    with pytest.raises(CommandError) as excinfo:
        g.run("cat-file", "-t", missing)
    err = excinfo.value
    assert err.cmd == ["git", "cat-file", "-t", missing]
    assert err.returncode == 128
    assert err.stderr.startswith("fatal:")
    assert str(err).splitlines()[:2] == [
        "command failed with exit code 128:",
        f"  $ git cat-file -t {missing}",
    ]


def test_run_uses_cwd_of_the_git_instance(work: Work, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert Git(cwd=work.path).output("rev-parse", "--show-toplevel") == str(
        work.path.resolve()
    )
    with pytest.raises(CommandError) as excinfo:
        Git(cwd=elsewhere).run("rev-parse", "--show-toplevel")
    assert excinfo.value.returncode == 128


# --------------------------------------------------------------------------- #
# Repository state
# --------------------------------------------------------------------------- #
def test_root_is_repository_toplevel(work: Work, g: Git) -> None:
    assert g.root() == work.path.resolve()


def test_root_from_subdirectory(work: Work) -> None:
    sub = work.path / "deep" / "er"
    sub.mkdir(parents=True)
    assert Git(cwd=sub).root() == work.path.resolve()


def test_root_defaults_to_process_cwd(work: Work) -> None:
    assert Git().root() == work.path.resolve()


def test_root_outside_repository_raises(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(PstackError, match="not inside a git repository"):
        Git(cwd=outside).root()


def test_git_dir(work: Work, g: Git) -> None:
    assert g.git_dir() == (work.path / ".git").resolve()


def test_git_dir_from_subdirectory(work: Work) -> None:
    sub = work.path / "sub"
    sub.mkdir()
    assert Git(cwd=sub).git_dir() == (work.path / ".git").resolve()


def test_rebase_in_progress_false_normally(g: Git) -> None:
    assert g.rebase_in_progress() is False


@pytest.mark.parametrize("state_dir", ["rebase-merge", "rebase-apply"])
def test_rebase_in_progress_detects_state_directory(
    work: Work, g: Git, state_dir: str
) -> None:
    (work.path / ".git" / state_dir).mkdir()
    assert g.rebase_in_progress() is True


def test_remote_url_is_configured_url_not_insteadof_target(
    work: Work, remote: Remote, g: Git
) -> None:
    assert g.remote_url("origin") == "git@github.com:octo/widgets.git"
    # The insteadOf rewrite is in effect: git itself resolves the URL to the
    # bare repository, which is what makes fetch and push work offline.
    assert work.git("remote", "get-url", "origin") == str(remote.path)


def test_remote_url_unknown_remote_raises(g: Git) -> None:
    with pytest.raises(PstackError, match="remote 'upstream' does not exist"):
        g.remote_url("upstream")


# --------------------------------------------------------------------------- #
# Revisions
# --------------------------------------------------------------------------- #
def test_try_rev_parse_branch(work: Work, g: Git, stack3: list[str]) -> None:
    assert g.try_rev_parse("feature") == stack3[2]
    assert g.try_rev_parse("refs/heads/feature") == stack3[2]
    assert g.try_rev_parse("main") == work.head("origin/main")


def test_try_rev_parse_full_and_abbreviated_sha(g: Git, stack3: list[str]) -> None:
    assert g.try_rev_parse(stack3[1]) == stack3[1]
    assert g.try_rev_parse(stack3[1][:10]) == stack3[1]


def test_try_rev_parse_relative_revision(work: Work, g: Git, stack3: list[str]) -> None:
    assert g.try_rev_parse("HEAD~1") == stack3[1]
    assert g.try_rev_parse("HEAD^^") == stack3[0]
    assert g.try_rev_parse("feature~3") == work.head("origin/main")


def test_try_rev_parse_remote_tracking_branch(work: Work, g: Git) -> None:
    main = work.head("origin/main")
    assert g.try_rev_parse("origin/main") == main
    assert g.try_rev_parse("refs/remotes/origin/main") == main


def test_try_rev_parse_peels_annotated_tag(
    work: Work, g: Git, stack3: list[str]
) -> None:
    work.git("tag", "-a", "-m", "release", "v1", stack3[0])
    tag_sha = work.git("rev-parse", "v1")
    assert tag_sha != stack3[0]
    assert g.try_rev_parse("v1") == stack3[0]
    assert g.try_rev_parse(tag_sha) == stack3[0]


def test_try_rev_parse_unknown_is_none(g: Git) -> None:
    assert g.try_rev_parse("no-such-branch") is None
    assert g.try_rev_parse("HEAD~99") is None
    assert g.try_rev_parse("deadbeef" * 5) is None


def test_try_rev_parse_rejects_tree_and_blob(
    work: Work, g: Git, stack3: list[str]
) -> None:
    tree = work.tree()
    blob = work.git("rev-parse", "HEAD:a.txt")
    assert g.try_rev_parse(tree) is None
    assert g.try_rev_parse(blob) is None
    assert g.try_rev_parse("HEAD^{tree}") is None
    assert g.try_rev_parse("HEAD:a.txt") is None


def test_rev_parse_returns_sha(g: Git, stack3: list[str]) -> None:
    assert g.rev_parse("HEAD") == stack3[2]
    assert g.rev_parse(stack3[0][:12]) == stack3[0]


def test_rev_parse_invalid_raises(g: Git) -> None:
    with pytest.raises(PstackError, match="'nope' is not a valid commit"):
        g.rev_parse("nope")


def test_rev_parse_tree_raises(work: Work, g: Git, stack3: list[str]) -> None:
    tree = work.tree()
    with pytest.raises(PstackError, match=f"'{tree}' is not a valid commit"):
        g.rev_parse(tree)


def test_symbolic_full_name_branch(g: Git, stack3: list[str]) -> None:
    assert g.symbolic_full_name("feature") == "refs/heads/feature"
    assert g.symbolic_full_name("refs/heads/feature") == "refs/heads/feature"
    assert g.symbolic_full_name("main") == "refs/heads/main"


def test_symbolic_full_name_head_on_branch(g: Git) -> None:
    assert g.symbolic_full_name("HEAD") == "refs/heads/feature"


def test_symbolic_full_name_head_detached(work: Work, g: Git) -> None:
    work.git("checkout", "-q", "--detach")
    assert g.symbolic_full_name("HEAD") == "HEAD"


def test_symbolic_full_name_sha_is_none(g: Git, stack3: list[str]) -> None:
    assert g.symbolic_full_name(stack3[2]) is None
    assert g.symbolic_full_name(stack3[2][:8]) is None


def test_symbolic_full_name_relative_revision_is_none(
    g: Git, stack3: list[str]
) -> None:
    assert g.symbolic_full_name("HEAD~1") is None
    assert g.symbolic_full_name("feature^") is None


def test_symbolic_full_name_remote_branch(g: Git) -> None:
    assert g.symbolic_full_name("origin/main") == "refs/remotes/origin/main"


def test_symbolic_full_name_unknown_revision_is_none(g: Git) -> None:
    assert g.symbolic_full_name("no-such-thing") is None


def test_current_branch_ref_on_branch(g: Git) -> None:
    assert g.current_branch_ref() == "refs/heads/feature"


def test_current_branch_ref_detached_is_none(work: Work, g: Git) -> None:
    work.git("checkout", "-q", "--detach")
    assert g.current_branch_ref() is None


def test_merge_base(work: Work, g: Git, stack3: list[str]) -> None:
    main = work.head("origin/main")
    assert g.merge_base("feature", "origin/main") == main
    assert g.merge_base("origin/main", "feature") == main
    assert g.merge_base(stack3[0], stack3[2]) == stack3[0]
    assert g.merge_base(stack3[2], stack3[2]) == stack3[2]


def test_merge_base_of_diverged_branches(work: Work, g: Git, stack3: list[str]) -> None:
    side = work.git(
        "commit-tree", work.tree(), "-p", stack3[0], "-F", "-", input="Side\n"
    )
    assert g.merge_base(side, stack3[2]) == stack3[0]


def test_merge_base_unrelated_histories_raises(work: Work, g: Git) -> None:
    orphan = work.git("commit-tree", work.tree(), "-F", "-", input="Orphan\n")
    with pytest.raises(
        PstackError, match=f"'{orphan}' and 'HEAD' have no common ancestor"
    ):
        g.merge_base(orphan, "HEAD")


def test_is_ancestor_both_directions(work: Work, g: Git, stack3: list[str]) -> None:
    main = work.head("origin/main")
    assert g.is_ancestor(main, stack3[2]) is True
    assert g.is_ancestor(stack3[2], main) is False
    assert g.is_ancestor(stack3[0], stack3[1]) is True
    assert g.is_ancestor(stack3[1], stack3[0]) is False
    assert g.is_ancestor("origin/main", "feature") is True


def test_is_ancestor_of_itself(g: Git, stack3: list[str]) -> None:
    assert g.is_ancestor(stack3[1], stack3[1]) is True


def test_is_ancestor_unrelated_commit(work: Work, g: Git) -> None:
    orphan = work.git("commit-tree", work.tree(), "-F", "-", input="Orphan\n")
    assert g.is_ancestor(orphan, "HEAD") is False
    assert g.is_ancestor("HEAD", orphan) is False


def test_is_ancestor_invalid_revision_raises_command_error(g: Git) -> None:
    with pytest.raises(CommandError) as excinfo:
        g.is_ancestor("deadbeef", "HEAD")
    assert excinfo.value.returncode == 128
    assert excinfo.value.cmd == [
        "git",
        "merge-base",
        "--is-ancestor",
        "deadbeef",
        "HEAD",
    ]


def test_rev_list_oldest_first(work: Work, g: Git, stack3: list[str]) -> None:
    assert g.rev_list("origin/main", "feature") == stack3
    assert g.rev_list(work.head("origin/main"), stack3[2]) == stack3


def test_rev_list_partial_range(g: Git, stack3: list[str]) -> None:
    assert g.rev_list(stack3[0], stack3[2]) == stack3[1:]
    assert g.rev_list(stack3[1], "HEAD") == [stack3[2]]


def test_rev_list_empty_when_base_equals_head(g: Git, stack3: list[str]) -> None:
    assert g.rev_list("HEAD", "HEAD") == []
    assert g.rev_list(stack3[2], stack3[2]) == []


def test_rev_list_empty_when_head_is_behind_base(g: Git, stack3: list[str]) -> None:
    assert g.rev_list(stack3[2], stack3[0]) == []


# --------------------------------------------------------------------------- #
# Reading objects
# --------------------------------------------------------------------------- #
def test_read_commit_fields(work: Work, g: Git, stack3: list[str]) -> None:
    commit = g.read_commit(stack3[0])
    assert commit.sha == stack3[0]
    assert commit.tree == work.tree(stack3[0])
    assert commit.parents == (work.head("origin/main"),)
    assert (commit.author.name, commit.author.email) == (
        "Ada Author",
        "ada@example.com",
    )
    assert (commit.committer.name, commit.committer.email) == (
        "Cy Committer",
        "cy@example.com",
    )
    timestamp, _, tz = commit.author.date.partition(" ")
    assert timestamp == work.git("log", "-1", "--format=%at", stack3[0])
    assert re.fullmatch(r"[+-]\d{4}", tz)
    assert commit.message == "Add a\n"
    assert commit.title == "Add a"
    assert commit.short == stack3[0][:8]
    assert not commit.is_merge


def test_read_commit_multiline_message(work: Work, g: Git) -> None:
    text = "Title line\n\nParagraph one.\n\nParagraph two.\n"
    sha = work.commit("m.txt", text)
    commit = g.read_commit(sha)
    assert commit.message == text
    assert commit.title == "Title line"


def test_read_commit_merge_commit(work: Work, g: Git, stack3: list[str]) -> None:
    side = work.git(
        "commit-tree", work.tree(), "-p", stack3[0], "-F", "-", input="Side\n"
    )
    merge = work.git(
        "commit-tree", work.tree(), "-p", stack3[2], "-p", side, "-F", "-", input="M\n"
    )
    commit = g.read_commit(merge)
    assert commit.parents == (stack3[2], side)
    assert commit.is_merge


def test_read_commits_preserves_requested_order(g: Git, stack3: list[str]) -> None:
    wanted = [stack3[2], stack3[0], stack3[1]]
    commits = g.read_commits(wanted)
    assert [c.sha for c in commits] == wanted
    assert [c.title for c in commits] == ["Add c", "Add a", "Add b"]


def test_read_commits_allows_duplicates(g: Git, stack3: list[str]) -> None:
    shas = [stack3[0], stack3[0], stack3[2]]
    assert [c.sha for c in g.read_commits(shas)] == shas


def test_read_commits_many(work: Work, g: Git) -> None:
    shas = make_chain(work, 60)
    commits = g.read_commits(shas)
    assert [c.sha for c in commits] == shas
    assert [c.message for c in commits] == [f"Commit {i}\n" for i in range(60)]
    expected_parents = [(p,) for p in [work.head(), *shas[:-1]]]
    assert [c.parents for c in commits] == expected_parents


def test_read_commits_batch_slices_objects_by_exact_size(
    work: Work, g: Git, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One ``cat-file --batch`` reply holds all objects; each is cut by size.

    The messages are chosen to confuse anything but size-based slicing: blank
    lines, header-looking lines in the body, no trailing newline, and an empty
    message.
    """
    # Pin the dates so that the identities are identical across the commits
    # even when the loop below straddles a second boundary.
    monkeypatch.setenv("GIT_AUTHOR_DATE", "1700000000 +0100")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "1700000000 +0100")
    messages = [
        "Title\n\nParagraph.\n\ntree not-a-header\nparent not-a-header\n",
        "No trailing newline",
        "",
        "Trailing blank lines\n\n\n",
        "\n\nLeading blank lines\n",
        "Last\n",
    ]
    tree = work.tree()
    base = work.head()
    parent = base
    shas = []
    for text in messages:
        parent = work.git("commit-tree", tree, "-p", parent, "-F", "-", input=text)
        shas.append(parent)
    commits = g.read_commits(shas)
    assert [c.sha for c in commits] == shas
    assert [c.message for c in commits] == messages
    assert [c.tree for c in commits] == [tree] * len(messages)
    assert [c.parents for c in commits] == [(p,) for p in [base, *shas[:-1]]]
    assert [c.author for c in commits] == [ADA] * len(messages)


def test_read_commit_empty_message(work: Work, g: Git) -> None:
    sha = work.git("commit-tree", work.tree(), "-p", work.head(), "-F", "-", input="")
    assert raw_object(work, sha).endswith(b"\n\n")
    commit = g.read_commit(sha)
    assert commit.message == ""
    assert commit.title == ""


def test_read_commits_empty(g: Git) -> None:
    assert g.read_commits([]) == []


def test_read_commits_rejects_tree_object(
    work: Work, g: Git, stack3: list[str]
) -> None:
    tree = work.tree()
    with pytest.raises(PstackError, match=f"{tree} is not a commit object"):
        g.read_commits([stack3[0], tree])


def test_read_commits_rejects_blob_object(
    work: Work, g: Git, stack3: list[str]
) -> None:
    blob = work.git("rev-parse", "HEAD:a.txt")
    with pytest.raises(PstackError, match=f"{blob} is not a commit object"):
        g.read_commit(blob)


def test_read_commits_missing_object_raises(g: Git) -> None:
    missing = "deadbeef" * 5
    with pytest.raises(PstackError, match=f"{missing} is not a commit object"):
        g.read_commits([missing])


def test_read_commit_non_utf8_message_round_trips(work: Work, g: Git) -> None:
    message = b"Caf\xe9\n\n\xff\xfe raw bytes\n"
    sha = commit_raw(work, message)
    assert raw_object(work, sha).endswith(b"\n\n" + message)
    commit = g.read_commit(sha)
    assert shell.encode(commit.message) == message
    assert commit.title == "Caf\udce9"


# --------------------------------------------------------------------------- #
# Writing objects
# --------------------------------------------------------------------------- #
def test_commit_tree_uses_exact_identities_and_dates(
    work: Work, g: Git, stack3: list[str]
) -> None:
    tree = work.tree(stack3[1])
    sha = g.commit_tree(
        tree=tree,
        parents=[stack3[2]],
        author=ADA,
        committer=CY,
        message="New commit\n\nBody.\n",
    )
    assert work.git("cat-file", "-p", sha) == (
        f"tree {tree}\n"
        f"parent {stack3[2]}\n"
        f"author {AUTHOR_LINE}\n"
        f"committer {COMMITTER_LINE}\n"
        "\n"
        "New commit\n"
        "\n"
        "Body."
    )
    assert g.read_commit(sha) == Commit(
        sha=sha,
        tree=tree,
        parents=(stack3[2],),
        author=ADA,
        committer=CY,
        message="New commit\n\nBody.\n",
    )


def test_commit_tree_does_not_move_any_ref(
    work: Work, g: Git, stack3: list[str]
) -> None:
    before = g.for_each_ref()
    sha = g.commit_tree(
        tree=work.tree(), parents=[stack3[2]], author=ADA, committer=CY, message="x\n"
    )
    assert g.for_each_ref() == before
    assert work.head() == stack3[2]
    assert work.git("for-each-ref", "--points-at", sha) == ""


def test_commit_tree_multiple_parents(work: Work, g: Git, stack3: list[str]) -> None:
    sha = g.commit_tree(
        tree=work.tree(),
        parents=[stack3[2], stack3[0]],
        author=ADA,
        committer=CY,
        message="Merge\n",
    )
    assert g.read_commit(sha).parents == (stack3[2], stack3[0])


def test_commit_tree_root_commit(work: Work, g: Git) -> None:
    sha = g.commit_tree(
        tree=work.tree(), parents=[], author=ADA, committer=CY, message="Root\n"
    )
    commit = g.read_commit(sha)
    assert commit.parents == ()
    assert commit.tree == work.tree()


def test_commit_tree_is_deterministic(work: Work, g: Git, stack3: list[str]) -> None:
    first = g.commit_tree(
        tree=work.tree(), parents=[stack3[2]], author=ADA, committer=CY, message="s\n"
    )
    second = g.commit_tree(
        tree=work.tree(), parents=[stack3[2]], author=ADA, committer=CY, message="s\n"
    )
    assert first == second


def test_commit_tree_passes_message_bytes_through_unchanged(
    work: Work, g: Git, stack3: list[str]
) -> None:
    """With a legacy commit encoding git stores the bytes verbatim.

    The tool hands git exactly the bytes it read (surrogateescape both ways);
    ``Commit.parse`` ignores the ``encoding`` header git adds in this case.
    """
    work.git("config", "i18n.commitEncoding", "ISO-8859-1")
    message = shell.decode(b"Caf\xe9\n\n\xff raw\n")
    sha = g.commit_tree(
        tree=work.tree(), parents=[stack3[2]], author=ADA, committer=CY, message=message
    )
    raw = raw_object(work, sha)
    assert b"\nencoding ISO-8859-1\n" in raw
    assert raw.endswith(b"\n\nCaf\xe9\n\n\xff raw\n")
    assert g.read_commit(sha).message == message
    assert shell.encode(g.read_commit(sha).message) == b"Caf\xe9\n\n\xff raw\n"


def test_commit_tree_with_default_encoding_lets_git_normalize_to_utf8(
    work: Work, g: Git, stack3: list[str]
) -> None:
    """Documents a git limitation, not a tool one.

    Without ``i18n.commitEncoding`` git's ``commit-tree`` reinterprets invalid
    UTF-8 bytes as Latin-1 and re-encodes them, exactly as ``git commit``
    would. The tool still passes the original bytes to git.
    """
    message = shell.decode(b"Caf\xe9\n")
    sha = g.commit_tree(
        tree=work.tree(), parents=[stack3[2]], author=ADA, committer=CY, message=message
    )
    assert raw_object(work, sha).endswith(b"\n\nCaf\xc3\xa9\n")
    assert g.read_commit(sha).message == "Café\n"


def test_commit_tree_message_without_trailing_newline(
    work: Work, g: Git, stack3: list[str]
) -> None:
    sha = g.commit_tree(
        tree=work.tree(), parents=[stack3[2]], author=ADA, committer=CY, message="bare"
    )
    assert raw_object(work, sha).endswith(b"\n\nbare")
    assert g.read_commit(sha).message == "bare"


def test_rewrite_unchanged_returns_same_sha_and_creates_no_object(
    work: Work, g: Git, stack3: list[str]
) -> None:
    commit = g.read_commit(stack3[1])
    before = object_count(work)
    assert (
        g.rewrite(commit, parents=commit.parents, message=commit.message) == stack3[1]
    )
    # A list with the same parents counts as unchanged as well.
    assert (
        g.rewrite(commit, parents=list(commit.parents), message=commit.message)
        == stack3[1]
    )
    assert object_count(work) == before


def test_rewrite_with_new_message(work: Work, g: Git, stack3: list[str]) -> None:
    commit = g.read_commit(stack3[1])
    message = f"Add b\n\n{STACK_INFO}\n"
    new = g.rewrite(commit, parents=commit.parents, message=message)
    assert new != stack3[1]
    assert g.read_commit(new) == Commit(
        sha=new,
        tree=commit.tree,
        parents=commit.parents,
        author=commit.author,
        committer=commit.committer,
        message=message,
    )
    assert work.head() == stack3[2]
    assert work.head("feature") == stack3[2]


def test_rewrite_with_new_parents(work: Work, g: Git, stack3: list[str]) -> None:
    commit = g.read_commit(stack3[2])
    new = g.rewrite(commit, parents=(stack3[0],), message=commit.message)
    assert new != stack3[2]
    rewritten = g.read_commit(new)
    assert rewritten.parents == (stack3[0],)
    assert rewritten.tree == commit.tree
    assert rewritten.author == commit.author
    assert rewritten.committer == commit.committer
    assert rewritten.message == commit.message


def test_rewrite_is_deterministic(g: Git, stack3: list[str]) -> None:
    commit = g.read_commit(stack3[0])
    first = g.rewrite(commit, parents=commit.parents, message="Changed\n")
    second = g.rewrite(commit, parents=commit.parents, message="Changed\n")
    assert first == second


def test_rewrite_chain_keeps_trees_and_links_new_parents(
    g: Git, stack3: list[str]
) -> None:
    commits = g.read_commits(stack3)
    parent = commits[0].parents[0]
    new_shas = []
    for c in commits:
        parent = g.rewrite(c, parents=(parent,), message=f"{c.message}\n{STACK_INFO}\n")
        new_shas.append(parent)
    rewritten = g.read_commits(new_shas)
    assert [r.tree for r in rewritten] == [c.tree for c in commits]
    assert [r.parents for r in rewritten] == [
        (commits[0].parents[0],),
        (new_shas[0],),
        (new_shas[1],),
    ]
    assert [r.title for r in rewritten] == ["Add a", "Add b", "Add c"]


# --------------------------------------------------------------------------- #
# Refs
# --------------------------------------------------------------------------- #
def test_for_each_ref_heads(work: Work, g: Git, stack3: list[str]) -> None:
    assert g.for_each_ref("refs/heads/") == {
        "refs/heads/feature": stack3[2],
        "refs/heads/main": work.head("origin/main"),
    }


def test_for_each_ref_glob(work: Work, g: Git, stack3: list[str]) -> None:
    work.git("update-ref", "refs/heads/testbot/stack/1", stack3[0])
    work.git("update-ref", "refs/heads/testbot/stack/2", stack3[1])
    work.git("update-ref", "refs/heads/testbot/other", stack3[2])
    assert g.for_each_ref("refs/heads/testbot/stack/*") == {
        "refs/heads/testbot/stack/1": stack3[0],
        "refs/heads/testbot/stack/2": stack3[1],
    }


def test_for_each_ref_multiple_patterns(work: Work, g: Git, stack3: list[str]) -> None:
    refs = g.for_each_ref("refs/heads/feature", "refs/remotes/origin/main")
    assert refs == {
        "refs/heads/feature": stack3[2],
        "refs/remotes/origin/main": work.head("origin/main"),
    }


def test_for_each_ref_no_match_is_empty(g: Git) -> None:
    assert g.for_each_ref("refs/heads/nothing/*") == {}
    assert g.for_each_ref("refs/remotes/origin/testbot/stack/*") == {}


def test_for_each_ref_without_pattern_lists_everything(
    work: Work, g: Git, stack3: list[str]
) -> None:
    refs = g.for_each_ref()
    assert set(refs) == {
        "refs/heads/feature",
        "refs/heads/main",
        "refs/remotes/origin/HEAD",
        "refs/remotes/origin/main",
    }
    assert refs["refs/heads/feature"] == stack3[2]
    assert refs["refs/remotes/origin/HEAD"] == work.head("origin/main")


def test_update_refs_moves_ref_and_records_reflog_message(
    work: Work, g: Git, stack3: list[str]
) -> None:
    commit = g.read_commit(stack3[2])
    new = g.rewrite(commit, parents=commit.parents, message=f"Add c\n\n{STACK_INFO}\n")
    g.update_refs(
        [RefUpdate(ref="refs/heads/feature", new=new, old=stack3[2])],
        message="pstack-pr export",
    )
    assert work.head("feature") == new
    assert work.head() == new
    assert work.git("symbolic-ref", "HEAD") == "refs/heads/feature"
    assert work.reflog("refs/heads/feature")[0] == "pstack-pr export"
    assert work.reflog("HEAD")[0] == "pstack-pr export"


def test_update_refs_does_not_touch_working_tree_or_index(
    work: Work, g: Git, stack3: list[str]
) -> None:
    (work.path / "a.txt").write_text("modified\n")
    (work.path / "new.txt").write_text("new\n")
    work.git("add", "new.txt")
    assert work.status() == " M a.txt\nA  new.txt"
    commit = g.read_commit(stack3[2])
    new = g.rewrite(commit, parents=commit.parents, message="Add c (rewritten)\n")
    g.update_refs(
        [RefUpdate(ref="refs/heads/feature", new=new, old=stack3[2])], message="m"
    )
    assert work.head() == new
    assert work.status() == " M a.txt\nA  new.txt"
    assert (work.path / "a.txt").read_text() == "modified\n"


def test_update_refs_stale_old_value_fails_and_moves_nothing(
    work: Work, g: Git, stack3: list[str]
) -> None:
    work.git("update-ref", "refs/heads/other", stack3[0])
    reflog_before = work.reflog("refs/heads/feature")
    updates = [
        RefUpdate(ref="refs/heads/feature", new=stack3[1], old=stack3[2]),
        RefUpdate(ref="refs/heads/other", new=stack3[2], old=stack3[1]),
    ]
    with pytest.raises(CommandError) as excinfo:
        g.update_refs(updates, message="m")
    assert "cannot lock ref 'refs/heads/other'" in excinfo.value.stderr
    assert work.head("feature") == stack3[2]
    assert work.head("other") == stack3[0]
    assert work.reflog("refs/heads/feature") == reflog_before


def test_update_refs_single_stale_fails(work: Work, g: Git, stack3: list[str]) -> None:
    with pytest.raises(CommandError):
        g.update_refs(
            [RefUpdate(ref="refs/heads/feature", new=stack3[0], old=stack3[1])],
            message="m",
        )
    assert work.head("feature") == stack3[2]


def test_update_refs_moves_several_refs_together(
    work: Work, g: Git, stack3: list[str]
) -> None:
    work.git("update-ref", "refs/heads/other", stack3[0])
    g.update_refs(
        [
            RefUpdate(ref="refs/heads/feature", new=stack3[1], old=stack3[2]),
            RefUpdate(ref="refs/heads/other", new=stack3[2], old=stack3[0]),
        ],
        message="both",
    )
    assert work.head("feature") == stack3[1]
    assert work.head("other") == stack3[2]
    assert work.reflog("refs/heads/feature")[0] == "both"
    assert work.reflog("refs/heads/other")[0] == "both"


def test_update_refs_creates_ref_when_old_is_zero(
    work: Work, g: Git, stack3: list[str]
) -> None:
    g.update_refs(
        [RefUpdate(ref="refs/heads/brand-new", new=stack3[1], old=ZERO_SHA)],
        message="create",
    )
    assert work.head("brand-new") == stack3[1]
    assert work.reflog("refs/heads/brand-new") == ["create"]


def test_update_refs_zero_old_fails_if_ref_exists(
    work: Work, g: Git, stack3: list[str]
) -> None:
    with pytest.raises(CommandError):
        g.update_refs(
            [RefUpdate(ref="refs/heads/feature", new=stack3[1], old=ZERO_SHA)],
            message="m",
        )
    assert work.head("feature") == stack3[2]


def test_update_refs_detached_head(work: Work, g: Git, stack3: list[str]) -> None:
    work.git("checkout", "-q", "--detach")
    g.update_refs([RefUpdate(ref="HEAD", new=stack3[1], old=stack3[2])], message="d")
    assert work.head() == stack3[1]
    assert g.current_branch_ref() is None
    assert work.head("feature") == stack3[2]
    assert work.reflog("HEAD")[0] == "d"


def test_update_refs_via_head_symref_moves_checked_out_branch(
    work: Work, g: Git, stack3: list[str]
) -> None:
    g.update_refs(
        [RefUpdate(ref="HEAD", new=stack3[1], old=stack3[2])], message="via HEAD"
    )
    assert work.git("symbolic-ref", "HEAD") == "refs/heads/feature"
    assert work.head("feature") == stack3[1]
    assert work.reflog("refs/heads/feature")[0] == "via HEAD"


def test_update_refs_rejects_new_value_that_is_not_an_object(
    work: Work, g: Git, stack3: list[str]
) -> None:
    with pytest.raises(CommandError):
        g.update_refs(
            [RefUpdate(ref="refs/heads/feature", new="deadbeef" * 5, old=stack3[2])],
            message="m",
        )
    assert work.head("feature") == stack3[2]


def test_update_refs_empty_is_noop(g: Git, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record_runs(monkeypatch)
    g.update_refs([], message="m")
    assert calls == []


def test_update_refs_command_line(
    g: Git, monkeypatch: pytest.MonkeyPatch, stack3: list[str]
) -> None:
    calls = record_runs(monkeypatch)
    g.update_refs(
        [
            RefUpdate(ref="refs/heads/feature", new=stack3[0], old=stack3[2]),
            RefUpdate(ref="HEAD", new=stack3[1], old=stack3[2]),
        ],
        message="msg",
    )
    assert calls == [
        (
            ["update-ref", "-m", "msg", "--stdin"],
            (
                "start\n"
                f"update refs/heads/feature {stack3[0]} {stack3[2]}\n"
                f"update HEAD {stack3[1]} {stack3[2]}\n"
                "prepare\n"
                "commit\n"
            ),
        )
    ]


# --------------------------------------------------------------------------- #
# Remote refs without fetching (ls-remote)
# --------------------------------------------------------------------------- #
def create_remote_branches(work: Work, branches: dict[str, str]) -> None:
    """Create ``branches`` (name -> sha) on the remote by pushing from ``work``."""
    work.git(
        "push",
        "-q",
        "origin",
        *(f"{sha}:refs/heads/{name}" for name, sha in branches.items()),
    )


def test_ls_remote_exact_ref(
    work: Work, remote: Remote, g: Git, stack3: list[str]
) -> None:
    # Similar names must not match an exact pattern.
    create_remote_branches(work, {"maintenance": stack3[0], "main2": stack3[1]})
    assert g.ls_remote("origin", ["refs/heads/main"]) == {
        "refs/heads/main": remote.sha("main")
    }
    assert g.ls_remote("origin", [BR_X]) == {}


def test_ls_remote_glob_returns_only_matching_refs(
    work: Work, g: Git, stack3: list[str]
) -> None:
    create_remote_branches(
        work,
        {
            "u/stack/1": stack3[0],
            "u/stack/2": stack3[1],
            "u/other": stack3[2],
            "u/stack-old": stack3[2],
            "other/u/stack/3": stack3[2],
        },
    )
    assert g.ls_remote("origin", ["refs/heads/u/stack/*"]) == {
        "refs/heads/u/stack/1": stack3[0],
        "refs/heads/u/stack/2": stack3[1],
    }


def test_ls_remote_several_patterns_in_one_call(
    work: Work, remote: Remote, g: Git, stack3: list[str]
) -> None:
    create_remote_branches(
        work, {"u/stack/1": stack3[0], "u/stack/2": stack3[1], "u/other": stack3[2]}
    )
    refs = g.ls_remote(
        "origin", ["refs/heads/main", "refs/heads/u/stack/*", "refs/heads/nope"]
    )
    assert refs == {
        "refs/heads/main": remote.sha("main"),
        "refs/heads/u/stack/1": stack3[0],
        "refs/heads/u/stack/2": stack3[1],
    }


def test_ls_remote_no_match_is_empty(g: Git) -> None:
    assert g.ls_remote("origin", ["refs/heads/nope"]) == {}
    assert g.ls_remote("origin", ["refs/heads/nothing/*"]) == {}
    assert g.ls_remote("origin", ["refs/heads/nope", "refs/heads/nothing/*"]) == {}


def test_ls_remote_never_returns_head_or_tags(
    work: Work, remote: Remote, g: Git, stack3: list[str]
) -> None:
    work.git("tag", "-a", "-m", "release", "v1", stack3[1])
    work.git("tag", "light", stack3[0])
    work.git("push", "-q", "origin", "refs/tags/v1", "refs/tags/light")
    assert remote_tags(remote) == {"refs/tags/v1", "refs/tags/light"}
    # ``--heads``: the server advertises branches only, so a tag-only pattern
    # is empty (and no peeled ``refs/tags/v1^{}`` entry can show up either),
    # whether the tag is named in full or matched by its tail.
    assert g.ls_remote("origin", ["refs/tags/*"]) == {}
    assert g.ls_remote("origin", ["refs/tags/v1", "refs/tags/light"]) == {}
    assert g.ls_remote("origin", ["v1", "light"]) == {}
    assert g.ls_remote("origin", ["HEAD"]) == {}
    # Branches are unaffected by the presence of tags.
    assert g.ls_remote("origin", ["refs/heads/main", "refs/tags/*", "HEAD"]) == {
        "refs/heads/main": remote.sha("main")
    }


def test_ls_remote_branch_and_tag_with_the_same_name_returns_only_the_branch(
    work: Work, g: Git, stack3: list[str]
) -> None:
    create_remote_branches(work, {"release": stack3[2]})
    work.git("tag", "release", stack3[0])
    work.git("push", "-q", "origin", "refs/tags/release")
    # A tail pattern matches both refs on the server; only the branch is
    # advertised with ``--heads``.
    assert g.ls_remote("origin", ["release"]) == {"refs/heads/release": stack3[2]}
    assert g.ls_remote("origin", ["refs/heads/release", "refs/tags/release"]) == {
        "refs/heads/release": stack3[2]
    }
    assert g.ls_remote("origin", ["refs/tags/release"]) == {}


def test_ls_remote_empty_patterns_makes_no_git_call(
    g: Git, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = record_commands(monkeypatch)
    assert g.ls_remote("origin", []) == {}
    assert g.ls_remote("nope", []) == {}
    assert calls == []


def test_ls_remote_unknown_remote_raises_command_error(g: Git) -> None:
    with pytest.raises(CommandError) as excinfo:
        g.ls_remote("nope", ["refs/heads/main"])
    assert excinfo.value.cmd == [
        "git",
        "ls-remote",
        "--heads",
        "--refs",
        "nope",
        "refs/heads/main",
    ]
    assert excinfo.value.returncode == 128


def test_ls_remote_works_through_insteadof(work: Work, remote: Remote, g: Git) -> None:
    # origin's configured URL is a GitHub one; git rewrites it to the bare
    # repository, and ls-remote goes through the same rewrite as fetch/push.
    assert g.remote_url("origin") == "git@github.com:octo/widgets.git"
    assert work.git("remote", "get-url", "origin") == str(remote.path)
    assert g.ls_remote("origin", ["refs/heads/main"]) == {
        "refs/heads/main": remote.sha("main")
    }


def test_ls_remote_asks_the_remote_and_transfers_nothing(
    work: Work, remote: Remote, tmp_path: Path, g: Git
) -> None:
    old_main = work.head("origin/main")
    new_main = push_from_other_clone(remote, tmp_path, "main")
    topic = push_from_other_clone(remote, tmp_path, "topic")
    assert new_main != old_main
    before = object_count(work)
    refs = g.ls_remote("origin", ["refs/heads/main", "refs/heads/topic"])
    assert refs == {"refs/heads/main": new_main, "refs/heads/topic": topic}
    # The answer came from the remote, and nothing was fetched: no objects
    # arrived and no remote-tracking ref was created or moved.
    assert work.head("origin/main") == old_main
    assert g.for_each_ref("refs/remotes/") == {
        "refs/remotes/origin/HEAD": old_main,
        "refs/remotes/origin/main": old_main,
    }
    assert object_count(work) == before
    assert has_commit(work, new_main) is False
    assert has_commit(work, topic) is False


def test_ls_remote_deleted_branch_is_absent_despite_stale_tracking_ref(
    work: Work, remote: Remote, g: Git, stack3: list[str]
) -> None:
    g.push("origin", [PushRef(dst=BR_X, src=stack3[0], expect="")])
    assert g.ls_remote("origin", [BR_X]) == {BR_X: stack3[0]}
    # Deleted on the remote behind our back; the tracking ref still has it.
    git("update-ref", "-d", BR_X, cwd=remote.path)
    assert work.head("origin/testbot/stack/1") == stack3[0]
    assert g.ls_remote("origin", [BR_X]) == {}


def test_ls_remote_command_line(g: Git, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record_commands(monkeypatch)
    g.ls_remote("origin", ["refs/heads/main", "refs/heads/master"])
    g.ls_remote("origin", [BR_X, "refs/heads/testbot/stack/*"])
    assert calls == [
        [
            "ls-remote",
            "--heads",
            "--refs",
            "origin",
            "refs/heads/main",
            "refs/heads/master",
        ],
        [
            "ls-remote",
            "--heads",
            "--refs",
            "origin",
            BR_X,
            "refs/heads/testbot/stack/*",
        ],
    ]


# --------------------------------------------------------------------------- #
# Fetching one branch
# --------------------------------------------------------------------------- #
def test_fetch_branch_updates_tracking_ref_and_makes_objects_available(
    work: Work, remote: Remote, tmp_path: Path, g: Git, stack3: list[str]
) -> None:
    old_main = work.head("origin/main")
    new_main = push_from_other_clone(remote, tmp_path, "main")
    assert new_main != old_main
    assert has_commit(work, new_main) is False
    assert g.fetch_branch("origin", "main") is True
    assert work.head("origin/main") == new_main
    assert g.rev_parse("refs/remotes/origin/main") == new_main
    assert has_commit(work, new_main) is True
    fetched = g.read_commit(new_main)
    assert fetched.title == "Add main"
    assert fetched.parents == (old_main,)
    # Local branches and the checkout are untouched.
    assert work.head("main") == old_main
    assert work.head("feature") == stack3[2]
    assert work.head() == stack3[2]


def test_fetch_branch_does_not_create_other_remote_tracking_refs(
    work: Work, remote: Remote, tmp_path: Path, g: Git
) -> None:
    new_main = push_from_other_clone(remote, tmp_path, "main")
    topic = push_from_other_clone(remote, tmp_path, "topic")
    assert remote.sha("topic") == topic
    assert g.fetch_branch("origin", "main") is True
    assert g.for_each_ref("refs/remotes/") == {
        "refs/remotes/origin/HEAD": new_main,
        "refs/remotes/origin/main": new_main,
    }
    assert has_commit(work, topic) is False


def test_fetch_branch_of_a_stack_branch_creates_only_its_tracking_ref(
    work: Work, remote: Remote, tmp_path: Path, g: Git
) -> None:
    one = push_from_other_clone(remote, tmp_path, "testbot/stack/1")
    two = push_from_other_clone(remote, tmp_path, "testbot/stack/2")
    assert g.fetch_branch("origin", "testbot/stack/1") is True
    assert g.for_each_ref("refs/remotes/origin/testbot/") == {
        "refs/remotes/origin/testbot/stack/1": one
    }
    assert has_commit(work, one) is True
    assert has_commit(work, two) is False


def test_fetch_branch_does_not_fetch_tags(
    work: Work, remote: Remote, tmp_path: Path, g: Git
) -> None:
    new_main = push_from_other_clone(remote, tmp_path, "main")
    other = tmp_path / "other"
    git("tag", "-a", "-m", "release", "v1", new_main, cwd=other)
    git("tag", "light", new_main, cwd=other)
    git("push", "-q", "origin", "refs/tags/v1", "refs/tags/light", cwd=other)
    assert remote_tags(remote) == {"refs/tags/v1", "refs/tags/light"}
    assert g.fetch_branch("origin", "main") is True
    assert work.head("origin/main") == new_main
    # Both tags point at the commit just fetched; without --no-tags git would
    # auto-follow them.
    assert g.for_each_ref("refs/tags/") == {}


def test_fetch_branch_force_updates_after_remote_history_rewrite(
    work: Work, remote: Remote, tmp_path: Path, g: Git
) -> None:
    c0 = work.head("origin/main")
    c1 = push_from_other_clone(remote, tmp_path, "main")
    assert g.fetch_branch("origin", "main") is True
    assert work.head("origin/main") == c1
    # main is force-pushed to a commit that does not descend from c1.
    other = tmp_path / "other"
    git("reset", "-q", "--hard", c0, cwd=other)
    (other / "rewritten.txt").write_text("rewritten\n")
    git("add", "rewritten.txt", cwd=other)
    git("commit", "-q", "-m", "Rewritten main", cwd=other)
    c2 = git("rev-parse", "HEAD", cwd=other)
    git("push", "-q", "--force", "origin", "HEAD:refs/heads/main", cwd=other)
    assert remote.sha("main") == c2
    assert g.fetch_branch("origin", "main") is True
    assert work.head("origin/main") == c2
    assert g.is_ancestor(c1, c2) is False
    assert g.read_commit(c2).parents == (c0,)


def test_fetch_branch_is_a_noop_when_nothing_changed(
    work: Work, g: Git, stack3: list[str]
) -> None:
    before = g.for_each_ref()
    count = object_count(work)
    assert g.fetch_branch("origin", "main") is True
    assert g.for_each_ref() == before
    assert object_count(work) == count
    assert work.head() == stack3[2]


@pytest.mark.parametrize("branch", ["nope", "testbot/stack/9", "main2"])
def test_fetch_branch_unknown_branch_returns_false_and_creates_no_ref(
    work: Work, g: Git, stack3: list[str], branch: str
) -> None:
    before = g.for_each_ref()
    count = object_count(work)
    assert g.fetch_branch("origin", branch) is False
    assert g.for_each_ref(f"refs/remotes/origin/{branch}") == {}
    assert g.for_each_ref() == before
    assert object_count(work) == count
    assert work.head() == stack3[2]


def test_fetch_branch_of_a_branch_deleted_on_the_remote_returns_false(
    work: Work, remote: Remote, g: Git, stack3: list[str]
) -> None:
    g.push("origin", [PushRef(dst=BR_X, src=stack3[0], expect="")])
    assert g.fetch_branch("origin", "testbot/stack/1") is True
    git("update-ref", "-d", BR_X, cwd=remote.path)
    assert remote.sha("testbot/stack/1") is None
    assert g.fetch_branch("origin", "testbot/stack/1") is False
    # git does not prune with an explicit refspec: the stale tracking ref is
    # left alone, which is why the tool never reads branch state from it.
    assert work.head("origin/testbot/stack/1") == stack3[0]


def test_fetch_branch_unknown_remote_raises_command_error(g: Git) -> None:
    before = g.for_each_ref()
    with pytest.raises(CommandError) as excinfo:
        g.fetch_branch("nope", "main")
    err = excinfo.value
    assert err.returncode == 128
    assert err.cmd[:2] == ["git", "fetch"]
    assert "nope" in err.cmd
    assert "couldn't find remote ref" not in err.stderr
    assert "nope" in err.stderr
    assert g.for_each_ref("refs/remotes/nope/") == {}
    assert g.for_each_ref() == before


def test_fetch_branch_unknown_remote_is_not_mistaken_for_a_missing_branch(
    work: Work, g: Git
) -> None:
    # A remote that exists but cannot be reached must not look like "the
    # branch is missing": that would make the tool report a wrong cause.
    work.git("remote", "add", "broken", str(work.path / "does-not-exist.git"))
    with pytest.raises(CommandError) as excinfo:
        g.fetch_branch("broken", "main")
    assert excinfo.value.returncode == 128
    assert g.for_each_ref("refs/remotes/broken/") == {}


def test_fetch_branch_only_a_missing_remote_ref_means_false(
    g: Git, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The return value is decided by git's stderr, not by the exit code alone."""

    def failing_with(stderr: bytes) -> Any:
        def fake_run(
            _self: Git, *args: str, **kwargs: Any
        ) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(["git", *args], 128, b"", stderr)

        return fake_run

    monkeypatch.setattr(
        Git, "run", failing_with(b"fatal: couldn't find remote ref refs/heads/nope\n")
    )
    assert g.fetch_branch("origin", "nope") is False

    unreachable = b"fatal: 'nope' does not appear to be a git repository\n"
    monkeypatch.setattr(Git, "run", failing_with(unreachable))
    with pytest.raises(CommandError) as excinfo:
        g.fetch_branch("nope", "main")
    assert excinfo.value.returncode == 128
    assert excinfo.value.stderr == shell.decode(unreachable)
    assert excinfo.value.cmd[:2] == ["git", "fetch"]


def test_fetch_branch_command_line(g: Git, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record_runs(monkeypatch)
    assert g.fetch_branch("origin", "main") is True
    assert g.fetch_branch("upstream", "testbot/stack/1") is True
    assert calls == [
        (
            [
                "fetch",
                "--quiet",
                "--no-tags",
                "origin",
                "+refs/heads/main:refs/remotes/origin/main",
            ],
            None,
        ),
        (
            [
                "fetch",
                "--quiet",
                "--no-tags",
                "upstream",
                "+refs/heads/testbot/stack/1:refs/remotes/upstream/testbot/stack/1",
            ],
            None,
        ),
    ]


# --------------------------------------------------------------------------- #
# Pushing
# --------------------------------------------------------------------------- #
def test_push_ref_refspec() -> None:
    assert PushRef(dst="refs/heads/x", src="abc").refspec == "abc:refs/heads/x"
    assert PushRef(dst="refs/heads/x", src="").refspec == ":refs/heads/x"
    assert PushRef(dst="refs/heads/x", src="abc").expect is None


def test_push_new_branch_with_empty_expect(
    remote: Remote, g: Git, stack3: list[str]
) -> None:
    g.push("origin", [PushRef(dst=BR_X, src=stack3[0], expect="")])
    assert remote.sha("testbot/stack/1") == stack3[0]
    assert remote.message(stack3[0]) == "Add a"


def test_push_raw_sha_updates_local_remote_tracking_ref(
    work: Work, g: Git, stack3: list[str]
) -> None:
    g.push("origin", [PushRef(dst=BR_X, src=stack3[0], expect="")])
    assert g.for_each_ref("refs/remotes/origin/testbot/stack/1") == {
        "refs/remotes/origin/testbot/stack/1": stack3[0]
    }
    assert work.head("origin/testbot/stack/1") == stack3[0]
    # No local branch is created for the pushed sha.
    assert g.for_each_ref("refs/heads/") == {
        "refs/heads/feature": stack3[2],
        "refs/heads/main": work.head("origin/main"),
    }


def test_push_several_refs_at_once(remote: Remote, g: Git, stack3: list[str]) -> None:
    refs = [
        PushRef(dst=f"refs/heads/testbot/stack/{i + 1}", src=sha, expect="")
        for i, sha in enumerate(stack3)
    ]
    g.push("origin", refs)
    branches = remote.branches()
    del branches["main"]
    assert branches == {
        "testbot/stack/1": stack3[0],
        "testbot/stack/2": stack3[1],
        "testbot/stack/3": stack3[2],
    }


def test_push_empty_expect_fails_when_branch_exists(
    remote: Remote, g: Git, stack3: list[str]
) -> None:
    g.push("origin", [PushRef(dst=BR_X, src=stack3[0], expect="")])
    with pytest.raises(CommandError) as excinfo:
        g.push("origin", [PushRef(dst=BR_X, src=stack3[1], expect="")])
    assert "stale info" in excinfo.value.stderr
    assert remote.sha("testbot/stack/1") == stack3[0]


def test_push_empty_expect_fails_for_branch_unknown_locally(
    remote: Remote, tmp_path: Path, g: Git, stack3: list[str]
) -> None:
    other_sha = push_from_other_clone(remote, tmp_path, "testbot/stack/1")
    assert g.for_each_ref("refs/remotes/origin/testbot/stack/1") == {}
    with pytest.raises(CommandError) as excinfo:
        g.push("origin", [PushRef(dst=BR_X, src=stack3[0], expect="")])
    assert "stale info" in excinfo.value.stderr
    assert remote.sha("testbot/stack/1") == other_sha


def test_push_empty_expect_checks_remote_not_stale_local_tracking_ref(
    work: Work, remote: Remote, g: Git, stack3: list[str]
) -> None:
    g.push("origin", [PushRef(dst=BR_X, src=stack3[0], expect="")])
    # The branch is deleted on the remote behind our back (say, through the
    # web UI); the local remote-tracking ref still remembers it.
    git("update-ref", "-d", BR_X, cwd=remote.path)
    assert remote.sha("testbot/stack/1") is None
    assert work.head("origin/testbot/stack/1") == stack3[0]
    g.push("origin", [PushRef(dst=BR_X, src=stack3[1], expect="")])
    assert remote.sha("testbot/stack/1") == stack3[1]
    assert work.head("origin/testbot/stack/1") == stack3[1]


def test_push_already_up_to_date_succeeds(
    remote: Remote, g: Git, stack3: list[str]
) -> None:
    g.push("origin", [PushRef(dst=BR_X, src=stack3[0], expect="")])
    g.push("origin", [PushRef(dst=BR_X, src=stack3[0], expect=stack3[0])])
    assert remote.sha("testbot/stack/1") == stack3[0]


def test_push_with_correct_expect(remote: Remote, g: Git, stack3: list[str]) -> None:
    g.push("origin", [PushRef(dst=BR_X, src=stack3[0], expect="")])
    g.push("origin", [PushRef(dst=BR_X, src=stack3[1], expect=stack3[0])])
    assert remote.sha("testbot/stack/1") == stack3[1]


def test_push_with_correct_expect_allows_non_fast_forward(
    remote: Remote, g: Git, stack3: list[str]
) -> None:
    g.push("origin", [PushRef(dst=BR_X, src=stack3[2], expect="")])
    g.push("origin", [PushRef(dst=BR_X, src=stack3[0], expect=stack3[2])])
    assert remote.sha("testbot/stack/1") == stack3[0]


def test_push_with_correct_expect_for_sha_not_in_local_repo(
    remote: Remote, tmp_path: Path, g: Git, stack3: list[str]
) -> None:
    other_sha = push_from_other_clone(remote, tmp_path, "testbot/stack/1")
    assert g.try_rev_parse(other_sha) is None
    g.push("origin", [PushRef(dst=BR_X, src=stack3[0], expect=other_sha)])
    assert remote.sha("testbot/stack/1") == stack3[0]


def test_push_with_wrong_expect_fails_atomically(
    work: Work, remote: Remote, g: Git, stack3: list[str]
) -> None:
    g.push("origin", [PushRef(dst=BR_X, src=stack3[0], expect="")])
    wrong = work.head("origin/main")
    with pytest.raises(CommandError) as excinfo:
        g.push(
            "origin",
            [
                PushRef(dst=BR_X, src=stack3[1], expect=wrong),
                PushRef(dst=BR_Y, src=stack3[1], expect=""),
            ],
        )
    assert "stale info" in excinfo.value.stderr
    assert "atomic push failed" in excinfo.value.stderr
    assert remote.sha("testbot/stack/1") == stack3[0]
    assert remote.sha("testbot/stack/2") is None
    assert g.for_each_ref("refs/remotes/origin/testbot/stack/1") == {
        "refs/remotes/origin/testbot/stack/1": stack3[0]
    }
    assert g.for_each_ref("refs/remotes/origin/testbot/stack/2") == {}


def test_push_non_atomic_updates_the_accepted_refs(
    work: Work, remote: Remote, g: Git, stack3: list[str]
) -> None:
    g.push("origin", [PushRef(dst=BR_X, src=stack3[0], expect="")])
    wrong = work.head("origin/main")
    with pytest.raises(CommandError):
        g.push(
            "origin",
            [
                PushRef(dst=BR_X, src=stack3[1], expect=wrong),
                PushRef(dst=BR_Y, src=stack3[1], expect=""),
            ],
            atomic=False,
        )
    assert remote.sha("testbot/stack/1") == stack3[0]
    assert remote.sha("testbot/stack/2") == stack3[1]


def test_push_force_when_expect_is_none(
    remote: Remote, tmp_path: Path, g: Git, stack3: list[str]
) -> None:
    other_sha = push_from_other_clone(remote, tmp_path, "testbot/stack/1")
    assert other_sha not in stack3
    g.push("origin", [PushRef(dst=BR_X, src=stack3[0], expect=None)])
    assert remote.sha("testbot/stack/1") == stack3[0]


def test_push_force_on_one_ref_keeps_lease_on_others(
    work: Work, remote: Remote, g: Git, stack3: list[str]
) -> None:
    g.push(
        "origin",
        [
            PushRef(dst=BR_X, src=stack3[0], expect=""),
            PushRef(dst=BR_Y, src=stack3[0], expect=""),
        ],
    )
    wrong = work.head("origin/main")
    with pytest.raises(CommandError):
        g.push(
            "origin",
            [
                PushRef(dst=BR_X, src=stack3[1], expect=wrong),
                PushRef(dst=BR_Y, src=stack3[1], expect=None),
            ],
        )
    assert remote.sha("testbot/stack/1") == stack3[0]


def test_push_delete_with_expected_sha(
    remote: Remote, g: Git, stack3: list[str]
) -> None:
    g.push("origin", [PushRef(dst=BR_X, src=stack3[0], expect="")])
    g.push("origin", [PushRef(dst=BR_X, src="", expect=stack3[0])])
    assert remote.sha("testbot/stack/1") is None
    assert g.for_each_ref("refs/remotes/origin/testbot/stack/1") == {}


def test_push_delete_with_wrong_expect_fails(
    remote: Remote, g: Git, stack3: list[str]
) -> None:
    g.push("origin", [PushRef(dst=BR_X, src=stack3[0], expect="")])
    with pytest.raises(CommandError):
        g.push("origin", [PushRef(dst=BR_X, src="", expect=stack3[1])])
    assert remote.sha("testbot/stack/1") == stack3[0]


def test_push_delete_with_force(remote: Remote, g: Git, stack3: list[str]) -> None:
    g.push("origin", [PushRef(dst=BR_X, src=stack3[0], expect="")])
    g.push("origin", [PushRef(dst=BR_X, src="", expect=None)])
    assert remote.sha("testbot/stack/1") is None
    assert g.for_each_ref("refs/remotes/origin/testbot/stack/1") == {}


def test_push_delete_with_empty_expect_is_rejected(
    remote: Remote, g: Git, stack3: list[str]
) -> None:
    # expect="" means "must not exist", which contradicts deleting a branch.
    g.push("origin", [PushRef(dst=BR_X, src=stack3[0], expect="")])
    with pytest.raises(CommandError):
        g.push("origin", [PushRef(dst=BR_X, src="", expect="")])
    assert remote.sha("testbot/stack/1") == stack3[0]


def test_push_empty_list_is_noop(g: Git, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record_runs(monkeypatch)
    g.push("origin", [])
    assert calls == []


def test_push_command_line(
    g: Git, monkeypatch: pytest.MonkeyPatch, stack3: list[str]
) -> None:
    calls = record_runs(monkeypatch)
    g.push(
        "origin",
        [
            PushRef(dst=BR_X, src=stack3[0], expect=""),
            PushRef(dst=BR_Y, src=stack3[1], expect=stack3[0]),
            PushRef(dst="refs/heads/z", src="", expect=None),
        ],
    )
    assert calls == [
        (
            [
                "push",
                "--quiet",
                "--atomic",
                f"--force-with-lease={BR_X}:",
                f"--force-with-lease={BR_Y}:{stack3[0]}",
                "origin",
                f"{stack3[0]}:{BR_X}",
                f"{stack3[1]}:{BR_Y}",
                "+:refs/heads/z",
            ],
            None,
        )
    ]


def test_push_command_line_forced_ref_has_no_lease_and_no_global_force(
    g: Git, monkeypatch: pytest.MonkeyPatch, stack3: list[str]
) -> None:
    calls = record_runs(monkeypatch)
    g.push("origin", [PushRef(dst=BR_X, src=stack3[0], expect=None)])
    assert calls == [
        (["push", "--quiet", "--atomic", "origin", f"+{stack3[0]}:{BR_X}"], None)
    ]
    assert not any(a.startswith("--force") for a in calls[0][0])


def test_push_command_line_non_atomic(
    g: Git, monkeypatch: pytest.MonkeyPatch, stack3: list[str]
) -> None:
    calls = record_runs(monkeypatch)
    g.push("origin", [PushRef(dst=BR_X, src=stack3[0], expect="")], atomic=False)
    assert calls == [
        (
            [
                "push",
                "--quiet",
                f"--force-with-lease={BR_X}:",
                "origin",
                f"{stack3[0]}:{BR_X}",
            ],
            None,
        )
    ]
