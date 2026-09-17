"""End-to-end tests of failure modes, safety properties and recovery.

Everything runs against the offline harness from ``conftest.py``: a bare
repository standing in for GitHub and a fake ``gh`` whose post-receive hook
auto-closes pull requests the way GitHub does.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from pstack_pr.cli import main
from pstack_pr.stack import TMP_DRAFT_MARKER
from tests.conftest import FakeGitHub, Remote, RunExport, Work, git

SLUG = "github.com/octo/widgets"
PR_URL = "https://github.com/octo/widgets/pull/{}"
BRANCH = "testbot/stack/{}"


def stack_info(number: int) -> str:
    return f"stack-info: PR: {PR_URL.format(number)}, branch: {BRANCH.format(number)}"


def calls_after(fake_gh: FakeGitHub, start: int) -> list[list[str]]:
    calls: list[list[str]] = fake_gh.state()["calls"]
    return calls[start:]


def write_calls_after(fake_gh: FakeGitHub, start: int) -> list[list[str]]:
    writes = (["pr", "create"], ["pr", "edit"], ["pr", "ready"], ["pr", "close"])
    return [c for c in calls_after(fake_gh, start) if c[:2] in writes]


def n_calls(fake_gh: FakeGitHub) -> int:
    return len(fake_gh.state()["calls"])


def export_in_subprocess(work: Work, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the CLI in a fresh interpreter, for assertions on the ``-vv`` log.

    Under pytest the root logger already has handlers, so the CLI's
    ``logging.basicConfig`` (which the command log on stderr relies on) would
    be a no-op in-process.
    """
    return subprocess.run(
        [sys.executable, "-m", "pstack_pr", "export", *args],
        cwd=work.path,
        env=dict(os.environ),
        capture_output=True,
        text=True,
        check=False,
    )


def fetch_head(work: Work) -> Path:
    """Written by every ``git fetch``; a fresh clone does not have it."""
    return work.path / ".git" / "FETCH_HEAD"


def fetch_line(target: str = "main") -> str:
    """The ``-vv`` log line of the one fetch every export performs."""
    return (
        "$ git fetch --quiet --no-tags origin "
        f"+refs/heads/{target}:refs/remotes/origin/{target}\n"
    )


def has_commit(work: Work, rev: str) -> bool:
    """True if ``rev`` resolves to a commit in ``work``'s object store.

    Decided by the exit status of ``git cat-file -e``, not by parsing output.
    """
    proc = subprocess.run(
        ["git", "cat-file", "-e", f"{rev}^{{commit}}"],
        cwd=work.path,
        capture_output=True,
        check=False,
    )
    return proc.returncode == 0


def remote_tracking_refs(work: Work) -> dict[str, str]:
    """``refs/remotes/origin/*`` of ``work`` as branch name -> sha (HEAD left out)."""
    prefix = "refs/remotes/origin/"
    out = work.git("for-each-ref", "--format=%(refname) %(objectname)", prefix)
    refs = dict(line.split() for line in out.splitlines())
    return {
        name.removeprefix(prefix): sha
        for name, sha in refs.items()
        if name != prefix + "HEAD"
    }


def push_from_other_clone(
    tmp_path: Path, remote: Remote, branch: str, name: str
) -> str:
    """Commit file ``name`` on ``branch`` (forked from the remote's ``main``) in
    a separate clone of ``remote`` and push it; returns the new commit's sha."""
    clone = tmp_path / "other-clone"
    if not clone.exists():
        git("clone", "-q", str(remote.path), str(clone), cwd=tmp_path)
    git("fetch", "-q", "origin", cwd=clone)
    git("checkout", "-q", "-B", branch, "origin/main", cwd=clone)
    (clone / name).write_text(f"{name}\n")
    git("add", name, cwd=clone)
    git("commit", "-q", "-m", f"Add {name}", cwd=clone)
    git("push", "-q", "origin", f"HEAD:refs/heads/{branch}", cwd=clone)
    return git("rev-parse", "HEAD", cwd=clone)


def swap_top_two(work: Work) -> tuple[str, str]:
    """Reorder ``HEAD~1`` and ``HEAD`` keeping their messages (and stack-info)."""
    top, below = work.head("HEAD"), work.head("HEAD~1")
    work.git("reset", "-q", "--hard", "HEAD~2")
    work.git("cherry-pick", top)
    work.git("cherry-pick", below)
    return work.head("HEAD~1"), work.head("HEAD")


def add_merge_commit(work: Work, base: str, title: str) -> str:
    """Create ``side`` from ``base`` with one commit and merge it into HEAD."""
    current = work.git("symbolic-ref", "--short", "HEAD")
    work.git("checkout", "-q", "-b", "side", base)
    work.commit("side.txt", "Side change")
    work.git("checkout", "-q", current)
    work.git("merge", "-q", "--no-ff", "-m", title, "side")
    return work.head()


