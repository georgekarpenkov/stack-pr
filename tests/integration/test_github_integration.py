"""End-to-end tests of ``pstack-pr export`` against a real GitHub repository.

Opt in with ``uv run pytest tests/integration --integration``. The tests build
on one another and run in file order: each records what it achieved in the
module-scoped :class:`Progress` object, and a test whose prerequisite did not
complete is skipped instead of failing for a misleading reason.

Sleeps only appear inside :meth:`GhRepo.wait_for_pr`, to ride out GitHub's
eventual consistency right after a push or an edit; the tool itself never waits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pytest

from tests.integration.conftest import GhRepo, normalize

STACK_INFO_RE = re.compile(
    r"^stack-info: PR: (?P<url>\S+), branch: (?P<branch>\S+)$", re.MULTILINE
)
DESCRIPTION_A = "First commit of the integration stack."

# What ``-vv`` logs to stderr for every git/gh call; the tests assert on the
# exact sequence of commands that reach the network. Every run, a dry run
# included, starts with one fetch of the target branch through an explicit
# refspec (a single round trip that also refreshes refs/remotes/origin/main)
# followed by one ls-remote for the stack branches; nothing else is fetched.
FETCH_TARGET = (
    "$ git fetch --quiet --no-tags origin +refs/heads/main:refs/remotes/origin/main"
)
LS_REMOTE = "$ git ls-remote --heads --refs origin"
GRAPHQL_LOOKUP = "$ gh api --hostname github.com graphql"
USER_LOOKUP = "$ gh api --hostname github.com user"


def logged(stderr: str, prefix: str = "") -> list[str]:
    """The ``$ <command>`` lines of a ``-vv`` run starting with ``$ <prefix>``."""
    start = f"$ {prefix}" if prefix else "$ "
    return [line for line in stderr.splitlines() if line.startswith(start)]


def remote_git_calls(stderr: str) -> list[str]:
    """The git commands of a ``-vv`` run that talk to the remote, in order."""
    return [
        line
        for line in logged(stderr, "git ")
        if line.startswith(("$ git fetch ", "$ git ls-remote ", "$ git push "))
    ]


def gh_calls(stderr: str) -> list[str]:
    """Every ``gh`` command of a ``-vv`` run, in order."""
    return logged(stderr, "gh ")


def ls_remote_stack(*branches: str) -> str:
    """The one ls-remote of a run whose commits all carry a stack-info trailer."""
    return " ".join([LS_REMOTE, *(f"refs/heads/{b}" for b in branches)])


def ls_remote_template(gh_repo: GhRepo) -> str:
    """The one ls-remote of a run that has to allocate branch names.

    No commit has a stack-info trailer yet, so the only pattern is the glob of
    the branch template (quoted by ``shlex.join`` because of the ``*``).
    """
    return f"{LS_REMOTE} 'refs/heads/{gh_repo.branch_prefix}*'"


def push_line(*refs: tuple[str, str, str]) -> str:
    """``$ git push`` as logged for ``(branch, lease: expected remote sha, new sha)``.

    An empty lease means "the branch must not exist yet".
    """
    leases = [f"--force-with-lease=refs/heads/{b}:{expect}" for b, expect, _ in refs]
    refspecs = [f"{new}:refs/heads/{b}" for b, _, new in refs]
    return " ".join(["$ git push --quiet --atomic", *leases, "origin", *refspecs])


@dataclass
class Progress:
    """State handed from one test to the next (bottom of the stack first)."""

    titles: list[str] = field(default_factory=list)
    shas: list[str] = field(default_factory=list)
    trees: list[str] = field(default_factory=list)
    identities: list[str] = field(default_factory=list)
    prs: list[int] = field(default_factory=list)
    # Branches of this run on the remote that are not part of the stack
    # (name -> sha); ``remote_branches()`` reports them alongside the stack.
    unrelated: dict[str, str] = field(default_factory=dict)
    done: set[str] = field(default_factory=set)

    def require(self, step: str) -> None:
        if step not in self.done:
            pytest.skip(f"prerequisite step {step!r} did not complete")


@pytest.fixture(scope="module")
def progress() -> Progress:
    return Progress()


@pytest.fixture(scope="module")
def two_commits(gh_repo: GhRepo, progress: Progress) -> Progress:
    """``Add a`` and ``Add b`` on the run's branch, nothing exported yet."""
    tag = f"(itest {gh_repo.run_id})"
    progress.titles = [f"Add a {tag}", f"Add b {tag}"]

    (gh_repo.path / "a.txt").write_text("a\n")
    gh_repo.git("add", "a.txt")
    gh_repo.git("commit", "-q", "-m", progress.titles[0], "-m", DESCRIPTION_A)
    (gh_repo.path / "b.txt").write_text("b\n")
    gh_repo.git("add", "b.txt")
    gh_repo.git("commit", "-q", "-m", progress.titles[1])

    progress.shas = gh_repo.shas()
    assert len(progress.shas) == 2
    progress.trees = [gh_repo.tree(s) for s in progress.shas]
    progress.identities = [gh_repo.identity(s) for s in progress.shas]
    return progress


