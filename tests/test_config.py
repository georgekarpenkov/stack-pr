"""Tests for :mod:`pstack_pr.config`: ``.pstack-pr.cfg`` parsing and lookup."""

from __future__ import annotations

from pathlib import Path

import pytest

from pstack_pr.config import (
    CONFIG_ENV_VAR,
    CONFIG_FILE_NAME,
    DEFAULT_BRANCH_TEMPLATE,
    Config,
    find_config_path,
    load_config,
)
from pstack_pr.errors import PstackError

FULL_CONFIG = """\
[repo]
remote = upstream
target = develop
reviewer = alice,bob
branch_name_template = $USERNAME/$BRANCH/pr

[common]
draft = true
keep_body = true
verbose = true
"""


def write(tmp_path: Path, text: str, name: str = CONFIG_FILE_NAME) -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #
def test_default_config_values() -> None:
    cfg = Config()
    assert cfg.remote == "origin"
    assert cfg.target == "main"
    assert cfg.reviewer == ""
    assert cfg.branch_name_template == "$USERNAME/stack"
    assert cfg.branch_name_template == DEFAULT_BRANCH_TEMPLATE
    assert cfg.draft is False
    assert cfg.keep_body is False
    assert cfg.verbose is False


def test_config_is_frozen() -> None:
    cfg = Config()
    with pytest.raises(AttributeError):
        cfg.target = "x"  # type: ignore[misc]


def test_constants() -> None:
    assert CONFIG_FILE_NAME == ".pstack-pr.cfg"
    assert CONFIG_ENV_VAR == "PSTACK_PR_CONFIG"


def test_load_config_none_path_gives_defaults() -> None:
    assert load_config(None) == Config()


def test_load_config_missing_file_gives_defaults(tmp_path: Path) -> None:
    assert load_config(tmp_path / "does-not-exist.cfg") == Config()


def test_load_config_directory_path_gives_defaults(tmp_path: Path) -> None:
    assert load_config(tmp_path) == Config()


def test_load_config_empty_file_gives_defaults(tmp_path: Path) -> None:
    assert load_config(write(tmp_path, "")) == Config()


def test_load_config_unknown_sections_and_keys_are_ignored(tmp_path: Path) -> None:
    path = write(tmp_path, "[other]\nfoo = bar\n\n[repo]\nunknown = 1\n")
    assert load_config(path) == Config()


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def test_load_config_parses_all_keys_from_both_sections(tmp_path: Path) -> None:
    cfg = load_config(write(tmp_path, FULL_CONFIG))
    assert cfg == Config(
        remote="upstream",
        target="develop",
        reviewer="alice,bob",
        branch_name_template="$USERNAME/$BRANCH/pr",
        draft=True,
        keep_body=True,
        verbose=True,
    )


def test_load_config_repo_section_only(tmp_path: Path) -> None:
    cfg = load_config(write(tmp_path, "[repo]\ntarget = master\n"))
    assert cfg.target == "master"
    assert cfg.remote == "origin"
    assert cfg.draft is False


def test_load_config_common_section_only(tmp_path: Path) -> None:
    cfg = load_config(write(tmp_path, "[common]\ndraft = yes\n"))
    assert cfg.draft is True
    assert cfg.keep_body is False
    assert cfg.target == "main"


def test_load_config_keys_are_section_specific(tmp_path: Path) -> None:
    # ``draft`` under [repo] and ``target`` under [common] are simply ignored.
    cfg = load_config(write(tmp_path, "[repo]\ndraft = yes\n\n[common]\ntarget = x\n"))
    assert cfg == Config()


def test_load_config_reviewer_keeps_raw_comma_string(tmp_path: Path) -> None:
    cfg = load_config(write(tmp_path, "[repo]\nreviewer = a, b,,c\n"))
    assert cfg.reviewer == "a, b,,c"


def test_load_config_values_are_stripped(tmp_path: Path) -> None:
    cfg = load_config(write(tmp_path, "[repo]\nremote =    upstream   \n"))
    assert cfg.remote == "upstream"


def test_load_config_colon_separator_and_comments(tmp_path: Path) -> None:
    text = "# a comment\n[repo]\nremote: upstream\n; another\ntarget: dev\n"
    cfg = load_config(write(tmp_path, text))
    assert cfg.remote == "upstream"
    assert cfg.target == "dev"


def test_load_config_keys_are_case_insensitive(tmp_path: Path) -> None:
    cfg = load_config(write(tmp_path, "[repo]\nTarget = dev\n[common]\nDRAFT = 1\n"))
    assert cfg.target == "dev"
    assert cfg.draft is True


def test_load_config_dollar_signs_are_not_interpolated(tmp_path: Path) -> None:
    cfg = load_config(
        write(tmp_path, "[repo]\nbranch_name_template = $USERNAME/x/$ID\n")
    )
    assert cfg.branch_name_template == "$USERNAME/x/$ID"


