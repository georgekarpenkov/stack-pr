"""Plan and execute ``pstack-pr export``.

Planning is read-only apart from ``git fetch``: it works out which commits are
in the stack, which branches and pull requests they map to, and produces an
ordered list of :class:`Step` objects. ``--dry-run`` prints those steps;
otherwise they are executed in order.

Execution order, and why:

1. Retarget pull requests GitHub would otherwise auto-close (reordered stacks).
2. Push the *original* commits of entries that need a new pull request; GitHub
   needs the head branch to exist before a PR can be created.
3. Create the missing pull requests. Now every entry has a PR URL.
4. Create rewritten commit objects whose messages carry the ``stack-info``
   trailer (``git commit-tree``; trees, authors and committers are preserved).
5. Move the local branch to the rewritten commits in one atomic, compare-and-
   swap ``git update-ref`` transaction. This is the only local write.
6. Push the rewritten commits to all stack branches (``--atomic``,
   ``--force-with-lease``).
7. Bring titles, bodies (cross-links) and base branches of the PRs up to date.

If the process is interrupted before step 5 the local repository is untouched;
re-running adopts the branches pushed in step 2 by their commit sha, so no
duplicate pull requests are created. If it is interrupted after step 5 the
commit messages already reference the right PRs and re-running simply finishes
the remote side.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from pstack_pr.errors import PstackError
from pstack_pr.git import Commit, Git, PushRef, RefUpdate
from pstack_pr.github import (
    GitHub,
    Repo,
    parse_remote_url,
    pr_number_from_url,
    repo_of_pr_url,
)
from pstack_pr.shell import CommandError
from pstack_pr.stack import (
    TMP_DRAFT_MARKER,
    BranchTemplate,
    Stack,
    StackEntry,
    check_linear,
    check_titles,
    has_tmp_draft_marker,
    parse_stack_info,
    pr_body,
    pr_title,
    verify_existing_pr,
)
from pstack_pr.ui import UI

REFLOG_MESSAGE = "pstack-pr export"


@dataclass(frozen=True)
class ExportOptions:
    remote: str = "origin"
    target: str = "main"
    base: str | None = None
    head: str = "HEAD"
    draft: bool = False
    reviewers: tuple[str, ...] = ()
    keep_body: bool = False
    branch_template: str = "$USERNAME/stack"


@dataclass
class RefRewrite:
    """A local ref that must move once the stack has been rewritten."""

    ref: str  # "refs/heads/<name>" or "HEAD" (detached)
    old: str  # current sha
    descendants: list[Commit]  # commits between the stack top and ``old``
    new: str | None = None

    @property
    def display(self) -> str:
        if self.ref == "HEAD":
            return "HEAD (detached)"
        return self.ref.removeprefix("refs/heads/")


@dataclass
class Context:
    git: Git
    gh: GitHub
    opts: ExportOptions
    stack: Stack
    ui: UI
    ref_rewrites: list[RefRewrite] = field(default_factory=list)
    local_refs_updated: bool = False  # set the moment update-ref has committed

    @property
    def entries(self) -> list[StackEntry]:
        return self.stack.entries

    def heads_ref(self, branch: str) -> str:
        return f"refs/heads/{branch}"


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").strip()


# --------------------------------------------------------------------------- #
# Steps
# --------------------------------------------------------------------------- #
class Step:
    """One unit of work. ``describe`` returning ``[]`` means nothing to do."""

    touches_local_refs = False

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx

    def describe(self) -> list[str]:
        raise NotImplementedError

    def run(self) -> None:
        raise NotImplementedError


class ResetBases(Step):
    """Point at-risk PRs at the target branch while their branches are pushed."""

    def __init__(self, ctx: Context, entries: Sequence[StackEntry]) -> None:
        super().__init__(ctx)
        self.entries = list(entries)

    def describe(self) -> list[str]:
        lines = []
        for e in self.entries:
            assert e.pr is not None  # noqa: S101 - established by the planner
            draft = "" if e.pr.is_draft else ", marking it draft meanwhile"
            lines.append(
                f"retarget PR #{e.pr.number} to '{self.ctx.opts.target}' during the "
                f"push{draft} (GitHub would auto-close it: its base branch "
                f"'{e.pr.base}' now sits above it in the stack)"
            )
        return lines

    def run(self) -> None:
        gh, target = self.ctx.gh, self.ctx.opts.target
        for e in self.entries:
            assert e.pr is not None  # noqa: S101
            body = None
            if not e.pr.is_draft:
                try:
                    gh.set_draft(e.pr.number, draft=True)
                except CommandError:
                    # Draft PRs are not available on every plan; the draft is
                    # only a courtesy to suppress notifications, so carry on.
                    self.ctx.ui.warn(
                        f"could not mark PR #{e.pr.number} as draft; continuing"
                    )
                else:
                    e.pr.is_draft = True
                    e.tmp_draft = True
                    # Record the transient state in the PR itself so that a
                    # re-run after an interruption knows to undo it.
                    body = e.pr.body.rstrip() + "\n\n" + TMP_DRAFT_MARKER
            gh.edit_pr(e.pr.number, base=target, body=body)
            e.pr.base = target
            e.pr_edited = True
            if body is not None:
                e.pr.body = body


class PushOriginals(Step):
    """Push the unmodified commits of entries that need a new pull request."""

    def __init__(self, ctx: Context, entries: Sequence[StackEntry]) -> None:
        super().__init__(ctx)
        self.entries = list(entries)

    def describe(self) -> list[str]:
        parts = []
        for e in self.entries:
            suffix = " (new branch)" if e.remote_sha is None else ""
            parts.append(f"{e.commit.short} -> {e.branch}{suffix}")
        return [f"push to {self.ctx.opts.remote}: " + ", ".join(parts)]

    def run(self) -> None:
        refs = [
            PushRef(
                dst=self.ctx.heads_ref(e.branch),
                src=e.commit.sha,
                expect=e.remote_sha or "",
            )
            for e in self.entries
        ]
        self.ctx.git.push(self.ctx.opts.remote, refs)
        for e in self.entries:
            e.remote_sha = e.commit.sha
            e.branch_pushed = True


class CreatePullRequest(Step):
    def __init__(self, ctx: Context, entry: StackEntry) -> None:
        super().__init__(ctx)
        self.entry = entry

    def describe(self) -> list[str]:
        e, opts = self.entry, self.ctx.opts
        extra = []
        if opts.draft:
            extra.append("draft")
        if opts.reviewers:
            extra.append("reviewers: " + ", ".join(opts.reviewers))
        suffix = f" ({'; '.join(extra)})" if extra else ""
        return [f"create PR for {e.commit.short}: {e.branch} -> {e.base}{suffix}"]

    def run(self) -> None:
        e, opts = self.entry, self.ctx.opts
        body = pr_body(e, self.ctx.entries, with_toc=False)
        e.pr = self.ctx.gh.create_pr(
            base=e.base,
            head=e.branch,
            title=pr_title(e),
            body=body,
            draft=opts.draft,
            reviewers=opts.reviewers,
        )
        e.created = True


class RewriteCommits(Step):
    """Create the commit objects carrying stack-info; no ref is touched yet."""

    def describe(self) -> list[str]:
        n = sum(1 for e in self.ctx.entries if e.will_rewrite)
        d = sum(len(rr.descendants) for rr in self.ctx.ref_rewrites)
        text = (
            f"rewrite {n} commit message{'s' if n != 1 else ''} to embed stack-info "
            "(git commit-tree; file contents, authors and dates are unchanged)"
        )
        if d:
            text += f", then re-parent the {d} commit{'s' if d != 1 else ''} above the stack"
        return [text]

    def run(self) -> None:
        git, entries = self.ctx.git, self.ctx.entries
        for i, e in enumerate(entries):
            parents = (entries[i - 1].new_sha,) if i > 0 else e.commit.parents
            assert all(parents)  # noqa: S101 - previous entry was rewritten first
            e.new_sha = git.rewrite(
                e.commit,
                parents=tuple(p for p in parents if p),
                message=e.desired_message(),
            )
        top = entries[-1].new_sha
        assert top is not None  # noqa: S101
        for rr in self.ctx.ref_rewrites:
            parent = top
            for c in rr.descendants:
                parent = git.rewrite(c, parents=(parent,), message=c.message)
            rr.new = parent


class UpdateLocalRefs(Step):
    touches_local_refs = True

    def describe(self) -> list[str]:
        return [
            f"move {rr.display} from {rr.old[:8]} to the rewritten tip "
            f"(git update-ref, only if it is still at {rr.old[:8]})"
            for rr in self.ctx.ref_rewrites
        ]

    def run(self) -> None:
        updates = []
        for rr in self.ctx.ref_rewrites:
            assert rr.new is not None  # noqa: S101 - RewriteCommits ran first
            if rr.new != rr.old:
                updates.append(RefUpdate(ref=rr.ref, new=rr.new, old=rr.old))
        self.ctx.git.update_refs(updates, message=REFLOG_MESSAGE)
        if updates:
            self.ctx.local_refs_updated = True


class PushRewritten(Step):
    def _entries(self) -> list[StackEntry]:
        """Entries whose branch must move. Predicted before the rewrite ran."""
        pending = []
        for e in self.ctx.entries:
            if e.new_sha is None:
                # Plan time: a rewrite always yields a new sha, so the branch
                # will have to move even if it currently equals the commit.
                changed = e.will_rewrite or e.commit.sha != e.remote_sha
            else:
                changed = e.new_sha != e.remote_sha
            if changed:
                pending.append(e)
        return pending

    def describe(self) -> list[str]:
        entries = self._entries()
        if not entries:
            return []
        names = ", ".join(e.branch for e in entries)
        remote = self.ctx.opts.remote
        return [f"push the stack to {remote} (--atomic --force-with-lease): {names}"]

    def run(self) -> None:
        refs = []
        for e in self._entries():
            src = e.new_sha or e.commit.sha
            refs.append(
                PushRef(
                    dst=self.ctx.heads_ref(e.branch), src=src, expect=e.remote_sha or ""
                )
            )
        self.ctx.git.push(self.ctx.opts.remote, refs)
        for e in self._entries():
            e.remote_sha = e.new_sha or e.commit.sha
            e.branch_pushed = True


class UpdatePullRequest(Step):
    """Bring title, body and base of one PR in line with the stack."""

    def __init__(self, ctx: Context, entry: StackEntry) -> None:
        super().__init__(ctx)
        self.entry = entry
        self.existed_before = entry.pr is not None

    def _all_numbers_known(self) -> bool:
        return all(e.pr is not None for e in self.ctx.entries)

    def _desired_body(self) -> str:
        e = self.entry
        keep = self.ctx.opts.keep_body and self.existed_before and e.pr is not None
        existing = e.pr.body if keep and e.pr is not None else None
        return pr_body(e, self.ctx.entries, existing_body=existing)

    def _changes(self) -> dict[str, str | None]:
        """Fields to edit, mapped to their new values (None: not computable yet)."""
        e = self.entry
        changes: dict[str, str | None] = {}
        if e.pr is None:
            # Not created yet (plan time). The body will need cross-links.
            if len(self.ctx.entries) > 1:
                changes["body"] = None
            return changes
        if pr_title(e) != e.pr.title:
            changes["title"] = pr_title(e)
        if e.base != e.pr.base:
            changes["base"] = e.base
        if self._all_numbers_known():
            body = self._desired_body()
            if _normalize(body) != _normalize(e.pr.body):
                changes["body"] = body
        elif len(self.ctx.entries) > 1:
            changes["body"] = None
        return changes

    def describe(self) -> list[str]:
        e = self.entry
        changes = self._changes()
        parts = []
        if "title" in changes:
            parts.append("title")
        if "base" in changes:
            parts.append(f"base -> {changes['base']}")
        if "body" in changes:
            parts.append("body (cross-links)")
        if e.tmp_draft or (e.at_risk and e.pr is not None and not e.pr.is_draft):
            parts.append("mark ready for review again")
        if not parts:
            return []
        label = f"PR #{e.pr.number}" if e.pr else f"the new PR for {e.commit.short}"
        return [f"update {label}: " + ", ".join(parts)]

    def run(self) -> None:
        e, gh = self.entry, self.ctx.gh
        assert e.pr is not None  # noqa: S101 - all PRs exist by now
        changes = self._changes()
        if changes:
            gh.edit_pr(
                e.pr.number,
                title=changes.get("title"),
                body=changes.get("body"),
                base=changes.get("base"),
            )
            e.pr_edited = True
            e.pr.title = changes.get("title") or e.pr.title
            e.pr.body = changes.get("body") or e.pr.body
            e.pr.base = changes.get("base") or e.pr.base
        if e.tmp_draft:
            if e.pr.is_draft:
                gh.set_draft(e.pr.number, draft=False)
            e.pr.is_draft = False
            e.tmp_draft = False
            e.pr_edited = True


# --------------------------------------------------------------------------- #
# Plan
# --------------------------------------------------------------------------- #
@dataclass
class Plan:
    ctx: Context
    repo: Repo
    base_sha: str
    head_sha: str
    steps: list[Step]
    warnings: list[str] = field(default_factory=list)

    @property
    def stack(self) -> Stack:
        return self.ctx.stack

    @property
    def local_refs_updated(self) -> bool:
        return self.ctx.local_refs_updated

    def active_steps(self) -> list[tuple[Step, list[str]]]:
        active = []
        for step in self.steps:
            lines = step.describe()
            if lines:
                active.append((step, lines))
        return active

    def print_stack(self, ui: UI) -> None:
        opts = self.ctx.opts
        entries = self.ctx.entries
        where = ", ".join(rr.display for rr in self.ctx.ref_rewrites) or opts.head
        ui.header(
            f"Stack of {len(entries)} commit{'s' if len(entries) != 1 else ''} on "
            f"{where} (base: {opts.remote}/{opts.target} @ {self.base_sha[:8]})"
        )
        width = max(len(e.branch) for e in entries)
        pr_width = max(len(e.pr_label) for e in entries)
        for e in reversed(entries):
            label = e.pr_label
            label = ui.cyan(label) if e.pr else ui.yellow(label)
            label += " " * (pr_width - len(e.pr_label))
            note = " (recovered)" if e.adopted else ""
            ui.info(
                f"  {e.index + 1:>2}  {ui.dim(e.commit.short)}  {label}  "
                f"{e.branch:<{width}}  {e.commit.title}{note}"
            )

    def print_steps(self, ui: UI) -> None:
        active = self.active_steps()
        ui.info()
        if not active:
            ui.header("Everything is up to date; nothing to do.")
            return
        ui.header("Plan:")
        n = 0
        for _, lines in active:
            for line in lines:
                n += 1
                ui.info(f"  {n:>2}. {line}")
        for warning in self.warnings:
            ui.warn(warning)

    def execute(self, ui: UI, *, show_progress: bool = False) -> None:
        total = sum(len(lines) for _, lines in self.active_steps())
        n = 0
        for step in self.steps:
            # Decide again now: earlier steps may have changed what is needed.
            lines = step.describe()
            if not lines:
                continue
            for line in lines:
                n += 1
                if show_progress:
                    ui.info(f"  {ui.dim(f'[{n}/{total}]')} {line}")
            try:
                step.run()
            except Exception:
                if not show_progress:
                    ui.warn("failed while trying to: " + "; ".join(lines))
                raise

    def print_result(self, ui: UI) -> None:
        """The outcome: one line per pull request, plus the branches pushed."""
        entries = self.ctx.entries
        counts = Counter(e.status for e in entries)
        n = len(entries)
        noun = f"pull request{'s' if n != 1 else ''}"
        if counts["unchanged"] == n:
            ui.header(f"Up to date: {n} {noun}, nothing to push.")
        else:
            summary = ", ".join(
                f"{counts[k]} {k}" for k in ("new", "updated", "unchanged") if counts[k]
            )
            ui.header(f"Exported {n} {noun} ({summary}):")
        colors = {"new": ui.green, "updated": ui.cyan, "unchanged": ui.dim}
        width = max(len(f"#{e.pr.number}") for e in entries if e.pr is not None)
        for e in reversed(entries):
            assert e.pr is not None  # noqa: S101
            label = f"#{e.pr.number}"
            label = ui.bold(label) + " " * (width - len(label))
            status = colors[e.status](e.status) + " " * (9 - len(e.status))
            ui.info(f"  {e.index + 1:>2}  {label}  {status}  {e.pr.url}  {pr_title(e)}")
        pushed = [e for e in entries if e.branch_pushed]
        if pushed:
            parts = [
                f"{e.branch} ({'new' if not e.branch_existed else 'updated'})"
                for e in pushed
            ]
            ui.info("Branches pushed: " + ", ".join(parts))


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #
GitHubFactory = Callable[[Repo], GitHub]


def _resolve_target(git: Git, opts: ExportOptions) -> str:
    target_ref = f"refs/remotes/{opts.remote}/{opts.target}"
    target_sha = git.try_rev_parse(target_ref)
    if target_sha is not None:
        return target_sha
    hint = ""
    if opts.target == "main" and git.try_rev_parse(
        f"refs/remotes/{opts.remote}/master"
    ):
        hint = (
            "\nThis repository seems to use 'master'; pass '--target master' "
            "or set 'target = master' in the [repo] section of .pstack-pr.cfg."
        )
    raise PstackError(
        f"target branch '{opts.remote}/{opts.target}' does not exist{hint}"
    )


def _read_stack(git: Git, opts: ExportOptions) -> tuple[str, str, Stack]:
    """Return (base sha, head sha, stack) for the requested range."""
    target_sha = _resolve_target(git, opts)
    head_sha = git.rev_parse(opts.head)
    base_sha = (
        git.rev_parse(opts.base) if opts.base else git.merge_base(head_sha, target_sha)
    )
    if not git.is_ancestor(base_sha, head_sha):
        raise PstackError(f"base '{opts.base}' is not an ancestor of '{opts.head}'")
    if opts.base:
        merge_base = git.merge_base(head_sha, target_sha)
        if not git.is_ancestor(merge_base, base_sha):
            raise PstackError(
                f"base '{opts.base}' is below the merge base of '{opts.head}' and "
                f"'{opts.remote}/{opts.target}' ({merge_base[:8]}); commits that "
                f"are already on '{opts.target}' cannot be exported"
            )
    commits = git.read_commits(git.rev_list(base_sha, head_sha))
    check_linear(commits, "the stack")
    check_titles(commits)
    entries = [
        StackEntry(index=i, commit=c, info=parse_stack_info(c.message))
        for i, c in enumerate(commits)
    ]
    return base_sha, head_sha, Stack(entries)


def _assign_branches(ctx: Context, template: BranchTemplate) -> None:
    """Give every entry a head branch and record where it points on the remote."""
    git, opts, stack = ctx.git, ctx.opts, ctx.stack
    prefix = f"refs/remotes/{opts.remote}/"
    remote_branches = {
        name.removeprefix(prefix): sha
        for name, sha in git.for_each_ref(prefix + template.glob).items()
    }

    for e in stack.entries:
        if e.info is not None:
            e.branch = e.info.branch

    # A previous run may have pushed a commit and been interrupted before the
    # local branch was updated. Adopt such branches instead of allocating new
    # ones, so no duplicate pull requests get created.
    by_sha = {sha: name for name, sha in remote_branches.items()}
    for e in stack.entries:
        if e.info is None and e.commit.sha in by_sha:
            e.branch = by_sha[e.commit.sha]
            e.adopted = True

    taken = set(remote_branches) | {e.branch for e in stack.entries if e.branch}
    fresh = template.allocate(taken, sum(1 for e in stack.entries if not e.branch))
    for e in stack.entries:
        if not e.branch:
            e.branch = fresh.pop(0)

    seen: dict[str, StackEntry] = {}
    for e in stack.entries:
        if e.branch in seen:
            other = seen[e.branch]
            raise PstackError(
                f"commits {other.commit.short} and {e.commit.short} both claim "
                f"branch '{e.branch}'; remove the stack-info line from one of them"
            )
        seen[e.branch] = e

    for e in stack.entries:
        if e.branch in remote_branches:
            e.remote_sha = remote_branches[e.branch]
        else:
            e.remote_sha = git.for_each_ref(prefix + e.branch).get(prefix + e.branch)
    for e in stack.entries:
        e.branch_existed = e.remote_sha is not None


def _pr_number(entry: StackEntry, repo: Repo) -> int:
    """The PR number in a commit's stack-info, checked to be in ``repo``."""
    assert entry.info is not None  # noqa: S101
    url = entry.info.pr_url
    where = f"commit {entry.commit.short} ({entry.commit.title})"
    pr_repo = repo_of_pr_url(url)
    number = pr_number_from_url(url)
    if pr_repo is None or number is None:
        raise PstackError(f"{where} has a malformed stack-info PR link: {url}")
    if (pr_repo.host, pr_repo.owner.lower(), pr_repo.name.lower()) != (
        repo.host,
        repo.owner.lower(),
        repo.name.lower(),
    ):
        raise PstackError(
            f"{where} references {url}, which is not in {repo.url}.\n"
            "Pass --remote for the remote of that repository, or remove the "
            "'stack-info:' line from the commit message to create a new PR here."
        )
    return number