def stack_infos(gh_repo: GhRepo) -> list[tuple[str, str]]:
    """The single (url, branch) stack-info of every commit, bottom first."""
    result = []
    for message in gh_repo.messages():
        found = STACK_INFO_RE.findall(message)
        assert len(found) == 1, f"expected exactly one stack-info line:\n{message}"
        result.append((found[0][0], found[0][1]))
    return result


def pr_number(gh_repo: GhRepo, url: str) -> int:
    m = re.fullmatch(
        re.escape(f"https://github.com/{gh_repo.slug}/pull/") + r"(\d+)", url
    )
    assert m, f"not a pull request URL of {gh_repo.slug}: {url}"
    return int(m.group(1))


def expected_body(
    numbers: list[int], current: int, title: str, description: str = ""
) -> str:
    """The PR body the tool generates for a stack (``numbers`` bottom first)."""
    lines = ["Stacked PRs:"]
    for number in reversed(numbers):
        marker = "__->__" if number == current else ""
        lines.append(f" * {marker}#{number}")
    parts = ["\n".join(lines), "--- --- ---", f"### {title}"]
    if description:
        parts.append(description)
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
# 1. dry run
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_dry_run_changes_nothing(gh_repo: GhRepo, two_commits: Progress) -> None:
    progress = two_commits
    b1, b2 = gh_repo.stack_branch(1), gh_repo.stack_branch(2)

    res = gh_repo.export("--dry-run", "-vv")  # -vv: the network calls are asserted

    assert res.rc == 0, res.err
    assert "Dry run: nothing was changed." in res.out
    assert "Contacting origin..." in res.out
    assert "Stack of 2 commits on " + gh_repo.branch in res.out
    assert f"create PR for {progress.shas[0][:8]}: {b1} -> main" in res.out
    assert f"create PR for {progress.shas[1][:8]}: {b2} -> {b1}" in res.out
    # Nothing local moved and nothing reached GitHub.
    assert gh_repo.head() == progress.shas[1]
    assert gh_repo.shas() == progress.shas
    assert not any("stack-info" in m for m in gh_repo.messages())
    assert gh_repo.remote_branches() == {}
    assert gh_repo.open_prs() == []

    # Even a dry run fetches the target branch (one explicit refspec, nothing
    # else) and then asks the remote once for the branches matching the
    # template, which is what allocating names needs. Allocating also needs
    # the login; no pull request is known yet, so none is looked up.
    assert remote_git_calls(res.err) == [FETCH_TARGET, ls_remote_template(gh_repo)]
    assert [c[: len(USER_LOOKUP)] for c in gh_calls(res.err)] == [USER_LOOKUP]
    assert logged(res.err, "gh pr") == []
    # The fetch only refreshed refs/remotes/origin/main: no new tracking ref.
    assert gh_repo.remote_tracking_refs() == gh_repo.initial_tracking_refs
    progress.done.add("dry_run")


