---
name: pstack-pr
description: Export, submit or update a stack of GitHub pull requests with pstack-pr, one PR per commit with each PR based on the previous one. Use when the user asks to submit, export or update a PR stack, wants stacked pull requests or one PR per commit, or mentions pstack-pr or stack-pr.
---

# pstack-pr

`pstack-pr export` is the only command. It maps every commit between
`origin/main` and `HEAD` to a branch `<user>/stack/N` and a pull request based
on the previous one, appends a `stack-info:` trailer to each commit message and
updates the existing PRs on later runs. It never touches the working tree or
the index; the only local write is one atomic move of the current branch.

Use whichever form is available:

- `pstack-pr export ...` (installed with `uv tool install`)
- `uvx --from git+https://github.com/georgekarpenkov/stack-pr@v0.2.1 pstack-pr export ...`

## Check prerequisites

1. `gh auth status` succeeds. Otherwise ask the user to run `gh auth login`.
2. No rebase is in progress (`git status`). Uncommitted changes are fine.
3. The branch is above `main`: `git log --oneline origin/main..HEAD` lists the
   commits that will become PRs. If the repository uses `master`, add
   `--target master`.
4. That range is linear: no merge commits.
5. One commit per reviewable change, with a good message. The first line
   becomes the PR title and the rest the PR description; fix messages with
   `git rebase -i` (`reword`) before exporting instead of editing PRs later.

## Export

ALWAYS dry-run first and show the user the plan:

```sh
pstack-pr export --dry-run
```

Summarize it: how many commits, which get a `new PR` and which update an
existing `#N`, and any `retarget` or `move` lines. Then run:

```sh
pstack-pr export
```

Report the final `Exported N pull requests (...)` block: one line per PR with
`new`/`updated`/`unchanged` and its URL, plus the `Branches pushed:` line.
Useful flags: `--draft`, `--reviewer alice,bob`, `--keep-body` (keep
hand-edited PR descriptions), `-B`/`-H` for a sub-range, `-v` to see the plan
and each step as it runs, `-vv` to also print every git and gh command.

## Read the output

```
Stack of 3 commits on feature (base: origin/main @ 1a2b3c4d)
   3  a082cd30  new PR  alice/stack/3  Add c        <- index  sha  PR  branch  title
   2  5e6f7a8b  #12     alice/stack/2  Add b        <- existing PR, updated in place
   1  9c0d1e2f  #11     alice/stack/1  Add a

Plan:
   1. push to origin: a082cd30 -> alice/stack/3 (new branch)
   2. create PR for a082cd30: alice/stack/3 -> alice/stack/2
   3. rewrite 1 commit message to embed stack-info (git commit-tree; ...)
   4. move feature from a082cd30 to the rewritten tip (git update-ref, ...)
   5. push the stack to origin (--atomic --force-with-lease): alice/stack/3
   6. update PR #11: body (cross-links)
   ...
Dry run: nothing was changed.
```

A real run prints only the result:

```
Exported 3 pull requests (1 new, 1 updated, 1 unchanged):
   3  #13  new        https://github.com/o/r/pull/13  Add c
   2  #12  updated    https://github.com/o/r/pull/12  Add b
   1  #11  unchanged  https://github.com/o/r/pull/11  Add a
Branches pushed: alice/stack/3 (new), alice/stack/2 (updated)
```

`Up to date: N pull requests, nothing to push.` means the run made no writes.
`(recovered)` after a title in the dry-run table means an interrupted run
already pushed that commit; it is reused, not duplicated.

## Update a stack

- Change one PR: `git commit --fixup <sha>` then
  `git rebase -i --autosquash origin/main`, or `edit` that commit in
  `git rebase -i origin/main`.
- Reorder, insert or drop commits: `git rebase -i origin/main`.
- Then dry-run and export again. Only the PRs that changed are edited.
- After a PR is merged on GitHub: `git pull --rebase origin main` drops the
  merged commit; export again to retarget the rest. Merging happens in the
  GitHub UI; there is no land command.

## Rules

- Never edit or delete `stack-info:` lines by hand. Deleting one detaches the
  commit from its PR and the next export creates a new PR; do that only when
  the user asks for it.
- Never push, rebase or delete `<user>/stack/N` branches manually; export owns
  them.
- Do not run export during a rebase.
- If export is interrupted or fails midway, re-run it; it resumes and reuses
  everything already pushed.
- Do not change commits between the dry run and the real run.

## Troubleshooting

| Message | Do this |
| --- | --- |
| `target branch 'origin/main' does not exist` (with a hint about `master`) | pass `--target master`, or set `target = master` under `[repo]` in `.pstack-pr.cfg` |
| `commit X references PR #N, which is MERGED` or `CLOSED` | merged: `git pull --rebase origin main` to drop the commit; closed on purpose: remove the `stack-info:` line to get a new PR |
| `the stack must be linear, but contains merge commits` | `git rebase origin/main` to linearize; do not merge `main` into the branch |
| `commits X and Y both claim branch '...'` | a commit was duplicated (cherry-pick, copy); remove the `stack-info:` line from one of them |
| `base '...' is not an ancestor of 'HEAD'` | the `-B` value must be a commit below `HEAD` |
| `base '...' is below the merge base of ...` | the `-B` value includes commits already on the target; use a commit at or above `origin/main` |
| `no local branch points at '...'` | check out a branch that contains the commits (or pass `-H <branch>`); export needs a local branch to record the trailers in |
| `these commits have no subject line` | `git rebase -i` and `reword` the listed commits; GitHub needs a PR title |
| `commit X references <url>, which is not in <repo>` | the commit was exported to another repository; pass `--remote <that remote>` or remove the `stack-info:` line |
| `could not mark PR #N as draft; continuing` (warning) | harmless; draft PRs are not available on this plan, reviewers may get notifications during the push |
| `the GitHub CLI ('gh') is not installed` | install it from https://cli.github.com/ and run `gh auth login` |
| `a rebase is in progress; finish or abort it first` | `git rebase --continue` or `git rebase --abort`, then export |
| `local branches were already updated; re-run 'pstack-pr export'` | re-run export; it finishes the remote side |
