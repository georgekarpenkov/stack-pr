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


@dataclass
class Progress:
    """State handed from one test to the next (bottom of the stack first)."""

    titles: list[str] = field(default_factory=list)
    shas: list[str] = field(default_factory=list)
    trees: list[str] = field(default_factory=list)
    identities: list[str] = field(default_factory=list)
    prs: list[int] = field(default_factory=list)
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

    res = gh_repo.export("--dry-run")

    assert res.rc == 0, res.err
    assert "Dry run: nothing was changed." in res.out
    assert "Stack of 2 commits on " + gh_repo.branch in res.out
    assert f"create PR for {progress.shas[0][:8]}: {b1} -> main" in res.out
    assert f"create PR for {progress.shas[1][:8]}: {b2} -> {b1}" in res.out
    # Nothing local moved and nothing reached GitHub.
    assert gh_repo.head() == progress.shas[1]
    assert gh_repo.shas() == progress.shas
    assert not any("stack-info" in m for m in gh_repo.messages())
    assert gh_repo.remote_branches() == {}
    assert gh_repo.open_prs() == []
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

    res = gh_repo.export()

    assert res.rc == 0, res.err
    assert "Exported 2 pull requests:" in res.out

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
        summary = rf"#{number}\s+{re.escape(url)}  {re.escape(title)}$"
        assert re.search(summary, res.out, re.MULTILINE), res.out

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

    res = gh_repo.export()

    assert res.rc == 0, res.err
    assert "Everything is up to date; nothing to do." in res.out
    assert f"#{n1}" in res.out
    assert f"#{n2}" in res.out
    assert "Plan:" not in res.out
    assert gh_repo.head() == progress.shas[1]
    assert gh_repo.shas() == progress.shas
    assert gh_repo.status() == status_before
    assert gh_repo.reflog(gh_repo.branch)[0] == "pstack-pr export"
    assert gh_repo.remote_branches() == {b1: progress.shas[0], b2: progress.shas[1]}
    progress.done.add("reexported")


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

    res = gh_repo.export()

    assert res.rc == 0, res.err
    assert "Everything is up to date" not in res.out
    assert f"push the stack to origin (--atomic --force-with-lease): {b2}" in res.out
    assert "rewrite" not in res.out  # the message was already correct
    assert gh_repo.head() == amended
    assert gh_repo.shas() == [progress.shas[0], amended]
    assert gh_repo.status() == status_before
    assert gh_repo.remote_branches() == {b1: progress.shas[0], b2: amended}

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

    res = gh_repo.export()

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
    assert gh_repo.remote_branches() == {b2: new_b, b1: new_a}

    pr2 = gh_repo.wait_for_pr(n2, head_oid=new_b, base="main", is_draft=False)
    pr1 = gh_repo.wait_for_pr(n1, head_oid=new_a, base=b2)
    assert (pr2.state, pr2.is_draft, pr2.base, pr2.head) == ("OPEN", False, "main", b2)
    assert (pr1.state, pr1.is_draft, pr1.base, pr1.head) == ("OPEN", False, b2, b1)
    numbers = [n2, n1]  # bottom first, as the stack now reads
    assert normalize(pr2.body) == expected_body(numbers, n2, title_b)
    assert normalize(pr1.body) == expected_body(numbers, n1, title_a, DESCRIPTION_A)
    assert [pr.number for pr in gh_repo.open_prs()] == sorted(numbers)
    progress.done.add("swapped")
