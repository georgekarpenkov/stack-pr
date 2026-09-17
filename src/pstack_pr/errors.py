from __future__ import annotations


class PstackError(Exception):
    """An error with a message meant for the user.

    The CLI prints the message and exits with status 1; no traceback is shown.
    """
