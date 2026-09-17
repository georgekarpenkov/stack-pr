"""Tests for the subprocess wrapper in :mod:`pstack_pr.shell`."""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from pstack_pr.errors import PstackError
from pstack_pr.shell import CommandError, decode, encode, output, run

PY = sys.executable


def py(code: str) -> list[str]:
    """A command line running ``code`` with the test interpreter."""
    return [PY, "-c", code]


# --------------------------------------------------------------------------- #
# The run function
# --------------------------------------------------------------------------- #
def test_run_captures_stdout_and_stderr_separately() -> None:
    proc = run(py("import sys; sys.stdout.write('out'); sys.stderr.write('err')"))
    assert isinstance(proc, subprocess.CompletedProcess)
    assert proc.returncode == 0
    assert proc.stdout == b"out"
    assert proc.stderr == b"err"


def test_run_returns_raw_bytes_without_decoding() -> None:
    proc = run(py("import sys; sys.stdout.buffer.write(b'\\xff\\x00\\n')"))
    assert proc.stdout == b"\xff\x00\n"


def test_run_check_false_returns_nonzero_without_raising() -> None:
    proc = run(py("import sys; sys.stdout.write('partial'); sys.exit(3)"), check=False)
    assert proc.returncode == 3
    assert proc.stdout == b"partial"


def test_run_raises_command_error_on_failure_by_default() -> None:
    cmd = py("import sys; print('oops'); print('bad', file=sys.stderr); sys.exit(7)")
    with pytest.raises(CommandError) as excinfo:
        run(cmd)
    err = excinfo.value
    assert err.cmd == cmd
    assert err.returncode == 7
    assert err.stdout == "oops\n"
    assert err.stderr == "bad\n"


def test_command_error_is_a_pstack_error() -> None:
    assert issubclass(CommandError, PstackError)


def test_run_uses_cwd(tmp_path: Path) -> None:
    out = output(py("import os; print(os.getcwd())"), cwd=tmp_path)
    assert Path(out).resolve() == tmp_path.resolve()


def test_run_accepts_any_sequence_of_strings() -> None:
    cmd = tuple(py("print('tuple ok')"))
    assert output(cmd) == "tuple ok"


# --------------------------------------------------------------------------- #
# CommandError formatting
# --------------------------------------------------------------------------- #
def test_command_error_message_has_exit_code_command_and_both_streams() -> None:
    cmd = ["git", "push", "--force-with-lease=refs/heads/x:abc", "origin", "a:b"]
    err = CommandError(cmd, 128, "line1\nline2\n", "fatal: nope\n")
    assert str(err) == "\n".join(
        [
            "command failed with exit code 128:",
            f"  $ {shlex.join(cmd)}",
            "  stdout:",
            "    line1",
            "    line2",
            "  stderr:",
            "    fatal: nope",
        ]
    )


def test_command_error_message_quotes_arguments_with_shlex() -> None:
    cmd = ["gh", "pr", "create", "--title", "Add a thing", "--body", ""]
    err = CommandError(cmd, 1, "", "")
    assert f"  $ {shlex.join(cmd)}" in str(err).splitlines()
    assert "'Add a thing'" in str(err)
    assert "''" in str(err)


def test_command_error_message_omits_empty_stdout_section() -> None:
    err = CommandError(["false"], 1, "", "only stderr\n")
    assert str(err) == "\n".join(
        [
            "command failed with exit code 1:",
            "  $ false",
            "  stderr:",
            "    only stderr",
        ]
    )


def test_command_error_message_omits_empty_stderr_section() -> None:
    err = CommandError(["false"], 2, "only stdout\n", "")
    assert str(err) == "\n".join(
        [
            "command failed with exit code 2:",
            "  $ false",
            "  stdout:",
            "    only stdout",
        ]
    )


def test_command_error_message_with_no_output_is_two_lines() -> None:
    err = CommandError(["false"], 1, "", "")
    assert str(err) == "command failed with exit code 1:\n  $ false"