# --------------------------------------------------------------------------- #
# 2. first export, with a dirty working tree and index
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_export_creates_two_stacked_prs(gh_repo: GhRepo, two_commits: Progress) -> None:
    progress = two_commits
    progress.require("dry_run")
    b1, b2 = gh_repo.stack_branch(1), gh_repo.stack_branch(2)
    title_a, title_b = progress.titles

    (gh_repo.path / "a.txt").write_text("a, edited but not committed\n")
    (gh_repo.path / "staged.txt").write_text("staged, not committed\n")
    gh_repo.git("add", "staged.txt")
    status_before = gh_repo.status()
    assert status_before == " M a.txt\nA  staged.txt"

    res = gh_repo.export("-vv")  # -vv: the network calls are asserted below

    assert res.rc == 0, res.err
    assert "Exported 2 pull requests (2 new):" in res.out
    assert f"Branches pushed: {b1} (new), {b2} (new)" in res.out

    # Local side: messages gained exactly one stack-info trailer each.
    infos = stack_infos(gh_repo)
    assert [branch for _, branch in infos] == [b1, b2]
    n1, n2 = (pr_number(gh_repo, url) for url, _ in infos)
    assert n1 != n2
    url1, url2 = gh_repo.pr_url(n1), gh_repo.pr_url(n2)
    assert gh_repo.messages() == [
        f"{title_a}\n\n{DESCRIPTION_A}\n\nstack-info: PR: {url1}, branch: {b1}",
        f"{title_b}\n\nstack-info: PR: {url2}, branch: {b2}",
    ]
    shas = gh_repo.shas()
    assert shas != progress.shas
    assert gh_repo.head() == shas[1]
    assert gh_repo.head(gh_repo.branch) == shas[1]
    assert [gh_repo.tree(s) for s in shas] == progress.trees
    assert [gh_repo.identity(s) for s in shas] == progress.identities
    assert gh_repo.status() == status_before
    assert gh_repo.reflog(gh_repo.branch)[0] == "pstack-pr export"
    for number, url, title in ((n1, url1, title_a), (n2, url2, title_b)):
        summary = rf"#{number}\s+new\s+{re.escape(url)}  {re.escape(title)}$"
        assert re.search(summary, res.out, re.MULTILINE), res.out

    # Network side: one fetch of main, one ls-remote (the template glob, as no
    # commit had stack-info yet) whose empty answer makes both branches "new",
    # the login for allocating names and no PR lookup. Two pushes: first the
    # original commits, so that GitHub has the head branches when the PRs are
    # created, then the rewritten ones with leases on what was just pushed.
    orig_a, orig_b = progress.shas
    assert remote_git_calls(res.err) == [
        FETCH_TARGET,
        ls_remote_template(gh_repo),
        push_line((b1, "", orig_a), (b2, "", orig_b)),
        push_line((b1, orig_a, shas[0]), (b2, orig_b, shas[1])),
    ]
    assert len(logged(res.err, USER_LOOKUP[2:])) == 1
    assert logged(res.err, GRAPHQL_LOOKUP[2:]) == []
    assert logged(res.err, "gh pr view") == []
    assert logged(res.err, "gh pr list") == []
    assert len(logged(res.err, "gh pr create")) == 2

    # Remote side: both branches point at the rewritten commits.
    assert gh_repo.remote_branches() == {b1: shas[0], b2: shas[1]}

    pr1 = gh_repo.wait_for_pr(n1, head_oid=shas[0])
    pr2 = gh_repo.wait_for_pr(n2, head_oid=shas[1])
    assert (pr1.state, pr1.is_draft, pr1.base, pr1.head) == ("OPEN", False, "main", b1)
    assert (pr2.state, pr2.is_draft, pr2.base, pr2.head) == ("OPEN", False, b1, b2)
    assert (pr1.title, pr2.title) == (title_a, title_b)
    assert (pr1.url, pr2.url) == (url1, url2)
    assert "Stacked PRs:" in pr1.body
    assert "Stacked PRs:" in pr2.body
    numbers = [n1, n2]
    assert normalize(pr1.body) == expected_body(numbers, n1, title_a, DESCRIPTION_A)
    assert normalize(pr2.body) == expected_body(numbers, n2, title_b)
    assert [pr.number for pr in gh_repo.open_prs()] == sorted(numbers)

    progress.shas = shas
    progress.prs = numbers
    progress.done.add("exported")


