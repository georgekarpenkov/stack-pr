"""Unit tests for :mod:`pstack_pr.stack`: pure functions, no git and no ``gh``."""

from __future__ import annotations

import pytest

from pstack_pr.errors import PstackError
from pstack_pr.git import Commit, Identity
from pstack_pr.github import Comment, PullRequest
from pstack_pr.stack import (
    CROSS_LINKS_DELIMITER,
    STACK_COMMENT_MARKER,
    TMP_DRAFT_MARKER,
    TOC_CURRENT_MARKER,
    TOC_HEADER,
    BranchTemplate,
    Stack,
    StackEntry,
    StackInfo,
    add_stack_info,
    check_linear,
    check_titles,
    description_from_existing_body,
    find_stack_comment,
    has_tmp_draft_marker,
    parse_stack_info,
    pr_body,
    pr_title,
    stack_comment_body,
    strip_stack_info,
    title_and_description,
    toc,
    verify_existing_pr,
)

PR_URL = "https://github.com/octo/widgets/pull"
IDENTITY = Identity(name="Ada Author", email="ada@example.com", date="1700000000 +0100")
TREE = "3" * 40
ROOT = "0" * 40


def sha_for(n: int) -> str:
    """A distinct, deterministic 40-hex sha for entry ``n``."""
    return f"{n:040x}"


def make_commit(
    message: str,
    *,
    sha: str = sha_for(0xA),
    parents: tuple[str, ...] = (ROOT,),
) -> Commit:
    return Commit(
        sha=sha,
        tree=TREE,
        parents=parents,
        author=IDENTITY,
        committer=IDENTITY,
        message=message,
    )


def make_pr(
    number: int,
    *,
    head: str,
    base: str = "main",
    state: str = "OPEN",
    url: str | None = None,
    title: str = "",
    body: str = "",
    is_draft: bool = False,
) -> PullRequest:
    return PullRequest(
        number=number,
        url=url if url is not None else f"{PR_URL}/{number}",
        state=state,
        is_draft=is_draft,
        title=title,
        body=body,
        base=base,
        head=head,
    )


def make_entry(
    index: int,
    message: str,
    *,
    branch: str = "",
    pr: PullRequest | None = None,
    info: StackInfo | None = None,
) -> StackEntry:
    return StackEntry(
        index=index,
        commit=make_commit(message, sha=sha_for(index + 1)),
        info=info,
        branch=branch,
        pr=pr,
    )


def linked_stack(messages: list[str]) -> list[StackEntry]:
    """Entries ``0..n`` on ``testbot/stack/1..n`` with PRs ``#1..n`` chained."""
    entries = []
    for i, message in enumerate(messages):
        branch = f"testbot/stack/{i + 1}"
        base = "main" if i == 0 else f"testbot/stack/{i}"
        entries.append(
            make_entry(
                i, message, branch=branch, pr=make_pr(i + 1, head=branch, base=base)
            )
        )
    return entries


def template(spec: str = "$USERNAME/stack") -> BranchTemplate:
    return BranchTemplate(spec, username="testbot", current_branch="feature")


# --------------------------------------------------------------------------- #
# StackInfo / parse_stack_info
# --------------------------------------------------------------------------- #
def test_stack_info_line_format() -> None:
    info = StackInfo(pr_url=f"{PR_URL}/30", branch="user/stack/7")
    assert info.line == f"stack-info: PR: {PR_URL}/30, branch: user/stack/7"


def test_parse_stack_info_none_when_absent() -> None:
    assert parse_stack_info("Add a\n\nA description.\n") is None


def test_parse_stack_info_none_for_empty_message() -> None:
    assert parse_stack_info("") is None


def test_parse_stack_info_single_trailer() -> None:
    message = f"Add a\n\nstack-info: PR: {PR_URL}/1, branch: testbot/stack/1\n"
    assert parse_stack_info(message) == StackInfo(
        pr_url=f"{PR_URL}/1", branch="testbot/stack/1"
    )


def test_parse_stack_info_line_without_trailing_newline() -> None:
    message = f"Add a\n\nstack-info: PR: {PR_URL}/1, branch: testbot/stack/1"
    assert parse_stack_info(message) == StackInfo(f"{PR_URL}/1", "testbot/stack/1")


def test_parse_stack_info_repeated_last_wins() -> None:
    message = (
        "Add a\n\n"
        f"stack-info: PR: {PR_URL}/1, branch: testbot/stack/1\n"
        f"stack-info: PR: {PR_URL}/9, branch: testbot/stack/9\n"
    )
    assert parse_stack_info(message) == StackInfo(f"{PR_URL}/9", "testbot/stack/9")


def test_parse_stack_info_tolerates_trailing_spaces_and_tabs() -> None:
    message = f"Add a\n\nstack-info: PR: {PR_URL}/1, branch: testbot/stack/1 \t \n"
    assert parse_stack_info(message) == StackInfo(f"{PR_URL}/1", "testbot/stack/1")


def test_parse_stack_info_requires_column_zero() -> None:
    message = f"Add a\n\n  stack-info: PR: {PR_URL}/1, branch: testbot/stack/1\n"
    assert parse_stack_info(message) is None


def test_parse_stack_info_ignores_line_with_extra_words() -> None:
    message = f"Add a\n\nstack-info: PR: {PR_URL}/1, branch: testbot/stack/1 extra\n"
    assert parse_stack_info(message) is None


