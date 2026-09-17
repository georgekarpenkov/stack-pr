"""GitHub access through the ``gh`` command line tool."""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass

from pstack_pr import shell
from pstack_pr.errors import PstackError

# scp-like syntax: [user@]host:owner/repo(.git)
_SCP_URL_RE = re.compile(r"^(?:[\w.-]+@)?(?P<host>[\w.-]+):(?P<path>[^/:].*)$")
# real URLs: scheme://[user@]host[:port]/owner/repo(.git)
_URL_RE = re.compile(
    r"^(?:ssh|git|https?|git\+ssh)://(?:[^@/]+@)?(?P<host>[\w.-]+)(?::\d+)?/(?P<path>.+)$"
)
# GitHub's SSH-over-HTTPS endpoint serves github.com repositories.
_HOST_ALIASES = {"ssh.github.com": "github.com"}

PR_JSON_FIELDS = "number,url,state,isDraft,title,body,baseRefName,headRefName"


@dataclass(frozen=True)
class Repo:
    host: str
    owner: str
    name: str

    @property
    def slug(self) -> str:
        """Value for ``gh --repo``.

        The host is always included; a bare ``owner/name`` would be resolved
        against ``GH_HOST`` or gh's default host, not against this remote.
        """
        return f"{self.host}/{self.owner}/{self.name}"

    @property
    def url(self) -> str:
        return f"https://{self.host}/{self.owner}/{self.name}"


def parse_remote_url(url: str) -> Repo:
    """Extract host, owner and repository name from a git remote URL."""
    url = url.strip()
    m = _URL_RE.match(url) or _SCP_URL_RE.match(url)
    if not m:
        raise PstackError(f"cannot parse GitHub repository from remote URL {url!r}")
    path = m.group("path").strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    parts = path.split("/")
    if len(parts) != 2 or not all(parts):  # noqa: PLR2004
        raise PstackError(f"cannot parse GitHub repository from remote URL {url!r}")
    host = m.group("host").lower()
    return Repo(host=_HOST_ALIASES.get(host, host), owner=parts[0], name=parts[1])


def repo_of_pr_url(url: str) -> Repo | None:
    """The repository a pull request URL belongs to, if it looks like one."""
    m = re.match(
        r"^https?://(?P<host>[\w.-]+)/(?P<owner>[^/]+)/(?P<name>[^/]+)/pull/\d+/?$",
        url.strip(),
    )
    if not m:
        return None
    return Repo(
        host=m.group("host").lower(), owner=m.group("owner"), name=m.group("name")
    )


@dataclass
class PullRequest:
    number: int
    url: str
    state: str  # OPEN, CLOSED, MERGED
    is_draft: bool
    title: str
    body: str
    base: str
    head: str

    @classmethod
    def from_json(cls, data: dict[str, object]) -> PullRequest:
        try:
            return cls(
                number=int(str(data["number"])),
                url=str(data["url"]),
                state=str(data["state"]),
                is_draft=bool(data["isDraft"]),
                title=str(data["title"]),
                body=str(data.get("body") or ""),
                base=str(data["baseRefName"]),
                head=str(data["headRefName"]),
            )
        except KeyError as e:
            raise PstackError(f"unexpected response from gh, missing field {e}") from e


def pr_number_from_url(url: str) -> int | None:
    m = re.search(r"/pull/(\d+)/?$", url.strip())
    return int(m.group(1)) if m else None


def check_gh_installed() -> None:
    if shutil.which("gh") is None:
        raise PstackError(
            "the GitHub CLI ('gh') is not installed; see https://cli.github.com/"
        )


class GitHub:
    """Operations on pull requests of one repository."""

    def __init__(self, repo: Repo) -> None:
        self.repo = repo

    def _gh(self, *args: str, input: str | None = None) -> str:  # noqa: A002
        return shell.output(["gh", *args], input=input)

    def username(self) -> str:
        login = self._gh(
            "api", "--hostname", self.repo.host, "user", "--jq", ".login"
        ).strip()
        if not login:
            raise PstackError(
                "could not determine the GitHub user; run 'gh auth login'"
            )
        return login

    def view_pr(self, number: int) -> PullRequest:
        """Look up a pull request by number in this repository.

        Only numbers are accepted on purpose: given a URL, ``gh`` would use the
        repository named in the URL and ignore ``--repo``.
        """
        out = self._gh(
            "pr",
            "view",
            str(number),
            "--repo",
            self.repo.slug,
            "--json",
            PR_JSON_FIELDS,
        )
        return PullRequest.from_json(json.loads(out))

    def find_open_pr(self, head: str) -> PullRequest | None:
        """The open pull request whose head branch is ``head``, if any."""
        out = self._gh(
            "pr", "list", "--repo", self.repo.slug, "--head", head,
            "--state", "open", "--limit", "1", "--json", PR_JSON_FIELDS,
        )  # fmt: skip
        items = json.loads(out or "[]")
        return PullRequest.from_json(items[0]) if items else None

    def create_pr(
        self,
        *,
        base: str,
        head: str,
        title: str,
        body: str,
        draft: bool,
        reviewers: Sequence[str] = (),
    ) -> PullRequest:
        args = [
            "pr", "create", "--repo", self.repo.slug,
            "--base", base, "--head", head, "--title", title, "--body-file", "-",
        ]  # fmt: skip
        if draft:
            args.append("--draft")
        for reviewer in reviewers:
            args += ["--reviewer", reviewer]
        out = self._gh(*args, input=body)
        url = out.strip().splitlines()[-1].strip() if out.strip() else ""
        number = pr_number_from_url(url)
        if number is None:
            raise PstackError(
                f"'gh pr create' did not return a pull request URL:\n{out}"
            )
        return PullRequest(
            number=number, url=url, state="OPEN", is_draft=draft,
            title=title, body=body, base=base, head=head,
        )  # fmt: skip

    def edit_pr(
        self,
        number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        base: str | None = None,
    ) -> None:
        args = ["pr", "edit", str(number), "--repo", self.repo.slug]
        if title is not None:
            args += ["--title", title]
        if body is not None:
            args += ["--body-file", "-"]
        if base is not None:
            args += ["--base", base]
        if len(args) == 5:  # noqa: PLR2004 - nothing to change
            return
        self._gh(*args, input=body)

    def set_draft(self, number: int, *, draft: bool) -> None:
        args = ["pr", "ready", str(number), "--repo", self.repo.slug]
        if draft:
            args.append("--undo")
        self._gh(*args)
