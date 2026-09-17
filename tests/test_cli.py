"""Tests for :mod:`pstack_pr.cli`: argument parsing and the ``main`` entry point."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from pstack_pr import __version__
from pstack_pr.cli import build_parser, main
from pstack_pr.config import Config
from tests.conftest import FakeGitHub, RunExport, Work


def parse(*argv: str, config: Config | None = None) -> argparse.Namespace:
    return build_parser(config or Config()).parse_args(list(argv))


# --------------------------------------------------------------------------- #
# Parser defaults
# --------------------------------------------------------------------------- #
def test_build_parser_returns_argument_parser_with_prog_name() -> None:
    parser = build_parser(Config())
    assert isinstance(parser, argparse.ArgumentParser)
    assert parser.prog == "pstack-pr"


def test_export_defaults_from_default_config() -> None:
    args = parse("export")
    assert args.command == "export"
    assert args.remote == "origin"
    assert args.target == "main"
    assert args.head == "HEAD"
    assert args.base is None
    assert args.draft is False
    assert args.reviewer == ""
    assert args.dry_run is False
    assert args.verbose is False
    assert args.keep_body is False
    assert args.branch_name_template == "$USERNAME/stack"


def test_export_defaults_exact_namespace() -> None:
    assert vars(parse("export")) == {
        "command": "export",
        "dry_run": False,
        "remote": "origin",
        "target": "main",
        "base": None,
        "head": "HEAD",
        "draft": False,
        "reviewer": "",
        "keep_body": False,
        "branch_name_template": "$USERNAME/stack",
        "verbose": False,
    }


def test_config_values_become_parser_defaults() -> None:
    config = Config(
        remote="upstream",
        target="master",
        reviewer="a,b",
        branch_name_template="$USERNAME/$BRANCH/pr",
        draft=True,
        keep_body=True,
        verbose=True,
    )
    args = parse("export", config=config)
    assert args.remote == "upstream"
    assert args.target == "master"
    assert args.reviewer == "a,b"
    assert args.branch_name_template == "$USERNAME/$BRANCH/pr"
    assert args.draft is True
    assert args.keep_body is True
    assert args.verbose is True
    # Not configurable: always the same regardless of the config.
    assert args.base is None
    assert args.head == "HEAD"
    assert args.dry_run is False


def test_config_target_draft_reviewer_example() -> None:
    args = parse("export", config=Config(target="master", draft=True, reviewer="a,b"))
    assert (args.target, args.draft, args.reviewer) == ("master", True, "a,b")
    assert args.remote == "origin"


def test_flags_override_config_defaults() -> None:
    config = Config(remote="upstream", target="master", reviewer="a,b")
    args = parse(
        "export", "-R", "origin", "-T", "main", "--reviewer", "", config=config
    )
    assert (args.remote, args.target, args.reviewer) == ("origin", "main", "")


def test_config_defaults_appear_in_help_text() -> None:
    parser = build_parser(Config(remote="upstream", target="trunk"))
    export = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    ).choices["export"]
    help_text = " ".join(export.format_help().split())  # undo argparse wrapping
    assert "(default: upstream)" in help_text
    assert "(default: trunk)" in help_text


# --------------------------------------------------------------------------- #
# Parser flags
# --------------------------------------------------------------------------- #
def test_short_flags_parse() -> None:
    args = parse(
        "export",
        "-n",
        "-R",
        "up",
        "-T",
        "dev",
        "-B",
        "abc123",
        "-H",
        "feature",
        "-d",
        "-v",
    )
    assert args.dry_run is True
    assert args.remote == "up"
    assert args.target == "dev"
    assert args.base == "abc123"
    assert args.head == "feature"
    assert args.draft is True
    assert args.verbose is True


def test_long_flags_parse() -> None:
    args = parse(
        "export",
        "--dry-run",
        "--remote", "up",
        "--target", "dev",
        "--base", "abc123",
        "--head", "feature~2",
        "--draft",
        "--reviewer", "x,y",
        "--keep-body",
        "--branch-name-template", "t/$ID",
        "--verbose",
    )  # fmt: skip
    assert vars(args) == {
        "command": "export",
        "dry_run": True,
        "remote": "up",
        "target": "dev",
        "base": "abc123",
        "head": "feature~2",
        "draft": True,
        "reviewer": "x,y",
        "keep_body": True,
        "branch_name_template": "t/$ID",
        "verbose": True,
    }


def test_reviewer_flag_keeps_raw_string() -> None:
    assert parse("export", "--reviewer", "a, b,,c").reviewer == "a, b,,c"


def test_keep_body_flag() -> None:
    assert parse("export", "--keep-body").keep_body is True


def test_branch_name_template_flag() -> None:
    args = parse("export", "--branch-name-template", "$USERNAME/$BRANCH/$ID")
    assert args.branch_name_template == "$USERNAME/$BRANCH/$ID"


def test_flags_with_equals_syntax() -> None:
    args = parse("export", "--target=dev", "--reviewer=a,b", "--base=HEAD~3")
    assert (args.target, args.reviewer, args.base) == ("dev", "a,b", "HEAD~3")


def test_flags_may_precede_or_follow_each_other() -> None:
    assert parse("export", "-d", "-n").dry_run is True
    assert parse("export", "-n", "-d").draft is True


def test_store_true_flags_cannot_be_unset_on_the_command_line() -> None:
    # With draft = true in the config there is no --no-draft; documents the behaviour.
    args = parse("export", config=Config(draft=True))
    assert args.draft is True
    with pytest.raises(SystemExit) as excinfo:
        parse("export", "--no-draft", config=Config(draft=True))
    assert excinfo.value.code == 2


# --------------------------------------------------------------------------- #
# Parser version and usage errors
# --------------------------------------------------------------------------- #
def test_version_flag_prints_version_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        parse("--version")
    assert excinfo.value.code == 0
    out, err = capsys.readouterr()
    assert out == f"pstack-pr {__version__}\n"
    assert err == ""


def test_version_matches_pyproject() -> None:
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    m = re.search(r'^version = "([^"]+)"$', pyproject, re.MULTILINE)
    assert m is not None
    assert __version__ == m.group(1)


def test_no_command_exits_with_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        parse()
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "usage: pstack-pr" in err
    assert "COMMAND" in err


def test_unknown_command_exits_with_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        parse("frobnicate")
    assert excinfo.value.code == 2
    assert "invalid choice: 'frobnicate'" in capsys.readouterr().err


def test_unknown_export_flag_exits_with_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        parse("export", "--bogus")
    assert excinfo.value.code == 2
    assert "unrecognized arguments: --bogus" in capsys.readouterr().err


def test_export_help_mentions_design_guarantees(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        parse("export", "--help")
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "git commit-tree" in out
    assert "git update-ref" in out
    assert "--dry-run" in out
    assert "--branch-name-template" in out


# --------------------------------------------------------------------------- #
# main(): before touching git or gh
# --------------------------------------------------------------------------- #
def test_main_version_exits_zero_outside_repo(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert capsys.readouterr().out == f"pstack-pr {__version__}\n"


def test_main_without_command_exits_two(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2


def test_main_outside_git_repo_returns_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    rc = main(["export"])
    out, err = capsys.readouterr()
    assert rc == 1
    assert "not inside a git repository" in err
    assert err.startswith("error: ")
    assert out == ""


def test_main_outside_git_repo_still_validates_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        main(["export", "--nope"])
    assert excinfo.value.code == 2


def test_main_malformed_config_in_repo_returns_one(
    work: Work, capsys: pytest.CaptureFixture[str]
) -> None:
    (work.path / ".pstack-pr.cfg").write_text("[common]\ndraft = maybe\n")
    rc = main(["export"])
    out, err = capsys.readouterr()
    assert rc == 1
    assert "cannot read config file" in err
    assert ".pstack-pr.cfg" in err
    assert out == ""


def test_main_version_works_despite_malformed_config(
    work: Work, capsys: pytest.CaptureFixture[str]
) -> None:
    (work.path / ".pstack-pr.cfg").write_text("[common]\ndraft = maybe\n")
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert capsys.readouterr().out == f"pstack-pr {__version__}\n"


def test_main_config_from_env_var_supplies_defaults(
    work: Work,
    fake_gh: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = tmp_path / "custom.cfg"
    cfg.write_text("[repo]\ntarget = nope\n")
    monkeypatch.setenv("PSTACK_PR_CONFIG", str(cfg))
    rc = main(["export"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "target branch 'origin/nope' does not exist" in err


def test_main_config_file_in_repo_root_supplies_defaults(
    work: Work, fake_gh: FakeGitHub, capsys: pytest.CaptureFixture[str]
) -> None:
    (work.path / ".pstack-pr.cfg").write_text("[repo]\nremote = nowhere\n")
    rc = main(["export"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "remote 'nowhere' does not exist" in err


def test_main_flag_beats_config_file(
    work: Work, fake_gh: FakeGitHub, capsys: pytest.CaptureFixture[str]
) -> None:
    (work.path / ".pstack-pr.cfg").write_text("[repo]\ntarget = nope\n")
    rc = main(["export", "--target", "main"])
    out, err = capsys.readouterr()
    assert rc == 0
    assert "Nothing to export" in out
    assert err == ""


def test_main_missing_gh_returns_one_before_any_git_write(
    work: Work,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real_git = shutil.which("git")
    assert real_git is not None
    bin_dir = tmp_path / "git-only-bin"
    bin_dir.mkdir()
    (bin_dir / "git").symlink_to(real_git)
    monkeypatch.setenv("PATH", str(bin_dir))
    assert shutil.which("gh") is None

    rc = main(["export"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "the GitHub CLI ('gh') is not installed" in err


# --------------------------------------------------------------------------- #
# main(['export']) in a repository
# --------------------------------------------------------------------------- #
def test_export_nothing_to_export_when_no_commits_above_main(
    work: Work, fake_gh: FakeGitHub, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["export"])
    out, err = capsys.readouterr()
    assert rc == 0
    assert "Nothing to export: no commits in origin/main..HEAD." in out
    assert "Fetching origin..." in out
    assert err == ""
    assert fake_gh.calls() == []
    assert work.remote.branches() == {"main": work.head("origin/main")}


def test_export_nothing_to_export_via_run_export_fixture(
    run_export: RunExport, fake_gh: FakeGitHub
) -> None:
    rc, out, err = run_export()
    assert rc == 0
    assert "Nothing to export" in out
    assert err == ""
    assert fake_gh.write_calls() == []


def test_export_nothing_to_export_message_uses_explicit_base(
    run_export: RunExport,
) -> None:
    rc, out, _ = run_export("-B", "HEAD")
    assert rc == 0
    assert "Nothing to export: no commits in HEAD..HEAD." in out


def test_export_nothing_to_export_message_uses_remote_and_target(
    work: Work, run_export: RunExport
) -> None:
    work.git("branch", "other", "origin/main")
    work.git("push", "-q", "origin", "other")
    rc, out, _ = run_export("-T", "other")
    assert rc == 0
    assert "Nothing to export: no commits in origin/other..HEAD." in out


def test_export_dry_run_changes_nothing(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    head = work.head()
    rc, out, err = run_export("-n")
    assert rc == 0
    assert "Dry run" in out
    assert "nothing was changed" in out
    assert err == ""
    assert work.head() == head
    assert work.shas() == stack3
    assert fake_gh.write_calls() == []
    assert fake_gh.prs() == {}
    assert work.remote.branches() == {"main": work.head("origin/main")}
    # Only read-only gh traffic happened (the username lookup).
    assert fake_gh.calls() == [
        ["api", "--hostname", "github.com", "user", "--jq", ".login"]
    ]


def test_export_reviewers_are_split_and_passed_separately(
    fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    rc, _, _ = run_export("--reviewer", "a, b,,c")
    assert rc == 0
    creates = fake_gh.calls("pr", "create")
    assert len(creates) == 3
    for call in creates:
        reviewers = [call[i + 1] for i, a in enumerate(call) if a == "--reviewer"]
        assert reviewers == ["a", "b", "c"]
        assert "--draft" not in call
    assert [fake_gh.pr(n)["reviewers"] for n in (1, 2, 3)] == [["a", "b", "c"]] * 3


def test_export_reviewer_whitespace_only_means_no_reviewers(
    fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    rc, _, _ = run_export("--reviewer", " , ,")
    assert rc == 0
    for call in fake_gh.calls("pr", "create"):
        assert "--reviewer" not in call
    assert fake_gh.pr(1)["reviewers"] == []


def test_export_draft_flag_reaches_gh(
    fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    rc, _, _ = run_export("-d")
    assert rc == 0
    creates = fake_gh.calls("pr", "create")
    assert len(creates) == 3
    assert all("--draft" in call for call in creates)
    assert all(fake_gh.pr(n)["isDraft"] is True for n in (1, 2, 3))


def test_export_reviewers_from_config_reach_gh(
    work: Work, fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    (work.path / ".pstack-pr.cfg").write_text("[repo]\nreviewer = cfg-a, cfg-b\n")
    rc, _, _ = run_export()
    assert rc == 0
    assert fake_gh.pr(3)["reviewers"] == ["cfg-a", "cfg-b"]


def test_export_prints_result_with_pr_urls(
    fake_gh: FakeGitHub, run_export: RunExport, stack3: list[str]
) -> None:
    rc, out, err = run_export()
    assert rc == 0
    assert err == ""
    assert "Exported 3 pull requests:" in out
    for n, title in ((1, "Add a"), (2, "Add b"), (3, "Add c")):
        assert f"https://github.com/octo/widgets/pull/{n}  {title}" in out


def test_export_error_from_plan_is_printed_and_returns_one(
    run_export: RunExport, stack3: list[str]
) -> None:
    rc, _, err = run_export("--branch-name-template", "$ID/x/$ID")
    assert rc == 1
    assert err.startswith("error: ")
    assert "must contain '$ID' exactly once" in err


def test_export_invalid_head_returns_one(run_export: RunExport) -> None:
    rc, _, err = run_export("-H", "no-such-rev")
    assert rc == 1
    assert "'no-such-rev' is not a valid commit" in err


# --------------------------------------------------------------------------- #
# python -m pstack_pr
# --------------------------------------------------------------------------- #
def test_module_entry_point_version() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "pstack_pr", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert proc.stdout == f"pstack-pr {__version__}\n"


def test_module_entry_point_outside_repo(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "pstack_pr", "export"],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    assert proc.returncode == 1
    assert "error: not inside a git repository" in proc.stderr
