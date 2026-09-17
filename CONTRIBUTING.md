# Contributing

## Setup

Install [uv](https://docs.astral.sh/uv/) and run `uv sync` in the checkout.
That creates `.venv` with the package and the dev dependencies (pytest, ruff,
mypy). Run the tool from the checkout with `uv run pstack-pr export ...`.

## Checks

```sh
uv run pytest           # offline tests; no network, no real GitHub
uv run ruff check .
uv run ruff format .
uv run mypy
```

pytest runs test files in parallel (pytest-xdist, `--dist loadfile`); pass
`-n 0` for a serial run with readable output.

CI runs the same four commands on every pull request (Python 3.10 and 3.13).
All of them must pass. mypy runs in strict mode, so annotate everything,
including tests.

The offline tests use the fixtures in [tests/conftest.py](tests/conftest.py):
a bare repository stands in for GitHub, a fake `gh`
([tests/fake_gh.py](tests/fake_gh.py)) keeps pull requests in a JSON file and
records every call, and a `post-receive` hook emulates GitHub auto-closing PRs
whose head branch has no commits beyond its base. Prefer small, focused tests
that assert on concrete values (shas, branch names, PR bodies, `gh` call
argument lists).

## Integration tests

Tests marked `integration` talk to a real GitHub repository and are skipped
unless you opt in. Do not point them at a repository you care about: they
create and close pull requests and force-push branches. Create your own private
scratch repository with a `main` branch, check that `gh auth status` is green,
then:

```sh
PSTACK_PR_TEST_REPO=you/scratch uv run pytest --integration
```

The default repository is `georgekarpenkov/pstack-pr-test`, which only the
maintainer can write to.

## Pull requests

- One logical change per commit. The first line of the commit message becomes
  the PR title and the rest the PR description, so explain what and why there.
- Submit stacks with pstack-pr itself: `uv run pstack-pr export --dry-run`,
  then `uv run pstack-pr export`. One PR per commit keeps reviews small.
- Add or adjust tests for behaviour changes, and note user-visible changes in
  `CHANGELOG.md` under "Top of tree".
- Keep the design constraints: no working tree or index access, local refs
  only via compare-and-swap `update-ref`, pushes `--atomic --force-with-lease`,
  and a re-export with nothing to do must make no write calls.