def test_parse_stack_info_ignores_mention_inside_a_sentence() -> None:
    message = f"Add a\n\nSee stack-info: PR: {PR_URL}/1, branch: testbot/stack/1\n"
    assert parse_stack_info(message) is None


def test_parse_stack_info_found_in_middle_of_message() -> None:
    message = f"Add a\n\nstack-info: PR: {PR_URL}/4, branch: b/4\n\nMore text.\n"
    assert parse_stack_info(message) == StackInfo(f"{PR_URL}/4", "b/4")


# --------------------------------------------------------------------------- #
# strip_stack_info
# --------------------------------------------------------------------------- #
def test_strip_stack_info_removes_trailer_and_blank_lines() -> None:
    message = f"Add a\n\nBody.\n\nstack-info: PR: {PR_URL}/1, branch: b/1\n"
    assert strip_stack_info(message) == "Add a\n\nBody.\n"


def test_strip_stack_info_removes_all_trailers() -> None:
    message = (
        "Add a\n\n"
        f"stack-info: PR: {PR_URL}/1, branch: b/1\n"
        f"stack-info: PR: {PR_URL}/2, branch: b/2\n"
    )
    assert strip_stack_info(message) == "Add a\n"


def test_strip_stack_info_leaves_message_without_trailer_alone() -> None:
    assert strip_stack_info("Add a\n\nBody.\n") == "Add a\n\nBody.\n"


def test_strip_stack_info_ends_with_exactly_one_newline() -> None:
    assert strip_stack_info("Add a") == "Add a\n"
    assert strip_stack_info("Add a\n\n\n\n") == "Add a\n"
    assert strip_stack_info("Add a  \n \t\n") == "Add a\n"


def test_strip_stack_info_empty_message() -> None:
    assert strip_stack_info("") == ""


def test_strip_stack_info_whitespace_only_message() -> None:
    assert strip_stack_info("\n  \n") == ""


def test_strip_stack_info_trailer_only_message_becomes_empty() -> None:
    assert strip_stack_info(f"stack-info: PR: {PR_URL}/1, branch: b/1\n") == ""


def test_strip_stack_info_keeps_indented_lookalike() -> None:
    message = f"Add a\n\n  stack-info: PR: {PR_URL}/1, branch: b/1\n"
    assert strip_stack_info(message) == message


def test_strip_stack_info_trailer_in_middle_keeps_surrounding_text() -> None:
    message = f"Add a\n\nstack-info: PR: {PR_URL}/1, branch: b/1\n\nBody.\n"
    result = strip_stack_info(message)
    assert "stack-info" not in result
    assert result.startswith("Add a\n")
    assert result.endswith("Body.\n")


# --------------------------------------------------------------------------- #
# add_stack_info
# --------------------------------------------------------------------------- #
INFO = StackInfo(pr_url=f"{PR_URL}/7", branch="testbot/stack/7")


def test_add_stack_info_title_only() -> None:
    assert add_stack_info("Add a\n", INFO) == f"Add a\n\n{INFO.line}\n"


def test_add_stack_info_without_trailing_newline() -> None:
    assert add_stack_info("Add a", INFO) == f"Add a\n\n{INFO.line}\n"


def test_add_stack_info_preserves_body() -> None:
    message = "Add a\n\nFirst paragraph.\n\nSecond paragraph.\n"
    expected = f"Add a\n\nFirst paragraph.\n\nSecond paragraph.\n\n{INFO.line}\n"
    assert add_stack_info(message, INFO) == expected


def test_add_stack_info_collapses_trailing_blank_lines() -> None:
    assert add_stack_info("Add a\n\n\n\n", INFO) == f"Add a\n\n{INFO.line}\n"


def test_add_stack_info_is_idempotent() -> None:
    once = add_stack_info("Add a\n\nBody.\n", INFO)
    assert add_stack_info(once, INFO) == once


def test_add_stack_info_exactly_one_trailer_paragraph() -> None:
    result = add_stack_info("Add a\n\nBody.\n", INFO)
    assert result.count("stack-info:") == 1
    assert result.endswith(f"\n\n{INFO.line}\n")
    assert "\n\n\n" not in result


def test_add_stack_info_replaces_old_trailer() -> None:
    old = StackInfo(pr_url=f"{PR_URL}/1", branch="testbot/stack/1")
    message = add_stack_info("Add a\n\nBody.\n", old)
    result = add_stack_info(message, INFO)
    assert result == f"Add a\n\nBody.\n\n{INFO.line}\n"
    assert old.line not in result


def test_add_stack_info_replaces_multiple_old_trailers() -> None:
    message = (
        "Add a\n\n"
        f"stack-info: PR: {PR_URL}/1, branch: b/1\n"
        f"stack-info: PR: {PR_URL}/2, branch: b/2\n"
    )
    assert add_stack_info(message, INFO) == f"Add a\n\n{INFO.line}\n"


def test_add_stack_info_round_trips_through_parse() -> None:
    assert parse_stack_info(add_stack_info("Add a\n\nBody.\n", INFO)) == INFO


# --------------------------------------------------------------------------- #
# title_and_description
# --------------------------------------------------------------------------- #
def test_title_and_description_title_only() -> None:
    assert title_and_description("Add a\n") == ("Add a", "")


def test_title_and_description_single_paragraph() -> None:
    assert title_and_description("Add a\n\nBody.\n") == ("Add a", "Body.")


