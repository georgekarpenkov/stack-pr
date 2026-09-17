"""End-to-end tests of failure modes, safety properties and recovery.

Everything runs against the offline harness from ``conftest.py``: a bare
repository standing in for GitHub and a fake ``gh`` whose post-receive hook
auto-closes pull requests the way GitHub does.
"""

from __future__ import annotations

import os
import shutil
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

    rc, out, err = run_export()
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
    rc, out, err = run_export()
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

    rc, _, err = run_export()
    assert rc == 1
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

    rc, out, err = run_export()
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
    rc, out, err = run_export()
    assert rc == 0, err
    assert "Everything is up to date; nothing to do." in out
    assert "create PR" not in out
    assert "rewrite" not in out
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

    rc, out, err = run_export()
    assert rc == 0, err
    assert work.shas() != stack3
    assert "push the stack to origin" in out
    assert [remote.sha(BRANCH.format(i)) for i in (1, 2, 3)] == work.shas()


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

    rc, out, err = run_export()
    assert rc == 0, err
    assert "rewrite" not in out
    assert "move feature" not in out
    assert "create PR" not in out
    assert (
        "push the stack to origin (--atomic --force-with-lease): testbot/stack/2" in out
    )
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


# --------------------------------------------------------------------------- #
# 6. A rebase is in progress
# --------------------------------------------------------------------------- #
def test_rebase_in_progress_aborts_before_fetching(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    (work.path / ".git" / "rebase-merge").mkdir()
    assert not (work.path / ".git" / "FETCH_HEAD").exists()

    rc, _, err = run_export()
    assert rc == 1
    assert "a rebase is in progress; finish or abort it first" in err
    assert fake_gh.calls() == []
    assert not (work.path / ".git" / "FETCH_HEAD").exists()
    assert work.head() == stack3[-1]


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
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    rc, _, err = run_export("-T", "develop")
    assert rc == 1
    assert "target branch 'origin/develop' does not exist" in err
    assert "seems to use 'master'" not in err
    assert fake_gh.calls() == []
    assert work.head() == stack3[-1]


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

    rc, _, err = run_export("-T", "master")
    assert rc == 0, err
    assert fake_gh.pr(1)["baseRefName"] == "master"


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

    rc, out, err = run_export("-H", stack3[-1])
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

    rc, out, err = run_export("-H", "feature")
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
    rc, out, err = run_export()
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
def install_fetch_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clone: Path, branch: str
) -> Path:
    """Put a ``git`` shim first on PATH that pushes ``clone``'s HEAD to
    ``branch`` right after the first ``git fetch`` (then behaves normally)."""
    real_git = shutil.which("git")
    assert real_git is not None
    assert not real_git.startswith(str(tmp_path))
    shim_dir = tmp_path / "race-bin"
    shim_dir.mkdir()
    marker = tmp_path / "race-happened"
    shim = shim_dir / "git"
    shim.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = fetch ] && [ ! -e "{marker}" ]; then\n'
        f'  "{real_git}" "$@" || exit $?\n'
        f'  touch "{marker}"\n'
        f'  "{real_git}" -C "{clone}" push -q origin HEAD:refs/heads/{branch} || exit $?\n'
        "  exit 0\n"
        "fi\n"
        f'exec "{real_git}" "$@"\n'
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")
    return marker


def test_push_lease_rejects_update_that_raced_the_fetch(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    work: Work,
    remote: Remote,
    fake_gh: FakeGitHub,
    run_export: RunExport,
    stack3: list[str],
) -> None:
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

    marker = install_fetch_race(tmp_path, monkeypatch, clone, BRANCH.format(2))
    start = n_calls(fake_gh)
    rc, _, err = run_export()
    assert marker.exists()
    assert rc == 1
    assert "stale info" in err
    assert "warning: local branches were not modified" in err
    # Atomic push: neither branch moved, and the concurrent commit is intact.
    assert remote.sha(BRANCH.format(2)) == theirs
    assert remote.sha(BRANCH.format(3)) == c1
    assert work.shas() == [a1, b2, c2]
    assert write_calls_after(fake_gh, start) == []
    assert fake_gh.pr(2)["state"] == "OPEN"

    # After fetching the concurrent update, the re-run takes the lease on it.
    start = n_calls(fake_gh)
    rc, _, err = run_export()
    assert rc == 0, err
    assert remote.sha(BRANCH.format(2)) == b2
    assert remote.sha(BRANCH.format(3)) == c2
    assert work.shas() == [a1, b2, c2]
    assert write_calls_after(fake_gh, start) == []
    assert {n: pr["state"] for n, pr in fake_gh.prs().items()} == {
        1: "OPEN",
        2: "OPEN",
        3: "OPEN",
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
    start = n_calls(fake_gh)

    rc, out, err = run_export("--branch-name-template", "other/$ID")
    assert rc == 0, err
    assert "Everything is up to date; nothing to do." in out
    assert "new branch" not in out
    assert "push" not in out
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

    rc, _, err = run_export("--branch-name-template", "other/$ID")
    assert rc == 0, err
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
    assert title in out
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
