"""Command line interface: ``pstack-pr export``."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

from pstack_pr import __version__
from pstack_pr.config import Config, find_config_path, load_config
from pstack_pr.errors import PstackError
from pstack_pr.export import ExportOptions, plan_export
from pstack_pr.git import Git
from pstack_pr.github import check_gh_installed
from pstack_pr.ui import UI

EXPORT_DESCRIPTION = """\
Export the commits between the target branch and HEAD as a stack of pull
requests, one per commit. Each PR is based on the previous one; the first is
based on the target branch. Commit messages get a 'stack-info:' trailer that
links them to their PR, so running export again after amending, reordering or
adding commits updates the existing PRs.

The working tree and index are never touched: commits are rewritten with
'git commit-tree' and the current branch is moved with a single atomic
'git update-ref'. Uncommitted changes are fine. Interrupting the command at any
point leaves the repository consistent; re-run to finish.
"""


def build_parser(config: Config) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pstack-pr",
        description="Stacked pull requests for GitHub, one commit per PR.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    export = commands.add_parser(
        "export",
        help="create or update the stack of pull requests for the current commits",
        description=EXPORT_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    export.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="print what would be done and exit without changing anything",
    )
    export.add_argument(
        "-R",
        "--remote",
        default=config.remote,
        help=f"remote name (default: {config.remote})",
    )
    export.add_argument(
        "-T",
        "--target",
        default=config.target,
        help=f"branch on the remote the stack is based on (default: {config.target})",
    )
    export.add_argument(
        "-B",
        "--base",
        default=None,
        help="bottom of the stack, exclusive (default: merge base of HEAD and REMOTE/TARGET)",
    )
    export.add_argument(
        "-H",
        "--head",
        default="HEAD",
        help="top of the stack, inclusive (default: HEAD)",
    )
    export.add_argument(
        "-d",
        "--draft",
        action="store_true",
        default=config.draft,
        help="create new pull requests as drafts",
    )
    export.add_argument(
        "--reviewer",
        default=config.reviewer,
        help="comma-separated GitHub handles to request reviews from on new PRs",
    )
    export.add_argument(
        "--keep-body",
        action="store_true",
        default=config.keep_body,
        help="keep existing PR descriptions instead of regenerating them from the commit messages",
    )
    export.add_argument(
        "--branch-name-template",
        default=config.branch_name_template,
        help=(
            "template for stack branch names; $USERNAME, $BRANCH and $ID are "
            f"expanded (default: {config.branch_name_template})"
        ),
    )
    export.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=1 if config.verbose else 0,
        help="show the plan and progress while it runs; -vv also shows every git and gh command",
    )
    return parser


def run_export(git: Git, args: argparse.Namespace, ui: UI) -> int:
    check_gh_installed()
    reviewers = tuple(r.strip() for r in args.reviewer.split(",") if r.strip())
    opts = ExportOptions(
        remote=args.remote,
        target=args.target,
        base=args.base,
        head=args.head,
        draft=args.draft,
        reviewers=reviewers,
        keep_body=args.keep_body,
        branch_template=args.branch_name_template,
    )
    verbose = args.dry_run or args.verbose >= 1
    plan = plan_export(git, opts, ui, show_progress=verbose)
    if not plan.stack.entries:
        base = args.base or f"{opts.remote}/{opts.target}"
        ui.info(f"Nothing to export: no commits in {base}..{opts.head}.")
        return 0

    if verbose:
        plan.print_stack(ui)
        plan.print_steps(ui)
    if args.dry_run:
        ui.info()
        ui.info(ui.bold("Dry run") + ": nothing was changed.")
        return 0

    show_progress = verbose and bool(plan.active_steps())
    if show_progress:
        ui.info()
    try:
        plan.execute(ui, show_progress=show_progress)
    except (KeyboardInterrupt, PstackError):
        if show_progress:
            ui.info()
        if plan.local_refs_updated:
            ui.warn(
                "local branches were already updated; re-run 'pstack-pr export' "
                "to finish updating the remote branches and pull requests"
            )
        else:
            ui.warn(
                "local branches were not modified; re-run 'pstack-pr export' to "
                "resume (branches pushed so far are reused)"
            )
        raise
    if show_progress:
        ui.info()
    plan.print_result(ui)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ui = UI()
    git = Git()
    try:
        repo_root = git.root()
    except PstackError:
        repo_root = None

    # A broken config file must not get in the way of --help and --version, so
    # parse the command line first and complain afterwards.
    config_error: PstackError | None = None
    try:
        config = load_config(find_config_path(repo_root))
    except PstackError as e:
        config_error, config = e, Config()

    args = build_parser(config).parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose >= 2 else logging.WARNING,  # noqa: PLR2004
        format="%(message)s",
        stream=sys.stderr,
    )

    if config_error is not None:
        ui.error(str(config_error))
        return 1
    if repo_root is None:
        ui.error("not inside a git repository")
        return 1
    try:
        return run_export(git, args, ui)
    except KeyboardInterrupt:
        ui.error("interrupted")
        return 130
    except PstackError as e:
        ui.error(str(e))
        return 1