def test_title_and_description_multi_paragraph_body() -> None:
    message = "Add a\n\nFirst paragraph\nspans two lines.\n\nSecond paragraph.\n"
    assert title_and_description(message) == (
        "Add a",
        "First paragraph\nspans two lines.\n\nSecond paragraph.",
    )


def test_title_and_description_drops_trailer() -> None:
    message = f"Add a\n\nBody.\n\n{INFO.line}\n"
    assert title_and_description(message) == ("Add a", "Body.")


def test_title_and_description_trailer_only_after_title() -> None:
    assert title_and_description(f"Add a\n\n{INFO.line}\n") == ("Add a", "")


def test_title_and_description_strips_whitespace() -> None:
    assert title_and_description("  Add a  \n\n\n  Body.  \n\n") == ("Add a", "Body.")


def test_title_and_description_body_directly_below_title() -> None:
    assert title_and_description("Add a\nBody.\n") == ("Add a", "Body.")


def test_title_and_description_empty_message() -> None:
    assert title_and_description("") == ("", "")


# --------------------------------------------------------------------------- #
# StackEntry
# --------------------------------------------------------------------------- #
def test_stack_entry_defaults() -> None:
    entry = make_entry(0, "Add a\n")
    assert entry.info is None
    assert entry.branch == ""
    assert entry.base == ""
    assert entry.pr is None
    assert entry.remote_sha is None
    assert entry.new_sha is None
    assert entry.adopted is False
    assert entry.at_risk is False
    assert entry.tmp_draft is False
    assert entry.will_rewrite is False
    assert entry.has_pr is False


def test_stack_entry_pr_label_new() -> None:
    assert make_entry(0, "Add a\n").pr_label == "new PR"


def test_stack_entry_pr_label_existing() -> None:
    entry = make_entry(0, "Add a\n", pr=make_pr(12, head="testbot/stack/1"))
    assert entry.pr_label == "#12"
    assert entry.has_pr is True


def test_stack_entry_desired_info_requires_pr() -> None:
    entry = make_entry(0, "Add a\n", branch="testbot/stack/1")
    with pytest.raises(PstackError, match="pull request not created yet"):
        entry.desired_info()


def test_stack_entry_desired_info_uses_pr_url_and_own_branch() -> None:
    pr = make_pr(3, head="testbot/stack/3")
    entry = make_entry(0, "Add a\n", branch="testbot/stack/3", pr=pr)
    assert entry.desired_info() == StackInfo(
        pr_url=f"{PR_URL}/3", branch="testbot/stack/3"
    )


def test_stack_entry_desired_info_prefers_entry_branch_over_pr_head() -> None:
    # The branch assigned by the planner is authoritative for the trailer.
    pr = make_pr(3, head="stale/head")
    entry = make_entry(0, "Add a\n", branch="testbot/stack/3", pr=pr)
    assert entry.desired_info().branch == "testbot/stack/3"


def test_stack_entry_desired_message_appends_trailer() -> None:
    pr = make_pr(3, head="testbot/stack/3")
    entry = make_entry(0, "Add a\n\nBody.\n", branch="testbot/stack/3", pr=pr)
    assert entry.desired_message() == (
        f"Add a\n\nBody.\n\nstack-info: PR: {PR_URL}/3, branch: testbot/stack/3\n"
    )


def test_stack_entry_desired_message_is_stable_when_trailer_present() -> None:
    pr = make_pr(3, head="testbot/stack/3")
    message = f"Add a\n\nstack-info: PR: {PR_URL}/3, branch: testbot/stack/3\n"
    entry = make_entry(0, message, branch="testbot/stack/3", pr=pr)
    assert entry.desired_message() == message


def test_stack_entry_desired_message_rewrites_stale_trailer() -> None:
    pr = make_pr(3, head="testbot/stack/3")
    message = f"Add a\n\nstack-info: PR: {PR_URL}/1, branch: testbot/stack/1\n"
    entry = make_entry(0, message, branch="testbot/stack/3", pr=pr)
    assert entry.desired_message() == (
        f"Add a\n\nstack-info: PR: {PR_URL}/3, branch: testbot/stack/3\n"
    )


def test_pr_title_is_commit_title() -> None:
    entry = make_entry(0, "  Add a  \n\nBody.\n")
    assert pr_title(entry) == "Add a"


def test_pr_title_strips_stack_info() -> None:
    # A message consisting only of a trailer has no title, unlike Commit.title.
    entry = make_entry(0, f"{INFO.line}\n")
    assert entry.commit.title == INFO.line
    assert pr_title(entry) == ""


def test_pr_title_with_trailer_after_title() -> None:
    entry = make_entry(0, f"Add a\n\n{INFO.line}\n")
    assert pr_title(entry) == "Add a"


# --------------------------------------------------------------------------- #
# BranchTemplate
# --------------------------------------------------------------------------- #
def test_branch_template_appends_id_when_missing() -> None:
    t = template("$USERNAME/stack")
    assert t.template == "$USERNAME/stack/$ID"
    assert t.expanded == "testbot/stack/$ID"


def test_branch_template_keeps_explicit_id() -> None:
    t = template("$USERNAME/stack/$ID")
    assert t.template == "$USERNAME/stack/$ID"
    assert t.expanded == "testbot/stack/$ID"


def test_branch_template_expands_username_and_branch() -> None:
    t = template("$USERNAME/$BRANCH/pr")
    assert t.expanded == "testbot/feature/pr/$ID"
    assert t.name(1) == "testbot/feature/pr/1"


