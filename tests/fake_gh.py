"""A stand-in for the GitHub CLI used by the offline end-to-end tests.

State lives in a JSON file named by ``$FAKE_GH_STATE``. Pull requests are
validated against the bare "remote" repository at ``$FAKE_GH_REMOTE`` so that
the fake behaves like GitHub where it matters:

* ``pr create`` requires both branches to exist and the head to have commits
  that are not in the base;
* issue comments can be listed (``--json comments``, GraphQL), created
  (``api --method POST .../issues/N/comments``) and edited
  (``api --method PATCH .../issues/comments/ID``);
* the ``post-receive`` entry point (installed as a hook in the bare repo)
  closes every open PR whose head branch no longer has commits beyond its base
  branch, which is what GitHub does after a push.

Every invocation is appended to ``calls`` so tests can assert what happened.
``$FAKE_GH_FAIL_ON`` (e.g. ``pr create:2``) makes the N-th invocation of a
subcommand fail, to simulate an interrupted run.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn

HOST = "https://github.com"


def die(msg: str, code: int = 1) -> NoReturn:
    print(msg, file=sys.stderr)
    sys.exit(code)


class State:
    def __init__(self, path: Path) -> None:
        self.path = path
        if path.exists():
            self.data: dict[str, Any] = json.loads(path.read_text())
        else:
            self.data = {"next_number": 1, "prs": {}, "calls": []}

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=1, sort_keys=True))

    @property
    def prs(self) -> dict[str, dict[str, Any]]:
        return self.data["prs"]  # type: ignore[no-any-return]

    def find(self, ref: str) -> dict[str, Any] | None:
        ref = ref.strip().rstrip("/")
        number = ref.rsplit("/", 1)[-1].lstrip("#")
        return self.prs.get(number)

    def new_comment_id(self) -> int:
        # Well away from PR numbers so that mixing them up shows in tests.
        comment_id = int(self.data.get("next_comment_id", 1001))
        self.data["next_comment_id"] = comment_id + 1
        return comment_id


def git_remote(*args: str) -> subprocess.CompletedProcess[str]:
    remote = os.environ["FAKE_GH_REMOTE"]
    return subprocess.run(
        ["git", "-C", remote, *args], capture_output=True, text=True, check=False
    )


def branch_sha(branch: str) -> str | None:
    proc = git_remote("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    return proc.stdout.strip() if proc.returncode == 0 else None


def head_within_base(head: str, base: str) -> bool:
    """True when ``head`` has no commits that are not in ``base``."""
    h, b = branch_sha(head), branch_sha(base)
    if h is None or b is None:
        return False
    return git_remote("merge-base", "--is-ancestor", h, b).returncode == 0


def parse_flags(argv: list[str]) -> tuple[list[str], dict[str, list[str]]]:
    """Split ``argv`` into positional arguments and ``--flag value`` pairs."""
    positional: list[str] = []
    flags: dict[str, list[str]] = {}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-f", "-F"):
            # gh api field: -f key=value
            i += 1
            key, _, value = argv[i].partition("=")
            flags.setdefault(key, []).append(value)
        elif arg.startswith("--"):
            name = arg[2:]
            if "=" in name:
                name, value = name.split("=", 1)
                flags.setdefault(name, []).append(value)
            elif name in BOOL_FLAGS:
                flags.setdefault(name, []).append("true")
            else:
                i += 1
                flags.setdefault(name, []).append(argv[i] if i < len(argv) else "")
        else:
            positional.append(arg)
        i += 1
    return positional, flags


BOOL_FLAGS = {"draft", "undo", "web", "fill"}


def to_json(pr: dict[str, Any], fields: str) -> dict[str, Any]:
    """Like ``gh --json``: comments carry the node id and url, not databaseId."""
    data = {f: pr[f] for f in fields.split(",") if f in pr}
    if "comments" in data:
        data["comments"] = [
            {k: v for k, v in c.items() if k != "databaseId"} for c in data["comments"]
        ]
    return data


def cmd_api(state: State, args: list[str]) -> None:
    positional, flags = parse_flags(args)
    if positional[:1] == ["graphql"]:
        graphql(state, flags)
        return
    if positional and positional[0].startswith("repos/"):
        rest_call(state, positional[0], flags)
        return
    if positional[:1] == ["user"]:
        login = os.environ.get("FAKE_GH_USER", "testbot")
        if flags.get("jq") == [".login"]:
            print(login)
        else:
            print(json.dumps({"login": login}))
        return
    die(f"fake gh: unsupported api call {args}")


COMMENTS_SELECTION_RE = re.compile(r"comments\(first: \d+\) \{ nodes \{ ([^}]*) \} \}")


def rest_call(state: State, path: str, flags: dict[str, list[str]]) -> None:
    """The two REST endpoints the tool uses, for issue comments."""
    method = flags.get("method", ["GET"])[0]
    payload = json.loads(sys.stdin.read()) if flags.get("input") == ["-"] else {}
    parts = path.split("/")
    if method == "POST" and parts[3:4] == ["issues"] and parts[5:] == ["comments"]:
        pr = state.prs.get(parts[4])
        if pr is None:
            die(f"gh: Not Found (HTTP 404)\n{path}")
        number = int(parts[4])
        comment_id = state.new_comment_id()
        comment = {
            "databaseId": comment_id,
            "id": f"IC_{comment_id}",
            "url": f"{pr['url']}#issuecomment-{comment_id}",
            "body": payload.get("body", ""),
        }
        pr.setdefault("comments", []).append(comment)
        print(
            json.dumps(
                {
                    "id": comment_id,
                    "body": comment["body"],
                    "html_url": comment["url"],
                    "issue_number": number,
                }
            )
        )
        return
    if method == "PATCH" and parts[3:5] == ["issues", "comments"] and len(parts) == 6:
        for pr in state.prs.values():
            for comment in pr.get("comments", []):
                if str(comment["databaseId"]) == parts[5]:
                    comment["body"] = payload.get("body", comment["body"])
                    print(
                        json.dumps(
                            {
                                "id": comment["databaseId"],
                                "body": comment["body"],
                                "html_url": comment["url"],
                            }
                        )
                    )
                    return
        die(f"gh: Not Found (HTTP 404)\n{path}")
    die(f"fake gh: unsupported REST call {method} {path}")


def graphql(state: State, flags: dict[str, list[str]]) -> None:
    """Answer the batched pull request lookup the tool sends.

    Only the shape ``alias: pullRequest(number: N) { fields }`` is understood,
    where the fields may include a ``comments(first: N) { nodes { ... } }``
    selection. Like gh, a missing pull request produces a payload with
    ``errors`` and a non-zero exit status.
    """
    query = flags["query"][0]
    wanted = re.findall(r"(\w+): pullRequest\(number: (\d+)\)", query)
    comments_match = COMMENTS_SELECTION_RE.search(query)
    comment_fields = comments_match.group(1).split() if comments_match else None
    query_flat = COMMENTS_SELECTION_RE.sub("", query)
    fields_match = re.search(r"pullRequest\(number: \d+\) \{ ([^}]*) \}", query_flat)
    fields = fields_match.group(1).split() if fields_match else []
    data: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []
    for alias, number in wanted:
        pr = state.prs.get(number)
        if pr is None:
            data[alias] = None
            errors.append(
                {
                    "type": "NOT_FOUND",
                    "path": ["repository", alias],
                    "message": f"Could not resolve to a PullRequest with the number of {number}.",
                }
            )
        else:
            node = {f: pr[f] for f in fields if f in pr}
            if comment_fields is not None:
                node["comments"] = {
                    "nodes": [
                        {f: c[f] for f in comment_fields if f in c}
                        for c in pr.get("comments", [])
                    ]
                }
            data[alias] = node
    payload: dict[str, Any] = {"data": {"repository": data}}
    if errors:
        payload["errors"] = errors
        print(json.dumps(payload))
        die("gh: " + " ".join(e["message"] for e in errors))
    print(json.dumps(payload))


def cmd_pr(state: State, args: list[str]) -> None:
    sub, rest = args[0], args[1:]
    positional, flags = parse_flags(rest)
    repo = flags.get("repo", [""])[0]
    # gh accepts [HOST/]OWNER/REPO; the URL is built from OWNER/REPO only.
    if repo.count("/") == 2:
        host, repo = repo.split("/", 1)
        if host != "github.com":
            die(f"fake gh: unexpected host {host!r} in --repo")

    if sub == "view":
        pr = state.find(positional[0])
        if pr is None:
            die(
                f"GraphQL: Could not resolve to a PullRequest with the number of {positional[0]}."
            )
        print(json.dumps(to_json(pr, flags["json"][0])))
        return

    if sub == "list":
        head = flags.get("head", [None])[0]
        want_state = flags.get("state", ["open"])[0].upper()
        found = [
            pr
            for pr in state.prs.values()
            if (head is None or pr["headRefName"] == head)
            and (want_state == "ALL" or pr["state"] == want_state)
        ]
        limit = int(flags.get("limit", ["30"])[0])
        print(json.dumps([to_json(pr, flags["json"][0]) for pr in found[:limit]]))
        return

    if sub == "create":
        pr_create(state, repo, flags)
        return

    if sub == "edit":
        pr_edit(state, positional[0], flags)
        return

    if sub == "ready":
        pr = state.find(positional[0])
        if pr is None:
            die(f"no pull requests found for {positional[0]}")
        pr["isDraft"] = "undo" in flags
        return

    if sub == "close":
        pr = state.find(positional[0])
        if pr is None:
            die(f"no pull requests found for {positional[0]}")
        pr["state"] = "CLOSED"
        return

    die(f"fake gh: unsupported pr subcommand {sub}")


def pr_create(state: State, repo: str, flags: dict[str, list[str]]) -> None:
    base, head = flags["base"][0], flags["head"][0]
    if branch_sha(head) is None:
        die(
            f"pull request create failed: GraphQL: Head ref must be a branch (createPullRequest)\nfake gh: branch '{head}' does not exist on the remote"
        )
    if branch_sha(base) is None:
        die(
            f"pull request create failed: GraphQL: Base ref must be a branch (createPullRequest)\nfake gh: branch '{base}' does not exist on the remote"
        )
    if head_within_base(head, base):
        die(
            f"pull request create failed: GraphQL: No commits between {base} and {head} (createPullRequest)"
        )
    body = (
        sys.stdin.read()
        if flags.get("body-file") == ["-"]
        else flags.get("body", [""])[0]
    )
    number = state.data["next_number"]
    state.data["next_number"] = number + 1
    pr = {
        "number": number,
        "url": f"{HOST}/{repo}/pull/{number}",
        "state": "OPEN",
        "isDraft": "draft" in flags,
        "title": flags["title"][0],
        "body": body,
        "baseRefName": base,
        "headRefName": head,
        "reviewers": flags.get("reviewer", []),
        "comments": [],
    }
    state.prs[str(number)] = pr
    print(pr["url"])


def pr_edit(state: State, ref: str, flags: dict[str, list[str]]) -> None:
    pr = state.find(ref)
    if pr is None:
        die(f"no pull requests found for {ref}")
    if "title" in flags:
        pr["title"] = flags["title"][0]
    if flags.get("body-file") == ["-"]:
        pr["body"] = sys.stdin.read()
    elif "body" in flags:
        pr["body"] = flags["body"][0]
    if "base" in flags:
        if branch_sha(flags["base"][0]) is None:
            die(f"fake gh: base branch '{flags['base'][0]}' does not exist")
        pr["baseRefName"] = flags["base"][0]
    pr.setdefault("edits", 0)
    pr["edits"] += 1
    print(pr["url"])


def post_receive(state: State) -> None:
    """Emulate GitHub closing PRs whose head is fully contained in their base."""
    for pr in state.prs.values():
        if pr["state"] != "OPEN":
            continue
        if branch_sha(pr["headRefName"]) is None:
            pr["state"] = "CLOSED"
            pr["closedBy"] = "head branch deleted"
        elif head_within_base(pr["headRefName"], pr["baseRefName"]):
            pr["state"] = "CLOSED"
            pr["closedBy"] = "auto-close after push"


def maybe_fail(state: State, argv: list[str]) -> None:
    spec = os.environ.get("FAKE_GH_FAIL_ON")
    if not spec:
        return
    name, _, nth = spec.partition(":")
    matching = [c for c in state.data["calls"] if " ".join(c[:2]) == name]
    if " ".join(argv[:2]) == name and len(matching) == int(nth or 1):
        state.save()
        die(f"fake gh: injected failure for '{name}' invocation #{nth}", 42)


def main(argv: list[str]) -> None:
    state = State(Path(os.environ["FAKE_GH_STATE"]))
    if argv[:1] == ["post-receive"]:
        post_receive(state)
        state.save()
        return
    state.data["calls"].append(argv)
    maybe_fail(state, argv)
    try:
        if argv[:1] == ["api"]:
            cmd_api(state, argv[1:])
        elif argv[:1] == ["pr"]:
            cmd_pr(state, argv[1:])
        elif argv[:1] == ["--version"]:
            print("gh version 0.0.0-fake")
        else:
            die(f"fake gh: unsupported command {argv}")
    finally:
        state.save()


if __name__ == "__main__":
    main(sys.argv[1:])