def _load_pull_requests(ctx: Context) -> None:
    """Fetch existing PRs, set base branches, decide which commits change."""
    gh, opts, stack = ctx.gh, ctx.opts, ctx.stack
    for e in stack.entries:
        if e.info is not None:
            e.pr = gh.view_pr(_pr_number(e, gh.repo))
            verify_existing_pr(e)
            if has_tmp_draft_marker(e.pr.body):
                # A previous run was interrupted while the PR was a draft.
                e.tmp_draft = True
        elif e.adopted:
            e.pr = gh.find_open_pr(e.branch)

    for i, e in enumerate(stack.entries):
        e.base = stack.entries[i - 1].branch if i > 0 else opts.target
    stack.mark_at_risk()

    previous_rewritten = False
    for e in stack.entries:
        changed = e.pr is None or e.desired_message() != e.commit.message
        e.will_rewrite = changed or previous_rewritten
        previous_rewritten = e.will_rewrite


def _find_ref_rewrites(ctx: Context, head_sha: str, current_ref: str | None) -> None:
    """Work out which local refs must move to the rewritten commits.

    That is the ref named by ``--head`` if it is a local branch, and the
    checked out branch (or detached HEAD) when the stack is part of its history.
    Commits above the stack on that branch are re-parented, never recreated.
    """
    git, opts = ctx.git, ctx.opts
    head_ref = git.symbolic_full_name(opts.head)
    if head_ref is not None and (
        head_ref.startswith("refs/heads/") or head_ref == "HEAD"
    ):
        ctx.ref_rewrites.append(RefRewrite(ref=head_ref, old=head_sha, descendants=[]))

    cur_ref = current_ref or "HEAD"
    if any(rr.ref == cur_ref for rr in ctx.ref_rewrites):
        return
    cur_sha = git.rev_parse("HEAD")
    if cur_sha == head_sha:
        ctx.ref_rewrites.append(RefRewrite(ref=cur_ref, old=cur_sha, descendants=[]))
    elif git.is_ancestor(head_sha, cur_sha):
        above = git.read_commits(git.rev_list(head_sha, cur_sha))
        check_linear(above, f"the history between '{opts.head}' and HEAD")
        ctx.ref_rewrites.append(RefRewrite(ref=cur_ref, old=cur_sha, descendants=above))