def test_branch_template_expands_repeated_placeholders() -> None:
    t = template("$USERNAME-$USERNAME/$BRANCH-$BRANCH")
    assert t.expanded == "testbot-testbot/feature-feature/$ID"


def test_branch_template_glob() -> None:
    assert template().glob == "testbot/stack/*"


def test_branch_template_name() -> None:
    t = template()
    assert t.name(1) == "testbot/stack/1"
    assert t.name(42) == "testbot/stack/42"


@pytest.mark.parametrize(
    ("branch", "expected"),
    [
        ("testbot/stack/12", 12),
        ("testbot/stack/1", 1),
        ("testbot/stack/007", 7),
        ("testbot/stack/12/x", None),
        ("other/stack/12", None),
        ("testbot/stack/abc", None),
        ("testbot/stack/", None),
        ("testbot/stack", None),
        ("testbot/stack/12x", None),
        ("xtestbot/stack/12", None),
        ("", None),
    ],
)
def test_branch_template_parse_id(branch: str, expected: int | None) -> None:
    assert template().parse_id(branch) == expected


def test_branch_template_parse_id_round_trips_name() -> None:
    t = template()
    assert t.parse_id(t.name(123)) == 123


def test_branch_template_allocate_from_empty() -> None:
    assert template().allocate([], 3) == [
        "testbot/stack/1",
        "testbot/stack/2",
        "testbot/stack/3",
    ]


def test_branch_template_allocate_zero() -> None:
    assert template().allocate(["testbot/stack/5"], 0) == []


def test_branch_template_allocate_after_highest_taken_with_gaps() -> None:
    taken = {"testbot/stack/1", "testbot/stack/3", "testbot/stack/7"}
    assert template().allocate(taken, 2) == ["testbot/stack/8", "testbot/stack/9"]


def test_branch_template_allocate_ignores_other_users_and_foreign_names() -> None:
    taken = ["alice/stack/50", "main", "testbot/stack/2/wip", "testbot/stack/2"]
    assert template().allocate(taken, 1) == ["testbot/stack/3"]


def test_branch_template_allocate_only_foreign_names_starts_at_one() -> None:
    assert template().allocate(["alice/stack/50", "main"], 1) == ["testbot/stack/1"]


def test_branch_template_allocate_accepts_any_iterable() -> None:
    assert template().allocate(iter(["testbot/stack/4"]), 1) == ["testbot/stack/5"]


def test_branch_template_id_in_the_middle() -> None:
    t = template("stack-$ID-$USERNAME")
    assert t.expanded == "stack-$ID-testbot"
    assert t.glob == "stack-*-testbot"
    assert t.name(3) == "stack-3-testbot"
    assert t.parse_id("stack-3-testbot") == 3
    assert t.parse_id("stack-3-alice") is None
    assert t.parse_id("stack-3-testbot-x") is None
    assert t.parse_id("stack--testbot") is None
    assert t.allocate(["stack-3-testbot", "stack-9-alice"], 1) == ["stack-4-testbot"]


def test_branch_template_id_in_the_middle_with_prefix() -> None:
    t = template("$USERNAME/$ID/$BRANCH")
    assert t.name(2) == "testbot/2/feature"
    assert t.parse_id("testbot/2/feature") == 2
    assert t.parse_id("testbot/2/other") is None


def test_branch_template_regex_metacharacters_in_expansion_are_literal() -> None:
    t = BranchTemplate("$USERNAME.stack", username="a.b", current_branch="x")
    assert t.parse_id("a.b.stack/1") == 1
    assert t.parse_id("aXbXstack/1") is None


def test_branch_template_rejects_duplicate_id() -> None:
    with pytest.raises(PstackError, match=r"'\$ID' exactly once"):
        template("$USERNAME/$ID/$ID")


def test_branch_template_rejects_id_in_username_expansion() -> None:
    with pytest.raises(PstackError, match=r"'\$ID' exactly once"):
        BranchTemplate("$USERNAME/stack", username="$ID", current_branch="feature")


# --------------------------------------------------------------------------- #
# toc / description_from_existing_body
# --------------------------------------------------------------------------- #
def test_toc_constants() -> None:
    assert TOC_HEADER == "Stacked PRs:"
    assert TOC_CURRENT_MARKER == "__->__"
    assert CROSS_LINKS_DELIMITER == "--- --- ---"


def test_toc_newest_first_with_marker() -> None:
    assert toc([1, 2, 3], 2) == "Stacked PRs:\n * #3\n * __->__#2\n * #1"


def test_toc_marker_on_bottom_entry() -> None:
    assert toc([4, 5], 4) == "Stacked PRs:\n * #5\n * __->__#4"


def test_toc_marker_on_top_entry() -> None:
    assert toc([4, 5], 5) == "Stacked PRs:\n * __->__#5\n * #4"


def test_toc_single_entry() -> None:
    assert toc([9], 9) == "Stacked PRs:\n * __->__#9"


def test_toc_preserves_given_order_reversed_not_sorted() -> None:
    assert toc([10, 2, 7], 2) == "Stacked PRs:\n * #7\n * __->__#2\n * #10"


def test_description_from_existing_body_after_delimiter() -> None:
    body = "Stacked PRs:\n * #1\n\n--- --- ---\n\n### Add a\n\nHand written.\n"
    # The generated "### <title>" heading is dropped; pr_body regenerates it
    # from the current commit title.
    assert description_from_existing_body(body) == "Hand written."