# --------------------------------------------------------------------------- #
# 3. re-export without changes
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_reexport_without_changes_is_a_noop(
    gh_repo: GhRepo, two_commits: Progress
) -> None:
    progress = two_commits
    progress.require("exported")
    b1, b2 = gh_repo.stack_branch(1), gh_repo.stack_branch(2)
    n1, n2 = progress.prs
    status_before = gh_repo.status()

    res = gh_repo.export("-vv")  # -vv: every git and gh command is asserted

    assert res.rc == 0, res.err
    assert "Up to date: 2 pull requests, nothing to push." in res.out
    assert re.search(rf"#{n1}\s+unchanged", res.out)
    assert re.search(rf"#{n2}\s+unchanged", res.out)
    assert "Branches pushed:" not in res.out
    assert "Plan:" not in res.out
    assert "Everything is up to date; nothing to do." in res.out
    assert gh_repo.head() == progress.shas[1]
    assert gh_repo.shas() == progress.shas
    assert gh_repo.status() == status_before
    assert gh_repo.reflog(gh_repo.branch)[0] == "pstack-pr export"
    assert gh_repo.remote_branches() == {b1: progress.shas[0], b2: progress.shas[1]}

    # Exactly three round trips: the target branch is fetched (even though the
    # clone is up to date), the remote is asked once for exactly the two stack
    # branches, and both pull requests are looked up in one GraphQL request.
    # The login is not needed since every commit has a stack-info trailer, and
    # 'gh pr view' is never used during planning.
    assert "Contacting origin..." in res.out
    assert "Fetching origin..." not in res.out
    main_sha = gh_repo.head("origin/main")
    assert f"(base: origin/main @ {main_sha[:8]})" in res.out
    commands = logged(res.err)
    assert commands, res.err
    assert logged(res.err, "git fetch") == [FETCH_TARGET]
    assert logged(res.err, "git ls-remote") == [ls_remote_stack(b1, b2)]
    assert logged(res.err, "git push") == []
    assert remote_git_calls(res.err) == [FETCH_TARGET, ls_remote_stack(b1, b2)]
    assert len(logged(res.err, GRAPHQL_LOOKUP[2:])) == 1
    assert logged(res.err, USER_LOOKUP[2:]) == []
    assert logged(res.err, "gh pr view") == []
    assert logged(res.err, "gh pr list") == []
    assert gh_calls(res.err) == logged(res.err, GRAPHQL_LOOKUP[2:])
    # The fetch comes first: the stack is read against the fresh origin/main.
    assert commands.index(FETCH_TARGET) < commands.index(ls_remote_stack(b1, b2))

    # Nothing but main and this run's own stack branches ever became a
    # remote-tracking ref of the clone: no other branch of the remote was
    # fetched, whatever else is going on in the scratch repository.
    tracking = gh_repo.remote_tracking_refs()
    own = {gh_repo.tracking_ref(b1), gh_repo.tracking_ref(b2)}
    assert gh_repo.tracking_ref("main") in tracking
    assert gh_repo.head(gh_repo.tracking_ref("main")) == main_sha
    assert tracking - gh_repo.initial_tracking_refs <= own, sorted(tracking)
    assert {ref for ref in tracking if "/itest/" in ref} <= own, sorted(tracking)
    progress.done.add("reexported")


# --------------------------------------------------------------------------- #
# 3b. a branch pushed by someone else is never fetched
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_unrelated_remote_branch_is_never_fetched(
    gh_repo: GhRepo, second_clone: GhRepo, two_commits: Progress
) -> None:
    progress = two_commits
    progress.require("reexported")
    b1, b2 = gh_repo.stack_branch(1), gh_repo.stack_branch(2)
    unrelated = gh_repo.unrelated_branch
    assert second_clone.path != gh_repo.path
    assert second_clone.head("origin/main") == gh_repo.head("origin/main")

    # Another clone pushes a commit the first clone has never seen, on a branch
    # that even matches this run's branch template glob (itest/<runid>/*).
    (second_clone.path / "unrelated.txt").write_text("not part of the stack\n")
    second_clone.git("add", "unrelated.txt")
    second_clone.git("commit", "-q", "-m", f"Unrelated commit (itest {gh_repo.run_id})")
    sha = second_clone.head()
    second_clone.git("push", "--quiet", "origin", f"HEAD:refs/heads/{unrelated}")
    progress.unrelated[unrelated] = sha  # the session cleanup deletes it again
    assert gh_repo.remote_branches() == {
        b1: progress.shas[0],
        b2: progress.shas[1],
        unrelated: sha,
    }
    assert second_clone.has_commit(sha)
    assert not gh_repo.has_commit(sha)
    assert not gh_repo.has_ref(gh_repo.tracking_ref(unrelated))
    status_before = gh_repo.status()

    res = gh_repo.export("-vv")  # -vv: the git commands are asserted below

    assert res.rc == 0, res.err
    assert "Up to date: 2 pull requests, nothing to push." in res.out
    assert "Contacting origin..." in res.out
    assert logged(res.err), res.err
    # The same three round trips as any no-op re-export: the fetch names main
    # explicitly and the ls-remote names the two stack branches, so the remote
    # is never even asked about the new branch.
    assert logged(res.err, "git fetch") == [FETCH_TARGET]
    assert logged(res.err, "git ls-remote") == [ls_remote_stack(b1, b2)]
    assert logged(res.err, "git push") == []
    assert remote_git_calls(res.err) == [FETCH_TARGET, ls_remote_stack(b1, b2)]
    assert len(logged(res.err, GRAPHQL_LOOKUP[2:])) == 1
    assert logged(res.err, USER_LOOKUP[2:]) == []
    assert logged(res.err, "gh pr view") == []
    assert unrelated not in res.err, res.err
    # The unrelated branch reached neither the refs nor the object store of
    # the clone, and the stack itself was left alone.
    assert not gh_repo.has_ref(gh_repo.tracking_ref(unrelated))
    assert gh_repo.tracking_ref(unrelated) not in gh_repo.remote_tracking_refs()
    assert not gh_repo.has_commit(sha)
    tracking = gh_repo.remote_tracking_refs()
    own = {gh_repo.tracking_ref(b1), gh_repo.tracking_ref(b2)}
    assert tracking - gh_repo.initial_tracking_refs <= own, sorted(tracking)
    assert gh_repo.head() == progress.shas[1]
    assert gh_repo.shas() == progress.shas
    assert gh_repo.status() == status_before
    assert gh_repo.remote_branches() == {
        b1: progress.shas[0],
        b2: progress.shas[1],
        unrelated: sha,
    }
    open_prs = gh_repo.open_prs()
    assert [pr.number for pr in open_prs] == sorted(progress.prs)
    assert all(pr.head != unrelated for pr in open_prs)
    progress.done.add("unrelated")


