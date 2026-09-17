# Top of tree

# Version 0.2.1

* `export` is quiet by default: it prints only the result, one line per pull
  request marked `new`, `updated` or `unchanged`, plus the branches pushed.
  `-v` shows the plan and each step as it runs (what used to be the default);
  `-vv` also logs every `git` and `gh` command (what `-v` used to do).
* Installation instructions pin the release tag.

# Version 0.2.0

* Renamed the project to `pstack-pr`: the package is `pstack-pr`, the module
  `pstack_pr`, the command `pstack-pr`, the config file `.pstack-pr.cfg` and the
  environment variable `PSTACK_PR_CONFIG`. This avoids confusion with the
  upstream `stack-pr` project this repository was forked from.
* Rewrote `export` on top of git plumbing. Commits are read with `git cat-file`
  and rewritten with `git commit-tree`; only the message gains the
  `stack-info:` trailer, trees, authors, committers and dates are preserved.
  The local branch is moved once, in an atomic compare-and-swap
  `git update-ref --stdin` transaction, and pushes use
  `--atomic --force-with-lease`. The working tree and index are never touched,
  so uncommitted changes are fine and `--stash` is gone.
* Interrupting `export` at any point leaves the repository consistent.
  Re-running adopts branches that were already pushed (matched by commit sha)
  instead of allocating new ones, so no duplicate pull requests are created.
* `export --dry-run` (`-n`) prints the stack and the numbered plan of local and
  remote operations and exits. Without it the same plan is printed and then
  executed step by step.
* Pull requests are only edited when their title, body or base actually
  differs; a no-op re-export makes no write calls. PRs that GitHub would
  auto-close after reordering are temporarily retargeted (and marked draft
  meanwhile) instead of retargeting every PR on every run. The temporary
  draft state is recorded in the PR description so that an interrupted run
  is repaired by the next one; on plans without draft PRs the draft step is
  skipped with a warning.
* `--keep-body` no longer duplicates the generated `### <title>` heading, and
  `gh` is always called with a host-qualified `--repo`, so `GH_HOST` and
  GitHub Enterprise remotes work. `ssh://host:port/` remote URLs are parsed.
* Removed the `land`, `abandon`, `view` and `config` commands, the `submit`
  alias, `--draft-bitmask`, `--stash` and `--hyperlinks`. Merge through the
  GitHub UI, then rebase onto the target branch and export again; `view` is
  `export --dry-run`; edit `.pstack-pr.cfg` directly.
* Build and developer tooling moved to uv (`uv_build` backend, `uv run
  pytest`/`ruff`/`mypy`). Python 3.10 is the minimum; the runtime has no
  dependencies. Added an offline end-to-end test harness with a fake `gh` and
  a bare repository standing in for GitHub, plus opt-in integration tests
  (`--integration`).
* Removed the PyPI release workflow; the package is not published to PyPI.
  Install it from git with `uv tool install` or run it with `uvx --from`.

# Version 0.1.3

* Fix a bug with replacing $USERNAME in the branch name. (#44)

# Version 0.1.2

* Added config files - now defaults for the CL options can be customized with
  local config files (#32).
* Added a feature to customize branch names for stacked PRs (#33).
* Fixed a bug with branches not being deleted when a stack is abandoned (#27).
* Subcommands outputs is suppressed for less spammy look (#26).

# Version 0.1.1
