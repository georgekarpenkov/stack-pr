"""Defaults from ``.pstack-pr.cfg`` at the repository root.

Example::

    [repo]
    remote = origin
    target = main
    reviewer = alice,bob
    branch_name_template = $USERNAME/stack

    [common]
    draft = false
    keep_body = false
    verbose = false

The path can be overridden with the ``PSTACK_PR_CONFIG`` environment variable.
"""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass
from pathlib import Path

from pstack_pr.errors import PstackError

CONFIG_FILE_NAME = ".pstack-pr.cfg"
CONFIG_ENV_VAR = "PSTACK_PR_CONFIG"
DEFAULT_BRANCH_TEMPLATE = "$USERNAME/stack"


@dataclass(frozen=True)
class Config:
    remote: str = "origin"
    target: str = "main"
    reviewer: str = ""
    branch_name_template: str = DEFAULT_BRANCH_TEMPLATE
    draft: bool = False
    keep_body: bool = False
    verbose: bool = False


def find_config_path(repo_root: Path | None) -> Path | None:
    """The config file to use: ``$PSTACK_PR_CONFIG`` or the one in the repo."""
    override = os.environ.get(CONFIG_ENV_VAR)
    if override:
        return Path(override)
    if repo_root is None:
        return None
    return repo_root / CONFIG_FILE_NAME


def load_config(path: Path | None) -> Config:
    if path is None or not path.is_file():
        return Config()
    parser = configparser.ConfigParser()
    try:
        parser.read(path)
        return Config(
            remote=parser.get("repo", "remote", fallback=Config.remote),
            target=parser.get("repo", "target", fallback=Config.target),
            reviewer=parser.get("repo", "reviewer", fallback=Config.reviewer),
            branch_name_template=parser.get(
                "repo", "branch_name_template", fallback=Config.branch_name_template
            ),
            draft=parser.getboolean("common", "draft", fallback=Config.draft),
            keep_body=parser.getboolean(
                "common", "keep_body", fallback=Config.keep_body
            ),
            verbose=parser.getboolean("common", "verbose", fallback=Config.verbose),
        )
    except (configparser.Error, ValueError) as e:
        raise PstackError(f"cannot read config file {path}: {e}") from e