def test_command_error_treats_whitespace_only_output_as_empty() -> None:
    err = CommandError(["false"], 1, "  \n\n", "\t\n")
    assert str(err) == "command failed with exit code 1:\n  $ false"


def test_command_error_copies_cmd_into_a_list() -> None:
    cmd = ("a", "b")
    err = CommandError(cmd, 1, "", "")
    assert err.cmd == ["a", "b"]
    assert isinstance(err.cmd, list)


def test_run_failure_message_matches_command_error_format() -> None:
    cmd = py("import sys; print('x'); sys.exit(5)")
    with pytest.raises(CommandError, match=r"exit code 5") as excinfo:
        run(cmd)
    lines = str(excinfo.value).splitlines()
    assert lines[0] == "command failed with exit code 5:"
    assert lines[1] == f"  $ {shlex.join(cmd)}"
    assert lines[2:] == ["  stdout:", "    x"]


# --------------------------------------------------------------------------- #
# The output function
# --------------------------------------------------------------------------- #
def test_output_strips_trailing_newlines_only() -> None:
    assert output(py("import sys; sys.stdout.write('a\\n\\n\\n')")) == "a"


def test_output_keeps_leading_whitespace_and_trailing_spaces() -> None:
    assert output(py("import sys; sys.stdout.write('  x  \\n')")) == "  x  "


def test_output_keeps_internal_newlines() -> None:
    assert output(py("import sys; sys.stdout.write('a\\n\\nb\\n')")) == "a\n\nb"


def test_output_of_empty_stdout_is_empty_string() -> None:
    assert output(py("pass")) == ""


def test_output_does_not_strip_carriage_returns() -> None:
    assert output(py("import sys; sys.stdout.write('a\\r\\n')")) == "a\r"


def test_output_raises_command_error_on_failure() -> None:
    with pytest.raises(CommandError, match="exit code 9"):
        output(py("import sys; sys.exit(9)"))


# --------------------------------------------------------------------------- #
# input
# --------------------------------------------------------------------------- #
CAT = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"


def test_run_input_as_str_is_encoded_utf8() -> None:
    proc = run(py(CAT), input="héllo\n")
    assert proc.stdout == "héllo\n".encode()


def test_run_input_as_bytes_is_passed_verbatim() -> None:
    proc = run(py(CAT), input=b"\xff\x00raw")
    assert proc.stdout == b"\xff\x00raw"


def test_run_input_str_with_surrogates_round_trips_invalid_bytes() -> None:
    raw = b"caf\xe9 \xff"
    proc = run(py(CAT), input=decode(raw))
    assert proc.stdout == raw


def test_run_without_input_gives_child_no_stdin_data() -> None:
    proc = run(py("import sys; sys.stdout.write(repr(sys.stdin.read()))"))
    assert proc.stdout == b"''"


def test_output_accepts_input() -> None:
    assert output(py(CAT), input="via output\n") == "via output"


# --------------------------------------------------------------------------- #
# env
# --------------------------------------------------------------------------- #
PRINT_ENV = "import os, sys; print(os.environ.get(sys.argv[1], '<unset>'))"


