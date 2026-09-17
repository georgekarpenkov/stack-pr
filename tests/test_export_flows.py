"""End-to-end happy paths of ``pstack-pr export`` against the offline harness."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from tests.conftest import FakeGitHub, Remote, RunExport, Work, git

REPO_URL = "https://github.com/octo/widgets"
REPO_SLUG = "github.com/octo/widgets"  # what every ``gh --repo`` receives
DELIMITER = "--- --- ---"
IDENTITY_FORMAT = "--format=%an|%ae|%ad|%cn|%ce|%cd"
PLAN_LINE_RE = re.compile(r"^\s*\d+\. (.+)$")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def plan_lines(out: str) -> list[str]:
    """The numbered lines printed under ``Plan:``, without their numbers."""
    return [m.group(1) for line in out.splitlines() if (m := PLAN_LINE_RE.match(line))]


def result_block(out: str) -> list[str]:
    """The result block: from the ``Exported``/``Up to date`` header to the end."""
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(("Exported ", "Up to date: ")):
            return lines[i:]
    raise AssertionError(f"no result block in:\n{out}")


def result_line(index: int, number: int, status: str, title: str) -> str:
    """One entry of the result block; ``status`` is padded to 9 columns."""
    return f"  {index:>2}  #{number}  {status:<9}  {REPO_URL}/pull/{number}  {title}"


def stack_info(number: int, branch: str | None = None) -> str:
    branch = branch or f"testbot/stack/{number}"
    return f"stack-info: PR: {REPO_URL}/pull/{number}, branch: {branch}"


def toc(numbers: list[int], current: int) -> str:
    lines = ["Stacked PRs:"]
    lines += [f" * {'__->__' if n == current else ''}#{n}" for n in reversed(numbers)]
    return "\n".join(lines)


def identities(work: Work, rng: str = "origin/main..HEAD") -> list[str]:
    return work.git("log", "--reverse", "--date=raw", IDENTITY_FORMAT, rng).splitlines()


def raw_message(work: Work, rev: str = "HEAD") -> str:
    """The exact message of a commit object, trailing newline included."""
    proc = subprocess.run(
        ["git", "cat-file", "commit", rev],
        cwd=work.path,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.split("\n\n", 1)[1]


def amend_content(work: Work, rev: str, name: str, content: str) -> str:
    """Detach at ``rev``, change ``name`` and amend keeping the message."""
    work.git("checkout", "-q", "--detach", rev)
    (work.path / name).write_text(content)
    work.git("add", name)
    work.git("commit", "-q", "--amend", "--no-edit")
    return work.head()


def rebase_onto(work: Work, new_base: str, old_base: str, branch: str) -> None:
    work.git("rebase", "-q", "--onto", new_base, old_base, branch)


# --------------------------------------------------------------------------- #
# 1. dry run
# --------------------------------------------------------------------------- #
def test_dry_run_prints_stack_and_plan_without_changing_anything(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    a, b, c = stack3
    messages_before = work.messages()

    rc, out, _err = run_export("--dry-run")

    assert rc == 0
    assert (
        f"Stack of 3 commits on feature (base: origin/main @ {work.head('origin/main')[:8]})"
        in out
    )
    assert out.count("  new PR  ") == 3
    assert f"   3  {c[:8]}  new PR  testbot/stack/3  Add c" in out
    assert f"   2  {b[:8]}  new PR  testbot/stack/2  Add b" in out
    assert f"   1  {a[:8]}  new PR  testbot/stack/1  Add a" in out
    assert "Plan:" in out
    assert plan_lines(out) == [
        (
            f"push to origin: {a[:8]} -> testbot/stack/1 (new branch), "
            f"{b[:8]} -> testbot/stack/2 (new branch), "
            f"{c[:8]} -> testbot/stack/3 (new branch)"
        ),
        f"create PR for {a[:8]}: testbot/stack/1 -> main",
        f"create PR for {b[:8]}: testbot/stack/2 -> testbot/stack/1",
        f"create PR for {c[:8]}: testbot/stack/3 -> testbot/stack/2",
        (
            "rewrite 3 commit messages to embed stack-info (git commit-tree; "
            "file contents, authors and dates are unchanged)"
        ),
        (
            f"move feature from {c[:8]} to the rewritten tip "
            f"(git update-ref, only if it is still at {c[:8]})"
        ),
        (
            "push the stack to origin (--atomic --force-with-lease): "
            "testbot/stack/1, testbot/stack/2, testbot/stack/3"
        ),
        f"update the new PR for {a[:8]}: body (cross-links)",
        f"update the new PR for {b[:8]}: body (cross-links)",
        f"update the new PR for {c[:8]}: body (cross-links)",
    ]
    assert "Dry run: nothing was changed." in out
    assert "Exported" not in out

    assert list(work.remote.branches()) == ["main"]
    assert fake_gh.write_calls() == []
    assert fake_gh.prs() == {}
    assert work.head() == c
    assert work.messages() == messages_before == ["Add a", "Add b", "Add c"]
    assert "pstack-pr export" not in work.reflog("feature")


def test_dry_run_short_flag_is_n(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    rc, out, _err = run_export("-n")

    assert rc == 0
    assert "Dry run: nothing was changed." in out
    assert work.head() == stack3[-1]
    assert fake_gh.write_calls() == []


# --------------------------------------------------------------------------- #
# 2. fresh export
# --------------------------------------------------------------------------- #
def test_fresh_export_creates_three_chained_pull_requests(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    rc, _out, _err = run_export()

    assert rc == 0
    prs = fake_gh.prs()
    assert sorted(prs) == [1, 2, 3]
    assert [(prs[n]["headRefName"], prs[n]["baseRefName"]) for n in (1, 2, 3)] == [
        ("testbot/stack/1", "main"),
        ("testbot/stack/2", "testbot/stack/1"),
        ("testbot/stack/3", "testbot/stack/2"),
    ]
    assert [prs[n]["title"] for n in (1, 2, 3)] == ["Add a", "Add b", "Add c"]
    assert [prs[n]["state"] for n in (1, 2, 3)] == ["OPEN", "OPEN", "OPEN"]
    assert [prs[n]["isDraft"] for n in (1, 2, 3)] == [False, False, False]
    assert [prs[n]["url"] for n in (1, 2, 3)] == [
        f"{REPO_URL}/pull/{n}" for n in (1, 2, 3)
    ]


def test_fresh_export_bodies_have_toc_newest_first_then_delimiter_and_title(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    run_export()

    assert fake_gh.pr(1)["body"] == (
        "Stacked PRs:\n * #3\n * #2\n * __->__#1\n\n--- --- ---\n\n### Add a"
    )
    assert fake_gh.pr(2)["body"] == (
        "Stacked PRs:\n * #3\n * __->__#2\n * #1\n\n--- --- ---\n\n### Add b"
    )
    assert fake_gh.pr(3)["body"] == (
        "Stacked PRs:\n * __->__#3\n * #2\n * #1\n\n--- --- ---\n\n### Add c"
    )


def test_fresh_export_rewrites_messages_with_stack_info_trailer(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    run_export()

    assert work.messages() == [
        f"Add a\n\n{stack_info(1)}",
        f"Add b\n\n{stack_info(2)}",
        f"Add c\n\n{stack_info(3)}",
    ]
    assert raw_message(work) == f"Add c\n\n{stack_info(3)}\n"
    assert work.head() != stack3[-1]
    assert work.shas() != stack3
    assert work.head("HEAD~3") == work.head("origin/main")


def test_fresh_export_pushes_rewritten_commits_to_stack_branches(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    run_export()

    new_shas = work.shas()
    branches = work.remote.branches()
    assert sorted(branches) == [
        "main",
        "testbot/stack/1",
        "testbot/stack/2",
        "testbot/stack/3",
    ]
    assert [branches[f"testbot/stack/{i}"] for i in (1, 2, 3)] == new_shas
    assert branches["main"] == work.head("origin/main")
    assert not any(sha in branches.values() for sha in stack3)
    # The remote sees the same rewritten messages.
    assert (
        work.remote.message(branches["testbot/stack/2"]) == f"Add b\n\n{stack_info(2)}"
    )


def test_fresh_export_preserves_trees_authors_and_committers(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    trees_before = [work.tree(sha) for sha in stack3]
    identities_before = identities(work)
    assert len(identities_before) == 3
    assert identities_before[0].startswith("Ada Author|ada@example.com|")
    assert "|Cy Committer|cy@example.com|" in identities_before[0]

    run_export()

    assert [work.tree(sha) for sha in work.shas()] == trees_before
    assert identities(work) == identities_before
    assert work.status() == ""


def test_fresh_export_leaves_reflog_entry_and_updates_head(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    run_export()

    reflog = work.reflog("feature")
    assert reflog[0] == "pstack-pr export"
    assert reflog.count("pstack-pr export") == 1
    assert work.git("symbolic-ref", "HEAD") == "refs/heads/feature"
    assert work.head("feature") == work.head()


def test_fresh_export_gh_calls_create_each_pr_once_and_edit_it_once(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    run_export()

    creates = fake_gh.calls("pr", "create")
    assert creates == [
        [
            "pr", "create", "--repo", REPO_SLUG,
            "--base", "main", "--head", "testbot/stack/1",
            "--title", "Add a", "--body-file", "-",
        ],
        [
            "pr", "create", "--repo", REPO_SLUG,
            "--base", "testbot/stack/1", "--head", "testbot/stack/2",
            "--title", "Add b", "--body-file", "-",
        ],
        [
            "pr", "create", "--repo", REPO_SLUG,
            "--base", "testbot/stack/2", "--head", "testbot/stack/3",
            "--title", "Add c", "--body-file", "-",
        ],
    ]  # fmt: skip
    edits = fake_gh.calls("pr", "edit")
    assert edits == [
        ["pr", "edit", str(n), "--repo", REPO_SLUG, "--body-file", "-"]
        for n in (1, 2, 3)
    ]
    assert [fake_gh.pr(n)["edits"] for n in (1, 2, 3)] == [1, 1, 1]
    assert fake_gh.calls("pr", "ready") == []
    assert fake_gh.calls("pr", "close") == []
    assert fake_gh.write_calls() == creates + edits


def test_fresh_export_prints_only_the_result_block_by_default(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    rc, out, err = run_export()

    assert rc == 0
    assert err == ""
    # No 'Contacting' notice, stack table, plan or progress lines: just the
    # outcome.
    assert out == (
        "Exported 3 pull requests (3 new):\n"
        f"   3  #3  new        {REPO_URL}/pull/3  Add c\n"
        f"   2  #2  new        {REPO_URL}/pull/2  Add b\n"
        f"   1  #1  new        {REPO_URL}/pull/1  Add a\n"
        "Branches pushed: testbot/stack/1 (new), testbot/stack/2 (new), "
        "testbot/stack/3 (new)\n"
    )


def test_fresh_export_verbose_prints_contacting_stack_plan_progress_then_result(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    a, b, c = stack3

    rc, out, err = run_export("-v")

    assert rc == 0
    assert err == ""
    lines = out.splitlines()
    assert lines[0] == "Contacting origin..."
    assert lines[1] == (
        "Stack of 3 commits on feature "
        f"(base: origin/main @ {work.head('origin/main')[:8]})"
    )
    assert lines[2:7] == [
        f"   3  {c[:8]}  new PR  testbot/stack/3  Add c",
        f"   2  {b[:8]}  new PR  testbot/stack/2  Add b",
        f"   1  {a[:8]}  new PR  testbot/stack/1  Add a",
        "",
        "Plan:",
    ]
    assert len(plan_lines(out)) == 10
    assert lines[7].startswith("   1. push to origin: ")
    assert lines[16] == f"  10. update the new PR for {c[:8]}: body (cross-links)"
    assert lines[17] == ""
    assert lines[18].startswith("  [1/10] push to origin: ")
    assert lines[19] == f"  [2/10] create PR for {a[:8]}: testbot/stack/1 -> main"
    # Progress is described at execution time, when the PR numbers are known.
    assert lines[27] == "  [10/10] update PR #3: body (cross-links)"
    assert lines[28] == ""
    assert lines[29:] == [
        "Exported 3 pull requests (3 new):",
        result_line(3, 3, "new", "Add c"),
        result_line(2, 2, "new", "Add b"),
        result_line(1, 1, "new", "Add a"),
        (
            "Branches pushed: testbot/stack/1 (new), testbot/stack/2 (new), "
            "testbot/stack/3 (new)"
        ),
    ]
    assert "Dry run" not in out


# --------------------------------------------------------------------------- #
# 3. idempotent re-export
# --------------------------------------------------------------------------- #
def test_reexport_without_changes_is_a_no_op(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    run_export()
    head = work.head()
    branches = work.remote.branches()
    write_calls = fake_gh.write_calls()
    prs = fake_gh.prs()
    reflog = work.reflog("feature")
    calls_before = fake_gh.calls()

    rc, out, err = run_export("-v")

    assert rc == 0
    assert err == ""
    assert "Everything is up to date; nothing to do." in out
    assert "Plan:" not in out
    assert "Exported" not in out
    assert "  #3  testbot/stack/3  Add c" in out  # the stack table lists the PRs
    assert not any(re.match(r"^\s*\[\d+/\d+\]", line) for line in out.splitlines())
    assert result_block(out) == [
        "Up to date: 3 pull requests, nothing to push.",
        result_line(3, 3, "unchanged", "Add c"),
        result_line(2, 2, "unchanged", "Add b"),
        result_line(1, 1, "unchanged", "Add a"),
    ]
    assert out.count("#1") == 2  # once in the stack table, once in the result
    assert work.head() == head
    assert work.remote.branches() == branches
    assert fake_gh.write_calls() == write_calls
    assert fake_gh.prs() == prs
    assert work.reflog("feature") == reflog
    # The second run only read, with one batched lookup of all three PRs; every
    # commit already carries a trailer, so no login lookup was needed either.
    new_calls = fake_gh.calls()[len(calls_before) :]
    assert len(new_calls) == 1
    assert new_calls[0][:4] == ["api", "--hostname", "github.com", "graphql"]
    assert fake_gh.calls("pr", "view") == []
    assert fake_gh.calls("pr", "list") == []


def test_reexport_without_changes_prints_only_up_to_date_by_default(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    run_export()

    rc, out, err = run_export()

    assert rc == 0
    assert err == ""
    assert out == (
        "Up to date: 3 pull requests, nothing to push.\n"
        f"   3  #3  unchanged  {REPO_URL}/pull/3  Add c\n"
        f"   2  #2  unchanged  {REPO_URL}/pull/2  Add b\n"
        f"   1  #1  unchanged  {REPO_URL}/pull/1  Add a\n"
    )


# --------------------------------------------------------------------------- #
# 4. amend the middle commit's content
# --------------------------------------------------------------------------- #
def test_amending_middle_commit_content_only_pushes_branches_above_it(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    run_export()
    a1, b1, _c1 = work.shas()
    branches_before = work.remote.branches()
    messages_before = work.messages()
    write_calls_before = fake_gh.write_calls()
    assert len(write_calls_before) == 6  # 3 creates + 3 edits

    new_b = amend_content(work, b1, "b.txt", "b changed\n")
    rebase_onto(work, new_b, b1, "feature")
    assert work.git("symbolic-ref", "HEAD") == "refs/heads/feature"
    assert work.messages() == messages_before
    a2, b2, c2 = work.shas()
    assert (a2, b2) == (a1, new_b)

    rc, out, _err = run_export("-v")

    assert rc == 0
    assert plan_lines(out) == [
        (
            "push the stack to origin (--atomic --force-with-lease): "
            "testbot/stack/2, testbot/stack/3"
        )
    ]
    assert "rewrite" not in out
    assert "update PR" not in out
    assert result_block(out) == [
        "Exported 3 pull requests (2 updated, 1 unchanged):",
        result_line(3, 3, "updated", "Add c"),
        result_line(2, 2, "updated", "Add b"),
        result_line(1, 1, "unchanged", "Add a"),
        "Branches pushed: testbot/stack/2 (updated), testbot/stack/3 (updated)",
    ]
    # Nothing was rewritten locally: the commits are exactly the rebased ones.
    assert work.shas() == [a2, b2, c2]
    assert work.messages() == messages_before
    branches = work.remote.branches()
    assert branches["testbot/stack/1"] == branches_before["testbot/stack/1"] == a1
    assert branches["testbot/stack/2"] == b2 != branches_before["testbot/stack/2"]
    assert branches["testbot/stack/3"] == c2 != branches_before["testbot/stack/3"]
    assert work.git("show", f"{branches['testbot/stack/2']}:b.txt") == "b changed"
    prs = fake_gh.prs()
    assert sorted(prs) == [1, 2, 3]
    assert [prs[n]["edits"] for n in (1, 2, 3)] == [1, 1, 1]
    assert prs[2]["body"] == (
        "Stacked PRs:\n * #3\n * __->__#2\n * #1\n\n--- --- ---\n\n### Add b"
    )
    assert [prs[n]["state"] for n in (1, 2, 3)] == ["OPEN", "OPEN", "OPEN"]
    assert fake_gh.write_calls() == write_calls_before  # no new gh writes


# --------------------------------------------------------------------------- #
# 5. reword the top commit
# --------------------------------------------------------------------------- #
def test_rewording_top_commit_title_updates_its_pr_title(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    run_export()
    branches_before = work.remote.branches()
    old_message = work.message()
    assert old_message.startswith("Add c\n")
    new_message = old_message.replace("Add c", "Add c (reworded)", 1)
    work.git("commit", "-q", "--amend", "-F", "-", input=new_message + "\n")
    reworded = work.head()
    assert work.message() == new_message

    rc, out, _err = run_export("-v")

    assert rc == 0
    assert plan_lines(out) == [
        "push the stack to origin (--atomic --force-with-lease): testbot/stack/3",
        "update PR #3: title, body (cross-links)",
    ]
    assert result_block(out) == [
        "Exported 3 pull requests (1 updated, 2 unchanged):",
        result_line(3, 3, "updated", "Add c (reworded)"),
        result_line(2, 2, "unchanged", "Add b"),
        result_line(1, 1, "unchanged", "Add a"),
        "Branches pushed: testbot/stack/3 (updated)",
    ]
    assert work.head() == reworded  # nothing to rewrite: stack-info still valid
    prs = fake_gh.prs()
    assert prs[3]["title"] == "Add c (reworded)"
    assert prs[3]["body"] == (
        "Stacked PRs:\n * __->__#3\n * #2\n * #1\n\n--- --- ---\n\n### Add c (reworded)"
    )
    assert [prs[n]["edits"] for n in (1, 2, 3)] == [1, 1, 2]
    assert [prs[n]["title"] for n in (1, 2)] == ["Add a", "Add b"]
    assert fake_gh.calls("pr", "edit")[-1] == [
        "pr", "edit", "3", "--repo", REPO_SLUG,
        "--title", "Add c (reworded)", "--body-file", "-",
    ]  # fmt: skip
    branches = work.remote.branches()
    assert branches["testbot/stack/3"] == reworded
    assert branches["testbot/stack/1"] == branches_before["testbot/stack/1"]
    assert branches["testbot/stack/2"] == branches_before["testbot/stack/2"]


# --------------------------------------------------------------------------- #
# 6. insert a commit in the middle
# --------------------------------------------------------------------------- #
def test_inserting_commit_between_a_and_b_relinks_bases(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    run_export()
    a1 = work.shas()[0]

    work.git("checkout", "-q", "--detach", a1)
    inserted = work.commit("a2.txt", "Add a2")
    rebase_onto(work, inserted, a1, "feature")
    assert [work.message(s).splitlines()[0] for s in work.shas()] == [
        "Add a", "Add a2", "Add b", "Add c",
    ]  # fmt: skip
    assert work.shas()[0] == a1
    rebased = work.shas()
    trees_before = [work.tree(s) for s in rebased]
    identities_before = identities(work)

    rc, out, _err = run_export("-v")

    assert rc == 0
    assert plan_lines(out) == [
        f"push to origin: {inserted[:8]} -> testbot/stack/4 (new branch)",
        f"create PR for {inserted[:8]}: testbot/stack/4 -> testbot/stack/1",
        (
            "rewrite 3 commit messages to embed stack-info (git commit-tree; "
            "file contents, authors and dates are unchanged)"
        ),
        (
            f"move feature from {work.head('feature@{1}')[:8]} to the rewritten tip "
            f"(git update-ref, only if it is still at {work.head('feature@{1}')[:8]})"
        ),
        (
            "push the stack to origin (--atomic --force-with-lease): "
            "testbot/stack/4, testbot/stack/2, testbot/stack/3"
        ),
        "update PR #1: body (cross-links)",
        f"update the new PR for {inserted[:8]}: body (cross-links)",
        "update PR #2: base -> testbot/stack/4, body (cross-links)",
        "update PR #3: body (cross-links)",
    ]
    # #1 is "updated" although its branch stayed put: its cross-links changed.
    assert result_block(out) == [
        "Exported 4 pull requests (1 new, 3 updated):",
        result_line(4, 3, "updated", "Add c"),
        result_line(3, 2, "updated", "Add b"),
        result_line(2, 4, "new", "Add a2"),
        result_line(1, 1, "updated", "Add a"),
        (
            "Branches pushed: testbot/stack/4 (new), testbot/stack/2 (updated), "
            "testbot/stack/3 (updated)"
        ),
    ]

    prs = fake_gh.prs()
    assert sorted(prs) == [1, 2, 3, 4]
    assert [
        (prs[n]["title"], prs[n]["headRefName"], prs[n]["baseRefName"])
        for n in (1, 4, 2, 3)
    ] == [
        ("Add a", "testbot/stack/1", "main"),
        ("Add a2", "testbot/stack/4", "testbot/stack/1"),
        ("Add b", "testbot/stack/2", "testbot/stack/4"),
        ("Add c", "testbot/stack/3", "testbot/stack/2"),
    ]
    assert [prs[n]["state"] for n in (1, 2, 3, 4)] == ["OPEN"] * 4
    numbers = [1, 4, 2, 3]
    assert prs[1]["body"] == f"{toc(numbers, 1)}\n\n{DELIMITER}\n\n### Add a"
    assert prs[4]["body"] == f"{toc(numbers, 4)}\n\n{DELIMITER}\n\n### Add a2"
    assert prs[2]["body"] == f"{toc(numbers, 2)}\n\n{DELIMITER}\n\n### Add b"
    assert prs[3]["body"] == f"{toc(numbers, 3)}\n\n{DELIMITER}\n\n### Add c"
    assert prs[1]["body"].startswith("Stacked PRs:\n * #3\n * #2\n * #4\n * __->__#1\n")

    shas = work.shas()
    assert shas[0] == a1
    assert work.messages() == [
        f"Add a\n\n{stack_info(1)}",
        f"Add a2\n\n{stack_info(4)}",
        f"Add b\n\n{stack_info(2)}",
        f"Add c\n\n{stack_info(3)}",
    ]
    assert shas[1:] != rebased[1:]  # a2, b and c were rewritten
    assert [work.tree(s) for s in shas] == trees_before
    assert work.tree(shas[1]) == work.tree(inserted)
    assert identities(work) == identities_before
    branches = work.remote.branches()
    assert [branches[f"testbot/stack/{i}"] for i in (1, 4, 2, 3)] == shas
    assert fake_gh.calls("pr", "edit")[-2] == [
        "pr", "edit", "2", "--repo", REPO_SLUG,
        "--body-file", "-", "--base", "testbot/stack/4",
    ]  # fmt: skip
    assert fake_gh.calls("pr", "ready") == []


# --------------------------------------------------------------------------- #
# 7. --head below the branch tip
# --------------------------------------------------------------------------- #
def test_head_option_exports_partial_stack_and_reparents_commits_above(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    a, b, c = stack3
    identities_before = identities(work)

    rc, out, _err = run_export("-v", "-H", "HEAD~1")

    assert rc == 0
    assert "Stack of 2 commits on feature" in out
    assert out.count("  new PR  ") == 2
    assert (
        "rewrite 2 commit messages to embed stack-info (git commit-tree; file "
        "contents, authors and dates are unchanged), then re-parent the 1 commit "
        "above the stack"
    ) in plan_lines(out)
    assert result_block(out) == [
        "Exported 2 pull requests (2 new):",
        result_line(2, 2, "new", "Add b"),
        result_line(1, 1, "new", "Add a"),
        "Branches pushed: testbot/stack/1 (new), testbot/stack/2 (new)",
    ]
    assert sorted(fake_gh.prs()) == [1, 2]
    assert fake_gh.pr(2)["title"] == "Add b"
    assert sorted(work.remote.branches()) == [
        "main",
        "testbot/stack/1",
        "testbot/stack/2",
    ]

    shas = work.shas()
    assert len(shas) == 3
    assert work.head() != c
    assert work.head() == shas[2]
    assert work.messages() == [
        f"Add a\n\n{stack_info(1)}",
        f"Add b\n\n{stack_info(2)}",
        "Add c",
    ]
    assert [work.tree(s) for s in shas] == [work.tree(a), work.tree(b), work.tree(c)]
    assert identities(work) == identities_before
    assert work.head("HEAD~1") == work.remote.sha("testbot/stack/2")
    assert work.reflog("feature")[0] == "pstack-pr export"


def test_plain_export_after_partial_export_adds_pr_for_the_top_commit(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    run_export("-H", "HEAD~1")
    a1, b1, c1 = work.shas()
    branches_before = work.remote.branches()
    prs_before = fake_gh.prs()

    rc, out, _err = run_export("-v")

    assert rc == 0
    assert out.count("  new PR  ") == 1
    # #1 and #2 gain a cross-link to #3, so they count as updated.
    assert result_block(out) == [
        "Exported 3 pull requests (1 new, 2 updated):",
        result_line(3, 3, "new", "Add c"),
        result_line(2, 2, "updated", "Add b"),
        result_line(1, 1, "updated", "Add a"),
        "Branches pushed: testbot/stack/3 (new)",
    ]
    prs = fake_gh.prs()
    assert sorted(prs) == [1, 2, 3]
    assert prs[3]["title"] == "Add c"
    assert prs[3]["headRefName"] == "testbot/stack/3"
    assert prs[3]["baseRefName"] == "testbot/stack/2"
    assert prs[3]["state"] == "OPEN"
    for n in (1, 2):
        for key in ("title", "headRefName", "baseRefName", "state", "number", "url"):
            assert prs[n][key] == prs_before[n][key]
    assert (
        prs[1]["body"]
        == "Stacked PRs:\n * #3\n * #2\n * __->__#1\n\n--- --- ---\n\n### Add a"
    )

    shas = work.shas()
    assert shas[:2] == [a1, b1]
    assert shas[2] != c1
    assert work.tree(shas[2]) == work.tree(c1)
    assert work.message() == f"Add c\n\n{stack_info(3)}"
    branches = work.remote.branches()
    assert branches["testbot/stack/1"] == branches_before["testbot/stack/1"]
    assert branches["testbot/stack/2"] == branches_before["testbot/stack/2"]
    assert branches["testbot/stack/3"] == shas[2]
    assert len(fake_gh.calls("pr", "create")) == 3


# --------------------------------------------------------------------------- #
# 8. --base explicit
# --------------------------------------------------------------------------- #
def test_base_option_exports_only_commits_above_it_with_target_as_first_base(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    a, b, c = stack3

    rc, out, _err = run_export("-v", "-B", "HEAD~2")

    assert rc == 0
    assert "Stack of 2 commits on feature" in out
    assert result_block(out) == [
        "Exported 2 pull requests (2 new):",
        result_line(2, 2, "new", "Add c"),
        result_line(1, 1, "new", "Add b"),
        "Branches pushed: testbot/stack/1 (new), testbot/stack/2 (new)",
    ]
    assert f"create PR for {b[:8]}: testbot/stack/1 -> main" in plan_lines(out)
    assert f"create PR for {c[:8]}: testbot/stack/2 -> testbot/stack/1" in plan_lines(
        out
    )
    prs = fake_gh.prs()
    assert sorted(prs) == [1, 2]
    assert (prs[1]["title"], prs[1]["headRefName"], prs[1]["baseRefName"]) == (
        "Add b", "testbot/stack/1", "main",
    )  # fmt: skip
    assert (prs[2]["title"], prs[2]["headRefName"], prs[2]["baseRefName"]) == (
        "Add c", "testbot/stack/2", "testbot/stack/1",
    )  # fmt: skip
    assert fake_gh.calls("pr", "create")[0][4:8] == [
        "--base",
        "main",
        "--head",
        "testbot/stack/1",
    ]

    shas = work.shas()
    assert shas[0] == a  # below the base: untouched
    assert work.messages() == [
        "Add a",
        f"Add b\n\n{stack_info(1)}",
        f"Add c\n\n{stack_info(2)}",
    ]
    assert work.remote.sha("testbot/stack/1") == shas[1]
    assert work.remote.sha("testbot/stack/2") == shas[2]
    # The pushed branch carries the un-exported commit ``a`` in its history.
    assert work.git("rev-parse", f"{shas[1]}^") == a


# --------------------------------------------------------------------------- #
# 9. detached HEAD
# --------------------------------------------------------------------------- #
def test_detached_head_is_moved_and_branch_is_left_alone(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    c = stack3[-1]
    work.git("checkout", "-q", "--detach")
    assert work.git("symbolic-ref", "-q", "HEAD", check=False) == ""

    rc, out, _err = run_export("-v")

    assert rc == 0
    assert "Stack of 3 commits on HEAD (detached)" in out
    assert (
        f"move HEAD (detached) from {c[:8]} to the rewritten tip "
        f"(git update-ref, only if it is still at {c[:8]})"
    ) in plan_lines(out)
    assert result_block(out)[0] == "Exported 3 pull requests (3 new):"
    assert work.git("symbolic-ref", "-q", "HEAD", check=False) == ""  # still detached
    assert work.head("feature") == c
    assert work.head() != c
    assert work.message() == f"Add c\n\n{stack_info(3)}"
    assert work.messages("origin/main..feature") == ["Add a", "Add b", "Add c"]
    assert work.remote.sha("testbot/stack/3") == work.head()
    assert work.reflog("HEAD")[0] == "pstack-pr export"
    assert "pstack-pr export" not in work.reflog("feature")


# --------------------------------------------------------------------------- #
# 10. single commit
# --------------------------------------------------------------------------- #
def test_single_commit_stack_has_plain_body_and_no_edit(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport
) -> None:
    work.commit("a.txt", "Add a\n\nSome details about a.")

    rc, out, _err = run_export("-v")

    assert rc == 0
    assert "Stack of 1 commit on feature" in out
    assert plan_lines(out) == [
        f"push to origin: {work.head('feature@{1}')[:8]} -> testbot/stack/1 (new branch)",
        f"create PR for {work.head('feature@{1}')[:8]}: testbot/stack/1 -> main",
        (
            "rewrite 1 commit message to embed stack-info (git commit-tree; "
            "file contents, authors and dates are unchanged)"
        ),
        (
            f"move feature from {work.head('feature@{1}')[:8]} to the rewritten tip "
            f"(git update-ref, only if it is still at {work.head('feature@{1}')[:8]})"
        ),
        "push the stack to origin (--atomic --force-with-lease): testbot/stack/1",
    ]
    assert not any(line.startswith("update") for line in plan_lines(out))
    assert result_block(out) == [
        "Exported 1 pull request (1 new):",
        result_line(1, 1, "new", "Add a"),
        "Branches pushed: testbot/stack/1 (new)",
    ]
    prs = fake_gh.prs()
    assert list(prs) == [1]
    assert prs[1]["body"] == "Some details about a."
    assert "Stacked PRs:" not in prs[1]["body"]
    assert DELIMITER not in prs[1]["body"]
    assert "edits" not in prs[1]
    assert fake_gh.calls("pr", "edit") == []
    assert len(fake_gh.write_calls()) == 1
    assert work.message() == f"Add a\n\nSome details about a.\n\n{stack_info(1)}"


def test_single_commit_without_description_has_empty_body(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport
) -> None:
    work.commit("a.txt", "Add a")

    rc, _out, _err = run_export()

    assert rc == 0
    assert fake_gh.pr(1)["body"] == ""
    assert fake_gh.calls("pr", "edit") == []


# --------------------------------------------------------------------------- #
# 11. --draft and --reviewer
# --------------------------------------------------------------------------- #
def test_draft_and_reviewers_are_passed_to_pr_create(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    a = stack3[0]

    rc, out, _err = run_export("-v", "--draft", "--reviewer", "alice,bob")

    assert rc == 0
    assert (
        f"create PR for {a[:8]}: testbot/stack/1 -> main (draft; reviewers: alice, bob)"
    ) in plan_lines(out)
    creates = fake_gh.calls("pr", "create")
    assert len(creates) == 3
    for call in creates:
        assert "--draft" in call
        assert call[-5:] == ["--draft", "--reviewer", "alice", "--reviewer", "bob"]
    prs = fake_gh.prs()
    assert [prs[n]["isDraft"] for n in (1, 2, 3)] == [True, True, True]
    assert [prs[n]["reviewers"] for n in (1, 2, 3)] == [["alice", "bob"]] * 3
    assert fake_gh.calls("pr", "ready") == []
    assert "ready" not in out


def test_reexport_does_not_undraft_existing_draft_prs(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    run_export("-d")
    work.commit("d.txt", "Add d")

    rc, _out, _err = run_export()  # no --draft this time

    assert rc == 0
    prs = fake_gh.prs()
    assert [prs[n]["isDraft"] for n in (1, 2, 3, 4)] == [True, True, True, False]
    assert fake_gh.calls("pr", "ready") == []


# --------------------------------------------------------------------------- #
# 12. --keep-body
# --------------------------------------------------------------------------- #
HAND_WRITTEN = "Stacked PRs:\n * #1\n\n--- --- ---\n\nHand written notes"


def test_keep_body_preserves_hand_written_description_and_refreshes_toc(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    run_export()
    fake_gh.set_body(2, HAND_WRITTEN)
    work.commit("d.txt", "Add d")

    rc, _out, _err = run_export("--keep-body")

    assert rc == 0
    body = fake_gh.pr(2)["body"]
    assert body == (
        "Stacked PRs:\n * #4\n * #3\n * __->__#2\n * #1\n\n--- --- ---\n\n"
        "### Add b\n\nHand written notes"
    )
    assert body.endswith("Hand written notes")
    assert fake_gh.pr(2)["edits"] == 2
    assert sorted(fake_gh.prs()) == [1, 2, 3, 4]


def test_without_keep_body_the_description_reverts_to_the_commit_message(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    run_export()
    fake_gh.set_body(2, HAND_WRITTEN)
    work.commit("d.txt", "Add d")

    rc, _out, _err = run_export()

    assert rc == 0
    assert fake_gh.pr(2)["body"] == (
        "Stacked PRs:\n * #4\n * #3\n * __->__#2\n * #1\n\n--- --- ---\n\n### Add b"
    )
    assert "Hand written notes" not in fake_gh.pr(2)["body"]


def test_keep_body_does_not_duplicate_generated_title_heading(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    run_export()
    work.commit("d.txt", "Add d")

    rc, _out, _err = run_export("--keep-body")

    assert rc == 0
    assert fake_gh.pr(1)["body"] == (
        "Stacked PRs:\n * #4\n * #3\n * #2\n * __->__#1\n\n--- --- ---\n\n### Add a"
    )


# --------------------------------------------------------------------------- #
# 13. --branch-name-template
# --------------------------------------------------------------------------- #
TEMPLATE = "wip/$BRANCH/$ID-$USERNAME"


def test_branch_name_template_expands_branch_id_and_username(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    rc, out, _err = run_export("-v", "--branch-name-template", TEMPLATE)

    assert rc == 0
    assert "wip/feature/3-testbot  Add c" in out  # the stack table
    assert result_block(out)[-1] == (
        "Branches pushed: wip/feature/1-testbot (new), wip/feature/2-testbot (new), "
        "wip/feature/3-testbot (new)"
    )
    branches = work.remote.branches()
    assert sorted(branches) == [
        "main",
        "wip/feature/1-testbot",
        "wip/feature/2-testbot",
        "wip/feature/3-testbot",
    ]
    assert [branches[f"wip/feature/{i}-testbot"] for i in (1, 2, 3)] == work.shas()
    prs = fake_gh.prs()
    assert [prs[n]["headRefName"] for n in (1, 2, 3)] == [
        f"wip/feature/{n}-testbot" for n in (1, 2, 3)
    ]
    assert [prs[n]["baseRefName"] for n in (1, 2, 3)] == [
        "main", "wip/feature/1-testbot", "wip/feature/2-testbot",
    ]  # fmt: skip
    assert work.messages() == [
        f"Add a\n\n{stack_info(1, 'wip/feature/1-testbot')}",
        f"Add b\n\n{stack_info(2, 'wip/feature/2-testbot')}",
        f"Add c\n\n{stack_info(3, 'wip/feature/3-testbot')}",
    ]


def test_branch_name_template_reexport_reuses_branches(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    run_export("--branch-name-template", TEMPLATE)
    branches = work.remote.branches()
    head = work.head()

    rc, out, _err = run_export("--branch-name-template", TEMPLATE)

    assert rc == 0
    assert result_block(out)[0] == "Up to date: 3 pull requests, nothing to push."
    assert "Branches pushed:" not in out
    assert work.remote.branches() == branches
    assert work.head() == head
    assert len(fake_gh.calls("pr", "create")) == 3


def test_branch_name_template_allocation_skips_ids_taken_on_the_remote(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    work.git("push", "-q", "origin", "origin/main:refs/heads/wip/feature/7-testbot")
    assert work.remote.sha("wip/feature/7-testbot") == work.head("origin/main")

    rc, out, _err = run_export("--branch-name-template", TEMPLATE)

    assert rc == 0
    assert "(recovered)" not in out
    branches = work.remote.branches()
    assert sorted(branches) == [
        "main",
        "wip/feature/10-testbot",
        "wip/feature/7-testbot",
        "wip/feature/8-testbot",
        "wip/feature/9-testbot",
    ]
    assert branches["wip/feature/7-testbot"] == work.head("origin/main")
    assert [branches[f"wip/feature/{i}-testbot"] for i in (8, 9, 10)] == work.shas()
    prs = fake_gh.prs()
    assert [prs[n]["headRefName"] for n in (1, 2, 3)] == [
        "wip/feature/8-testbot", "wip/feature/9-testbot", "wip/feature/10-testbot",
    ]  # fmt: skip
    assert work.message() == f"Add c\n\n{stack_info(3, 'wip/feature/10-testbot')}"


# --------------------------------------------------------------------------- #
# 14. multi-paragraph commit message
# --------------------------------------------------------------------------- #
def test_multi_paragraph_message_keeps_description_and_adds_one_trailer_paragraph(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport
) -> None:
    description = "First paragraph\nspanning two lines.\n\nSecond paragraph."
    work.commit("a.txt", f"Add a\n\n{description}")
    work.commit("b.txt", "Add b")

    rc, _out, _err = run_export()

    assert rc == 0
    a_sha = work.shas()[0]
    assert raw_message(work, a_sha) == f"Add a\n\n{description}\n\n{stack_info(1)}\n"
    assert raw_message(work, a_sha).count("\n\n\n") == 0
    assert raw_message(work, a_sha).count("stack-info:") == 1
    assert fake_gh.pr(1)["body"] == (
        f"Stacked PRs:\n * #2\n * __->__#1\n\n{DELIMITER}\n\n### Add a\n\n{description}"
    )
    assert fake_gh.pr(1)["title"] == "Add a"
    assert "stack-info" not in fake_gh.pr(1)["body"]


def test_reexport_of_multi_paragraph_message_does_not_duplicate_trailer(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport
) -> None:
    work.commit("a.txt", "Add a\n\nPara one.\n\nPara two.")
    work.commit("b.txt", "Add b")
    run_export()
    head = work.head()

    rc, out, _err = run_export()

    assert rc == 0
    assert result_block(out)[0] == "Up to date: 2 pull requests, nothing to push."
    assert "Branches pushed:" not in out
    assert work.head() == head
    assert (
        raw_message(work, work.shas()[0])
        == f"Add a\n\nPara one.\n\nPara two.\n\n{stack_info(1)}\n"
    )


# --------------------------------------------------------------------------- #
# 15. --verbose
# --------------------------------------------------------------------------- #
def export_in_subprocess(work: Work, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the CLI in a fresh interpreter.

    Under pytest the root logger already has handlers, so the CLI's
    ``logging.basicConfig`` (which the command log relies on) would be a no-op
    in-process.
    """
    return subprocess.run(
        [sys.executable, "-m", "pstack_pr", "export", *args],
        cwd=work.path,
        env=dict(os.environ),
        capture_output=True,
        text=True,
        check=False,
    )