def test_load_config_percent_sign_is_a_parse_error(tmp_path: Path) -> None:
    # configparser's default BasicInterpolation treats '%' specially.
    path = write(tmp_path, "[repo]\nreviewer = 100%\n")
    with pytest.raises(PstackError, match="cannot read config file"):
        load_config(path)


# --------------------------------------------------------------------------- #
# Booleans
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("spelling", "expected"),
    [
        ("yes", True),
        ("no", False),
        ("1", True),
        ("0", False),
        ("True", True),
        ("False", False),
        ("true", True),
        ("false", False),
        ("TRUE", True),
        ("on", True),
        ("off", False),
        ("Yes", True),
        ("NO", False),
    ],
)
def test_load_config_boolean_spellings(
    tmp_path: Path, spelling: str, expected: bool
) -> None:
    text = (
        f"[common]\ndraft = {spelling}\nkeep_body = {spelling}\nverbose = {spelling}\n"
    )
    cfg = load_config(write(tmp_path, text))
    assert cfg.draft is expected
    assert cfg.keep_body is expected
    assert cfg.verbose is expected


@pytest.mark.parametrize("bad", ["maybe", "2", "", "yes please", "t"])
def test_load_config_invalid_boolean_raises_pstack_error(
    tmp_path: Path, bad: str
) -> None:
    path = write(tmp_path, f"[common]\ndraft = {bad}\n")
    with pytest.raises(PstackError, match="cannot read config file") as excinfo:
        load_config(path)
    assert str(path) in str(excinfo.value)


def test_load_config_invalid_boolean_names_the_key_value(tmp_path: Path) -> None:
    path = write(tmp_path, "[common]\nverbose = sometimes\n")
    with pytest.raises(PstackError, match="sometimes"):
        load_config(path)


# --------------------------------------------------------------------------- #
# Malformed files
# --------------------------------------------------------------------------- #
def test_load_config_missing_section_header_raises(tmp_path: Path) -> None:
    path = write(tmp_path, "target = main\n")
    with pytest.raises(PstackError, match="cannot read config file") as excinfo:
        load_config(path)
    assert str(path) in str(excinfo.value)


def test_load_config_duplicate_option_raises(tmp_path: Path) -> None:
    path = write(tmp_path, "[repo]\ntarget = a\ntarget = b\n")
    with pytest.raises(PstackError, match="cannot read config file"):
        load_config(path)


def test_load_config_duplicate_section_raises(tmp_path: Path) -> None:
    path = write(tmp_path, "[repo]\ntarget = a\n[repo]\nremote = b\n")
    with pytest.raises(PstackError, match="cannot read config file"):
        load_config(path)


def test_load_config_garbage_line_raises(tmp_path: Path) -> None:
    path = write(tmp_path, "[repo]\nthis is not a key value pair\n")
    with pytest.raises(PstackError, match="cannot read config file"):
        load_config(path)


def test_load_config_error_is_a_pstack_error_not_configparser_error(
    tmp_path: Path,
) -> None:
    path = write(tmp_path, "[[broken\n")
    with pytest.raises(PstackError):
        load_config(path)


# --------------------------------------------------------------------------- #
# find_config_path
# --------------------------------------------------------------------------- #
def test_find_config_path_none_when_no_repo_and_no_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
    assert find_config_path(None) is None


def test_find_config_path_uses_repo_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
    assert find_config_path(tmp_path) == tmp_path / ".pstack-pr.cfg"


def test_find_config_path_returns_repo_path_even_if_file_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
    path = find_config_path(tmp_path)
    assert path == tmp_path / CONFIG_FILE_NAME
    assert path is not None
    assert not path.exists()


def test_find_config_path_env_var_overrides_repo_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    override = tmp_path / "elsewhere" / "my.cfg"
    monkeypatch.setenv(CONFIG_ENV_VAR, str(override))
    assert find_config_path(tmp_path / "repo") == override


def test_find_config_path_env_var_without_repo_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    override = tmp_path / "my.cfg"
    monkeypatch.setenv(CONFIG_ENV_VAR, str(override))
    assert find_config_path(None) == override


def test_find_config_path_empty_env_var_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(CONFIG_ENV_VAR, "")
    assert find_config_path(tmp_path) == tmp_path / CONFIG_FILE_NAME
    assert find_config_path(None) is None


def test_find_config_path_result_is_a_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CONFIG_ENV_VAR, "/some/where/cfg.ini")
    result = find_config_path(None)
    assert isinstance(result, Path)
    assert result == Path("/some/where/cfg.ini")


def test_find_then_load_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    write(repo, "[repo]\ntarget = trunk\n")
    other = write(tmp_path, "[repo]\ntarget = via-env\n", name="override.cfg")

    monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
    assert load_config(find_config_path(repo)).target == "trunk"

    monkeypatch.setenv(CONFIG_ENV_VAR, str(other))
    assert load_config(find_config_path(repo)).target == "via-env"