def test_description_from_existing_body_drops_only_leading_heading() -> None:
    body = "toc\n--- --- ---\n\n### Old title\n\nText.\n\n### Notes\n\nMore.\n"
    assert description_from_existing_body(body) == "Text.\n\n### Notes\n\nMore."


def test_description_from_existing_body_heading_without_blank_line() -> None:
    body = "toc\n--- --- ---\n### Add a\nHand written.\n"
    assert description_from_existing_body(body) == "Hand written."


def test_description_from_existing_body_heading_only() -> None:
    assert description_from_existing_body("toc\n--- --- ---\n\n### Add a\n") == ""


def test_description_from_existing_body_keeps_heading_without_delimiter() -> None:
    # Without the delimiter nothing was generated by the tool, so a heading
    # the user wrote themselves is kept.
    assert description_from_existing_body("### Mine\n\nText.\n") == "### Mine\n\nText."


def test_description_from_existing_body_removes_tmp_draft_marker() -> None:
    body = f"toc\n--- --- ---\n\n### Add a\n\nText.\n\n{TMP_DRAFT_MARKER}\n"
    assert description_from_existing_body(body) == "Text."


def test_description_from_existing_body_removes_marker_without_delimiter() -> None:
    body = f"Plain body.\n\n{TMP_DRAFT_MARKER}\n"
    assert description_from_existing_body(body) == "Plain body."


def test_description_from_existing_body_marker_only_is_empty() -> None:
    assert description_from_existing_body(TMP_DRAFT_MARKER) == ""
    assert description_from_existing_body(f"toc\n--- --- ---\n{TMP_DRAFT_MARKER}") == ""


def test_description_from_existing_body_normalizes_crlf() -> None:
    body = "toc\r\n--- --- ---\r\n\r\n### Add a\r\n\r\nLine 1.\r\nLine 2.\r\n"
    assert description_from_existing_body(body) == "Line 1.\nLine 2."


def test_description_from_existing_body_without_delimiter_is_whole_body() -> None:
    assert description_from_existing_body("  Just a body.\n\n") == "Just a body."


def test_description_from_existing_body_empty() -> None:
    assert description_from_existing_body("") == ""


def test_description_from_existing_body_only_delimiter() -> None:
    assert description_from_existing_body("toc\n--- --- ---\n") == ""


def test_description_from_existing_body_splits_on_first_delimiter_only() -> None:
    body = "toc\n--- --- ---\nA\n--- --- ---\nB"
    assert description_from_existing_body(body) == "A\n--- --- ---\nB"


# --------------------------------------------------------------------------- #
# pr_body
# --------------------------------------------------------------------------- #
def test_pr_body_is_description_only() -> None:
    entries = linked_stack(["Add a\n\nBody of a.\n"])
    assert pr_body(entries[0]) == "Body of a."


def test_pr_body_without_description_is_empty() -> None:
    entries = linked_stack(["Add a\n"])
    assert pr_body(entries[0]) == ""


def test_pr_body_drops_trailer() -> None:
    entries = linked_stack([f"Add a\n\nBody of a.\n\n{INFO.line}\n"])
    body = pr_body(entries[0])
    assert body == "Body of a."
    assert "stack-info" not in body


def test_pr_body_works_without_pr() -> None:
    entry = make_entry(0, "Add a\n\nBody.\n", branch="testbot/stack/1")
    assert pr_body(entry) == "Body."


def test_pr_body_multi_paragraph_description() -> None:
    entries = linked_stack(["Add a\n\nPara 1.\n\nPara 2.\n", "Add b\n"])
    assert pr_body(entries[0]) == "Para 1.\n\nPara 2."


def test_pr_body_has_no_stack_list_heading_or_delimiter() -> None:
    # The stack list lives in a comment, so a squash merge that copies the PR
    # body into the commit message gets only the description.
    entries = linked_stack(["Add a\n\nBody.\n", "Add b\n", "Add c\n"])
    for entry in entries:
        body = pr_body(entry)
        assert TOC_HEADER not in body
        assert CROSS_LINKS_DELIMITER not in body
        assert "###" not in body


def test_pr_body_keep_body_keeps_existing_text() -> None:
    entries = linked_stack(["Add a\n\nFrom the commit.\n"])
    body = pr_body(entries[0], existing_body="Edited on GitHub.\n")
    assert body == "Edited on GitHub."


def test_pr_body_keep_body_empty_existing_body() -> None:
    entries = linked_stack(["Add a\n\nFrom the commit.\n"])
    assert pr_body(entries[0], existing_body="") == ""


def test_pr_body_keep_body_strips_legacy_stack_list_and_heading() -> None:
    entries = linked_stack(["Add a\n", "Add b\n\nFrom the commit.\n", "Add c\n"])
    existing = (
        "Stacked PRs:\n * #9\n * __->__#2\n\n--- --- ---\n\n### Add b\n\n"
        "Edited on GitHub.\n"
    )
    body = pr_body(entries[1], existing_body=existing)
    assert body == "Edited on GitHub."


def test_pr_body_keep_body_is_idempotent() -> None:
    entries = linked_stack(["Add a\n", "Add b\n\nBody of b.\n", "Add c\n"])
    generated = pr_body(entries[1])
    assert pr_body(entries[1], existing_body=generated) == generated


