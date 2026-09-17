"""Tests for :mod:`pstack_pr.github`.

The pure helpers (URL parsing, JSON decoding) are tested directly. The
:class:`GitHub` class is exercised against the fake ``gh`` from
``tests/fake_gh.py`` backed by the bare ``remote`` repository.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path

import pytest

from pstack_pr import github as github_module
from pstack_pr.errors import PstackError
from pstack_pr.github import (
    GRAPHQL_BATCH_SIZE,
    PR_GRAPHQL_FIELDS,
    PR_JSON_FIELDS,
    GitHub,
    PullRequest,
    Repo,
    check_gh_installed,
    parse_remote_url,
    pr_number_from_url,
    repo_of_pr_url,
)
from pstack_pr.shell import CommandError
from tests.conftest import FakeGitHub, Work

OCTO = Repo(host="github.com", owner="octo", name="widgets")
PR1_URL = "https://github.com/octo/widgets/pull/1"


# --------------------------------------------------------------------------- #
# parse_remote_url
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:owner/repo.git",
        "git@github.com:owner/repo",
        "ssh://git@github.com/owner/repo.git",
        "ssh://git@github.com/owner/repo",
        "https://github.com/owner/repo",
        "https://github.com/owner/repo.git",
        "https://user@github.com/owner/repo.git",
        "https://user:pass@github.com/owner/repo.git",
        "http://github.com/owner/repo",
        "https://github.com/owner/repo/",
        "https://github.com:443/owner/repo.git",
        "ssh://git@github.com:22/owner/repo.git",
        "ssh://github.com/owner/repo.git",
        "git://github.com/owner/repo.git",
        "git+ssh://git@github.com/owner/repo.git",
        "https://GitHub.COM/owner/repo",
        "GIT@GITHUB.COM:owner/repo.git",
        "git@ssh.github.com:owner/repo.git",
        "ssh://git@ssh.github.com:443/owner/repo.git",
    ],
)
def test_parse_remote_url_github_variants(url: str) -> None:
    repo = parse_remote_url(url)
    assert repo == Repo(host="github.com", owner="owner", name="repo")
    assert repo.slug == "github.com/owner/repo"
    assert repo.url == "https://github.com/owner/repo"


def test_parse_remote_url_https_enterprise_host() -> None:
    repo = parse_remote_url("https://ghe.example.com/o/r.git")
    assert repo == Repo(host="ghe.example.com", owner="o", name="r")
    assert repo.slug == "ghe.example.com/o/r"
    assert repo.url == "https://ghe.example.com/o/r"


def test_parse_remote_url_ssh_enterprise_host() -> None:
    repo = parse_remote_url("git@ghe.example.com:o/r.git")
    assert repo == Repo(host="ghe.example.com", owner="o", name="r")
    assert repo.slug == "ghe.example.com/o/r"
    assert repo.url == "https://ghe.example.com/o/r"


def test_parse_remote_url_lowercases_host_but_not_owner_or_name() -> None:
    repo = parse_remote_url("https://GHE.Example.COM/Org/Repo.git")
    assert repo == Repo(host="ghe.example.com", owner="Org", name="Repo")


def test_parse_remote_url_ssh_github_com_alias_maps_to_github_com() -> None:
    for url in (
        "git@ssh.github.com:o/r.git",
        "ssh://git@ssh.github.com:443/o/r.git",
        "ssh://git@SSH.GITHUB.COM/o/r",
    ):
        assert parse_remote_url(url) == Repo(host="github.com", owner="o", name="r")


def test_parse_remote_url_only_aliases_the_exact_ssh_host() -> None:
    repo = parse_remote_url("git@ssh.ghe.example.com:o/r.git")
    assert repo.host == "ssh.ghe.example.com"


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:owner/repo.git\n",
        "  git@github.com:owner/repo.git  ",
        "https://github.com/owner/repo.git\r\n",
        "\thttps://github.com/owner/repo\n\n",
    ],
)
def test_parse_remote_url_ignores_surrounding_whitespace(url: str) -> None:
    assert parse_remote_url(url) == Repo(host="github.com", owner="owner", name="repo")


def test_parse_remote_url_keeps_dots_and_dashes_in_names() -> None:
    repo = parse_remote_url("git@github.com:my-org.io/some.repo-name.git")
    assert repo == Repo(host="github.com", owner="my-org.io", name="some.repo-name")


def test_parse_remote_url_only_strips_one_git_suffix() -> None:
    assert parse_remote_url("https://github.com/o/r.git.git").name == "r.git"


def test_parse_remote_url_name_ending_in_git_word_is_kept() -> None:
    assert parse_remote_url("https://github.com/o/digit").name == "digit"


@pytest.mark.parametrize(
    "url",
    [
        "/tmp/x.git",
        "https://github.com/onlyowner",
        "https://github.com/onlyowner/",
        "https://github.com/",
        "https://github.com/a/b/c",
        "git@github.com:onlyowner.git",
        "git@github.com:a/b/c.git",
        "",
        "   \n",
        "github.com",
        "not a url at all",
    ],
)
def test_parse_remote_url_invalid_raises_pstack_error(url: str) -> None:
    with pytest.raises(PstackError, match="cannot parse GitHub repository"):
        parse_remote_url(url)


def test_parse_remote_url_error_mentions_the_url() -> None:
    with pytest.raises(PstackError, match=r"'/tmp/x\.git'"):
        parse_remote_url("/tmp/x.git")


@pytest.mark.parametrize(
    "url",
    [
        "../sibling/repo.git",
        "sub/dir/repo.git",
        "repo.git",
        "/abs/owner/repo.git",
        "/tmp/owner/repo",
        "file:///tmp/owner/repo.git",
    ],
)
def test_parse_remote_url_local_paths_are_rejected(url: str) -> None:
    with pytest.raises(PstackError, match="cannot parse GitHub repository"):
        parse_remote_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://github.com/owner/repo.git",
        "svn+ssh://github.com/owner/repo",
        "ssh://git@github.com:notaport/owner/repo.git",
    ],
)
def test_parse_remote_url_unknown_scheme_or_bad_port_is_rejected(url: str) -> None:
    with pytest.raises(PstackError, match="cannot parse GitHub repository"):
        parse_remote_url(url)


# --------------------------------------------------------------------------- #
# Repo
# --------------------------------------------------------------------------- #
def test_repo_slug_always_includes_host_even_for_github_com() -> None:
    # A bare owner/name would be resolved against GH_HOST, not this remote.
    assert OCTO.slug == "github.com/octo/widgets"
    assert OCTO.url == "https://github.com/octo/widgets"


def test_repo_slug_and_url_for_other_host() -> None:
    repo = Repo(host="ghe.example.com", owner="team", name="proj")
    assert repo.slug == "ghe.example.com/team/proj"
    assert repo.url == "https://ghe.example.com/team/proj"


def test_repo_is_frozen_and_hashable() -> None:
    assert hash(OCTO) == hash(Repo("github.com", "octo", "widgets"))
    with pytest.raises(AttributeError):
        OCTO.name = "other"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# pr_number_from_url
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/o/r/pull/12", 12),
        ("https://github.com/o/r/pull/12/", 12),
        ("https://github.com/o/r/pull/12\n", 12),
        ("  https://github.com/o/r/pull/12  ", 12),
        ("https://ghe.example.com/o/r/pull/7", 7),
        ("https://github.com/o/r/pull/0", 0),
        ("https://github.com/o/r/pull/123456789", 123456789),
        ("/pull/3", 3),
    ],
)
def test_pr_number_from_url_valid(url: str, expected: int) -> None:
    assert pr_number_from_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/o/r",
        "https://github.com/o/r/issues/12",
        "https://github.com/o/r/pull/12/files",
        "https://github.com/o/r/pull/abc",
        "https://github.com/o/r/pull/",
        "https://github.com/o/r/pulls/12",
        "12",
        "",
    ],
)
def test_pr_number_from_url_non_pr_url_returns_none(url: str) -> None:
    assert pr_number_from_url(url) is None


# --------------------------------------------------------------------------- #
# repo_of_pr_url
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        PR1_URL,
        PR1_URL + "/",
        f"  {PR1_URL}\n",
        "http://github.com/octo/widgets/pull/1",
        "https://GitHub.com/octo/widgets/pull/123456789",
    ],
)
def test_repo_of_pr_url_returns_the_repo(url: str) -> None:
    assert repo_of_pr_url(url) == OCTO


def test_repo_of_pr_url_keeps_owner_and_name_case() -> None:
    repo = repo_of_pr_url("https://GHE.example.com/Team/Proj/pull/7")
    assert repo == Repo(host="ghe.example.com", owner="Team", name="Proj")
    assert repo.slug == "ghe.example.com/Team/Proj"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/octo/widgets",
        "https://github.com/octo/widgets/pull/",
        "https://github.com/octo/widgets/pull/abc",
        "https://github.com/octo/widgets/pull/1/files",
        "https://github.com/octo/widgets/issues/1",
        "https://github.com/octo/pull/1",
        "https://github.com/a/b/c/pull/1",
        "github.com/octo/widgets/pull/1",
        "git@github.com:octo/widgets/pull/1",
        "/pull/3",
        "1",
        "",
    ],
)
def test_repo_of_pr_url_non_pr_url_returns_none(url: str) -> None:
    assert repo_of_pr_url(url) is None


# --------------------------------------------------------------------------- #
# PullRequest.from_json
# --------------------------------------------------------------------------- #
FULL_JSON: dict[str, object] = {
    "number": 30,
    "url": "https://github.com/octo/widgets/pull/30",
    "state": "OPEN",
    "isDraft": True,
    "title": "Add a",
    "body": "Some body",
    "baseRefName": "main",
    "headRefName": "testbot/stack/7",
}


def test_pull_request_from_json_full() -> None:
    pr = PullRequest.from_json(FULL_JSON)
    assert pr == PullRequest(
        number=30,
        url="https://github.com/octo/widgets/pull/30",
        state="OPEN",
        is_draft=True,
        title="Add a",
        body="Some body",
        base="main",
        head="testbot/stack/7",
    )


def test_pull_request_from_json_ignores_extra_fields() -> None:
    data = {**FULL_JSON, "reviewers": ["x"], "edits": 3}
    assert PullRequest.from_json(data).number == 30


def test_pull_request_from_json_body_null_becomes_empty_string() -> None:
    assert PullRequest.from_json({**FULL_JSON, "body": None}).body == ""


def test_pull_request_from_json_body_missing_becomes_empty_string() -> None:
    data = {k: v for k, v in FULL_JSON.items() if k != "body"}
    assert PullRequest.from_json(data).body == ""


def test_pull_request_from_json_number_as_string_is_converted() -> None:
    assert PullRequest.from_json({**FULL_JSON, "number": "31"}).number == 31


def test_pull_request_from_json_is_draft_false() -> None:
    assert PullRequest.from_json({**FULL_JSON, "isDraft": False}).is_draft is False


@pytest.mark.parametrize(
    "field",
    ["number", "url", "state", "isDraft", "title", "baseRefName", "headRefName"],
)
def test_pull_request_from_json_missing_field_raises_naming_it(field: str) -> None:
    data = {k: v for k, v in FULL_JSON.items() if k != field}
    with pytest.raises(PstackError, match="unexpected response from gh") as excinfo:
        PullRequest.from_json(data)
    assert field in str(excinfo.value)


def test_pull_request_from_json_empty_dict_raises() -> None:
    with pytest.raises(PstackError, match="missing field"):
        PullRequest.from_json({})


# --------------------------------------------------------------------------- #
# check_gh_installed
# --------------------------------------------------------------------------- #
def test_check_gh_installed_raises_when_gh_not_on_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    with pytest.raises(PstackError, match=r"GitHub CLI \('gh'\) is not installed"):
        check_gh_installed()


def test_check_gh_installed_error_points_to_install_docs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(PstackError, match=r"https://cli\.github\.com/"):
        check_gh_installed()


def test_check_gh_installed_ok_with_dummy_executable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text("#!/bin/sh\nexit 0\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    check_gh_installed()


def test_check_gh_installed_ignores_non_executable_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text("not executable\n")
    (bin_dir / "gh").chmod(0o644)
    monkeypatch.setenv("PATH", str(bin_dir))
    with pytest.raises(PstackError, match="not installed"):
        check_gh_installed()


# --------------------------------------------------------------------------- #
# GitHub class against the fake gh
# --------------------------------------------------------------------------- #
@pytest.fixture
def gh(fake_gh: FakeGitHub) -> GitHub:
    return GitHub(OCTO)


@pytest.fixture
def branches(work: Work, fake_gh: FakeGitHub) -> dict[str, str]:
    """Push two stack branches (one commit each, chained) to the remote.

    Returns branch name -> sha. ``testbot/stack/1`` holds 'Add a' on top of
    main; ``testbot/stack/2`` holds 'Add b' on top of that.
    """
    a = work.commit("a.txt", "Add a")
    b = work.commit("b.txt", "Add b")
    work.git(
        "push",
        "-q",
        "origin",
        f"{a}:refs/heads/testbot/stack/1",
        f"{b}:refs/heads/testbot/stack/2",
    )
    assert work.remote.branches() == {
        "main": work.head("origin/main"),
        "testbot/stack/1": a,
        "testbot/stack/2": b,
    }
    return {"testbot/stack/1": a, "testbot/stack/2": b}


def create_first(gh: GitHub, **overrides: object) -> PullRequest:
    kwargs: dict[str, object] = {
        "base": "main",
        "head": "testbot/stack/1",
        "title": "Add a",
        "body": "First body\n",
        "draft": False,
        "reviewers": (),
    }
    kwargs.update(overrides)
    return gh.create_pr(**kwargs)  # type: ignore[arg-type]


def test_username_returns_login_and_calls_gh_api(
    gh: GitHub, fake_gh: FakeGitHub
) -> None:
    assert gh.username() == "testbot"
    assert fake_gh.calls() == [
        ["api", "--hostname", "github.com", "user", "--jq", ".login"]
    ]


def test_username_passes_the_repo_host_to_gh_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[tuple[str, ...]] = []

    def fake(_self: GitHub, *args: str, **_kw: object) -> str:
        recorded.append(args)
        return "someone\n"

    monkeypatch.setattr(GitHub, "_gh", fake)
    ghe = GitHub(Repo(host="ghe.example.com", owner="team", name="proj"))
    assert ghe.username() == "someone"
    assert recorded == [
        ("api", "--hostname", "ghe.example.com", "user", "--jq", ".login")
    ]


def test_username_uses_configured_fake_user(
    gh: GitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_GH_USER", "someone-else")
    assert gh.username() == "someone-else"


def test_username_empty_login_raises(
    gh: GitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_GH_USER", "")
    with pytest.raises(PstackError, match="could not determine the GitHub user"):
        gh.username()


def test_create_pr_returns_number_url_base_head(
    gh: GitHub, branches: dict[str, str]
) -> None:
    pr = create_first(gh)
    assert pr.number == 1
    assert pr.url == PR1_URL
    assert pr.base == "main"
    assert pr.head == "testbot/stack/1"
    assert pr.title == "Add a"
    assert pr.body == "First body\n"
    assert pr.state == "OPEN"
    assert pr.is_draft is False


def test_create_pr_records_exact_gh_arguments(
    gh: GitHub, fake_gh: FakeGitHub, branches: dict[str, str]
) -> None:
    create_first(gh)
    assert fake_gh.calls("pr", "create") == [
        [
            "pr",
            "create",
            "--repo",
            "github.com/octo/widgets",
            "--base",
            "main",
            "--head",
            "testbot/stack/1",
            "--title",
            "Add a",
            "--body-file",
            "-",
        ]
    ]
    assert fake_gh.calls() == fake_gh.calls("pr", "create")


def test_create_pr_draft_and_reviewers_are_passed_to_gh(
    gh: GitHub, fake_gh: FakeGitHub, branches: dict[str, str]
) -> None:
    pr = create_first(gh, draft=True, reviewers=("alice", "bob"))
    assert pr.is_draft is True
    [call] = fake_gh.calls("pr", "create")
    assert call[-5:] == ["--draft", "--reviewer", "alice", "--reviewer", "bob"]
    assert call.count("--reviewer") == 2
    state = fake_gh.pr(1)
    assert state["isDraft"] is True
    assert state["reviewers"] == ["alice", "bob"]


def test_create_pr_body_is_sent_via_stdin(
    gh: GitHub, fake_gh: FakeGitHub, branches: dict[str, str]
) -> None:
    body = "Line one\n\nLine three with 'quotes' and $dollars\n"
    create_first(gh, body=body)
    state = fake_gh.pr(1)
    assert state["body"] == body
    assert state["title"] == "Add a"
    assert state["baseRefName"] == "main"
    assert state["headRefName"] == "testbot/stack/1"
    assert state["url"] == PR1_URL


def test_create_pr_numbers_increment(gh: GitHub, branches: dict[str, str]) -> None:
    first = create_first(gh)
    second = gh.create_pr(
        base="testbot/stack/1",
        head="testbot/stack/2",
        title="Add b",
        body="",
        draft=False,
    )
    assert (first.number, second.number) == (1, 2)
    assert second.url == "https://github.com/octo/widgets/pull/2"
    assert second.base == "testbot/stack/1"
    assert second.head == "testbot/stack/2"


def test_create_pr_missing_head_branch_raises_command_error(
    gh: GitHub, fake_gh: FakeGitHub, branches: dict[str, str]
) -> None:
    with pytest.raises(CommandError, match="Head ref must be a branch") as excinfo:
        create_first(gh, head="testbot/stack/99")
    assert excinfo.value.cmd[:2] == ["gh", "pr"]
    assert fake_gh.prs() == {}


def test_create_pr_missing_base_branch_raises_command_error(
    gh: GitHub, branches: dict[str, str]
) -> None:
    with pytest.raises(CommandError, match="Base ref must be a branch"):
        create_first(gh, base="no-such-base")


def test_create_pr_with_no_commits_beyond_base_raises(
    gh: GitHub, work: Work, branches: dict[str, str]
) -> None:
    # A branch pointing at main itself has nothing to merge.
    work.git("push", "-q", "origin", f"{work.head('origin/main')}:refs/heads/empty")
    with pytest.raises(CommandError, match="No commits between main and empty"):
        create_first(gh, head="empty")


def test_create_pr_without_url_in_gh_output_raises(
    gh: GitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(GitHub, "_gh", lambda *_a, **_k: "Creating pull request\n")
    with pytest.raises(PstackError, match="did not return a pull request URL"):
        create_first(gh)


def test_create_pr_with_empty_gh_output_raises(
    gh: GitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(GitHub, "_gh", lambda *_a, **_k: "")
    with pytest.raises(PstackError, match="did not return a pull request URL"):
        create_first(gh)


def test_create_pr_uses_last_output_line_as_url(
    gh: GitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = "Warning: 1 uncommitted change\n\nhttps://github.com/octo/widgets/pull/42\n"
    monkeypatch.setattr(GitHub, "_gh", lambda *_a, **_k: out)
    pr = create_first(gh, draft=True)
    assert pr.number == 42
    assert pr.url == "https://github.com/octo/widgets/pull/42"
    assert pr.is_draft is True


def test_view_pr_by_number(
    gh: GitHub, fake_gh: FakeGitHub, branches: dict[str, str]
) -> None:
    created = create_first(gh, draft=True)
    viewed = gh.view_pr(1)
    assert viewed == created
    assert fake_gh.calls("pr", "view") == [
        [
            "pr",
            "view",
            "1",
            "--repo",
            "github.com/octo/widgets",
            "--json",
            PR_JSON_FIELDS,
        ]
    ]


def test_view_pr_number_from_url_goes_through_number_only(
    gh: GitHub, fake_gh: FakeGitHub, branches: dict[str, str]
) -> None:
    # view_pr takes a number on purpose: given a URL, gh would use the
    # repository named in the URL and silently ignore --repo. Callers derive
    # the number (and check the repository) from the URL first.
    created = create_first(gh)
    number = pr_number_from_url(created.url)
    assert number == 1
    assert repo_of_pr_url(created.url) == gh.repo
    assert gh.view_pr(number) == created
    [call] = fake_gh.calls("pr", "view")
    assert call[2] == "1"
    assert PR1_URL not in call
    assert call[3:5] == ["--repo", "github.com/octo/widgets"]


def test_view_pr_reflects_state_changes(
    gh: GitHub, fake_gh: FakeGitHub, branches: dict[str, str]
) -> None:
    create_first(gh)
    fake_gh.close(1)
    fake_gh.set_body(1, "edited elsewhere")
    pr = gh.view_pr(1)
    assert pr.state == "CLOSED"
    assert pr.body == "edited elsewhere"


def test_view_pr_unknown_number_raises_command_error_with_gh_stderr(
    gh: GitHub, fake_gh: FakeGitHub
) -> None:
    with pytest.raises(CommandError) as excinfo:
        gh.view_pr(999)
    err = excinfo.value
    assert err.returncode == 1
    assert "Could not resolve to a PullRequest with the number of 999" in err.stderr
    assert "Could not resolve to a PullRequest with the number of 999" in str(err)
    assert "command failed with exit code 1:" in str(err)
    assert "gh pr view 999 --repo github.com/octo/widgets --json" in str(err)


# --------------------------------------------------------------------------- #
# GitHub.view_prs: the batched lookup through 'gh api graphql'
# --------------------------------------------------------------------------- #
GRAPHQL_PREFIX = ["api", "--hostname", "github.com", "graphql"]
REJECTED = "GitHub rejected the pull request lookup in https://github.com/octo/widgets:"


def seed_prs(fake_gh: FakeGitHub, numbers: Iterable[int]) -> dict[int, PullRequest]:
    """Write open pull requests straight into the fake gh state.

    One ``gh pr create`` is a subprocess; for dozens of PRs that is slow and
    needs as many branches. Only the lookup is exercised here, so the PRs are
    planted directly. Returns what :meth:`GitHub.view_prs` must hand back.
    """
    data = fake_gh.state()
    expected: dict[int, PullRequest] = {}
    for n in numbers:
        pr = PullRequest(
            number=n,
            url=f"https://github.com/octo/widgets/pull/{n}",
            state="OPEN" if n % 3 else "MERGED",
            is_draft=n % 2 == 0,
            title=f"Change {n}",
            body=f"Body {n}\nwith a second line\n",
            base="main" if n == 1 else f"testbot/stack/{n - 1}",
            head=f"testbot/stack/{n}",
        )
        data["prs"][str(n)] = {
            "number": pr.number,
            "url": pr.url,
            "state": pr.state,
            "isDraft": pr.is_draft,
            "title": pr.title,
            "body": pr.body,
            "baseRefName": pr.base,
            "headRefName": pr.head,
            "reviewers": [],
        }
        expected[n] = pr
        data["next_number"] = max(data["next_number"], n + 1)
    fake_gh.state_path.write_text(json.dumps(data))
    return expected


def graphql_calls(fake_gh: FakeGitHub) -> list[list[str]]:
    return fake_gh.calls(*GRAPHQL_PREFIX)


def requested_numbers(call: list[str]) -> list[int]:
    """The PR numbers one 'gh api graphql' call asks for, in query order."""
    assert call[:4] == GRAPHQL_PREFIX
    assert call[4] == "-f"
    assert call[5].startswith("query=")
    assert call[6:] == ["-f", "owner=octo", "-f", "name=widgets"]
    query = call[5][len("query=") :]
    pairs = re.findall(r"pr(\d+): pullRequest\(number: (\d+)\)", query)
    assert pairs, query
    assert all(alias == number for alias, number in pairs)
    return [int(number) for _, number in pairs]


@pytest.fixture
def three_prs(gh: GitHub, work: Work, branches: dict[str, str]) -> list[PullRequest]:
    """Three chained PRs created through the fake ``gh pr create``."""
    c = work.commit("c.txt", "Add c")
    work.git("push", "-q", "origin", f"{c}:refs/heads/testbot/stack/3")
    return [
        create_first(gh, draft=True, reviewers=("alice",)),
        gh.create_pr(
            base="testbot/stack/1",
            head="testbot/stack/2",
            title="Add b",
            body="Second body with 'quotes', $dollars and\n\nblank lines\n",
            draft=False,
        ),
        gh.create_pr(
            base="testbot/stack/2",
            head="testbot/stack/3",
            title="Add c",
            body="",
            draft=False,
        ),
    ]


def test_view_prs_empty_returns_empty_dict_without_calling_gh(
    gh: GitHub, fake_gh: FakeGitHub
) -> None:
    assert gh.view_prs([]) == {}
    assert fake_gh.calls() == []


def test_view_prs_three_numbers_make_exactly_one_graphql_call(
    gh: GitHub, fake_gh: FakeGitHub, three_prs: list[PullRequest]
) -> None:
    before = len(fake_gh.calls())
    prs = gh.view_prs([1, 2, 3])
    new_calls = fake_gh.calls()[before:]
    assert len(new_calls) == 1
    [call] = new_calls
    assert call[:4] == GRAPHQL_PREFIX
    assert call[6:] == ["-f", "owner=octo", "-f", "name=widgets"]
    query = call[5]
    assert query.startswith("query=query($owner: String!, $name: String!) {")
    assert "repository(owner: $owner, name: $name)" in query
    for n in (1, 2, 3):
        assert f"pr{n}: pullRequest(number: {n}) {{ {PR_GRAPHQL_FIELDS} }}" in query
    assert requested_numbers(call) == [1, 2, 3]
    # The repository is passed as GraphQL variables, never inlined.
    assert "octo" not in query
    assert "widgets" not in query
    assert set(prs) == {1, 2, 3}


def test_view_prs_never_uses_gh_pr_view(
    gh: GitHub, fake_gh: FakeGitHub, three_prs: list[PullRequest]
) -> None:
    gh.view_prs([1, 2, 3])
    assert fake_gh.calls("pr", "view") == []
    assert fake_gh.calls("api", "--hostname", "github.com", "user") == []
    assert len(graphql_calls(fake_gh)) == 1


def test_view_prs_returns_pull_requests_keyed_by_number(
    gh: GitHub, three_prs: list[PullRequest]
) -> None:
    prs = gh.view_prs([1, 2, 3])
    assert set(prs) == {1, 2, 3}
    for n, pr in prs.items():
        assert isinstance(pr, PullRequest)
        assert pr.number == n
        assert pr.url == f"https://github.com/octo/widgets/pull/{n}"
    assert prs[1] == three_prs[0]
    assert prs[2] == three_prs[1]
    assert prs[3] == three_prs[2]


def test_view_prs_matches_view_pr_field_for_field(
    gh: GitHub, three_prs: list[PullRequest]
) -> None:
    prs = gh.view_prs([3, 1, 2])
    for n in (1, 2, 3):
        assert prs[n] == gh.view_pr(n)
    assert prs[1].is_draft is True
    assert prs[1].title == "Add a"
    assert prs[1].body == "First body\n"
    assert prs[1].base == "main"
    assert prs[1].head == "testbot/stack/1"
    assert prs[2].is_draft is False
    assert prs[2].body == "Second body with 'quotes', $dollars and\n\nblank lines\n"
    assert prs[2].base == "testbot/stack/1"
    assert prs[3].body == ""
    assert prs[3].base == "testbot/stack/2"
    assert prs[3].head == "testbot/stack/3"
    assert all(pr.state == "OPEN" for pr in prs.values())


def test_view_prs_accepts_any_order_and_a_subset(
    gh: GitHub, fake_gh: FakeGitHub, three_prs: list[PullRequest]
) -> None:
    prs = gh.view_prs([3, 1])
    assert set(prs) == {1, 3}
    assert prs[1] == three_prs[0]
    assert prs[3] == three_prs[2]
    [call] = graphql_calls(fake_gh)
    assert requested_numbers(call) == [3, 1]


def test_view_prs_single_number_matches_view_pr(
    gh: GitHub, three_prs: list[PullRequest]
) -> None:
    assert gh.view_prs([2]) == {2: gh.view_pr(2)}


def test_view_prs_returns_fresh_objects_on_every_call(
    gh: GitHub, three_prs: list[PullRequest]
) -> None:
    first = gh.view_prs([1, 2])
    second = gh.view_prs([1, 2])
    assert first == second
    assert first[1] is not second[1]
    assert first[1] is not first[2]
    first[1].body = "mutated locally"
    assert second[1].body == "First body\n"
    assert gh.view_prs([1])[1].body == "First body\n"


def test_view_prs_reflects_state_changes(
    gh: GitHub, fake_gh: FakeGitHub, three_prs: list[PullRequest]
) -> None:
    fake_gh.close(1)
    fake_gh.set_body(2, "edited elsewhere")
    prs = gh.view_prs([1, 2, 3])
    assert prs[1].state == "CLOSED"
    assert prs[2].body == "edited elsewhere"
    assert prs[3] == three_prs[2]


def test_view_prs_sees_prs_planted_in_the_fake_state(
    gh: GitHub, fake_gh: FakeGitHub
) -> None:
    # Sanity check for the seeding helper the batching tests rely on.
    expected = seed_prs(fake_gh, [1, 2, 3])
    assert gh.view_prs([1, 2, 3]) == expected
    assert gh.view_pr(2) == expected[2]
    assert expected[2].is_draft is True
    assert expected[3].state == "MERGED"


def test_view_prs_batch_size_is_fifty() -> None:
    assert GRAPHQL_BATCH_SIZE == 50


def test_view_prs_sixty_numbers_use_two_batches_of_fifty(
    gh: GitHub, fake_gh: FakeGitHub
) -> None:
    expected = seed_prs(fake_gh, range(1, 61))
    prs = gh.view_prs(list(range(1, 61)))
    calls = graphql_calls(fake_gh)
    assert len(calls) == 2
    assert fake_gh.calls() == calls
    assert requested_numbers(calls[0]) == list(range(1, 51))
    assert requested_numbers(calls[1]) == list(range(51, 61))
    assert prs == expected
    assert list(prs) == list(range(1, 61))
    assert prs[50] == gh.view_pr(50)
    assert prs[51] == gh.view_pr(51)
    assert prs[60] == gh.view_pr(60)


def test_view_prs_fifty_numbers_fit_in_one_batch(
    gh: GitHub, fake_gh: FakeGitHub
) -> None:
    expected = seed_prs(fake_gh, range(1, 51))
    assert gh.view_prs(list(range(1, 51))) == expected
    calls = graphql_calls(fake_gh)
    assert len(calls) == 1
    assert requested_numbers(calls[0]) == list(range(1, 51))


def test_view_prs_fifty_one_numbers_need_a_second_batch(
    gh: GitHub, fake_gh: FakeGitHub
) -> None:
    expected = seed_prs(fake_gh, range(1, 52))
    assert gh.view_prs(list(range(1, 52))) == expected
    calls = graphql_calls(fake_gh)
    assert [requested_numbers(c) for c in calls] == [list(range(1, 51)), [51]]


def test_view_prs_batches_follow_the_configured_size(
    gh: GitHub, fake_gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github_module, "GRAPHQL_BATCH_SIZE", 2)
    expected = seed_prs(fake_gh, [1, 2, 3, 4, 5])
    prs = gh.view_prs([1, 2, 3, 4, 5])
    calls = graphql_calls(fake_gh)
    assert [requested_numbers(c) for c in calls] == [[1, 2], [3, 4], [5]]
    assert prs == expected
    assert set(prs) == {1, 2, 3, 4, 5}


def test_view_prs_batches_keep_the_callers_order(
    gh: GitHub, fake_gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github_module, "GRAPHQL_BATCH_SIZE", 2)
    expected = seed_prs(fake_gh, [7, 9, 12])
    prs = gh.view_prs([12, 7, 9])
    assert [requested_numbers(c) for c in graphql_calls(fake_gh)] == [[12, 7], [9]]
    assert prs == expected


def test_view_prs_missing_number_raises_pstack_error_with_gh_message(
    gh: GitHub, fake_gh: FakeGitHub
) -> None:
    with pytest.raises(PstackError) as excinfo:
        gh.view_prs([999])
    err = excinfo.value
    assert not isinstance(err, CommandError)
    assert isinstance(err.__cause__, CommandError)
    assert err.__cause__.returncode == 1
    message = str(err)
    assert message.startswith(REJECTED)
    assert "Could not resolve to a PullRequest with the number of 999" in message
    assert message.splitlines() == [
        REJECTED,
        "  Could not resolve to a PullRequest with the number of 999.",
    ]
    assert len(graphql_calls(fake_gh)) == 1


def test_view_prs_missing_number_next_to_existing_ones_raises(
    gh: GitHub, fake_gh: FakeGitHub, three_prs: list[PullRequest]
) -> None:
    with pytest.raises(PstackError) as excinfo:
        gh.view_prs([1, 404, 2])
    message = str(excinfo.value)
    assert "Could not resolve to a PullRequest with the number of 404" in message
    assert "number of 1" not in message
    assert "number of 2" not in message
    [call] = graphql_calls(fake_gh)
    assert requested_numbers(call) == [1, 404, 2]


def test_view_prs_lists_every_missing_number(
    gh: GitHub, fake_gh: FakeGitHub, three_prs: list[PullRequest]
) -> None:
    with pytest.raises(PstackError) as excinfo:
        gh.view_prs([1, 500, 600])
    lines = str(excinfo.value).splitlines()
    assert lines[0] == REJECTED
    assert lines[1:] == [
        "  Could not resolve to a PullRequest with the number of 500.",
        "  Could not resolve to a PullRequest with the number of 600.",
    ]


def test_view_prs_missing_number_in_a_later_batch_still_raises(
    gh: GitHub, fake_gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github_module, "GRAPHQL_BATCH_SIZE", 2)
    seed_prs(fake_gh, [1, 2, 3])
    with pytest.raises(PstackError, match="number of 999"):
        gh.view_prs([1, 2, 3, 999])
    # The first batch succeeded; the failing one was the second.
    assert [requested_numbers(c) for c in graphql_calls(fake_gh)] == [[1, 2], [3, 999]]


def test_view_prs_null_node_without_errors_raises_does_not_exist(
    gh: GitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    # GitHub returns null for a node it cannot resolve; a payload without an
    # "errors" list still must not be mistaken for a found pull request.
    payload = {"data": {"repository": {"pr7": None}}}
    monkeypatch.setattr(GitHub, "_gh", lambda *_a, **_k: json.dumps(payload))
    with pytest.raises(PstackError) as excinfo:
        gh.view_prs([7])
    assert str(excinfo.value) == (
        "pull request #7 does not exist in https://github.com/octo/widgets"
    )


def test_view_prs_node_missing_from_payload_raises_does_not_exist(
    gh: GitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {"data": {"repository": {"pr1": FULL_JSON}}}
    monkeypatch.setattr(GitHub, "_gh", lambda *_a, **_k: json.dumps(payload))
    with pytest.raises(PstackError, match=r"pull request #2 does not exist"):
        gh.view_prs([1, 2])


@pytest.mark.parametrize(
    "output",
    [
        "",
        "{}",
        json.dumps({"data": None}),
        json.dumps({"data": {}}),
        json.dumps({"data": {"repository": None}}),
    ],
)
def test_view_prs_missing_repository_raises(
    gh: GitHub, monkeypatch: pytest.MonkeyPatch, output: str
) -> None:
    monkeypatch.setattr(GitHub, "_gh", lambda *_a, **_k: output)
    with pytest.raises(PstackError) as excinfo:
        gh.view_prs([1])
    assert str(excinfo.value) == (
        "repository github.com/octo/widgets was not found on GitHub"
    )


def test_view_prs_node_with_missing_field_raises_from_json_error(
    gh: GitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = {k: v for k, v in FULL_JSON.items() if k != "headRefName"}
    payload = {"data": {"repository": {"pr30": node}}}
    monkeypatch.setattr(GitHub, "_gh", lambda *_a, **_k: json.dumps(payload))
    with pytest.raises(PstackError, match="unexpected response from gh") as excinfo:
        gh.view_prs([30])
    assert "headRefName" in str(excinfo.value)


def test_view_prs_graphql_errors_are_joined_into_one_message(
    gh: GitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    stdout = json.dumps(
        {
            "data": {"repository": {"pr1": None, "pr2": None}},
            "errors": [
                {"type": "NOT_FOUND", "message": "first problem"},
                {"type": "NOT_FOUND", "message": "second problem"},
                {"type": "NOT_FOUND"},
                "not a dict",
                {"message": ""},
            ],
        }
    )

    def fail(_self: GitHub, *args: str, **_kw: object) -> str:
        raise CommandError(["gh", *args], 1, stdout, "gh: first problem")

    monkeypatch.setattr(GitHub, "_gh", fail)
    with pytest.raises(PstackError) as excinfo:
        gh.view_prs([1, 2])
    assert str(excinfo.value) == f"{REJECTED}\n  first problem\n  second problem"


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "not json at all",
        "{}",
        json.dumps({"errors": []}),
        json.dumps({"errors": None}),
    ],
)
def test_view_prs_gh_failure_without_graphql_errors_keeps_command_error_text(
    gh: GitHub, monkeypatch: pytest.MonkeyPatch, stdout: str
) -> None:
    def fail(_self: GitHub, *args: str, **_kw: object) -> str:
        raise CommandError(["gh", *args], 4, stdout, "HTTP 502: bad gateway")

    monkeypatch.setattr(GitHub, "_gh", fail)
    with pytest.raises(PstackError) as excinfo:
        gh.view_prs([1])
    err = excinfo.value
    assert not isinstance(err, CommandError)
    assert isinstance(err.__cause__, CommandError)
    message = str(err)
    assert message == str(err.__cause__)
    assert message.startswith("command failed with exit code 4:")
    assert "HTTP 502: bad gateway" in message
    assert "gh api --hostname github.com graphql" in message
    assert REJECTED not in message


def test_view_prs_passes_host_owner_and_name_of_the_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[tuple[str, ...]] = []
    node = {**FULL_JSON, "url": "https://ghe.example.com/team/proj/pull/30"}

    def fake(_self: GitHub, *args: str, **_kw: object) -> str:
        recorded.append(args)
        return json.dumps({"data": {"repository": {"pr30": node}}})

    monkeypatch.setattr(GitHub, "_gh", fake)
    ghe = GitHub(Repo(host="ghe.example.com", owner="team", name="proj"))
    prs = ghe.view_prs([30])
    assert prs[30].url == "https://ghe.example.com/team/proj/pull/30"
    [args] = recorded
    assert args[:4] == ("api", "--hostname", "ghe.example.com", "graphql")
    assert args[4] == "-f"
    assert args[5].startswith("query=")
    assert args[6:] == ("-f", "owner=team", "-f", "name=proj")
    assert "pr30: pullRequest(number: 30)" in args[5]
    assert "team" not in args[5]
    assert "proj" not in args[5]


def test_view_prs_error_for_other_host_names_that_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"data": {"repository": {"pr3": None}}}
    monkeypatch.setattr(GitHub, "_gh", lambda *_a, **_k: json.dumps(payload))
    ghe = GitHub(Repo(host="ghe.example.com", owner="team", name="proj"))
    with pytest.raises(PstackError) as excinfo:
        ghe.view_prs([3])
    assert str(excinfo.value) == (
        "pull request #3 does not exist in https://ghe.example.com/team/proj"
    )


def test_view_prs_batch_helper_is_what_view_prs_uses(
    gh: GitHub, fake_gh: FakeGitHub, three_prs: list[PullRequest]
) -> None:
    direct = gh._view_prs_batch([1, 2, 3])
    assert direct == gh.view_prs([1, 2, 3])
    assert len(graphql_calls(fake_gh)) == 2


def test_find_open_pr_unknown_branch_returns_none(
    gh: GitHub, fake_gh: FakeGitHub
) -> None:
    assert gh.find_open_pr("testbot/stack/404") is None
    assert fake_gh.calls("pr", "list") == [
        [
            "pr",
            "list",
            "--repo",
            "github.com/octo/widgets",
            "--head",
            "testbot/stack/404",
            "--state",
            "open",
            "--limit",
            "1",
            "--json",
            PR_JSON_FIELDS,
        ]
    ]


def test_find_open_pr_returns_matching_pr(gh: GitHub, branches: dict[str, str]) -> None:
    created = create_first(gh)
    gh.create_pr(
        base="testbot/stack/1",
        head="testbot/stack/2",
        title="Add b",
        body="",
        draft=False,
    )
    assert gh.find_open_pr("testbot/stack/1") == created
    found = gh.find_open_pr("testbot/stack/2")
    assert found is not None
    assert found.number == 2


def test_find_open_pr_ignores_closed_pr(
    gh: GitHub, fake_gh: FakeGitHub, branches: dict[str, str]
) -> None:
    create_first(gh)
    fake_gh.close(1)
    assert gh.find_open_pr("testbot/stack/1") is None


def test_edit_pr_title_only(
    gh: GitHub, fake_gh: FakeGitHub, branches: dict[str, str]
) -> None:
    create_first(gh)
    gh.edit_pr(1, title="Add a (v2)")
    assert fake_gh.calls("pr", "edit") == [
        [
            "pr",
            "edit",
            "1",
            "--repo",
            "github.com/octo/widgets",
            "--title",
            "Add a (v2)",
        ]
    ]
    state = fake_gh.pr(1)
    assert state["title"] == "Add a (v2)"
    assert state["body"] == "First body\n"
    assert state["baseRefName"] == "main"


def test_edit_pr_body_only(
    gh: GitHub, fake_gh: FakeGitHub, branches: dict[str, str]
) -> None:
    create_first(gh)
    gh.edit_pr(1, body="New body\nwith two lines\n")
    assert fake_gh.calls("pr", "edit") == [
        ["pr", "edit", "1", "--repo", "github.com/octo/widgets", "--body-file", "-"]
    ]
    state = fake_gh.pr(1)
    assert state["body"] == "New body\nwith two lines\n"
    assert state["title"] == "Add a"


def test_edit_pr_empty_body_is_still_an_edit(
    gh: GitHub, fake_gh: FakeGitHub, branches: dict[str, str]
) -> None:
    create_first(gh)
    gh.edit_pr(1, body="")
    assert len(fake_gh.calls("pr", "edit")) == 1
    assert fake_gh.pr(1)["body"] == ""


def test_edit_pr_base_only(
    gh: GitHub, fake_gh: FakeGitHub, branches: dict[str, str]
) -> None:
    gh.create_pr(
        base="main", head="testbot/stack/2", title="Add b", body="", draft=False
    )
    gh.edit_pr(1, base="testbot/stack/1")
    assert fake_gh.calls("pr", "edit") == [
        [
            "pr",
            "edit",
            "1",
            "--repo",
            "github.com/octo/widgets",
            "--base",
            "testbot/stack/1",
        ]
    ]
    assert fake_gh.pr(1)["baseRefName"] == "testbot/stack/1"


def test_edit_pr_all_fields_in_fixed_order(
    gh: GitHub, fake_gh: FakeGitHub, branches: dict[str, str]
) -> None:
    gh.create_pr(
        base="main", head="testbot/stack/2", title="Add b", body="", draft=False
    )
    gh.edit_pr(1, title="T", body="B", base="testbot/stack/1")
    assert fake_gh.calls("pr", "edit") == [
        [
            "pr",
            "edit",
            "1",
            "--repo",
            "github.com/octo/widgets",
            "--title",
            "T",
            "--body-file",
            "-",
            "--base",
            "testbot/stack/1",
        ]
    ]
    state = fake_gh.pr(1)
    assert (state["title"], state["body"], state["baseRefName"]) == (
        "T",
        "B",
        "testbot/stack/1",
    )


def test_edit_pr_with_no_fields_makes_no_gh_call(
    gh: GitHub, fake_gh: FakeGitHub, branches: dict[str, str]
) -> None:
    create_first(gh)
    before = fake_gh.calls()
    gh.edit_pr(1)
    assert fake_gh.calls() == before
    assert fake_gh.calls("pr", "edit") == []
    assert fake_gh.pr(1).get("edits") is None


def test_edit_pr_unknown_number_raises_command_error(
    gh: GitHub, fake_gh: FakeGitHub
) -> None:
    with pytest.raises(CommandError, match="no pull requests found for 77"):
        gh.edit_pr(77, title="x")


def test_edit_pr_unknown_base_branch_raises(
    gh: GitHub, branches: dict[str, str]
) -> None:
    create_first(gh)
    with pytest.raises(CommandError, match="base branch 'ghost' does not exist"):
        gh.edit_pr(1, base="ghost")


def test_set_draft_true_marks_pr_draft(
    gh: GitHub, fake_gh: FakeGitHub, branches: dict[str, str]
) -> None:
    create_first(gh, draft=False)
    assert fake_gh.pr(1)["isDraft"] is False
    gh.set_draft(1, draft=True)
    assert fake_gh.pr(1)["isDraft"] is True
    assert fake_gh.calls("pr", "ready") == [
        ["pr", "ready", "1", "--repo", "github.com/octo/widgets", "--undo"]
    ]


def test_set_draft_false_marks_pr_ready(
    gh: GitHub, fake_gh: FakeGitHub, branches: dict[str, str]
) -> None:
    create_first(gh, draft=True)
    assert fake_gh.pr(1)["isDraft"] is True
    gh.set_draft(1, draft=False)
    assert fake_gh.pr(1)["isDraft"] is False
    assert fake_gh.calls("pr", "ready") == [
        ["pr", "ready", "1", "--repo", "github.com/octo/widgets"]
    ]


def test_set_draft_toggles_back_and_forth(
    gh: GitHub, fake_gh: FakeGitHub, branches: dict[str, str]
) -> None:
    create_first(gh, draft=False)
    gh.set_draft(1, draft=True)
    gh.set_draft(1, draft=False)
    gh.set_draft(1, draft=True)
    assert fake_gh.pr(1)["isDraft"] is True
    assert gh.view_pr(1).is_draft is True
    assert [c[-1] for c in fake_gh.calls("pr", "ready")] == [
        "--undo",
        "github.com/octo/widgets",
        "--undo",
    ]


def test_set_draft_unknown_pr_raises_command_error(
    gh: GitHub, fake_gh: FakeGitHub
) -> None:
    with pytest.raises(CommandError, match="no pull requests found for 5"):
        gh.set_draft(5, draft=True)


def test_gh_calls_use_host_qualified_repo_slug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[tuple[str, ...]] = []

    def fake(_self: GitHub, *args: str, **_kw: object) -> str:
        recorded.append(args)
        return "[]" if args[:2] == ("pr", "list") else ""

    monkeypatch.setattr(GitHub, "_gh", fake)
    ghe = GitHub(Repo(host="ghe.example.com", owner="team", name="proj"))
    ghe.find_open_pr("testbot/stack/1")
    ghe.edit_pr(3, title="x")
    ghe.set_draft(3, draft=True)
    assert [c[:5] for c in recorded] == [
        ("pr", "list", "--repo", "ghe.example.com/team/proj", "--head"),
        ("pr", "edit", "3", "--repo", "ghe.example.com/team/proj"),
        ("pr", "ready", "3", "--repo", "ghe.example.com/team/proj"),
    ]


def test_fake_gh_rejects_other_hosts_in_repo_flag(
    fake_gh: FakeGitHub, branches: dict[str, str]
) -> None:
    # Documents the fake's limitation: every offline test talks to github.com.
    ghe = GitHub(Repo(host="ghe.example.com", owner="team", name="proj"))
    with pytest.raises(CommandError, match=r"unexpected host 'ghe\.example\.com'"):
        ghe.find_open_pr("testbot/stack/1")
    [call] = fake_gh.calls("pr", "list")
    assert call[2:4] == ["--repo", "ghe.example.com/team/proj"]


def test_full_lifecycle_against_fake(
    gh: GitHub, fake_gh: FakeGitHub, branches: dict[str, str]
) -> None:
    assert gh.username() == "testbot"
    pr1 = create_first(gh, draft=True, reviewers=("alice",))
    pr2 = gh.create_pr(
        base="testbot/stack/1",
        head="testbot/stack/2",
        title="Add b",
        body="second",
        draft=False,
    )
    gh.edit_pr(pr1.number, body="Stacked PRs:\n * #2\n * __->__#1")
    gh.set_draft(pr1.number, draft=False)

    assert gh.view_pr(pr1.number) == PullRequest(
        number=1,
        url=PR1_URL,
        state="OPEN",
        is_draft=False,
        title="Add a",
        body="Stacked PRs:\n * #2\n * __->__#1",
        base="main",
        head="testbot/stack/1",
    )
    assert gh.view_pr(pr2.number) == pr2
    assert [c[:2] for c in fake_gh.write_calls()] == [
        ["pr", "create"],
        ["pr", "create"],
        ["pr", "edit"],
        ["pr", "ready"],
    ]