def test_run_env_var_visible_to_child(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PSTACK_TEST_ONLY_VIA_ENV", raising=False)
    out = output(
        [*py(PRINT_ENV), "PSTACK_TEST_ONLY_VIA_ENV"],
        env={"PSTACK_TEST_ONLY_VIA_ENV": "hello"},
    )
    assert out == "hello"
    assert "PSTACK_TEST_ONLY_VIA_ENV" not in os.environ


def test_run_env_is_merged_with_os_environ_path_still_inherited() -> None:
    out = output([*py(PRINT_ENV), "PATH"], env={"PSTACK_TEST_EXTRA": "1"})
    assert out == os.environ["PATH"]


def test_run_env_overrides_inherited_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PSTACK_TEST_OVERRIDE", "from-environ")
    out = output(
        [*py(PRINT_ENV), "PSTACK_TEST_OVERRIDE"],
        env={"PSTACK_TEST_OVERRIDE": "from-env"},
    )
    assert out == "from-env"


def test_run_without_env_inherits_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PSTACK_TEST_INHERITED", "yes")
    assert output([*py(PRINT_ENV), "PSTACK_TEST_INHERITED"]) == "yes"


def test_run_with_empty_env_mapping_inherits_os_environ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PSTACK_TEST_INHERITED", "still")
    assert output([*py(PRINT_ENV), "PSTACK_TEST_INHERITED"], env={}) == "still"


def test_run_env_does_not_leak_into_parent_process() -> None:
    output(py("pass"), env={"PSTACK_TEST_LEAK": "x"})
    assert "PSTACK_TEST_LEAK" not in os.environ


# --------------------------------------------------------------------------- #
# encode / decode
# --------------------------------------------------------------------------- #
def test_decode_valid_utf8() -> None:
    assert decode(b"caf\xc3\xa9") == "café"


def test_encode_valid_utf8() -> None:
    assert encode("café") == b"caf\xc3\xa9"


def test_decode_invalid_utf8_does_not_raise() -> None:
    text = decode(b"\xff\xfe")
    assert len(text) == 2
    assert all(0xDC80 <= ord(c) <= 0xDCFF for c in text)


@pytest.mark.parametrize(
    "raw",
    [
        b"\xff",
        b"\xff\xfe\xfd",
        b"ok \xe9 latin-1 \xff",
        b"\xc3\x28",
        b"\xed\xa0\x80",
        b"mixed \xc3\xa9 \xff \xe2\x82\xac",
    ],
)
def test_encode_decode_round_trips_invalid_utf8(raw: bytes) -> None:
    assert encode(decode(raw)) == raw


def test_command_error_carries_decoded_invalid_utf8_from_child() -> None:
    cmd = py("import sys; sys.stdout.buffer.write(b'\\xff'); sys.exit(1)")
    with pytest.raises(CommandError) as excinfo:
        run(cmd)
    assert encode(excinfo.value.stdout) == b"\xff"


# --------------------------------------------------------------------------- #
# logging
# --------------------------------------------------------------------------- #
def test_debug_logging_emits_command_line(caplog: pytest.LogCaptureFixture) -> None:
    cmd = py("print('hi there')")
    with caplog.at_level(logging.DEBUG, logger="pstack_pr"):
        run(cmd)
    assert f"$ {shlex.join(cmd)}" in caplog.messages
    records = [r for r in caplog.records if r.name == "pstack_pr"]
    assert records
    assert all(r.levelno == logging.DEBUG for r in records)


def test_debug_logging_emits_stdout_and_stderr(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cmd = py("import sys; print('o1'); print('o2'); print('e1', file=sys.stderr)")
    with caplog.at_level(logging.DEBUG, logger="pstack_pr"):
        run(cmd)
    assert caplog.messages == [
        f"$ {shlex.join(cmd)}",
        "  [stdout] o1\n  | o2",
        "  [stderr] e1",
    ]


def test_debug_logging_skips_empty_streams(caplog: pytest.LogCaptureFixture) -> None:
    cmd = py("pass")
    with caplog.at_level(logging.DEBUG, logger="pstack_pr"):
        run(cmd)
    assert caplog.messages == [f"$ {shlex.join(cmd)}"]


def test_debug_logging_happens_even_when_command_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cmd = py("import sys; print('boom', file=sys.stderr); sys.exit(1)")
    with (
        caplog.at_level(logging.DEBUG, logger="pstack_pr"),
        pytest.raises(CommandError),
    ):
        run(cmd)
    assert caplog.messages == [f"$ {shlex.join(cmd)}", "  [stderr] boom"]


def test_nothing_logged_at_info_level(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="pstack_pr"):
        run(py("print('quiet')"))
    assert [r for r in caplog.records if r.name == "pstack_pr"] == []