def test_pr_body_keep_body_drops_tmp_draft_marker() -> None:
    entries = linked_stack(["Add a\n", "Add b\n\nBody of b.\n"])
    generated = pr_body(entries[1])
    marked = generated.rstrip() + "\n\n" + TMP_DRAFT_MARKER
    assert has_tmp_draft_marker(marked)
    body = pr_body(entries[1], existing_body=marked)
    assert body == generated
    assert not has_tmp_draft_marker(body)


# --------------------------------------------------------------------------- #
# stack_comment_body / find_stack_comment
# --------------------------------------------------------------------------- #
def test_stack_comment_marker_is_an_html_comment() -> None:
    assert STACK_COMMENT_MARKER.startswith("<!--")
    assert STACK_COMMENT_MARKER.endswith("-->")


def test_stack_comment_body_is_marker_then_toc() -> None:
    entries = linked_stack(["Add a\n", "Add b\n\nBody of b.\n", "Add c\n"])
    assert stack_comment_body(entries[1], entries) == (
        f"{STACK_COMMENT_MARKER}\nStacked PRs:\n * #3\n * __->__#2\n * #1"
    )


def test_stack_comment_body_marks_each_entry_itself() -> None:
    entries = linked_stack(["Add a\n", "Add b\n", "Add c\n"])
    assert stack_comment_body(entries[0], entries).endswith(
        "Stacked PRs:\n * #3\n * #2\n * __->__#1"
    )
    assert stack_comment_body(entries[2], entries).endswith(
        "Stacked PRs:\n * __->__#3\n * #2\n * #1"
    )


def test_stack_comment_body_single_entry() -> None:
    entries = linked_stack(["Add a\n"])
    assert stack_comment_body(entries[0], entries).endswith("Stacked PRs:\n * __->__#1")


def test_stack_comment_body_uses_non_sequential_pr_numbers() -> None:
    entries = linked_stack(["Add a\n", "Add b\n"])
    assert entries[0].pr is not None
    assert entries[1].pr is not None
    entries[0].pr.number = 40
    entries[1].pr.number = 17
    assert stack_comment_body(entries[1], entries).endswith(
        "Stacked PRs:\n * __->__#17\n * #40"
    )


def test_stack_comment_body_raises_when_another_entry_has_no_pr() -> None:
    entries = linked_stack(["Add a\n", "Add b\n"])
    entries[1].pr = None
    with pytest.raises(PstackError, match="needs all PR numbers"):
        stack_comment_body(entries[0], entries)


def test_stack_comment_body_raises_when_entry_itself_has_no_pr() -> None:
    entries = linked_stack(["Add a\n", "Add b\n"])
    entries[0].pr = None
    with pytest.raises(PstackError, match="needs all PR numbers"):
        stack_comment_body(entries[1], entries)


def test_stack_comment_body_raises_when_entry_is_outside_entries() -> None:
    entries = linked_stack(["Add a\n", "Add b\n"])
    outsider = make_entry(5, "Add z\n", branch="testbot/stack/9")
    with pytest.raises(PstackError, match="needs all PR numbers"):
        stack_comment_body(outsider, entries)


def test_find_stack_comment_none_without_comments() -> None:
    assert find_stack_comment(make_pr(1, head="testbot/stack/1")) is None


def test_find_stack_comment_ignores_other_comments() -> None:
    pr = make_pr(1, head="testbot/stack/1")
    pr.comments = [
        Comment(id=5, body="LGTM"),
        Comment(id=6, body="Stacked PRs:\n * #1"),
    ]
    assert find_stack_comment(pr) is None


def test_find_stack_comment_finds_marked_comment() -> None:
    pr = make_pr(1, head="testbot/stack/1")
    ours = Comment(id=7, body=f"{STACK_COMMENT_MARKER}\nStacked PRs:\n * __->__#1")
    pr.comments = [Comment(id=5, body="LGTM"), ours, Comment(id=9, body="Thanks")]
    assert find_stack_comment(pr) is ours


def test_find_stack_comment_returns_first_marked_comment() -> None:
    pr = make_pr(1, head="testbot/stack/1")
    first = Comment(id=1, body=f"{STACK_COMMENT_MARKER}\nold")
    second = Comment(id=2, body=f"{STACK_COMMENT_MARKER}\nnew")
    pr.comments = [first, second]
    assert find_stack_comment(pr) is first


def test_find_stack_comment_tolerates_crlf_and_leading_whitespace() -> None:
    pr = make_pr(1, head="testbot/stack/1")
    ours = Comment(id=3, body=f"\r\n {STACK_COMMENT_MARKER}\r\nStacked PRs:\r\n")
    pr.comments = [ours]
    assert find_stack_comment(pr) is ours


def test_has_tmp_draft_marker() -> None:
    assert has_tmp_draft_marker(f"Body.\n\n{TMP_DRAFT_MARKER}") is True
    assert has_tmp_draft_marker("Body.\n") is False
    assert has_tmp_draft_marker("") is False
    assert TMP_DRAFT_MARKER.startswith("<!--")
    assert TMP_DRAFT_MARKER.endswith("-->")


# --------------------------------------------------------------------------- #
# check_linear
# --------------------------------------------------------------------------- #
def test_check_linear_accepts_empty() -> None:
    check_linear([], "the stack")


def test_check_linear_accepts_root_and_single_parent_commits() -> None:
    commits = [
        make_commit("Root\n", sha=sha_for(1), parents=()),
        make_commit("Add a\n", sha=sha_for(2), parents=(sha_for(1),)),
    ]
    check_linear(commits, "the stack")