# --------------------------------------------------------------------------- #
# 4. amend the top commit
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_amending_top_commit_moves_only_its_branch(
    gh_repo: GhRepo, two_commits: Progress
) -> None:
    progress = two_commits
    progress.require("reexported")
    b1, b2 = gh_repo.stack_branch(1), gh_repo.stack_branch(2)
    n1, n2 = progress.prs
    message_before = gh_repo.message()

    # Unstage staged.txt (it stays as an untracked file) so that the amend only
    # picks up b.txt; a.txt keeps its uncommitted modification.
    gh_repo.git("rm", "--cached", "-q", "staged.txt")
    (gh_repo.path / "b.txt").write_text("b, second version\n")
    gh_repo.git("add", "b.txt")
    gh_repo.git("commit", "-q", "--amend", "--no-edit")
    amended = gh_repo.head()
    assert amended != progress.shas[1]
    assert gh_repo.message() == message_before  # stack-info survived the amend
    status_before = gh_repo.status()
    assert status_before == " M a.txt\n?? staged.txt"

    res = gh_repo.export("-vv")  # -vv: plan, progress and network calls asserted

    assert res.rc == 0, res.err
    assert "Up to date" not in res.out
    assert f"push the stack to origin (--atomic --force-with-lease): {b2}" in res.out
    assert "rewrite" not in res.out  # the message was already correct
    assert "Exported 2 pull requests (1 updated, 1 unchanged):" in res.out
    assert re.search(rf"#{n2}\s+updated", res.out)
    assert re.search(rf"#{n1}\s+unchanged", res.out)
    assert f"Branches pushed: {b2} (updated)" in res.out
    assert "Contacting origin..." in res.out
    assert "Fetching origin..." not in res.out
    assert gh_repo.head() == amended
    assert gh_repo.shas() == [progress.shas[0], amended]
    assert gh_repo.status() == status_before
    assert gh_repo.remote_branches() == {
        b1: progress.shas[0],
        b2: amended,
        **progress.unrelated,
    }

    # One fetch, one ls-remote, one PR lookup, no login; the single push moves
    # only b2, and its lease is the sha the ls-remote reported for b2 (which is
    # also what made the result say "(updated)" rather than "(new)").
    assert remote_git_calls(res.err) == [
        FETCH_TARGET,
        ls_remote_stack(b1, b2),
        push_line((b2, progress.shas[1], amended)),
    ]
    assert len(logged(res.err, GRAPHQL_LOOKUP[2:])) == 1
    assert logged(res.err, USER_LOOKUP[2:]) == []
    assert logged(res.err, "gh pr view") == []

    pr2 = gh_repo.wait_for_pr(n2, head_oid=amended)
    assert (pr2.state, pr2.is_draft, pr2.base, pr2.head) == ("OPEN", False, b1, b2)
    pr1 = gh_repo.pr(n1)
    assert (pr1.state, pr1.base, pr1.head_oid) == ("OPEN", "main", progress.shas[0])
    assert [pr.number for pr in gh_repo.open_prs()] == sorted([n1, n2])

    progress.shas[1] = amended
    progress.done.add("amended")


