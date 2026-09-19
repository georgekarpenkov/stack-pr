"""The stack model: commits, their branches, their pull requests.

Also the pure functions that decide what a commit message and a pull request
body should look like. Nothing in this module talks to git or GitHub.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field

from pstack_pr.errors import PstackError
from pstack_pr.git import Commit
from pstack_pr.github import Comment, PullRequest

# stack-info: PR: https://github.com/owner/repo/pull/30, branch: user/stack/7
STACK_INFO_RE = re.compile(
    r"^stack-info: PR: (?P<pr>\S+), branch: (?P<branch>\S+)[ \t]*$", re.MULTILINE
)

# The list of the stack's pull requests lives in a comment on each PR, not in
# the body: GitHub copies the body into the squash-merge commit message, and
# the list would be noise there. The comment is recognised by this first line.
STACK_COMMENT_MARKER = "<!-- pstack-pr: stack -->"
TOC_HEADER = "Stacked PRs:"
TOC_CURRENT_MARKER = "__->__"
# Older versions put the list at the top of the body, above this delimiter.
# It is still recognised so that such bodies are cleaned up on the next export.
CROSS_LINKS_DELIMITER = "--- --- ---"
# Appended to a PR body while the tool has temporarily converted the PR to a
# draft, so that an interrupted run can be repaired by the next one.
TMP_DRAFT_MARKER = "<!-- pstack-pr: temporarily a draft while the stack is pushed -->"
_GENERATED_HEADING_RE = re.compile(r"^### [^\n]*\n?")


@dataclass(frozen=True)
class StackInfo:
    """The metadata embedded in a commit message."""

    pr_url: str
    branch: str

    @property
    def line(self) -> str:
        return f"stack-info: PR: {self.pr_url}, branch: {self.branch}"


def parse_stack_info(message: str) -> StackInfo | None:
    """Return the stack-info from ``message``; the last one wins if repeated."""
    matches = list(STACK_INFO_RE.finditer(message))
    if not matches:
        return None
    m = matches[-1]
    return StackInfo(pr_url=m.group("pr"), branch=m.group("branch"))


def strip_stack_info(message: str) -> str:
    """Remove all stack-info lines and trailing blank lines from ``message``."""
    stripped = STACK_INFO_RE.sub("", message)
    return stripped.rstrip() + "\n" if stripped.strip() else ""


def add_stack_info(message: str, info: StackInfo) -> str:
    """``message`` with exactly one stack-info trailer paragraph."""
    body = strip_stack_info(message).rstrip("\n")
    return f"{body}\n\n{info.line}\n"


def title_and_description(message: str) -> tuple[str, str]:
    """Split a commit message into its first line and the rest (no stack-info)."""
    clean = strip_stack_info(message).strip()
    title, _, rest = clean.partition("\n")
    return title.strip(), rest.strip()


@dataclass
class StackEntry:
    """One commit of the stack and everything the tool knows about it."""

    index: int
    commit: Commit
    info: StackInfo | None = None  # what the commit message currently says
    branch: str = ""  # head branch of this entry's pull request
    base: str = ""  # base branch: previous entry's branch or the target
    pr: PullRequest | None = None  # the existing pull request, if any
    remote_sha: str | None = None  # where ``branch`` points on the remote
    adopted: bool = False  # branch recovered from an interrupted export
    at_risk: bool = False  # GitHub would auto-close the PR when pushed
    tmp_draft: bool = False  # PR was marked draft by us for the transient state
    new_sha: str | None = None  # the rewritten commit, once created
    will_rewrite: bool = False  # decided during planning
    branch_existed: bool = True  # was ``branch`` on the remote before this run
    # What this run did, for the final report.
    created: bool = False  # pull request created by this run
    pr_edited: bool = False  # pull request title/body/base/draft state changed
    branch_pushed: bool = False  # remote branch moved by this run

    @property
    def status(self) -> str:
        """``new``, ``updated`` or ``unchanged``: what this run did to the PR."""
        if self.created:
            return "new"
        if self.branch_pushed or self.pr_edited:
            return "updated"
        return "unchanged"

    @property
    def has_pr(self) -> bool:
        return self.pr is not None

    @property
    def pr_label(self) -> str:
        return f"#{self.pr.number}" if self.pr else "new PR"

    def desired_info(self) -> StackInfo:
        if self.pr is None:
            raise PstackError("internal error: pull request not created yet")
        return StackInfo(pr_url=self.pr.url, branch=self.branch)

    def desired_message(self) -> str:
        return add_stack_info(self.commit.message, self.desired_info())


# --------------------------------------------------------------------------- #
# Branch naming
# --------------------------------------------------------------------------- #
class BranchTemplate:
    """Expands ``$USERNAME``, ``$BRANCH`` and ``$ID`` in a branch name template."""

    def __init__(self, template: str, *, username: str, current_branch: str) -> None:
        if "$ID" not in template:
            template = f"{template}/$ID"
        expanded = template.replace("$USERNAME", username)
        expanded = expanded.replace("$BRANCH", current_branch)
        if expanded.count("$ID") != 1:
            raise PstackError("branch name template must contain '$ID' exactly once")
        self.template = template
        self.expanded = expanded
        prefix, suffix = expanded.split("$ID")
        self._regex = re.compile(re.escape(prefix) + r"(\d+)" + re.escape(suffix) + "$")

    @property
    def glob(self) -> str:
        """Pattern for ``for-each-ref``/``ls-remote`` matching all generated names."""
        return self.expanded.replace("$ID", "*")

    def name(self, branch_id: int) -> str:
        return self.expanded.replace("$ID", str(branch_id))

    def parse_id(self, branch: str) -> int | None:
        m = self._regex.match(branch)
        return int(m.group(1)) if m else None

    def allocate(self, taken: Iterable[str], count: int) -> list[str]:
        """``count`` fresh names, numbered after the highest name in ``taken``."""
        ids = [i for i in map(self.parse_id, taken) if i is not None]
        start = (max(ids) if ids else 0) + 1
        return [self.name(i) for i in range(start, start + count)]


# --------------------------------------------------------------------------- #
# Pull request contents
# --------------------------------------------------------------------------- #
def toc(numbers: Sequence[int], current: int) -> str:
    """The "Stacked PRs" list, newest first, with ``current`` marked."""
    lines = [TOC_HEADER]
    for number in reversed(numbers):
        marker = TOC_CURRENT_MARKER if number == current else ""
        lines.append(f" * {marker}#{number}")
    return "\n".join(lines)


def description_from_existing_body(body: str) -> str:
    """The hand-written part of a PR body.

    Everything the tool ever generated is dropped: the temporary-draft marker
    and, in bodies written by older versions, the cross-links before the
    delimiter and the ``### <title>`` heading right after it.
    """
    body = body.replace("\r\n", "\n").replace(TMP_DRAFT_MARKER, "")
    if CROSS_LINKS_DELIMITER not in body:
        return body.strip()
    description = body.split(CROSS_LINKS_DELIMITER, 1)[1].strip()
    return _GENERATED_HEADING_RE.sub("", description, count=1).strip()


def has_tmp_draft_marker(body: str) -> bool:
    return TMP_DRAFT_MARKER in body


def pr_body(entry: StackEntry, *, existing_body: str | None = None) -> str:
    """Body for ``entry``'s pull request: the commit message minus its title.

    With ``existing_body`` the description part of that body is kept instead
    (``--keep-body``); only what the tool itself generated is removed.
    """
    if existing_body is not None:
        return description_from_existing_body(existing_body)
    return title_and_description(entry.commit.message)[1]


def stack_comment_body(entry: StackEntry, entries: Sequence[StackEntry]) -> str:
    """The comment listing the stack's pull requests, as seen from ``entry``."""
    numbers = [e.pr.number for e in entries if e.pr is not None]
    if len(numbers) != len(entries) or entry.pr is None:
        raise PstackError("internal error: stack comment needs all PR numbers")
    return f"{STACK_COMMENT_MARKER}\n{toc(numbers, entry.pr.number)}"