USER_CALL = ["api", "--hostname", "github.com", "user", "--jq", ".login"]
GRAPHQL_CALL = ["api", "--hostname", "github.com", "graphql"]
# The one fetch every export runs: exactly the target branch, into its
# remote-tracking ref, nothing else (no tags, no other branches).
FETCH_MAIN = (
    "$ git fetch --quiet --no-tags origin +refs/heads/main:refs/remotes/origin/main"
)
# The one ls-remote for the stack branches: the template glob is only asked
# for when some commit has no stack-info trailer yet.
LS_REMOTE_TEMPLATE = (
    "$ git ls-remote --heads --refs origin 'refs/heads/testbot/stack/*'"
)
LS_REMOTE_KNOWN = (
    "$ git ls-remote --heads --refs origin refs/heads/testbot/stack/1 "
    "refs/heads/testbot/stack/2 refs/heads/testbot/stack/3"
)


def fetch_lines(proc: subprocess.CompletedProcess[str]) -> list[str]:
    """Every ``git fetch`` command line a -vv run logged, in order."""
    return [line for line in proc.stderr.splitlines() if line.startswith("$ git fetch")]


def test_double_verbose_logs_git_and_gh_commands_to_stderr(
    work: Work, fake_gh: FakeGitHub, stack3: list[str]
) -> None:
    proc = export_in_subprocess(work, "-vv", "-n")

    assert proc.returncode == 0, proc.stderr
    assert "Dry run: nothing was changed." in proc.stdout
    # Every export, a dry run included, refreshes origin/main with exactly one
    # targeted fetch, then asks the remote about the stack branches once. main
    # exists, so the 'master' hint is never looked up.
    assert fetch_lines(proc) == [FETCH_MAIN]
    assert LS_REMOTE_TEMPLATE in proc.stderr
    assert proc.stderr.count("$ git ls-remote") == 1
    assert "refs/heads/master" not in proc.stderr
    assert proc.stderr.index(FETCH_MAIN) < proc.stderr.index(LS_REMOTE_TEMPLATE)
    assert "$ git " in proc.stderr
    assert "$ gh api --hostname github.com user --jq .login" in proc.stderr
    assert "  [stdout] testbot" in proc.stderr
    assert "$ git push" not in proc.stderr
    assert "$ git " not in proc.stdout
    assert work.head() == stack3[-1]
    assert fake_gh.write_calls() == []