def test_check_linear_rejects_merge_commits_listing_them() -> None:
    merge1 = make_commit(
        "Merge branch 'x'\n\nDetails.\n",
        sha="1234567890" * 4,
        parents=(sha_for(1), sha_for(2)),
    )
    plain = make_commit("Add a\n", sha=sha_for(3), parents=(merge1.sha,))
    merge2 = make_commit(
        "Merge branch 'y'\n",
        sha="abcdefabcd" * 4,
        parents=(plain.sha, sha_for(4), sha_for(5)),
    )
    with pytest.raises(PstackError) as excinfo:
        check_linear([merge1, plain, merge2], "the stack")
    text = str(excinfo.value)
    assert text == (
        "the stack must be linear, but contains merge commits:\n"
        "  12345678 Merge branch 'x'\n"
        "  abcdefab Merge branch 'y'"
    )


def test_check_linear_uses_what_in_message() -> None:
    merge = make_commit("Merge\n", sha=sha_for(9), parents=(sha_for(1), sha_for(2)))
    with pytest.raises(PstackError, match=r"^the history between 'x' and HEAD must be"):
        check_linear([merge], "the history between 'x' and HEAD")


# --------------------------------------------------------------------------- #
# check_titles
# --------------------------------------------------------------------------- #
def test_check_titles_accepts_empty() -> None:
    check_titles([])


def test_check_titles_accepts_titled_commits() -> None:
    check_titles(
        [
            make_commit("Add a\n", sha=sha_for(1)),
            make_commit("Add b\n\nBody.\n", sha=sha_for(2)),
            make_commit(f"Add c\n\n{INFO.line}\n", sha=sha_for(3)),
            make_commit("Add d", sha=sha_for(4)),
        ]
    )


def test_check_titles_rejects_empty_message_listing_short_shas() -> None:
    empty = make_commit("", sha="1234567890" * 4)
    blank = make_commit("\n\n  \n", sha="abcdefabcd" * 4)
    titled = make_commit("Add a\n", sha=sha_for(1))
    with pytest.raises(PstackError) as excinfo:
        check_titles([empty, titled, blank])
    text = str(excinfo.value)
    assert "have no subject line" in text
    assert "pull request title" in text
    assert text.endswith("\n  12345678\n  abcdefab")
    assert sha_for(1)[:8] not in text


def test_check_titles_rejects_trailer_only_message() -> None:
    # The stack-info line is not a title; the commit message is effectively empty.
    with pytest.raises(PstackError, match="have no subject line"):
        check_titles([make_commit(f"{INFO.line}\n", sha=sha_for(1))])


# --------------------------------------------------------------------------- #
# verify_existing_pr
# --------------------------------------------------------------------------- #
def entry_with_pr(
    *,
    state: str = "OPEN",
    pr_head: str = "testbot/stack/1",
    pr_url: str | None = None,
    info_branch: str = "testbot/stack/1",
    info_url: str = f"{PR_URL}/1",
) -> StackEntry:
    pr = make_pr(1, head=pr_head, state=state, url=pr_url)
    info = StackInfo(pr_url=info_url, branch=info_branch)
    return make_entry(0, "Add a\n", branch=info_branch, pr=pr, info=info)


def test_verify_existing_pr_open_and_consistent() -> None:
    verify_existing_pr(entry_with_pr())


def test_verify_existing_pr_skips_entries_without_pr() -> None:
    entry = make_entry(0, "Add a\n", info=StackInfo(f"{PR_URL}/1", "b/1"))
    verify_existing_pr(entry)


def test_verify_existing_pr_skips_entries_without_info() -> None:
    entry = make_entry(0, "Add a\n", pr=make_pr(1, head="x", state="CLOSED"))
    verify_existing_pr(entry)


@pytest.mark.parametrize("state", ["CLOSED", "MERGED"])
def test_verify_existing_pr_rejects_non_open(state: str) -> None:
    with pytest.raises(PstackError) as excinfo:
        verify_existing_pr(entry_with_pr(state=state))
    text = str(excinfo.value)
    assert text.startswith(
        f"commit {sha_for(1)[:8]} (Add a) references PR #1, which is {state}."
    )
    assert f"\n  {PR_URL}/1\n" in text
    assert "rebase onto the target branch" in text
    assert "remove the 'stack-info:' line" in text


def test_verify_existing_pr_state_checked_before_head() -> None:
    entry = entry_with_pr(state="MERGED", pr_head="somebody/else")
    with pytest.raises(PstackError, match="which is MERGED"):
        verify_existing_pr(entry)


def test_verify_existing_pr_rejects_head_mismatch() -> None:
    entry = entry_with_pr(pr_head="testbot/stack/2")
    with pytest.raises(PstackError) as excinfo:
        verify_existing_pr(entry)
    assert str(excinfo.value) == (
        f"commit {sha_for(1)[:8]} (Add a) says its branch is 'testbot/stack/1', "
        "but PR #1 has head branch 'testbot/stack/2'."
    )


def test_verify_existing_pr_rejects_url_mismatch() -> None:
    entry = entry_with_pr(pr_url=f"{PR_URL}/1", info_url=f"{PR_URL}/2")
    with pytest.raises(PstackError) as excinfo:
        verify_existing_pr(entry)
    assert str(excinfo.value) == (
        f"commit {sha_for(1)[:8]} (Add a) references {PR_URL}/2, "
        f"but GitHub resolved it to {PR_URL}/1."
    )


