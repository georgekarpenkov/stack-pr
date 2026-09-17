"""Terminal output helpers."""

from __future__ import annotations

import os
import sys
from typing import TextIO

_CODES = {
    "bold": "1",
    "dim": "2",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "cyan": "36",
}


class UI:
    def __init__(
        self,
        out: TextIO | None = None,
        err: TextIO | None = None,
        *,
        color: bool | None = None,
    ) -> None:
        self.out = out or sys.stdout
        self.err = err or sys.stderr
        if color is None:
            color = self.out.isatty() and not os.environ.get("NO_COLOR")
        self.color = color

    def style(self, text: str, *styles: str) -> str:
        if not self.color or not styles:
            return text
        codes = ";".join(_CODES[s] for s in styles)
        return f"\033[{codes}m{text}\033[0m"

    def bold(self, text: str) -> str:
        return self.style(text, "bold")

    def dim(self, text: str) -> str:
        return self.style(text, "dim")

    def green(self, text: str) -> str:
        return self.style(text, "green")

    def yellow(self, text: str) -> str:
        return self.style(text, "yellow")

    def cyan(self, text: str) -> str:
        return self.style(text, "cyan")

    def info(self, text: str = "") -> None:
        print(text, file=self.out, flush=True)

    def header(self, text: str) -> None:
        self.info(self.bold(text))

    def warn(self, text: str) -> None:
        print(
            self.style("warning: ", "yellow", "bold") + text, file=self.err, flush=True
        )

    def error(self, text: str) -> None:
        print(self.style("error: ", "red", "bold") + text, file=self.err, flush=True)