def test_single_verbose_does_not_log_commands_to_stderr(
    work: Work, fake_gh: FakeGitHub, stack3: list[str]
) -> None:
    proc = export_in_subprocess(work, "-v", "-n")

    assert proc.returncode == 0, proc.stderr
    assert "Dry run: nothing was changed." in proc.stdout
    assert "Contacting origin..." in proc.stdout
    assert proc.stderr == ""


def test_without_verbose_nothing_is_logged_to_stderr(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    rc, out, err = run_export("-n")

    assert rc == 0
    assert err == ""
    assert "$ git" not in out


# --------------------------------------------------------------------------- #
# 16. selective fetch and batched lookups
# --------------------------------------------------------------------------- #
def has_object(work: Work, sha: str) -> bool:
    """Whether commit ``sha`` is in ``work``'s object store, by exit status."""
    proc = subprocess.run(
        ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
        cwd=work.path,
        capture_output=True,
        check=False,
    )
    return proc.returncode == 0


def tracking_refs(work: Work) -> list[str]:
    """Full names of the remote-tracking refs in ``work``, sorted."""
    out = work.git("for-each-ref", "--format=%(refname)", "refs/remotes/")
    return sorted(out.splitlines())


def graphql_calls(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if c[: len(GRAPHQL_CALL)] == GRAPHQL_CALL]


def graphql_numbers(call: list[str]) -> list[int]:
    """The pull request numbers one recorded ``gh api graphql`` call asked for."""
    query = next(a for a in call if a.startswith("query="))
    return [int(n) for n in re.findall(r"pullRequest\(number: (\d+)\)", query)]


def other_clone(tmp_path: Path, work: Work) -> Path:
    """A second clone of the bare remote, standing in for another developer."""
    path = tmp_path / "other"
    git("clone", "-q", str(work.remote.path), str(path), cwd=tmp_path)
    return path


def commit_in(clone: Path, name: str, message: str) -> str:
    (clone / name).write_text(f"{name}\n")
    git("add", name, cwd=clone)
    git("commit", "-q", "-m", message, cwd=clone)
    return git("rev-parse", "HEAD", cwd=clone)


def test_fresh_export_fetches_main_exactly_once_even_when_already_up_to_date(
    work: Work, fake_gh: FakeGitHub, stack3: list[str]
) -> None:
    main = work.head("origin/main")
    assert work.remote.sha("main") == main
    assert has_object(work, main)

    proc = export_in_subprocess(work, "-vv")

    assert proc.returncode == 0, proc.stderr
    assert "Contacting origin..." in proc.stdout
    assert "Exported 3 pull requests (3 new):" in proc.stdout
    # origin/main is refreshed with one fetch of exactly that branch although
    # its tip is already local: the explicit refspec makes that a single cheap
    # round trip. No other fetch happens, and no probe of the object store or
    # ls-remote of the target replaces it.
    assert fetch_lines(proc) == [FETCH_MAIN]
    assert work.head("origin/main") == main
    # Stack branch state comes from one ls-remote, after the fetch. Nothing is
    # known yet, so only the template's glob is asked for; main exists, so the
    # 'master' hint is never looked up.
    assert LS_REMOTE_TEMPLATE in proc.stderr
    assert proc.stderr.count("$ git ls-remote") == 1
    assert "refs/heads/master" not in proc.stderr
    assert proc.stderr.index(FETCH_MAIN) < proc.stderr.index(LS_REMOTE_TEMPLATE)
    # gh: one login lookup to name the branches, no pull request lookups (none
    # exist yet) and then only the writes.
    calls = fake_gh.calls()
    assert calls[0] == USER_CALL
    assert calls.count(USER_CALL) == 1
    assert graphql_calls(calls) == []
    assert fake_gh.calls("pr", "view") == []
    assert [c[:2] for c in calls[1:]] == [["pr", "create"]] * 3 + [["pr", "edit"]] * 3


def test_reexport_of_unchanged_stack_makes_exactly_one_batched_pr_lookup(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    run_export()
    start = len(fake_gh.calls())

    rc, out, err = run_export()

    assert rc == 0
    assert err == ""
    assert result_block(out)[0] == "Up to date: 3 pull requests, nothing to push."
    new_calls = fake_gh.calls()[start:]
    assert len(new_calls) == 1
    (lookup,) = new_calls
    assert lookup[:4] == GRAPHQL_CALL
    assert lookup[-4:] == ["-f", "owner=octo", "-f", "name=widgets"]
    assert graphql_numbers(lookup) == [1, 2, 3]
    # Every commit carries a trailer: no branch name had to be allocated, so
    # the login was not looked up, and no PR was viewed one by one.
    assert USER_CALL not in new_calls
    assert fake_gh.calls("pr", "view") == []
    assert fake_gh.calls("pr", "list") == []


def test_reexport_double_verbose_fetches_main_once_and_asks_for_known_branches(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    run_export()

    proc = export_in_subprocess(work, "-vv")

    assert proc.returncode == 0, proc.stderr
    assert "Up to date: 3 pull requests, nothing to push." in proc.stdout
    # A no-op re-export talks to the remote exactly three times: one targeted
    # fetch of main, one ls-remote for the branches named in the trailers (no
    # template glob: no new name has to be allocated) and one batched PR
    # lookup. No login lookup, no per-PR view, nothing pushed.
    assert fetch_lines(proc) == [FETCH_MAIN]
    assert LS_REMOTE_KNOWN in proc.stderr
    assert "refs/heads/testbot/stack/*" not in proc.stderr
    assert "refs/heads/master" not in proc.stderr
    assert proc.stderr.count("$ git ls-remote") == 1
    assert proc.stderr.index(FETCH_MAIN) < proc.stderr.index(LS_REMOTE_KNOWN)
    gh_lines = [line for line in proc.stderr.splitlines() if line.startswith("$ gh ")]
    assert len(gh_lines) == 1
    assert gh_lines[0].startswith("$ gh api --hostname github.com graphql -f ")
    assert "$ gh pr view" not in proc.stderr
    assert "$ gh api --hostname github.com user" not in proc.stderr
    assert "$ git push" not in proc.stderr


def test_export_fetches_only_the_target_branch_when_the_remote_moved(
    tmp_path: Path, work: Work, fake_gh: FakeGitHub, stack3: list[str]
) -> None:
    old_main = work.head("origin/main")
    other = other_clone(tmp_path, work)
    git("checkout", "-q", "-b", "unrelated", cwd=other)
    unrelated = commit_in(other, "unrelated.txt", "Unrelated work")
    git("push", "-q", "origin", "unrelated", cwd=other)
    git("checkout", "-q", "main", cwd=other)
    new_main = commit_in(other, "main.txt", "Advance main")
    git("push", "-q", "origin", "main", cwd=other)
    assert work.remote.sha("main") == new_main != old_main
    assert work.remote.sha("unrelated") == unrelated
    assert not has_object(work, new_main)
    assert not has_object(work, unrelated)

    proc = export_in_subprocess(work, "-vv")

    assert proc.returncode == 0, proc.stderr
    assert "Exported 3 pull requests (3 new):" in proc.stdout
    # Exactly one fetch, of exactly the target branch: origin/main now has the
    # new tip and its commit is local...
    assert fetch_lines(proc) == [FETCH_MAIN]
    assert work.head("origin/main") == new_main
    assert has_object(work, new_main)
    # ...while the unrelated branch was neither fetched nor given a tracking
    # ref. (The tracking refs of the stack branches are created by git itself
    # when they are pushed.)
    assert not has_object(work, unrelated)
    assert tracking_refs(work) == [
        "refs/remotes/origin/HEAD",
        "refs/remotes/origin/main",
        "refs/remotes/origin/testbot/stack/1",
        "refs/remotes/origin/testbot/stack/2",
        "refs/remotes/origin/testbot/stack/3",
    ]
    # The stack still sits on the old main, so that is its base (the merge
    # base), even though origin/main itself now points at the new tip.
    assert f"(base: origin/main @ {old_main[:8]})" in proc.stdout
    assert work.head("HEAD~3") == old_main

    # Rebased onto the freshly fetched main, the stack has the new tip as its
    # base. origin/main is refreshed with the same single fetch as always; it
    # just transfers nothing this time.
    work.git("rebase", "-q", "origin/main", "feature")
    assert work.head("HEAD~3") == new_main

    proc = export_in_subprocess(work, "-vv")

    assert proc.returncode == 0, proc.stderr
    assert f"(base: origin/main @ {new_main[:8]})" in proc.stdout
    assert fetch_lines(proc) == [FETCH_MAIN]
    assert work.head("origin/main") == new_main
    assert "Exported 3 pull requests (3 updated):" in proc.stdout
    assert (
        "Branches pushed: testbot/stack/1 (updated), testbot/stack/2 (updated), "
        "testbot/stack/3 (updated)"
    ) in proc.stdout
    assert not has_object(work, unrelated)
    assert "refs/remotes/origin/unrelated" not in tracking_refs(work)


def test_adding_a_commit_looks_up_the_login_once_and_the_prs_in_one_batch(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    run_export()
    start = len(fake_gh.calls())
    d = work.commit("d.txt", "Add d")

    rc, out, err = run_export()

    assert rc == 0
    assert err == ""
    new_calls = fake_gh.calls()[start:]
    # One commit has no trailer, so a branch name is allocated: one login
    # lookup. The three existing PRs are fetched with one GraphQL request.
    assert new_calls.count(USER_CALL) == 1
    lookups = graphql_calls(new_calls)
    assert len(lookups) == 1
    assert graphql_numbers(lookups[0]) == [1, 2, 3]
    assert new_calls[:2] == [USER_CALL, lookups[0]]
    assert fake_gh.calls("pr", "view") == []
    assert fake_gh.calls("pr", "list") == []
    assert [c[:2] for c in new_calls[2:]] == [["pr", "create"]] + [["pr", "edit"]] * 4
    assert result_block(out) == [
        "Exported 4 pull requests (1 new, 3 updated):",
        result_line(4, 4, "new", "Add d"),
        result_line(3, 3, "updated", "Add c"),
        result_line(2, 2, "updated", "Add b"),
        result_line(1, 1, "updated", "Add a"),
        "Branches pushed: testbot/stack/4 (new)",
    ]
    assert work.head() != d
    assert work.message() == f"Add d\n\n{stack_info(4)}"


def test_branches_pushed_labels_amended_branch_updated_and_new_branch_new(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    run_export()
    a1, b1, c1 = work.shas()
    # Amend the top commit's content (message and stack-info unchanged) and
    # put a brand new commit on top of it.
    (work.path / "c.txt").write_text("c changed\n")
    work.git("add", "c.txt")
    work.git("commit", "-q", "--amend", "--no-edit")
    c2 = work.head()
    assert c2 != c1
    assert work.message() == work.message(c1)
    d = work.commit("d.txt", "Add d")

    rc, out, err = run_export("-v")

    assert rc == 0
    assert err == ""
    assert plan_lines(out) == [
        f"push to origin: {d[:8]} -> testbot/stack/4 (new branch)",
        f"create PR for {d[:8]}: testbot/stack/4 -> testbot/stack/3",
        (
            "rewrite 1 commit message to embed stack-info (git commit-tree; "
            "file contents, authors and dates are unchanged)"
        ),
        (
            f"move feature from {d[:8]} to the rewritten tip "
            f"(git update-ref, only if it is still at {d[:8]})"
        ),
        (
            "push the stack to origin (--atomic --force-with-lease): "
            "testbot/stack/3, testbot/stack/4"
        ),
        "update PR #1: body (cross-links)",
        "update PR #2: body (cross-links)",
        "update PR #3: body (cross-links)",
        f"update the new PR for {d[:8]}: body (cross-links)",
    ]
    assert result_block(out) == [
        "Exported 4 pull requests (1 new, 3 updated):",
        result_line(4, 4, "new", "Add d"),
        result_line(3, 3, "updated", "Add c"),
        result_line(2, 2, "updated", "Add b"),
        result_line(1, 1, "updated", "Add a"),
        "Branches pushed: testbot/stack/3 (updated), testbot/stack/4 (new)",
    ]
    branches = work.remote.branches()
    assert branches["testbot/stack/1"] == a1
    assert branches["testbot/stack/2"] == b1
    assert branches["testbot/stack/3"] == c2
    assert branches["testbot/stack/4"] == work.head()
    assert work.git("show", f"{branches['testbot/stack/3']}:c.txt") == "c changed"


def test_lease_and_labels_follow_the_remote_not_stale_tracking_refs(
    tmp_path: Path,
    work: Work,
    fake_gh: FakeGitHub,
    run_export: RunExport,
    stack3: list[str],
) -> None:
    run_export()
    a1, b1, c1 = work.shas()
    # Pushing updated the remote-tracking ref of the stack branch...
    assert work.head("refs/remotes/origin/testbot/stack/2") == b1

    # ...but someone else moves that branch from another clone, so the
    # tracking ref is stale by the time we export again.
    other = other_clone(tmp_path, work)
    git("checkout", "-q", "testbot/stack/2", cwd=other)
    theirs = commit_in(other, "theirs.txt", "Their tweak")
    git("push", "-q", "origin", "HEAD:testbot/stack/2", cwd=other)
    assert work.remote.sha("testbot/stack/2") == theirs
    assert work.head("refs/remotes/origin/testbot/stack/2") == b1
    assert fake_gh.pr(2)["state"] == "OPEN"
    start = len(fake_gh.calls())

    rc, out, err = run_export("-v")

    # The lease was taken on what ls-remote reported, not on the stale ref, so
    # the push went through and the branch counts as updated, not new.
    assert rc == 0, err
    assert plan_lines(out) == [
        "push the stack to origin (--atomic --force-with-lease): testbot/stack/2"
    ]
    assert result_block(out) == [
        "Exported 3 pull requests (1 updated, 2 unchanged):",
        result_line(3, 3, "unchanged", "Add c"),
        result_line(2, 2, "updated", "Add b"),
        result_line(1, 1, "unchanged", "Add a"),
        "Branches pushed: testbot/stack/2 (updated)",
    ]
    assert work.remote.sha("testbot/stack/2") == b1
    assert work.remote.sha("testbot/stack/1") == a1
    assert work.remote.sha("testbot/stack/3") == c1
    assert work.shas() == [a1, b1, c1]
    assert not has_object(work, theirs)  # nothing but the target is ever fetched
    # Only the batched PR lookup talked to GitHub: no writes, no login lookup.
    new_calls = fake_gh.calls()[start:]
    assert len(new_calls) == 1
    assert new_calls[0][:4] == GRAPHQL_CALL


# --------------------------------------------------------------------------- #
# 17. partial and shallow clones
# --------------------------------------------------------------------------- #
def github_clone(remote: Remote, path: Path, *clone_flags: str) -> Work:
    """Clone ``remote`` the way the ``work`` fixture does, with extra clone flags.

    The source is a ``file://`` URL because git ignores ``--filter`` and
    ``--depth`` when cloning from a plain local path.
    """
    url = f"file://{remote.path}"
    git("clone", "-q", *clone_flags, url, str(path), cwd=path.parent)
    github_url = f"git@github.com:{remote.slug}.git"
    git("remote", "set-url", "origin", github_url, cwd=path)
    git("config", f"url.{remote.path}.insteadOf", github_url, cwd=path)
    git("checkout", "-q", "-b", "feature", cwd=path)
    return Work(path=path, remote=remote)


def test_export_from_partial_clone_fetches_main_once_and_nothing_else(
    tmp_path: Path, remote: Remote, fake_gh: FakeGitHub
) -> None:
    git("config", "uploadpack.allowFilter", "true", cwd=remote.path)
    partial = github_clone(remote, tmp_path / "partial", "--filter=blob:none")
    assert partial.git("config", "--get", "remote.origin.promisor") == "true"
    assert (
        partial.git("config", "--get", "remote.origin.partialclonefilter")
        == "blob:none"
    )
    main = partial.head("origin/main")
    assert remote.sha("main") == main
    a = partial.commit("a.txt", "Add a")
    b = partial.commit("b.txt", "Add b")

    proc = export_in_subprocess(partial, "-vv")

    assert proc.returncode == 0, proc.stderr
    assert f"(base: origin/main @ {main[:8]})" in proc.stdout
    assert result_block(proc.stdout) == [
        "Exported 2 pull requests (2 new):",
        result_line(2, 2, "new", "Add b"),
        result_line(1, 1, "new", "Add a"),
        "Branches pushed: testbot/stack/1 (new), testbot/stack/2 (new)",
    ]
    prs = fake_gh.prs()
    assert [
        (prs[n]["title"], prs[n]["headRefName"], prs[n]["baseRefName"]) for n in (1, 2)
    ] == [
        ("Add a", "testbot/stack/1", "main"),
        ("Add b", "testbot/stack/2", "testbot/stack/1"),
    ]
    # A partial clone fetches like any other: exactly one targeted fetch of
    # main, and the promisor remote's filter did not make the tool fetch more.
    assert fetch_lines(proc) == [FETCH_MAIN]
    assert partial.head("origin/main") == main
    assert tracking_refs(partial) == [
        "refs/remotes/origin/HEAD",
        "refs/remotes/origin/main",
        "refs/remotes/origin/testbot/stack/1",
        "refs/remotes/origin/testbot/stack/2",
    ]
    assert partial.messages() == [
        f"Add a\n\n{stack_info(1)}",
        f"Add b\n\n{stack_info(2)}",
    ]
    assert partial.head() != b
    assert [partial.tree(s) for s in partial.shas()] == [
        partial.tree(a),
        partial.tree(b),
    ]
    assert remote.sha("testbot/stack/2") == partial.head()
    assert remote.sha("testbot/stack/1") == partial.head("HEAD~1")
    assert partial.git("config", "--get", "remote.origin.promisor") == "true"


def test_export_from_shallow_clone_works_when_the_merge_base_is_in_its_history(
    tmp_path: Path, remote: Remote, fake_gh: FakeGitHub
) -> None:
    # main gets three commits; a depth-1 clone sees only the last of them.
    advance = tmp_path / "advance"
    git("clone", "-q", str(remote.path), str(advance), cwd=tmp_path)
    commit_in(advance, "m2.txt", "Add m2")
    commit_in(advance, "m3.txt", "Add m3")
    git("push", "-q", "origin", "HEAD:main", cwd=advance)
    main = remote.sha("main")
    assert main is not None
    assert git("rev-list", "--count", "main", cwd=remote.path) == "3"

    shallow = github_clone(remote, tmp_path / "shallow", "--depth", "1")
    assert shallow.git("rev-parse", "--is-shallow-repository") == "true"
    assert shallow.git("rev-list", "--count", "HEAD") == "1"
    assert shallow.head("origin/main") == main
    a = shallow.commit("a.txt", "Add a")

    proc = export_in_subprocess(shallow, "-vv")

    # The merge base of the stack and origin/main is the shallow boundary
    # commit itself, which the clone has, so the export goes through. (A
    # stack based below the boundary would have no merge base at all; that
    # case is not exercised here.)
    assert proc.returncode == 0, proc.stderr
    assert f"(base: origin/main @ {main[:8]})" in proc.stdout
    assert result_block(proc.stdout) == [
        "Exported 1 pull request (1 new):",
        result_line(1, 1, "new", "Add a"),
        "Branches pushed: testbot/stack/1 (new)",
    ]
    assert fetch_lines(proc) == [FETCH_MAIN]
    pr = fake_gh.pr(1)
    assert (pr["title"], pr["headRefName"], pr["baseRefName"]) == (
        "Add a",
        "testbot/stack/1",
        "main",
    )
    assert shallow.message() == f"Add a\n\n{stack_info(1)}"
    assert shallow.head() != a
    assert shallow.tree() == shallow.tree(a)
    assert shallow.head("HEAD~1") == main
    assert remote.sha("testbot/stack/1") == shallow.head()
    # The one fetch did not deepen the clone: it is still shallow and still
    # holds just the boundary commit below the stack.
    assert shallow.git("rev-parse", "--is-shallow-repository") == "true"
    assert shallow.git("rev-list", "--count", "HEAD") == "2"
    # --depth implies --single-branch: origin's fetch refspec covers only main,
    # so git creates no tracking ref for the pushed stack branch either.
    assert shallow.git("config", "--get", "remote.origin.fetch") == (
        "+refs/heads/main:refs/remotes/origin/main"
    )
    assert tracking_refs(shallow) == [
        "refs/remotes/origin/HEAD",
        "refs/remotes/origin/main",
    ]