def test_verify_existing_pr_rejects_url_on_other_repo() -> None:
    entry = entry_with_pr(info_url="https://github.com/octo/gadgets/pull/1")
    with pytest.raises(PstackError, match="GitHub resolved it to"):
        verify_existing_pr(entry)


def test_verify_existing_pr_tolerates_trailing_slash_in_commit() -> None:
    verify_existing_pr(entry_with_pr(info_url=f"{PR_URL}/1/"))


def test_verify_existing_pr_tolerates_trailing_slash_from_github() -> None:
    verify_existing_pr(entry_with_pr(pr_url=f"{PR_URL}/1/"))


# --------------------------------------------------------------------------- #
# Stack
# --------------------------------------------------------------------------- #
def test_stack_len_and_iter() -> None:
    entries = linked_stack(["Add a\n", "Add b\n"])
    stack = Stack(entries)
    assert len(stack) == 2
    assert list(stack) == entries
    assert len(Stack()) == 0


def test_stack_index_of_branch() -> None:
    stack = Stack(linked_stack(["Add a\n", "Add b\n", "Add c\n"]))
    assert stack.index_of_branch("testbot/stack/1") == 0
    assert stack.index_of_branch("testbot/stack/3") == 2
    assert stack.index_of_branch("main") is None
    assert stack.index_of_branch("") is None


def test_mark_at_risk_nothing_when_order_unchanged() -> None:
    stack = Stack(linked_stack(["Add a\n", "Add b\n", "Add c\n"]))
    stack.mark_at_risk()
    assert [e.at_risk for e in stack] == [False, False, False]


def test_mark_at_risk_when_swapped_entries_base_points_above() -> None:
    # Previously: A on stack/1 (base main), B on stack/2 (base stack/1).
    # Now B is at the bottom and A on top; GitHub still has B's base = stack/1.
    b = make_entry(
        0, "Add b\n", branch="testbot/stack/2",
        pr=make_pr(2, head="testbot/stack/2", base="testbot/stack/1"),
    )  # fmt: skip
    a = make_entry(
        1, "Add a\n", branch="testbot/stack/1",
        pr=make_pr(1, head="testbot/stack/1", base="main"),
    )  # fmt: skip
    stack = Stack([b, a])
    stack.mark_at_risk()
    assert b.at_risk is True
    assert a.at_risk is False


def test_mark_at_risk_not_when_base_outside_stack() -> None:
    entries = linked_stack(["Add a\n", "Add b\n"])
    assert entries[0].pr is not None
    assert entries[1].pr is not None
    entries[0].pr.base = "main"
    entries[1].pr.base = "release/1.0"
    stack = Stack(entries)
    stack.mark_at_risk()
    assert [e.at_risk for e in stack] == [False, False]


def test_mark_at_risk_not_when_base_is_lower_entry() -> None:
    # A new commit was inserted between a and c; c's base on GitHub is still a.
    a = make_entry(
        0, "Add a\n", branch="testbot/stack/1",
        pr=make_pr(1, head="testbot/stack/1", base="main"),
    )  # fmt: skip
    b = make_entry(1, "Add b\n", branch="testbot/stack/3")
    c = make_entry(
        2, "Add c\n", branch="testbot/stack/2",
        pr=make_pr(2, head="testbot/stack/2", base="testbot/stack/1"),
    )  # fmt: skip
    stack = Stack([a, b, c])
    stack.mark_at_risk()
    assert [e.at_risk for e in stack] == [False, False, False]


def test_mark_at_risk_skips_entries_without_pr() -> None:
    entries = [
        make_entry(0, "Add a\n", branch="testbot/stack/1"),
        make_entry(1, "Add b\n", branch="testbot/stack/2"),
    ]
    stack = Stack(entries)
    stack.mark_at_risk()
    assert [e.at_risk for e in stack] == [False, False]


def test_mark_at_risk_not_when_base_is_own_branch() -> None:
    entry = make_entry(
        0, "Add a\n", branch="testbot/stack/1",
        pr=make_pr(1, head="testbot/stack/1", base="testbot/stack/1"),
    )  # fmt: skip
    stack = Stack([entry, make_entry(1, "Add b\n", branch="testbot/stack/2")])
    stack.mark_at_risk()
    assert entry.at_risk is False


def test_mark_at_risk_moved_to_bottom_of_three() -> None:
    # Old order a, b, c; new order c, a, b. Only c's base (stack/2, now index 2)
    # sits above it; a's base is main and b's base (stack/1) is now below it.
    c = make_entry(
        0, "Add c\n", branch="testbot/stack/3",
        pr=make_pr(3, head="testbot/stack/3", base="testbot/stack/2"),
    )  # fmt: skip
    a = make_entry(
        1, "Add a\n", branch="testbot/stack/1",
        pr=make_pr(1, head="testbot/stack/1", base="main"),
    )  # fmt: skip
    b = make_entry(
        2, "Add b\n", branch="testbot/stack/2",
        pr=make_pr(2, head="testbot/stack/2", base="testbot/stack/1"),
    )  # fmt: skip
    stack = Stack([c, a, b])
    stack.mark_at_risk()
    assert [e.at_risk for e in stack] == [True, False, False]


def test_mark_at_risk_clears_stale_flag() -> None:
    stack = Stack(linked_stack(["Add a\n", "Add b\n"]))
    stack.entries[1].at_risk = True
    stack.mark_at_risk()
    assert stack.entries[1].at_risk is False