# --------------------------------------------------------------------------- #
# 1. Reordering commits: the PR that moves down must not be auto-closed
# --------------------------------------------------------------------------- #
def test_reorder_keeps_all_prs_open_via_transient_retarget(
    work: Work,
    remote: Remote,
    fake_gh: FakeGitHub,
    run_export: RunExport,
    stack3: list[str],
) -> None:
    rc, _, _ = run_export()
    assert rc == 0
    assert fake_gh.pr(3)["baseRefName"] == BRANCH.format(2)

    c_sha, b_sha = swap_top_two(work)  # order is now: a, c, b
    assert stack_info(3) in work.message(c_sha)
    assert stack_info(2) in work.message(b_sha)
    start = n_calls(fake_gh)
    head_before = work.head()

    rc, out, err = run_export("-v")
    assert rc == 0, err
    assert "retarget PR #3 to 'main' during the push, marking it draft meanwhile" in out
    assert "'testbot/stack/2' now sits above it in the stack" in out
    assert (
        "update PR #3: base -> testbot/stack/1, body (cross-links), "
        "mark ready for review again"
    ) in out

    # Without the retarget the hook would have closed #3: its head (c) became
    # an ancestor of its former base branch testbot/stack/2 (b, now on top).
    assert {n: pr["state"] for n, pr in fake_gh.prs().items()} == {
        1: "OPEN",
        2: "OPEN",
        3: "OPEN",
    }
    assert fake_gh.pr(3)["isDraft"] is False
    # Final bases follow the new order.
    assert fake_gh.pr(1)["baseRefName"] == "main"
    assert fake_gh.pr(3)["baseRefName"] == BRANCH.format(1)
    assert fake_gh.pr(2)["baseRefName"] == BRANCH.format(3)
    # The transient retarget and draft toggling happened in exactly this order:
    # everything up to the push first, then the final base/body edits. The
    # retarget edit also writes the body, which records the temporary draft.
    assert write_calls_after(fake_gh, start) == [
        ["pr", "ready", "3", "--repo", SLUG, "--undo"],
        ["pr", "edit", "3", "--repo", SLUG, "--body-file", "-", "--base", "main"],
        ["pr", "edit", "1", "--repo", SLUG, "--body-file", "-"],
        [
            "pr",
            "edit",
            "3",
            "--repo",
            SLUG,
            "--body-file",
            "-",
            "--base",
            BRANCH.format(1),
        ],
        ["pr", "ready", "3", "--repo", SLUG],
        [
            "pr",
            "edit",
            "2",
            "--repo",
            SLUG,
            "--body-file",
            "-",
            "--base",
            BRANCH.format(3),
        ],
    ]
    # Remote branches follow the commits; nothing had to be rewritten locally.
    assert remote.sha(BRANCH.format(3)) == c_sha
    assert remote.sha(BRANCH.format(2)) == b_sha
    assert remote.sha(BRANCH.format(1)) == work.head("HEAD~2")
    assert work.head() == head_before
    assert "rewrite" not in out
    # Every PR changed (bases and cross-links) and the two branches that had to
    # move are reported bottom of the stack first; #1 stayed where it was.
    assert "Exported 3 pull requests (3 updated):" in out
    assert f"   3  #2  updated    {PR_URL.format(2)}  Add b" in out
    assert f"   2  #3  updated    {PR_URL.format(3)}  Add c" in out
    assert f"   1  #1  updated    {PR_URL.format(1)}  Add a" in out
    assert (
        "Branches pushed: testbot/stack/3 (updated), testbot/stack/2 (updated)\n" in out
    )
    assert fake_gh.pr(3)["body"].startswith("Stacked PRs:\n * #2\n * __->__#3\n * #1\n")
    assert fake_gh.pr(2)["body"].startswith("Stacked PRs:\n * __->__#2\n * #3\n * #1\n")
    # The temporary-draft marker written during the retarget is gone again.
    assert all(TMP_DRAFT_MARKER not in pr["body"] for pr in fake_gh.prs().values())