# --------------------------------------------------------------------------- #
# 5. swap the two commits
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_swapping_commits_retargets_prs_without_closing_them(
    gh_repo: GhRepo, two_commits: Progress
) -> None:
    progress = two_commits
    progress.require("amended")
    b1, b2 = gh_repo.stack_branch(1), gh_repo.stack_branch(2)
    n1, n2 = progress.prs
    title_a, title_b = progress.titles
    sha_a, sha_b = progress.shas
    message_a, message_b = gh_repo.message(sha_a), gh_repo.message(sha_b)

    # Reorder with cherry-picks, which need a clean working tree.
    gh_repo.git("checkout", "-q", "--", "a.txt")
    (gh_repo.path / "staged.txt").unlink()
    assert gh_repo.status() == ""
    gh_repo.git("checkout", "-q", "--detach", "origin/main")
    gh_repo.git("cherry-pick", sha_b)
    gh_repo.git("cherry-pick", sha_a)
    gh_repo.git("checkout", "-q", "-B", gh_repo.branch)
    new_b, new_a = gh_repo.shas()
    assert (gh_repo.message(new_b), gh_repo.message(new_a)) == (message_b, message_a)
    assert new_b not in progress.shas
    assert new_a not in progress.shas

    res = gh_repo.export("-vv")  # -vv: plan lines and network calls asserted

    assert res.rc == 0, res.err
    assert (
        f"retarget PR #{n2} to 'main' during the push, marking it draft meanwhile"
        in res.out
    )
    assert f"update PR #{n1}: base -> {b2}, body (cross-links)" in res.out
    assert (
        f"update PR #{n2}: body (cross-links), mark ready for review again" in res.out
    )
    # The stack-info lines were still right, so no commit had to be rewritten.
    assert gh_repo.head() == new_a
    assert gh_repo.shas() == [new_b, new_a]
    assert gh_repo.status() == ""
    assert gh_repo.remote_branches() == {b2: new_b, b1: new_a, **progress.unrelated}

    # The stack now reads b2 then b1, and so does the ls-remote. Both branches
    # move in one atomic push whose leases are the shas that ls-remote reported.
    assert remote_git_calls(res.err) == [
        FETCH_TARGET,
        ls_remote_stack(b2, b1),
        push_line((b2, sha_b, new_b), (b1, sha_a, new_a)),
    ]
    assert len(logged(res.err, GRAPHQL_LOOKUP[2:])) == 1
    assert logged(res.err, USER_LOOKUP[2:]) == []
    assert logged(res.err, "gh pr view") == []

    pr2 = gh_repo.wait_for_pr(n2, head_oid=new_b, base="main", is_draft=False)
    pr1 = gh_repo.wait_for_pr(n1, head_oid=new_a, base=b2)
    assert (pr2.state, pr2.is_draft, pr2.base, pr2.head) == ("OPEN", False, "main", b2)
    assert (pr1.state, pr1.is_draft, pr1.base, pr1.head) == ("OPEN", False, b2, b1)
    numbers = [n2, n1]  # bottom first, as the stack now reads
    assert normalize(pr2.body) == expected_body(numbers, n2, title_b)
    assert normalize(pr1.body) == expected_body(numbers, n1, title_a, DESCRIPTION_A)
    assert [pr.number for pr in gh_repo.open_prs()] == sorted(numbers)

    # Several exports and pushes later the branch pushed from the other clone
    # still never reached this one: only main and the stack branches did.
    for branch, sha in progress.unrelated.items():
        assert not gh_repo.has_commit(sha)
        assert not gh_repo.has_ref(gh_repo.tracking_ref(branch))
    tracking = gh_repo.remote_tracking_refs()
    own = {gh_repo.tracking_ref(b1), gh_repo.tracking_ref(b2)}
    assert tracking - gh_repo.initial_tracking_refs <= own, sorted(tracking)
    progress.done.add("swapped")