def find_stack_comment(pr: PullRequest) -> Comment | None:
    """The comment on ``pr`` maintained by the tool, if it has one."""
    for comment in pr.comments:
        if comment.body.replace("\r\n", "\n").lstrip().startswith(STACK_COMMENT_MARKER):
            return comment
    return None


def pr_title(entry: StackEntry) -> str:
    return title_and_description(entry.commit.message)[0]


# --------------------------------------------------------------------------- #
# Consistency checks
# --------------------------------------------------------------------------- #
def check_linear(commits: Sequence[Commit], what: str) -> None:
    merges = [c for c in commits if c.is_merge]
    if merges:
        listing = "\n".join(f"  {c.short} {c.title}" for c in merges)
        raise PstackError(
            f"{what} must be linear, but contains merge commits:\n{listing}"
        )


def check_titles(commits: Sequence[Commit]) -> None:
    """Every commit needs a subject line; GitHub requires a PR title."""
    untitled = [c for c in commits if not title_and_description(c.message)[0]]
    if untitled:
        listing = "\n".join(f"  {c.short}" for c in untitled)
        raise PstackError(
            f"these commits have no subject line, which is needed as the pull "
            f"request title:\n{listing}"
        )


def verify_existing_pr(entry: StackEntry) -> None:
    """Make sure the PR referenced by a commit's stack-info is usable."""
    pr, info = entry.pr, entry.info
    if pr is None or info is None:
        return
    where = f"commit {entry.commit.short} ({entry.commit.title})"
    if pr.state != "OPEN":
        raise PstackError(
            f"{where} references PR #{pr.number}, which is {pr.state}.\n"
            f"  {pr.url}\n"
            "If the change was merged already, rebase onto the target branch to "
            "drop the commit. Otherwise remove the 'stack-info:' line from the "
            "commit message to have a new pull request created."
        )
    if pr.head != info.branch:
        raise PstackError(
            f"{where} says its branch is '{info.branch}', but PR #{pr.number} "
            f"has head branch '{pr.head}'."
        )
    if pr.url.rstrip("/") != info.pr_url.rstrip("/"):
        raise PstackError(
            f"{where} references {info.pr_url}, but GitHub resolved it to {pr.url}."
        )


@dataclass
class Stack:
    """All entries plus a few lookups over them."""

    entries: list[StackEntry] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self) -> Iterator[StackEntry]:
        return iter(self.entries)

    def index_of_branch(self, branch: str) -> int | None:
        for e in self.entries:
            if e.branch == branch:
                return e.index
        return None

    def mark_at_risk(self) -> None:
        """Flag PRs GitHub would auto-close once the stack branches are pushed.

        A pull request is closed automatically when its head branch contains no
        commits that are not already in its base branch. After the push that
        happens exactly when the PR's current base branch on GitHub belongs to
        an entry that now sits *above* the PR's own entry, i.e. when commits
        were reordered.
        """
        for e in self.entries:
            if e.pr is None:
                continue
            j = self.index_of_branch(e.pr.base)
            e.at_risk = j is not None and j > e.index
