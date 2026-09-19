"""GitHub access through the ``gh`` command line tool."""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field

from pstack_pr import shell
from pstack_pr.errors import PstackError
from pstack_pr.shell import CommandError

# scp-like syntax: [user@]host:owner/repo(.git)
_SCP_URL_RE = re.compile(r"^(?:[\w.-]+@)?(?P<host>[\w.-]+):(?P<path>[^/:].*)$")
# real URLs: scheme://[user@]host[:port]/owner/repo(.git)
_URL_RE = re.compile(
    r"^(?:ssh|git|https?|git\+ssh)://(?:[^@/]+@)?(?P<host>[\w.-]+)(?::\d+)?/(?P<path>.+)$"
)
# GitHub's SSH-over-HTTPS endpoint serves github.com repositories.
_HOST_ALIASES = {"ssh.github.com": "github.com"}

PR_JSON_FIELDS = "number,url,state,isDraft,title,body,baseRefName,headRefName,comments"
# The same fields for GraphQL. Comments are needed to find the stack comment
# the tool maintains; it is posted right after the PR is created, so it is
# among the first ones.
PR_GRAPHQL_FIELDS = (
    "number url state isDraft title body baseRefName headRefName "
    "comments(first: 100) { nodes { databaseId url body } }"
)
# Pull requests looked up per GraphQL request; well below GitHub's node limits.
GRAPHQL_BATCH_SIZE = 50


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
class Comment:
    """An issue comment on a pull request; ``id`` is the REST (database) id."""

    id: int
    body: str

    @classmethod
    def from_json(cls, data: dict[str, object]) -> Comment | None:
        """``None`` when the id cannot be determined.

        The REST API calls it ``id``; the GraphQL lookup asks for
        ``databaseId``; ``gh pr view --json comments`` gives the GraphQL node
        id as ``id`` and the REST id only inside ``url``
        (``...#issuecomment-<id>``).
        """
        comment_id: object = data.get("databaseId")
        if comment_id is None and isinstance(data.get("id"), int):
            comment_id = data["id"]
        if comment_id is None:
            url = str(data.get("url") or data.get("html_url") or "")
            m = re.search(r"#issuecomment-(\d+)$", url)
            comment_id = m.group(1) if m else None
        if comment_id is None:
            return None
        return cls(id=int(str(comment_id)), body=str(data.get("body") or ""))


def _comments_from_json(data: object) -> list[Comment]:
    """Comments from either ``gh --json`` (a list) or GraphQL (``{nodes: []}``)."""
    if isinstance(data, dict):
        data = data.get("nodes")
    if not isinstance(data, list):
        return []
    comments = []
    for item in data:
        if isinstance(item, dict):
            comment = Comment.from_json(item)
            if comment is not None:
                comments.append(comment)
    return comments


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
    comments: list[Comment] = field(default_factory=list)

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
                comments=_comments_from_json(data.get("comments")),
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


def _graphql_failure(error: CommandError, repo: Repo) -> str:
    """A readable message for a failed ``gh api graphql`` call."""
    messages: list[str] = []
    try:
        payload = json.loads(error.stdout or "{}")
    except ValueError:
        payload = {}
    for item in payload.get("errors") or []:
        if isinstance(item, dict) and item.get("message"):
            messages.append(str(item["message"]))
    if messages:
        return (
            f"GitHub rejected the pull request lookup in {repo.url}:\n  "
            + "\n  ".join(messages)
        )
    return str(error)


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

    def view_prs(self, numbers: Sequence[int]) -> dict[int, PullRequest]:
        """Look up many pull requests by number with one request per 50.

        Unlike one ``gh pr view`` per PR this costs a single round trip for a
        whole stack. A number that does not exist in the repository is an
        error.
        """
        prs: dict[int, PullRequest] = {}
        for start in range(0, len(numbers), GRAPHQL_BATCH_SIZE):
            prs.update(
                self._view_prs_batch(numbers[start : start + GRAPHQL_BATCH_SIZE])
            )
        return prs

    def _view_prs_batch(self, numbers: Sequence[int]) -> dict[int, PullRequest]:
        selections = " ".join(
            f"pr{n}: pullRequest(number: {n}) {{ {PR_GRAPHQL_FIELDS} }}"
            for n in numbers
        )
        query = (
            "query($owner: String!, $name: String!) { "
            f"repository(owner: $owner, name: $name) {{ {selections} }} }}"
        )
        try:
            out = self._gh(
                "api", "--hostname", self.repo.host, "graphql",
                "-f", f"query={query}",
                "-f", f"owner={self.repo.owner}",
                "-f", f"name={self.repo.name}",
            )  # fmt: skip
        except CommandError as e:
            raise PstackError(_graphql_failure(e, self.repo)) from e
        data = json.loads(out or "{}")
        repository = (data.get("data") or {}).get("repository")
        if not isinstance(repository, dict):
            raise PstackError(f"repository {self.repo.slug} was not found on GitHub")
        prs: dict[int, PullRequest] = {}
        for n in numbers:
            node = repository.get(f"pr{n}")
            if not isinstance(node, dict):
                raise PstackError(
                    f"pull request #{n} does not exist in {self.repo.url}"
                )
            prs[n] = PullRequest.from_json(node)
        return prs

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

    def _rest(self, method: str, path: str, payload: dict[str, object]) -> object:
        """A REST call with a JSON body; ``path`` is relative to the repository."""
        out = self._gh(
            "api", "--hostname", self.repo.host, "--method", method,
            f"repos/{self.repo.owner}/{self.repo.name}/{path}", "--input", "-",
            input=json.dumps(payload),
        )  # fmt: skip
        return json.loads(out or "null")

    def create_comment(self, number: int, body: str) -> Comment:
        """Post ``body`` as a new comment on pull request ``number``."""
        data = self._rest("POST", f"issues/{number}/comments", {"body": body})
        comment = Comment.from_json(data) if isinstance(data, dict) else None
        if comment is None:
            raise PstackError(
                f"'gh api' did not return the comment created on PR #{number}"
            )
        return comment

    def edit_comment(self, comment_id: int, body: str) -> None:
        self._rest("PATCH", f"issues/comments/{comment_id}", {"body": body})
