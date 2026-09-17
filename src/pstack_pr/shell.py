"""Thin subprocess wrapper used for every ``git`` and ``gh`` invocation.

All commands are run without a shell, with stdout and stderr captured. The
command line and its output are logged at DEBUG level, which ``--verbose``
turns on.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from pstack_pr.errors import PstackError

log = logging.getLogger("pstack_pr")

# Commit messages are decoded losslessly so that a message with bytes that are
# not valid UTF-8 survives a round trip through the tool unchanged.
ENCODING = "utf-8"
ERRORS = "surrogateescape"


def decode(data: bytes) -> str:
    return data.decode(ENCODING, ERRORS)


def encode(text: str) -> bytes:
    return text.encode(ENCODING, ERRORS)


class CommandError(PstackError):
    """A subprocess exited with a non-zero status."""

    def __init__(
        self, cmd: Sequence[str], returncode: int, stdout: str, stderr: str
    ) -> None:
        self.cmd = list(cmd)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(self._format())

    def _format(self) -> str:
        lines = [f"command failed with exit code {self.returncode}:"]
        lines.append(f"  $ {shlex.join(self.cmd)}")
        for stream, raw in (("stdout", self.stdout), ("stderr", self.stderr)):
            text = raw.strip()
            if text:
                lines.append(f"  {stream}:")
                lines.extend(f"    {line}" for line in text.splitlines())
        return "\n".join(lines)


def run(
    cmd: Sequence[str],
    *,
    input: bytes | str | None = None,  # noqa: A002 - mirrors subprocess.run
    check: bool = True,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run ``cmd`` with output captured; raise :class:`CommandError` on failure."""
    log.debug("$ %s", shlex.join(cmd))
    data = encode(input) if isinstance(input, str) else input
    full_env = None
    if env:
        full_env = dict(os.environ)
        full_env.update(env)
    proc = subprocess.run(
        list(cmd),
        input=data,
        capture_output=True,
        cwd=cwd,
        env=full_env,
        check=False,
    )
    if log.isEnabledFor(logging.DEBUG):
        for stream, data in (("stdout", proc.stdout), ("stderr", proc.stderr)):
            text = decode(data).rstrip()
            if text:
                log.debug("  [%s] %s", stream, text.replace("\n", "\n  | "))
    if check and proc.returncode != 0:
        raise CommandError(
            cmd, proc.returncode, decode(proc.stdout), decode(proc.stderr)
        )
    return proc


def output(
    cmd: Sequence[str],
    *,
    input: bytes | str | None = None,  # noqa: A002 - mirrors subprocess.run
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Run ``cmd`` and return its stdout with trailing newlines removed."""
    return decode(run(cmd, input=input, cwd=cwd, env=env).stdout).rstrip("\n")