def _build_steps(ctx: Context) -> list[Step]:
    entries = ctx.entries
    steps: list[Step] = []
    at_risk = [e for e in entries if e.at_risk]
    if at_risk:
        steps.append(ResetBases(ctx, at_risk))
    new_entries = [e for e in entries if e.pr is None]
    to_push = [e for e in new_entries if e.remote_sha != e.commit.sha]
    if to_push:
        steps.append(PushOriginals(ctx, to_push))
    steps += [CreatePullRequest(ctx, e) for e in new_entries]
    if any(e.will_rewrite for e in entries):
        steps.append(RewriteCommits(ctx))
        if ctx.ref_rewrites:
            steps.append(UpdateLocalRefs(ctx))
    steps.append(PushRewritten(ctx))
    steps += [UpdatePullRequest(ctx, e) for e in entries]
    return steps


def plan_export(
    git: Git,
    opts: ExportOptions,
    ui: UI,
    *,
    github_factory: GitHubFactory = GitHub,
    show_progress: bool = False,
) -> Plan:
    if git.rebase_in_progress():
        raise PstackError("a rebase is in progress; finish or abort it first")

    repo = parse_remote_url(git.remote_url(opts.remote))
    gh = github_factory(repo)

    if show_progress:
        ui.info(ui.dim(f"Fetching {opts.remote}..."))
    git.fetch(opts.remote)

    base_sha, head_sha, stack = _read_stack(git, opts)
    ctx = Context(git=git, gh=gh, opts=opts, stack=stack, ui=ui)
    plan = Plan(ctx=ctx, repo=repo, base_sha=base_sha, head_sha=head_sha, steps=[])
    if not stack.entries:
        return plan

    current_ref = git.current_branch_ref()
    current_branch = current_ref.removeprefix("refs/heads/") if current_ref else "HEAD"
    template = BranchTemplate(
        opts.branch_template, username=gh.username(), current_branch=current_branch
    )
    _assign_branches(ctx, template)
    _load_pull_requests(ctx)
    _find_ref_rewrites(ctx, head_sha, current_ref)

    if any(e.will_rewrite for e in stack.entries) and not ctx.ref_rewrites:
        raise PstackError(
            f"no local branch points at '{opts.head}' and HEAD does not contain "
            "it, so the stack-info trailers could not be recorded locally and "
            "re-running would create duplicate pull requests.\n"
            "Check out a branch that contains these commits (or pass -H <branch>) "
            "and try again."
        )
    plan.steps = _build_steps(ctx)
    return plan