def test_reorder_of_draft_pr_does_not_toggle_ready_state(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    rc, _, _ = run_export("--draft")
    assert rc == 0
    assert all(pr["isDraft"] for pr in fake_gh.prs().values())

    swap_top_two(work)
    start = n_calls(fake_gh)
    rc, out, err = run_export("-v")
    assert rc == 0, err
    assert "retarget PR #3 to 'main' during the push (GitHub would auto-close" in out
    assert "marking it draft" not in out
    assert fake_gh.calls("pr", "ready") == []
    assert [
        c for c in write_calls_after(fake_gh, start) if c[:2] == ["pr", "ready"]
    ] == []
    assert {n: pr["state"] for n, pr in fake_gh.prs().items()} == {
        1: "OPEN",
        2: "OPEN",
        3: "OPEN",
    }
    assert all(pr["isDraft"] for pr in fake_gh.prs().values())
    assert fake_gh.pr(3)["baseRefName"] == BRANCH.format(1)
    assert fake_gh.pr(2)["baseRefName"] == BRANCH.format(3)


def test_reorder_would_have_closed_pr_without_retarget(
    work: Work,
    remote: Remote,
    fake_gh: FakeGitHub,
    run_export: RunExport,
    stack3: list[str],
) -> None:
    """Sanity check of the harness: the hook really closes a contained PR."""
    rc, _, _ = run_export()
    assert rc == 0
    c_sha, b_sha = swap_top_two(work)
    work.git(
        "push", "-q", "--force", "origin",
        f"{c_sha}:refs/heads/{BRANCH.format(3)}", f"{b_sha}:refs/heads/{BRANCH.format(2)}",
    )  # fmt: skip
    assert fake_gh.pr(3)["state"] == "CLOSED"
    assert fake_gh.pr(3)["closedBy"] == "auto-close after push"
    assert fake_gh.pr(2)["state"] == "OPEN"


# --------------------------------------------------------------------------- #
# 2. Interrupted before the local branch is touched: nothing local changes
# --------------------------------------------------------------------------- #
def test_failure_during_pr_creation_leaves_local_repo_untouched(
    *,
    work: Work,
    remote: Remote,
    fake_gh: FakeGitHub,
    run_export: RunExport,
    stack3: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_GH_FAIL_ON", "pr create:2")
    messages_before = work.messages()
    reflog_before = work.reflog("feature")

    rc, out, err = run_export()
    assert rc == 1
    # A quiet run names the step that failed before the error, and there is no
    # result block to print.
    assert (
        "warning: failed while trying to: create PR for "
        f"{stack3[1][:8]}: {BRANCH.format(2)} -> {BRANCH.format(1)}"
    ) in err
    assert out.strip() == ""
    assert "warning: local branches were not modified" in err
    assert (
        "re-run 'pstack-pr export' to resume (branches pushed so far are reused)" in err
    )
    assert "fake gh: injected failure for 'pr create' invocation #2" in err
    assert "command failed with exit code 42" in err

    assert work.head() == stack3[-1]
    assert work.messages() == messages_before
    assert work.reflog("feature") == reflog_before
    assert work.status() == ""
    # Step 2 pushed the original commits; step 3 created one PR then failed.
    assert remote.branches() == {
        "main": work.head("origin/main"),
        BRANCH.format(1): stack3[0],
        BRANCH.format(2): stack3[1],
        BRANCH.format(3): stack3[2],
    }
    assert list(fake_gh.prs()) == [1]
    assert fake_gh.pr(1)["headRefName"] == BRANCH.format(1)


def test_rerun_after_interrupted_pr_creation_adopts_pushed_branches(
    *,
    work: Work,
    remote: Remote,
    fake_gh: FakeGitHub,
    run_export: RunExport,
    stack3: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_GH_FAIL_ON", "pr create:2")
    rc, _, _ = run_export()
    assert rc == 1
    monkeypatch.delenv("FAKE_GH_FAIL_ON")
    start = n_calls(fake_gh)

    rc, out, err = run_export("-v")
    assert rc == 0, err
    assert out.count("(recovered)") == 3
    # Original commits are already on the remote, so nothing is pushed before
    # the missing PRs are created.
    assert "new branch" not in out
    assert [c[:2] for c in write_calls_after(fake_gh, start)] == [
        ["pr", "create"],
        ["pr", "create"],
        ["pr", "edit"],
        ["pr", "edit"],
        ["pr", "edit"],
    ]
    assert sorted(fake_gh.prs()) == [1, 2, 3]
    assert {n: pr["headRefName"] for n, pr in fake_gh.prs().items()} == {
        1: BRANCH.format(1),
        2: BRANCH.format(2),
        3: BRANCH.format(3),
    }
    assert fake_gh.pr(1)["title"] == "Add a"
    assert fake_gh.pr(2)["title"] == "Add b"
    assert fake_gh.pr(3)["title"] == "Add c"
    # PR #1 survived the interrupted run, so only two PRs are new. All three
    # branches were pushed by that run already, so none of them is new.
    assert "Exported 3 pull requests (2 new, 1 updated):" in out
    assert f"   3  #3  new        {PR_URL.format(3)}  Add c" in out
    assert f"   2  #2  new        {PR_URL.format(2)}  Add b" in out
    assert f"   1  #1  updated    {PR_URL.format(1)}  Add a" in out
    assert (
        "Branches pushed: testbot/stack/1 (updated), testbot/stack/2 (updated), "
        "testbot/stack/3 (updated)\n"
    ) in out

    new_shas = work.shas()
    assert new_shas != stack3
    assert work.messages() == [
        f"Add a\n\n{stack_info(1)}",
        f"Add b\n\n{stack_info(2)}",
        f"Add c\n\n{stack_info(3)}",
    ]
    assert work.tree() == work.tree(stack3[-1])
    # The recovery run also pushed the rewritten commits, so a third run has
    # nothing left to do.
    assert [remote.sha(BRANCH.format(i)) for i in (1, 2, 3)] == new_shas
    rc, out, err = run_export("-v")
    assert rc == 0, err
    assert "Everything is up to date; nothing to do." in out
    assert "create PR" not in out
    assert "rewrite" not in out
    assert "Up to date: 3 pull requests, nothing to push." in out
    assert out.count("  unchanged  ") == 3
    assert "Branches pushed:" not in out
    assert [remote.sha(BRANCH.format(i)) for i in (1, 2, 3)] == new_shas


def test_recovery_run_pushes_rewritten_commits_to_remote(
    *,
    work: Work,
    remote: Remote,
    fake_gh: FakeGitHub,
    run_export: RunExport,
    stack3: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_GH_FAIL_ON", "pr create:2")
    rc, _, _ = run_export()
    assert rc == 1
    monkeypatch.delenv("FAKE_GH_FAIL_ON")

    rc, out, err = run_export("-v")
    assert rc == 0, err
    assert work.shas() != stack3
    assert "push the stack to origin" in out
    assert (
        "Branches pushed: testbot/stack/1 (updated), testbot/stack/2 (updated), "
        "testbot/stack/3 (updated)\n"
    ) in out
    assert [remote.sha(BRANCH.format(i)) for i in (1, 2, 3)] == work.shas()


def test_recovery_adopts_pushed_branches_known_only_to_the_remote(
    *,
    work: Work,
    remote: Remote,
    fake_gh: FakeGitHub,
    run_export: RunExport,
    stack3: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Branches pushed by an interrupted run are adopted by commit sha from
    ``git ls-remote``, not from local remote-tracking refs and not from a
    fetch of those branches: with the tracking refs the interrupted push left
    behind deleted, the re-run still recovers every branch while the only
    fetch is the one of the target branch."""
    monkeypatch.setenv("FAKE_GH_FAIL_ON", "pr create:2")
    rc, _, _ = run_export()
    assert rc == 1
    monkeypatch.delenv("FAKE_GH_FAIL_ON")
    for n in (1, 2, 3):
        work.git("update-ref", "-d", f"refs/remotes/origin/{BRANCH.format(n)}")
    assert list(remote_tracking_refs(work)) == ["main"]

    # No commit has a stack-info yet, so no branch name is known up front: the
    # remote is asked for every branch matching the template, and the login
    # is looked up because a name might have to be allocated.
    proc = export_in_subprocess(work, "-vv", "-n")
    assert proc.returncode == 0, proc.stderr
    ls_remote = "$ git ls-remote --heads --refs origin 'refs/heads/testbot/stack/*'\n"
    assert ls_remote in proc.stderr
    assert proc.stderr.count("$ git ls-remote") == 1
    assert fetch_line() in proc.stderr
    assert proc.stderr.count("$ git fetch") == 1
    assert proc.stderr.index(fetch_line()) < proc.stderr.index(ls_remote)
    assert "$ gh api --hostname github.com user --jq .login\n" in proc.stderr
    assert "graphql" not in proc.stderr
    assert proc.stdout.count("(recovered)") == 3
    # The stack branches were adopted without being fetched: no tracking ref
    # of theirs came back, only the target branch was touched.
    assert list(remote_tracking_refs(work)) == ["main"]

    start = n_calls(fake_gh)
    rc, out, err = run_export("-v")
    assert rc == 0, err
    assert out.count("(recovered)") == 3
    for n, title in ((1, "Add a"), (2, "Add b"), (3, "Add c")):
        assert f"{BRANCH.format(n)}  {title} (recovered)" in out
    assert "new branch" not in out
    # The PRs of adopted branches are found by head branch; without trailers
    # there is no batched lookup by number.
    calls = calls_after(fake_gh, start)
    assert calls[0] == ["api", "--hostname", "github.com", "user", "--jq", ".login"]
    assert [c[:2] for c in calls[1:]] == [
        ["pr", "list"],
        ["pr", "list"],
        ["pr", "list"],
        ["pr", "create"],
        ["pr", "create"],
        ["pr", "edit"],
        ["pr", "edit"],
        ["pr", "edit"],
    ]
    assert all("graphql" not in c for c in calls)
    assert sorted(fake_gh.prs()) == [1, 2, 3]
    assert {n: pr["headRefName"] for n, pr in fake_gh.prs().items()} == {
        1: BRANCH.format(1),
        2: BRANCH.format(2),
        3: BRANCH.format(3),
    }
    assert "Exported 3 pull requests (2 new, 1 updated):" in out
    assert (
        "Branches pushed: testbot/stack/1 (updated), testbot/stack/2 (updated), "
        "testbot/stack/3 (updated)\n"
    ) in out
    assert [remote.sha(BRANCH.format(i)) for i in (1, 2, 3)] == work.shas()
    # The tracking refs of the stack branches are back only because the tool
    # pushed them (git records every branch it pushes); nothing fetched them.
    assert remote_tracking_refs(work) == {
        "main": work.head("origin/main"),
        **{BRANCH.format(i): s for i, s in zip((1, 2, 3), work.shas(), strict=True)},
    }


# --------------------------------------------------------------------------- #
# 3. Interrupted after the local branch was updated: only the remote is fixed
# --------------------------------------------------------------------------- #
def test_rerun_after_local_update_only_finishes_remote_side(
    work: Work,
    remote: Remote,
    fake_gh: FakeGitHub,
    run_export: RunExport,
    stack3: list[str],
) -> None:
    rc, _, _ = run_export()
    assert rc == 0
    a1, b1, c1 = work.shas()
    # Simulate a crash between step 5 and 7: the remote lags behind the local
    # branch and the PR bodies do not have their cross-links yet.
    work.git(
        "push", "-q", "--force", "origin", f"{stack3[1]}:refs/heads/{BRANCH.format(2)}"
    )
    assert remote.sha(BRANCH.format(2)) == stack3[1]
    for n in (1, 2, 3):
        fake_gh.set_body(n, "")
    reflog_before = work.reflog("feature")
    start = n_calls(fake_gh)

    rc, out, err = run_export("-v")
    assert rc == 0, err
    assert "rewrite" not in out
    assert "move feature" not in out
    assert "create PR" not in out
    assert (
        "push the stack to origin (--atomic --force-with-lease): testbot/stack/2" in out
    )
    # Only branch 2 lagged behind; every PR got its cross-links back.
    assert "Exported 3 pull requests (3 updated):" in out
    assert "Branches pushed: testbot/stack/2 (updated)\n" in out
    assert work.head() == c1
    assert work.shas() == [a1, b1, c1]
    assert work.reflog("feature") == reflog_before
    assert remote.sha(BRANCH.format(2)) == b1
    assert [c[:3] for c in write_calls_after(fake_gh, start)] == [
        ["pr", "edit", "1"],
        ["pr", "edit", "2"],
        ["pr", "edit", "3"],
    ]
    assert fake_gh.pr(2)["body"] == (
        "Stacked PRs:\n * #3\n * __->__#2\n * #1\n\n--- --- ---\n\n### Add b"
    )


# --------------------------------------------------------------------------- #
# 4. Merge commits in the stack
# --------------------------------------------------------------------------- #
def test_merge_commit_in_stack_is_rejected_before_any_write(
    work: Work, remote: Remote, fake_gh: FakeGitHub, run_export: RunExport
) -> None:
    work.commit("a.txt", "Add a")
    merge = add_merge_commit(work, "origin/main", "Merge side into feature")
    head_before = work.head()
    branches_before = remote.branches()

    rc, _, err = run_export()
    assert rc == 1
    assert "the stack must be linear, but contains merge commits:" in err
    assert f"  {merge[:8]} Merge side into feature" in err
    assert remote.branches() == branches_before
    assert fake_gh.calls() == []
    assert work.head() == head_before


# --------------------------------------------------------------------------- #
# 5. A commit references a pull request that is no longer open
# --------------------------------------------------------------------------- #
def test_closed_pr_referenced_by_commit_aborts_export(
    work: Work,
    remote: Remote,
    fake_gh: FakeGitHub,
    run_export: RunExport,
    stack3: list[str],
) -> None:
    rc, _, _ = run_export()
    assert rc == 0
    fake_gh.close(2)
    shas = work.shas()
    branches_before = remote.branches()
    start = n_calls(fake_gh)

    rc, _, err = run_export()
    assert rc == 1
    assert f"commit {shas[1][:8]} (Add b) references PR #2, which is CLOSED." in err
    assert PR_URL.format(2) in err
    assert "If the change was merged already, rebase onto the target branch" in err
    assert "remove the 'stack-info:' line from the commit message" in err
    assert remote.branches() == branches_before
    assert work.head() == shas[-1]
    assert write_calls_after(fake_gh, start) == []


def test_stack_info_pointing_at_missing_pr_aborts_before_any_write(
    work: Work,
    remote: Remote,
    fake_gh: FakeGitHub,
    run_export: RunExport,
    stack3: list[str],
) -> None:
    """Existing pull requests are looked up in one batched GraphQL request; a
    number GitHub cannot resolve fails that lookup, before anything is pushed,
    created or edited."""
    rc, _, _ = run_export()
    assert rc == 0
    # Point the top commit at a pull request that was never created.
    work.git("commit", "-q", "--amend", "-m", f"Add c\n\n{stack_info(7)}")
    top = work.head()
    branches_before = remote.branches()
    start = n_calls(fake_gh)

    rc, out, err = run_export("-v")
    assert rc == 1
    assert (
        "error: GitHub rejected the pull request lookup in "
        "https://github.com/octo/widgets:"
    ) in err
    assert "Could not resolve to a PullRequest with the number of 7." in err
    assert "Plan:" not in out
    assert "push" not in out
    assert remote.branches() == branches_before
    assert work.head() == top
    assert write_calls_after(fake_gh, start) == []
    assert sorted(fake_gh.prs()) == [1, 2, 3]
    # A single 'gh api graphql' call covered all three numbers: no per-PR
    # 'gh pr view', and no username lookup since every commit has a trailer.
    new_calls = calls_after(fake_gh, start)
    assert [c[:4] for c in new_calls] == [
        ["api", "--hostname", "github.com", "graphql"]
    ]
    query = next(a for a in new_calls[0] if a.startswith("query="))
    assert "pullRequest(number: 1)" in query
    assert "pullRequest(number: 2)" in query
    assert "pullRequest(number: 7)" in query
    assert "pullRequest(number: 3)" not in query


# --------------------------------------------------------------------------- #
# 6. A rebase is in progress
# --------------------------------------------------------------------------- #
def test_rebase_in_progress_aborts_before_contacting_the_remote(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    (work.path / ".git" / "rebase-merge").mkdir()
    assert not fetch_head(work).exists()

    rc, _, err = run_export()
    assert rc == 1
    assert "a rebase is in progress; finish or abort it first" in err
    assert fake_gh.calls() == []
    assert not fetch_head(work).exists()
    assert work.head() == stack3[-1]

    proc = export_in_subprocess(work, "-vv")
    assert proc.returncode == 1
    assert "a rebase is in progress" in proc.stderr
    assert "$ git ls-remote" not in proc.stderr
    assert "$ git fetch" not in proc.stderr
    assert "$ gh" not in proc.stderr


def test_rebase_apply_in_progress_is_detected_too(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    (work.path / ".git" / "rebase-apply").mkdir()
    rc, _, err = run_export()
    assert rc == 1
    assert "a rebase is in progress" in err
    assert fake_gh.calls() == []


# --------------------------------------------------------------------------- #
# 7. Target branch problems
# --------------------------------------------------------------------------- #
def test_missing_target_branch(
    work: Work,
    remote: Remote,
    fake_gh: FakeGitHub,
    run_export: RunExport,
    stack3: list[str],
) -> None:
    branches_before = remote.branches()
    rc, out, err = run_export("-v", "-T", "develop")
    assert rc == 1
    assert "Contacting origin..." in out
    assert "Fetching" not in out
    assert "Plan:" not in out
    assert "target branch 'origin/develop' does not exist" in err
    assert "seems to use 'master'" not in err
    assert fake_gh.calls() == []
    assert work.head() == stack3[-1]
    assert remote.branches() == branches_before
    # The fetch that found the branch missing left no tracking ref behind.
    assert list(remote_tracking_refs(work)) == ["main"]

    # The one fetch of the target branch is what reports it missing, and the
    # export stops right there: no lookup of stack branches (the 'master' hint
    # is only tried for target 'main'), no GitHub call, nothing pushed.
    proc = export_in_subprocess(work, "-vv", "-T", "develop")
    assert proc.returncode == 1
    assert "error: target branch 'origin/develop' does not exist" in proc.stderr
    assert fetch_line("develop") in proc.stderr
    assert proc.stderr.count("$ git fetch") == 1
    assert "couldn't find remote ref refs/heads/develop" in proc.stderr
    assert "$ git ls-remote" not in proc.stderr
    assert "$ git push" not in proc.stderr
    assert "$ gh" not in proc.stderr
    assert fake_gh.calls() == []
    assert remote.branches() == branches_before
    assert list(remote_tracking_refs(work)) == ["main"]


def test_repository_using_master_gets_a_hint(
    work: Work, remote: Remote, fake_gh: FakeGitHub, run_export: RunExport
) -> None:
    work.git("push", "-q", "origin", "origin/main:refs/heads/master")
    git("symbolic-ref", "HEAD", "refs/heads/master", cwd=remote.path)
    work.git("push", "-q", "origin", "--delete", "main")
    assert sorted(remote.branches()) == ["master"]
    work.commit("a.txt", "Add a")

    rc, _, err = run_export()
    assert rc == 1
    assert "target branch 'origin/main' does not exist" in err
    assert "This repository seems to use 'master'; pass '--target master'" in err
    assert "set 'target = master' in the [repo] section of .pstack-pr.cfg" in err
    assert fake_gh.calls() == []
    assert "main" not in remote_tracking_refs(work)

    # The fetch of 'main' reports it missing; only then is the remote asked
    # whether it has 'master' (for the hint), and the export stops there.
    proc = export_in_subprocess(work, "-vv")
    assert proc.returncode == 1
    assert "This repository seems to use 'master'" in proc.stderr
    assert fetch_line() in proc.stderr
    assert proc.stderr.count("$ git fetch") == 1
    ls_remote = "$ git ls-remote --heads --refs origin refs/heads/master\n"
    assert ls_remote in proc.stderr
    assert proc.stderr.count("$ git ls-remote") == 1
    assert proc.stderr.index(fetch_line()) < proc.stderr.index(ls_remote)
    assert "$ git push" not in proc.stderr
    assert "$ gh" not in proc.stderr
    assert fake_gh.calls() == []
    assert sorted(remote.branches()) == ["master"]
    assert "main" not in remote_tracking_refs(work)

    rc, _, err = run_export("-T", "master")
    assert rc == 0, err
    assert fake_gh.pr(1)["baseRefName"] == "master"
    # This run fetched 'master' (and only that): its tracking ref is current,
    # and no 'main' tracking ref was conjured up.
    assert remote_tracking_refs(work)["master"] == remote.sha("master")
    assert "main" not in remote_tracking_refs(work)


def test_only_the_target_branch_is_fetched(
    *,
    tmp_path: Path,
    work: Work,
    remote: Remote,
    fake_gh: FakeGitHub,
    run_export: RunExport,
    stack3: list[str],
) -> None:
    """Every export fetches the target branch, and nothing but that branch:
    the explicit refspec makes it one cheap round trip that keeps the tracking
    ref fresh, while other remote branches never arrive (no tracking ref, no
    objects)."""
    old_main = work.head("origin/main")
    new_main = push_from_other_clone(tmp_path, remote, "main", "upstream.txt")
    unrelated = push_from_other_clone(tmp_path, remote, "unrelated", "unrelated.txt")
    assert remote.sha("main") == new_main
    assert not has_commit(work, new_main)
    assert not has_commit(work, unrelated)
    assert remote_tracking_refs(work) == {"main": old_main}

    proc = export_in_subprocess(work, "-vv", "-n")
    assert proc.returncode == 0, proc.stderr
    ls_remote = "$ git ls-remote --heads --refs origin 'refs/heads/testbot/stack/*'\n"
    assert fetch_line() in proc.stderr
    assert proc.stderr.count("$ git fetch") == 1
    assert ls_remote in proc.stderr
    assert proc.stderr.count("$ git ls-remote") == 1
    assert proc.stderr.index(fetch_line()) < proc.stderr.index(ls_remote)
    assert "unrelated" not in proc.stderr
    # The stack is measured against the merge base with the fresh tip.
    assert f"(base: origin/main @ {old_main[:8]})" in proc.stdout
    assert remote_tracking_refs(work) == {"main": new_main}
    assert has_commit(work, new_main)
    assert not has_commit(work, "refs/remotes/origin/unrelated")
    assert not has_commit(work, unrelated)

    # Nothing changed on the remote, and the fetch happens again all the same
    # (a no-op), still bringing nothing but the target branch.
    proc = export_in_subprocess(work, "-vv", "-n")
    assert proc.returncode == 0, proc.stderr
    assert fetch_line() in proc.stderr
    assert proc.stderr.count("$ git fetch") == 1
    assert proc.stderr.count("$ git ls-remote") == 1
    assert "unrelated" not in proc.stderr
    assert remote_tracking_refs(work) == {"main": new_main}
    assert not has_commit(work, unrelated)

    rc, out, err = run_export()
    assert rc == 0, err
    assert "Exported 3 pull requests (3 new):" in out
    assert fake_gh.pr(1)["baseRefName"] == "main"
    # git records the branches the tool pushed as tracking refs; that is all
    # that appeared, the unrelated branch is still unknown here.
    assert remote_tracking_refs(work) == {
        "main": new_main,
        **{BRANCH.format(i): s for i, s in zip((1, 2, 3), work.shas(), strict=True)},
    }
    assert not has_commit(work, "refs/remotes/origin/unrelated")
    assert not has_commit(work, unrelated)


# --------------------------------------------------------------------------- #
# 8. The working tree and index are never touched
# --------------------------------------------------------------------------- #
def test_uncommitted_changes_survive_export(
    work: Work, run_export: RunExport, stack3: list[str]
) -> None:
    (work.path / "a.txt").write_text("a.txt\nlocal edit\n")
    (work.path / "staged.txt").write_text("staged\n")
    work.git("add", "staged.txt")
    (work.path / "untracked.txt").write_text("untracked\n")
    assert sorted(work.status().splitlines()) == [
        " M a.txt",
        "?? untracked.txt",
        "A  staged.txt",
    ]

    rc, _, err = run_export()
    assert rc == 0, err
    assert work.head() != stack3[-1]
    assert work.tree() == work.tree(stack3[-1])
    assert sorted(work.status().splitlines()) == [
        " M a.txt",
        "?? untracked.txt",
        "A  staged.txt",
    ]
    assert (work.path / "a.txt").read_text() == "a.txt\nlocal edit\n"
    assert (work.path / "staged.txt").read_text() == "staged\n"
    assert (work.path / "untracked.txt").read_text() == "untracked\n"
    assert (work.path / "b.txt").read_text() == "b.txt\n"
    assert work.git("diff", "--cached", "--name-only") == "staged.txt"


def test_branch_move_is_recorded_in_reflog_once(
    work: Work, run_export: RunExport, stack3: list[str]
) -> None:
    reflog_before = work.reflog("feature")
    rc, _, err = run_export()
    assert rc == 0, err
    assert work.reflog("feature") == ["pstack-pr export", *reflog_before]
    assert work.git("rev-parse", "feature@{1}") == stack3[-1]


# --------------------------------------------------------------------------- #
# 9. Not a git repository
# --------------------------------------------------------------------------- #
def test_not_inside_a_git_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.chdir(empty)

    rc = main(["export"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "error: not inside a git repository" in captured.err
    assert captured.out == ""


# --------------------------------------------------------------------------- #
# 10. Two commits with the same stack-info
# --------------------------------------------------------------------------- #
def test_two_commits_claiming_the_same_branch_are_rejected(
    work: Work,
    remote: Remote,
    fake_gh: FakeGitHub,
    run_export: RunExport,
    stack3: list[str],
) -> None:
    rc, _, _ = run_export()
    assert rc == 0
    shas = work.shas()
    work.git("cherry-pick", "--keep-redundant-commits", shas[1])
    duplicate = work.head()
    assert stack_info(2) in work.message(duplicate)
    branches_before = remote.branches()
    start = n_calls(fake_gh)

    rc, _, err = run_export()
    assert rc == 1
    assert (
        f"commits {shas[1][:8]} and {duplicate[:8]} both claim branch "
        f"'{BRANCH.format(2)}'; remove the stack-info line from one of them"
    ) in err
    assert remote.branches() == branches_before
    assert work.head() == duplicate
    assert write_calls_after(fake_gh, start) == []


# --------------------------------------------------------------------------- #
# 11. --head names a commit no local branch points at
# --------------------------------------------------------------------------- #
def test_head_sha_without_branch_is_rejected_before_any_write(
    work: Work,
    remote: Remote,
    fake_gh: FakeGitHub,
    run_export: RunExport,
    stack3: list[str],
) -> None:
    """Without a local ref to record the stack-info in, a re-run would create
    duplicate pull requests, so the export refuses to start."""
    work.git("checkout", "-q", "main")
    main_sha = work.head()
    messages_before = work.messages("origin/main..feature")
    branches_before = remote.branches()

    # -v so that any plan or step text reaching stdout would be caught below.
    rc, out, err = run_export("-v", "-H", stack3[-1])
    assert rc == 1
    assert (
        f"error: no local branch points at '{stack3[-1]}' and HEAD does not "
        "contain it, so the stack-info trailers could not be recorded locally "
        "and re-running would create duplicate pull requests."
    ) in err
    assert "Check out a branch that contains these commits (or pass -H <branch>)" in err
    assert "Plan:" not in out
    assert "push" not in out
    # Nothing was pushed or created; the local repository is untouched.
    assert remote.branches() == branches_before
    assert fake_gh.prs() == {}
    assert fake_gh.write_calls() == []
    assert work.head() == main_sha
    assert work.head("feature") == stack3[-1]
    assert work.messages("origin/main..feature") == messages_before
    assert all("stack-info" not in m for m in messages_before)


def test_head_branch_option_moves_that_branch_not_the_checked_out_one(
    work: Work,
    remote: Remote,
    fake_gh: FakeGitHub,
    run_export: RunExport,
    stack3: list[str],
) -> None:
    work.git("checkout", "-q", "main")
    main_sha = work.head()

    rc, out, err = run_export("-v", "-H", "feature")
    assert rc == 0, err
    assert "warning:" not in err
    assert f"move feature from {stack3[-1][:8]} to the rewritten tip" in out
    assert work.head() == main_sha
    assert work.head("feature") != stack3[-1]
    assert work.messages("origin/main..feature") == [
        f"Add a\n\n{stack_info(1)}",
        f"Add b\n\n{stack_info(2)}",
        f"Add c\n\n{stack_info(3)}",
    ]
    assert remote.sha(BRANCH.format(3)) == work.head("feature")
    assert work.git("symbolic-ref", "--short", "HEAD") == "main"


def test_detached_head_is_moved_and_branch_left_alone(
    work: Work, remote: Remote, run_export: RunExport, stack3: list[str]
) -> None:
    work.git("checkout", "-q", "--detach")
    rc, out, err = run_export("-v")
    assert rc == 0, err
    assert f"move HEAD (detached) from {stack3[-1][:8]} to the rewritten tip" in out
    assert work.git("symbolic-ref", "-q", "HEAD", check=False) == ""
    assert work.head() != stack3[-1]
    assert stack_info(3) in work.message()
    assert work.head("feature") == stack3[-1]
    assert remote.sha(BRANCH.format(3)) == work.head()


# --------------------------------------------------------------------------- #
# 12. Commits above --head contain a merge
# --------------------------------------------------------------------------- #
def test_merge_above_head_option_is_rejected(
    work: Work, remote: Remote, fake_gh: FakeGitHub, run_export: RunExport
) -> None:
    a = work.commit("a.txt", "Add a")
    b = work.commit("b.txt", "Add b")
    merge = add_merge_commit(work, a, "Merge side")
    assert work.head("HEAD~1") == b
    branches_before = remote.branches()

    rc, _, err = run_export("-H", "HEAD~1")
    assert rc == 1
    assert (
        "the history between 'HEAD~1' and HEAD must be linear, but contains merge commits:"
        in err
    )
    assert f"  {merge[:8]} Merge side" in err
    assert remote.branches() == branches_before
    assert fake_gh.write_calls() == []
    assert work.head() == merge


# --------------------------------------------------------------------------- #
# 13. --base that is not an ancestor of --head
# --------------------------------------------------------------------------- #
def test_base_not_an_ancestor_of_head(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    work.git("branch", "-q", "other", "origin/main")
    work.git("checkout", "-q", "other")
    work.commit("other.txt", "Other change")
    work.git("checkout", "-q", "feature")

    rc, _, err = run_export("-B", "other")
    assert rc == 1
    assert "base 'other' is not an ancestor of 'HEAD'" in err
    assert fake_gh.calls() == []
    assert work.head() == stack3[-1]


# --------------------------------------------------------------------------- #
# 14. Nothing to export
# --------------------------------------------------------------------------- #
def test_nothing_to_export_when_feature_equals_main(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport
) -> None:
    assert work.head() == work.head("origin/main")
    rc, out, err = run_export()
    assert rc == 0
    assert "Nothing to export: no commits in origin/main..HEAD." in out
    assert err == ""
    assert fake_gh.calls() == []
    assert fake_gh.prs() == {}


# --------------------------------------------------------------------------- #
# 15. --force-with-lease against a concurrent update of a stack branch
# --------------------------------------------------------------------------- #
def install_ls_remote_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clone: Path, branch: str
) -> Path:
    """Put a ``git`` shim first on PATH that pushes ``clone``'s HEAD to
    ``branch`` right after the first ``git ls-remote`` that asks about
    ``branch`` (then behaves normally).

    That ls-remote is where the tool reads the shas it later passes as the
    ``--force-with-lease`` expected values, so the push lands exactly in the
    race window between planning and the tool's own push.
    """
    real_git = shutil.which("git")
    assert real_git is not None
    assert not real_git.startswith(str(tmp_path))
    shim_dir = tmp_path / "race-bin"
    shim_dir.mkdir()
    marker = tmp_path / "race-happened"
    shim = shim_dir / "git"
    shim.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = ls-remote ] && [ ! -e "{marker}" ]; then\n'
        '  case "$*" in\n'
        f'    *"refs/heads/{branch}"*)\n'
        f'      "{real_git}" "$@" || exit $?\n'
        f'      touch "{marker}"\n'
        f'      "{real_git}" -C "{clone}" push -q origin HEAD:refs/heads/{branch} '
        ">/dev/null || exit $?\n"
        "      exit 0\n"
        "      ;;\n"
        "  esac\n"
        "fi\n"
        f'exec "{real_git}" "$@"\n'
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")
    return marker


def test_push_lease_rejects_update_that_raced_the_planning_ls_remote(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    work: Work,
    remote: Remote,
    fake_gh: FakeGitHub,
    run_export: RunExport,
    stack3: list[str],
) -> None:
    """The ``--force-with-lease`` expected values are the shas ``git ls-remote``
    reported at planning time, so a branch that moves between that lookup and
    the push is protected: the atomic push is rejected as a whole and nothing
    is rewritten locally. The next run takes its lease from a fresh ls-remote
    (no fetch of the branch is needed) and goes through."""
    rc, _, _ = run_export()
    assert rc == 0
    a1, b1, c1 = work.shas()

    # Someone else prepares a commit on top of testbot/stack/2 in another clone.
    clone = tmp_path / "clone2"
    git("clone", "-q", str(remote.path), str(clone), cwd=tmp_path)
    git("checkout", "-q", BRANCH.format(2), cwd=clone)
    (clone / "theirs.txt").write_text("theirs\n")
    git("add", "theirs.txt", cwd=clone)
    git("commit", "-q", "-m", "Concurrent change", cwd=clone)
    theirs = git("rev-parse", "HEAD", cwd=clone)

    # We amend the content of commit 2 (message and stack-info unchanged).
    work.git("reset", "-q", "--hard", a1)
    b2 = work.commit("b.txt", message=work.message(b1), content="b v2\n")
    work.git("cherry-pick", c1)
    c2 = work.head()
    assert work.messages() == [work.message(a1), work.message(b1), work.message(c1)]

    marker = install_ls_remote_race(tmp_path, monkeypatch, clone, BRANCH.format(2))
    start = n_calls(fake_gh)
    rc, _, err = run_export()
    assert marker.exists()
    assert rc == 1
    assert (
        "warning: failed while trying to: push the stack to origin "
        f"(--atomic --force-with-lease): {BRANCH.format(2)}, {BRANCH.format(3)}"
    ) in err
    # The failed command is echoed with the leases it carried: the shas that
    # ls-remote reported before the concurrent push, not the new value.
    assert f"--force-with-lease=refs/heads/{BRANCH.format(2)}:{b1}" in err
    assert f"--force-with-lease=refs/heads/{BRANCH.format(3)}:{c1}" in err
    assert "stale info" in err
    assert "warning: local branches were not modified" in err
    # Atomic push: neither branch moved, and the concurrent commit is intact.
    assert remote.sha(BRANCH.format(2)) == theirs
    assert remote.sha(BRANCH.format(3)) == c1
    assert work.shas() == [a1, b2, c2]
    assert write_calls_after(fake_gh, start) == []
    assert fake_gh.pr(2)["state"] == "OPEN"
    # The concurrent commit never reached this clone: only the target branch
    # is fetched, so the tracking refs of the stack branches are what the
    # first export's push left them at.
    assert not has_commit(work, theirs)
    assert remote_tracking_refs(work) == {
        "main": work.head("origin/main"),
        BRANCH.format(1): a1,
        BRANCH.format(2): b1,
        BRANCH.format(3): c1,
    }

    # The re-run takes its lease from a fresh ls-remote and succeeds, although
    # the local remote-tracking ref still shows the value before the race.
    assert work.head(f"refs/remotes/origin/{BRANCH.format(2)}") == b1
    start = n_calls(fake_gh)
    rc, out, err = run_export()
    assert rc == 0, err
    # Only the amended commit and the one above it moved; #1 is untouched.
    assert "Exported 3 pull requests (2 updated, 1 unchanged):" in out
    assert f"   3  #3  updated    {PR_URL.format(3)}  Add c" in out
    assert f"   2  #2  updated    {PR_URL.format(2)}  Add b" in out
    assert f"   1  #1  unchanged  {PR_URL.format(1)}  Add a" in out
    assert (
        "Branches pushed: testbot/stack/2 (updated), testbot/stack/3 (updated)\n" in out
    )
    assert remote.sha(BRANCH.format(2)) == b2
    assert remote.sha(BRANCH.format(3)) == c2
    assert work.shas() == [a1, b2, c2]
    assert write_calls_after(fake_gh, start) == []
    assert {n: pr["state"] for n, pr in fake_gh.prs().items()} == {
        1: "OPEN",
        2: "OPEN",
        3: "OPEN",
    }
    # Still no fetch of the stack branches: the concurrent commit is unknown
    # here, and the tracking refs only moved because the tool pushed them.
    assert not has_commit(work, theirs)
    assert remote_tracking_refs(work) == {
        "main": work.head("origin/main"),
        BRANCH.format(1): a1,
        BRANCH.format(2): b2,
        BRANCH.format(3): c2,
    }


# --------------------------------------------------------------------------- #
# 16. Existing stack-info branches outside the current template glob
# --------------------------------------------------------------------------- #
def test_existing_branches_outside_template_glob_are_kept(
    work: Work,
    remote: Remote,
    fake_gh: FakeGitHub,
    run_export: RunExport,
    stack3: list[str],
) -> None:
    rc, _, _ = run_export()
    assert rc == 0
    head = work.head()
    branches_before = remote.branches()

    # Every commit has a stack-info trailer, so the remote is asked about
    # exactly those three branches (no template glob), the pull requests are
    # looked up in one batched request, and the login is not needed at all.
    proc = export_in_subprocess(
        work, "-vv", "-n", "--branch-name-template", "other/$ID"
    )
    assert proc.returncode == 0, proc.stderr
    ls_remote = (
        "$ git ls-remote --heads --refs origin refs/heads/testbot/stack/1 "
        "refs/heads/testbot/stack/2 refs/heads/testbot/stack/3\n"
    )
    assert ls_remote in proc.stderr
    assert "other/" not in proc.stderr
    assert "stack/*" not in proc.stderr
    # A no-op re-export talks to the remote exactly three times: the fetch of
    # the target branch, one ls-remote for the stack branches (after it), and
    # one batched pull request lookup.
    assert fetch_line() in proc.stderr
    assert proc.stderr.count("$ git fetch") == 1
    assert proc.stderr.count("$ git ls-remote") == 1
    assert proc.stderr.index(fetch_line()) < proc.stderr.index(ls_remote)
    assert "$ gh api --hostname github.com user" not in proc.stderr
    assert proc.stderr.count("$ gh api --hostname github.com graphql") == 1
    assert proc.stderr.count("$ gh") == 1
    assert "$ gh pr view" not in proc.stderr
    assert "$ git push" not in proc.stderr
    assert "Everything is up to date; nothing to do." in proc.stdout

    start = n_calls(fake_gh)
    rc, out, err = run_export("-v", "--branch-name-template", "other/$ID")
    assert rc == 0, err
    assert "Everything is up to date; nothing to do." in out
    assert "new branch" not in out
    assert "push to origin" not in out
    assert "push the stack" not in out
    assert "Up to date: 3 pull requests, nothing to push." in out
    assert "Branches pushed:" not in out
    assert "other/" not in out
    for n in (1, 2, 3):
        assert (
            f"  {n}  {work.head(f'HEAD~{3 - n}')[:8]}  #{n}  {BRANCH.format(n)}  "
            in out
        )
    assert work.head() == head
    assert remote.branches() == branches_before
    assert write_calls_after(fake_gh, start) == []
    assert {pr["headRefName"] for pr in fake_gh.prs().values()} == {
        BRANCH.format(1),
        BRANCH.format(2),
        BRANCH.format(3),
    }
    # The whole re-export cost exactly one gh call: the batched PR lookup.
    assert [c[:4] for c in calls_after(fake_gh, start)] == [
        ["api", "--hostname", "github.com", "graphql"]
    ]


def test_new_commit_with_other_template_gets_a_branch_from_that_template(
    work: Work,
    remote: Remote,
    fake_gh: FakeGitHub,
    run_export: RunExport,
    stack3: list[str],
) -> None:
    rc, _, _ = run_export()
    assert rc == 0
    work.commit("d.txt", "Add d")

    # One commit has no stack-info yet: now the remote is also asked for every
    # branch matching the new template, and the login is needed for the name.
    proc = export_in_subprocess(
        work, "-vv", "-n", "--branch-name-template", "other/$ID"
    )
    assert proc.returncode == 0, proc.stderr
    ls_remote = (
        "$ git ls-remote --heads --refs origin refs/heads/testbot/stack/1 "
        "refs/heads/testbot/stack/2 refs/heads/testbot/stack/3 'refs/heads/other/*'\n"
    )
    assert ls_remote in proc.stderr
    assert proc.stderr.count("$ git ls-remote") == 1
    assert "$ gh api --hostname github.com user --jq .login\n" in proc.stderr
    assert proc.stderr.count("$ gh api --hostname github.com graphql") == 1
    assert fetch_line() in proc.stderr
    assert proc.stderr.count("$ git fetch") == 1
    assert proc.stderr.index(fetch_line()) < proc.stderr.index(ls_remote)
    assert "testbot/stack/*" not in proc.stderr

    rc, out, err = run_export("--branch-name-template", "other/$ID")
    assert rc == 0, err
    # One new PR on a brand-new branch; the others only get fresh cross-links.
    assert "Exported 4 pull requests (1 new, 3 updated):" in out
    assert f"   4  #4  new        {PR_URL.format(4)}  Add d" in out
    assert "Branches pushed: other/1 (new)\n" in out
    assert fake_gh.pr(4)["headRefName"] == "other/1"
    assert fake_gh.pr(4)["baseRefName"] == BRANCH.format(3)
    assert remote.sha("other/1") == work.head()
    assert (
        "stack-info: PR: https://github.com/octo/widgets/pull/4, branch: other/1"
        in work.message()
    )


# --------------------------------------------------------------------------- #
# 17. Non-ASCII commit messages
# --------------------------------------------------------------------------- #
def test_non_ascii_message_round_trips(
    work: Work, remote: Remote, fake_gh: FakeGitHub, run_export: RunExport
) -> None:
    title = "Ünïcödé — title"
    body = "Body with emoji 🚀 and “curly quotes”, plus ¡ñ!"
    work.commit("a.txt", "Add a")
    sha = work.commit("u.txt", f"{title}\n\n{body}\n", content="ü\n")
    tree = work.tree()

    rc, out, err = run_export()
    assert rc == 0, err
    assert f"   2  #2  new        {PR_URL.format(2)}  {title}" in out
    assert fake_gh.pr(2)["title"] == title
    assert fake_gh.pr(2)["body"] == (
        f"Stacked PRs:\n * __->__#2\n * #1\n\n--- --- ---\n\n### {title}\n\n{body}"
    )
    assert work.message() == f"{title}\n\n{body}\n\n{stack_info(2)}"
    assert work.tree() == tree
    assert work.head() != sha
    remote_sha = remote.sha(BRANCH.format(2))
    assert remote_sha == work.head()
    assert remote.message(remote_sha) == work.message()
    assert (work.path / "u.txt").read_text() == "ü\n"
