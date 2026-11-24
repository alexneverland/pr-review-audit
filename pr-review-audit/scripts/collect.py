"""Collect and normalize GitHub PR metadata, commits, files, and review discussions.

Uses the GitHub CLI (`gh api`) with the user's existing authentication.
Read-only by design: performs only GraphQL queries and REST GET calls.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable
from urllib.parse import quote, urlparse


SCHEMA_VERSION = "1.0"
MAX_GRAPHQL_PAGES = 50
MAX_REST_PAGES = 50

_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+$")
_OWNER_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_PR_NUMBER_RE = re.compile(r"^[1-9][0-9]*$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_CODEX_STATE_RE = re.compile(r"<!--\s*codex-review-state:v\d+\s+(\{.*?\})\s*-->", re.DOTALL)
_ALLOWED_PR_SUBPATHS = {"files", "commits", "checks"}

GhRunner = Callable[[list[str], str | None], tuple[int, str, str]]


class GhApiError(RuntimeError):
    """Raised when `gh api` invocation fails or returns an invalid/error response."""


@dataclass(frozen=True, slots=True)
class PRRef:
    """Validated reference to a GitHub Pull Request."""

    host: str
    owner: str
    repo: str
    number: int
    url: str


def _validate_identifier(value: str, pattern: re.Pattern[str], label: str) -> str:
    if (
        not value
        or value in {".", ".."}
        or value.startswith("-")
        or ".." in value
        or not pattern.fullmatch(value)
    ):
        raise ValueError(f"Invalid {label}: {value!r}")
    return value


def parse_pr_url(url: str) -> PRRef:
    """Parse and strictly validate a GitHub or GitHub Enterprise PR URL."""
    if not isinstance(url, str) or not url:
        raise ValueError("PR URL must be a non-empty string.")
    if url.startswith("-"):
        raise ValueError("PR URL must not start with '-'.")
    if re.search(r"\s", url):
        raise ValueError("PR URL must not contain whitespace.")

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"Unsupported URL scheme {parsed.scheme!r}; expected http or https.")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise ValueError("PR URL must not contain credentials.")

    host = parsed.netloc
    if (
        not host
        or host.startswith("-")
        or host.startswith(".")
        or host.endswith(".")
        or ".." in host
        or not _HOST_RE.fullmatch(host)
    ):
        raise ValueError(f"Invalid GitHub host: {host!r}")

    segments = [seg for seg in parsed.path.split("/") if seg != ""]
    if len(segments) < 4 or len(segments) > 5:
        raise ValueError(
            "PR URL path must match /<owner>/<repo>/pull/<number>[/files|commits|checks]."
        )

    owner = _validate_identifier(segments[0], _OWNER_REPO_RE, "repository owner")
    repo = _validate_identifier(segments[1], _OWNER_REPO_RE, "repository name")
    if segments[2] != "pull":
        raise ValueError(f"Expected '/pull/' in PR URL path, got {segments[2]!r}.")
    if not _PR_NUMBER_RE.fullmatch(segments[3]):
        raise ValueError(f"Invalid pull request number: {segments[3]!r}")
    if len(segments) == 5 and segments[4] not in _ALLOWED_PR_SUBPATHS:
        raise ValueError(f"Unsupported pull request URL subpath: {segments[4]!r}")

    number = int(segments[3])
    canonical_url = f"{parsed.scheme}://{host}/{owner}/{repo}/pull/{number}"
    return PRRef(host=host, owner=owner, repo=repo, number=number, url=canonical_url)


def run_gh_api(args: list[str], stdin_data: str | None = None) -> tuple[int, str, str]:
    """Execute `gh api` safely without invoking a shell."""
    cmd = ["gh", "api", *args]
    try:
        completed = subprocess.run(
            cmd,
            input=stdin_data,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GhApiError(
            "GitHub CLI ('gh') was not found on PATH. Install `gh` and run `gh auth login`."
        ) from exc
    except OSError as exc:
        raise GhApiError(f"Failed to execute `gh api`: {exc}") from exc
    return completed.returncode, completed.stdout, completed.stderr


def _format_gh_error(context: str, returncode: int, stderr: str, stdout: str) -> str:
    detail = (stderr or stdout or "").strip()
    lower = detail.lower()
    hint = ""
    if "not logged into" in lower or "401" in lower or "authentication" in lower:
        hint = " Run `gh auth status` or `gh auth login` to verify GitHub authentication."
    elif "403" in lower or "forbidden" in lower or "resource not accessible" in lower:
        hint = " Verify your token/account has read access to this repository."
    elif "404" in lower or "not found" in lower or "could not resolve to a pullrequest" in lower:
        hint = " Verify the repository and pull request exist and are accessible."
    return f"{context} failed (exit {returncode}): {detail or 'no output'}.{hint}"


class GitHubClient:
    """Thin wrapper over `gh api` supporting GraphQL and paginated REST GETs."""

    def __init__(self, runner: GhRunner = run_gh_api) -> None:
        self.runner = runner

    def _host_args(self, host: str) -> list[str]:
        return ["--hostname", host] if host != "github.com" else []

    def graphql(
        self,
        host: str,
        query: str,
        variables: dict[str, Any],
        warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        args = ["graphql", *self._host_args(host), "--input", "-"]
        payload = json.dumps({"query": query, "variables": variables})
        rc, stdout, stderr = self.runner(args, payload)
        if rc != 0:
            raise GhApiError(_format_gh_error("GraphQL request", rc, stderr, stdout))

        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise GhApiError(f"GraphQL response was not valid JSON: {exc}") from exc

        if not isinstance(parsed, dict):
            raise GhApiError("Unexpected GraphQL response type; expected JSON object.")

        errors = parsed.get("errors")
        data = parsed.get("data")
        if errors:
            messages = "; ".join(
                str(err.get("message", err)) if isinstance(err, dict) else str(err)
                for err in (errors if isinstance(errors, list) else [errors])
            )
            if not data:
                raise GhApiError(f"GraphQL API error: {messages}")
            if warnings is not None:
                warnings.append(f"partial_graphql_errors: {messages}")

        if not isinstance(data, dict):
            raise GhApiError("GraphQL response missing `data` object.")
        return data

    def rest_get_json(self, host: str, path: str) -> Any:
        clean_path = path.lstrip("/")
        args = ["-X", "GET", *self._host_args(host), clean_path]
        rc, stdout, stderr = self.runner(args, None)
        if rc != 0:
            raise GhApiError(_format_gh_error(f"REST GET {clean_path}", rc, stderr, stdout))
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise GhApiError(f"REST GET {clean_path} returned invalid JSON: {exc}") from exc

    def rest_get_paginated(
        self,
        host: str,
        path: str,
        per_page: int = 100,
        warnings: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        separator = "&" if "?" in path else "?"
        for page in range(1, MAX_REST_PAGES + 1):
            paged_path = f"{path}{separator}per_page={per_page}&page={page}"
            chunk = self.rest_get_json(host, paged_path)
            if not isinstance(chunk, list):
                raise GhApiError(
                    f"Expected JSON array from paginated endpoint {path}, got {type(chunk).__name__}."
                )
            results.extend(item for item in chunk if isinstance(item, dict))
            if len(chunk) < per_page:
                break
        else:
            if warnings is not None:
                warnings.append(
                    f"rest_pagination_cap_reached: {path} reached {MAX_REST_PAGES} pages."
                )
        return results


_PR_META_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      title
      url
      state
      isDraft
      baseRefName
      headRefName
      baseRefOid
      headRefOid
      updatedAt
      author { login }
    }
  }
}
"""

_PR_COMMITS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $endCursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      commits(first: 100, after: $endCursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          commit {
            oid
            committedDate
            messageHeadline
            author {
              name
              user { login }
            }
          }
        }
      }
    }
  }
}
"""

_PR_THREADS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $endCursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $endCursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          isOutdated
          isCollapsed
          path
          line
          originalLine
          startLine
          originalStartLine
          diffSide
          resolvedBy { login }
          comments(first: 100) {
            pageInfo { hasNextPage endCursor }
            nodes {
              id
              databaseId
              url
              body
              createdAt
              updatedAt
              outdated
              diffHunk
              author { login }
              commit { oid }
              originalCommit { oid }
            }
          }
        }
      }
    }
  }
}
"""

_THREAD_COMMENTS_QUERY = """
query($threadId: ID!, $endCursor: String) {
  node(id: $threadId) {
    ... on PullRequestReviewThread {
      comments(first: 100, after: $endCursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          databaseId
          url
          body
          createdAt
          updatedAt
          outdated
          diffHunk
          author { login }
          commit { oid }
          originalCommit { oid }
        }
      }
    }
  }
}
"""

_PR_REVIEWS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $endCursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviews(first: 100, after: $endCursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          url
          state
          body
          submittedAt
          author { login }
          commit { oid }
        }
      }
    }
  }
}
"""

_PR_ISSUE_COMMENTS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $endCursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      comments(first: 100, after: $endCursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          url
          body
          createdAt
          updatedAt
          author { login }
        }
      }
    }
  }
}
"""


def _extract_pr_node(data: dict[str, Any], pr_ref: PRRef) -> dict[str, Any]:
    repo = data.get("repository")
    if not isinstance(repo, dict):
        raise GhApiError(
            f"Repository {pr_ref.owner}/{pr_ref.repo} could not be resolved or is inaccessible."
        )
    pr = repo.get("pullRequest")
    if not isinstance(pr, dict):
        raise GhApiError(
            f"Pull request #{pr_ref.number} in {pr_ref.owner}/{pr_ref.repo} could not be resolved."
        )
    return pr


def _paginate_pr_connection(
    client: GitHubClient,
    pr_ref: PRRef,
    query: str,
    connection_key: str,
    warnings: list[str],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(MAX_GRAPHQL_PAGES):
        data = client.graphql(
            pr_ref.host,
            query,
            {
                "owner": pr_ref.owner,
                "repo": pr_ref.repo,
                "number": pr_ref.number,
                "endCursor": cursor,
            },
            warnings=warnings,
        )
        pr = _extract_pr_node(data, pr_ref)
        conn = pr.get(connection_key) or {}
        nodes = conn.get("nodes") or []
        items.extend(node for node in nodes if isinstance(node, dict))
        page_info = conn.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        next_cursor = page_info.get("endCursor")
        if not next_cursor or next_cursor == cursor:
            warnings.append(f"graphql_cursor_stalled: {connection_key}")
            break
        cursor = next_cursor
    else:
        warnings.append(f"graphql_pagination_cap_reached: {connection_key}")
    return items


def _paginate_thread_comments(
    client: GitHubClient,
    host: str,
    thread: dict[str, Any],
    warnings: list[str],
) -> list[dict[str, Any]]:
    comments_conn = thread.get("comments") or {}
    nodes = [n for n in (comments_conn.get("nodes") or []) if isinstance(n, dict)]
    page_info = comments_conn.get("pageInfo") or {}
    thread_id = thread.get("id")
    cursor = page_info.get("endCursor")
    has_next = bool(page_info.get("hasNextPage"))

    pages = 0
    while has_next and thread_id and cursor and pages < MAX_GRAPHQL_PAGES:
        pages += 1
        data = client.graphql(
            host,
            _THREAD_COMMENTS_QUERY,
            {"threadId": thread_id, "endCursor": cursor},
            warnings=warnings,
        )
        node = data.get("node") or {}
        next_conn = node.get("comments") or {}
        nodes.extend(n for n in (next_conn.get("nodes") or []) if isinstance(n, dict))
        next_info = next_conn.get("pageInfo") or {}
        has_next = bool(next_info.get("hasNextPage"))
        next_cursor = next_info.get("endCursor")
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    if has_next:
        warnings.append(f"thread_comments_incomplete: {thread_id}")
    return nodes


def _login(actor: Any) -> str | None:
    if isinstance(actor, dict):
        login = actor.get("login")
        if isinstance(login, str) and login.strip():
            return login.strip()
    return None


def _oid(commit_obj: Any) -> str | None:
    if isinstance(commit_obj, dict):
        oid = commit_obj.get("oid")
        if isinstance(oid, str) and oid.strip():
            return oid.strip()
    return None


def _normalize_comment(raw: dict[str, Any]) -> dict[str, Any]:
    author = _login(raw.get("author")) or "<unknown>"
    return {
        "id": raw.get("id"),
        "database_id": raw.get("databaseId"),
        "url": raw.get("url"),
        "author": author,
        "body": raw.get("body") or "",
        "created_at": raw.get("createdAt"),
        "updated_at": raw.get("updatedAt"),
        "commit_sha": _oid(raw.get("commit")),
        "original_commit_sha": _oid(raw.get("originalCommit")),
    }


def _normalize_thread(
    client: GitHubClient,
    host: str,
    thread: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any]:
    raw_comments = _paginate_thread_comments(client, host, thread, warnings)
    comments = [_normalize_comment(c) for c in raw_comments]
    first = comments[0] if comments else {}
    first_raw = raw_comments[0] if raw_comments else {}

    author = first.get("author") or "<unknown>"
    commit_sha = first.get("commit_sha")
    original_commit_sha = first.get("original_commit_sha")
    path = thread.get("path")
    line = thread.get("line")
    original_line = thread.get("originalLine")

    item_warnings: list[str] = []
    if not comments:
        item_warnings.append("empty_thread_comments")
    if author == "<unknown>":
        item_warnings.append("missing_author")
    if not commit_sha and not original_commit_sha:
        item_warnings.append("missing_commit_sha")
    if not path:
        item_warnings.append("missing_path")
    if line is None and original_line is None:
        item_warnings.append("missing_line")

    return {
        "id": thread.get("id"),
        "source_type": "review_thread",
        "url": first.get("url"),
        "author": author,
        "created_at": first.get("created_at"),
        "review_state": None,
        "path": path,
        "line": line,
        "original_line": original_line,
        "start_line": thread.get("startLine"),
        "original_start_line": thread.get("originalStartLine"),
        "diff_side": thread.get("diffSide"),
        "commit_sha": commit_sha,
        "original_commit_sha": original_commit_sha,
        "is_resolved": thread.get("isResolved"),
        "is_outdated": thread.get("isOutdated"),
        "resolved_by": _login(thread.get("resolvedBy")),
        "diff_hunk": first_raw.get("diffHunk"),
        "comments": comments,
        "metadata_warnings": item_warnings,
    }


def _normalize_review_summary(review: dict[str, Any]) -> dict[str, Any] | None:
    body = (review.get("body") or "").strip()
    if not body:
        return None
    author = _login(review.get("author")) or "<unknown>"
    commit_sha = _oid(review.get("commit"))
    item_warnings: list[str] = []
    if author == "<unknown>":
        item_warnings.append("missing_author")
    if not commit_sha:
        item_warnings.append("missing_commit_sha")

    return {
        "id": review.get("id"),
        "source_type": "review_summary",
        "url": review.get("url"),
        "author": author,
        "created_at": review.get("submittedAt"),
        "review_state": review.get("state"),
        "path": None,
        "line": None,
        "original_line": None,
        "start_line": None,
        "original_start_line": None,
        "diff_side": None,
        "commit_sha": commit_sha,
        "original_commit_sha": commit_sha,
        "is_resolved": None,
        "is_outdated": None,
        "resolved_by": None,
        "diff_hunk": None,
        "comments": [
            {
                "id": review.get("id"),
                "database_id": None,
                "url": review.get("url"),
                "author": author,
                "body": body,
                "created_at": review.get("submittedAt"),
                "updated_at": None,
                "commit_sha": commit_sha,
                "original_commit_sha": commit_sha,
            }
        ],
        "metadata_warnings": item_warnings,
    }


def _extract_codex_commit_sha(body: str) -> str | None:
    """Extract `code_review.commit_sha` from embedded Codex PR summary metadata."""
    match = _CODEX_STATE_RE.search(body)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(1))
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    code_review = parsed.get("code_review")
    if not isinstance(code_review, dict):
        return None
    sha = code_review.get("commit_sha")
    if isinstance(sha, str):
        candidate = sha.strip()
        if _SHA_RE.fullmatch(candidate):
            return candidate
    return None


def _normalize_issue_comment(comment: dict[str, Any]) -> dict[str, Any] | None:
    body = (comment.get("body") or "").strip()
    if not body:
        return None
    author = _login(comment.get("author")) or "<unknown>"
    codex_sha = _extract_codex_commit_sha(body)
    item_warnings: list[str] = []
    if not codex_sha:
        item_warnings.append("missing_commit_sha")
    if author == "<unknown>":
        item_warnings.append("missing_author")

    normalized_comment = _normalize_comment(comment)
    if codex_sha:
        normalized_comment["commit_sha"] = codex_sha
        normalized_comment["original_commit_sha"] = codex_sha

    return {
        "id": comment.get("id"),
        "source_type": "issue_comment",
        "url": comment.get("url"),
        "author": author,
        "created_at": comment.get("createdAt"),
        "review_state": None,
        "path": None,
        "line": None,
        "original_line": None,
        "start_line": None,
        "original_start_line": None,
        "diff_side": None,
        "commit_sha": codex_sha,
        "original_commit_sha": codex_sha,
        "is_resolved": None,
        "is_outdated": None,
        "resolved_by": None,
        "diff_hunk": None,
        "comments": [normalized_comment],
        "metadata_warnings": item_warnings,
    }


def collect_pr_data(pr_url: str, runner: GhRunner = run_gh_api) -> dict[str, Any]:
    """Collect PR metadata, commits, files, review threads, reviews, and issue comments."""
    pr_ref = parse_pr_url(pr_url)
    client = GitHubClient(runner=runner)
    warnings: list[str] = []

    meta_data = client.graphql(
        pr_ref.host,
        _PR_META_QUERY,
        {"owner": pr_ref.owner, "repo": pr_ref.repo, "number": pr_ref.number},
        warnings=warnings,
    )
    pr_meta = _extract_pr_node(meta_data, pr_ref)

    raw_commits = _paginate_pr_connection(client, pr_ref, _PR_COMMITS_QUERY, "commits", warnings)
    raw_threads = _paginate_pr_connection(
        client, pr_ref, _PR_THREADS_QUERY, "reviewThreads", warnings
    )
    raw_reviews = _paginate_pr_connection(client, pr_ref, _PR_REVIEWS_QUERY, "reviews", warnings)
    raw_issue_comments = _paginate_pr_connection(
        client, pr_ref, _PR_ISSUE_COMMENTS_QUERY, "comments", warnings
    )

    files_endpoint = (
        f"repos/{quote(pr_ref.owner, safe='')}/{quote(pr_ref.repo, safe='')}"
        f"/pulls/{pr_ref.number}/files"
    )
    raw_files = client.rest_get_paginated(pr_ref.host, files_endpoint, warnings=warnings)

    commits: list[dict[str, Any]] = []
    for entry in raw_commits:
        c = entry.get("commit") if isinstance(entry.get("commit"), dict) else entry
        if not isinstance(c, dict):
            continue
        sha = c.get("oid")
        author_obj = c.get("author") or {}
        author = _login(author_obj.get("user")) or author_obj.get("name") or "<unknown>"
        commits.append(
            {
                "sha": sha,
                "short_sha": sha[:7] if isinstance(sha, str) else None,
                "committed_at": c.get("committedDate"),
                "message_headline": c.get("messageHeadline") or "",
                "author": author,
            }
        )

    files: list[dict[str, Any]] = []
    for f in raw_files:
        patch = f.get("patch")
        filename = f.get("filename")
        if patch is None and f.get("status") not in {"removed"} and f.get("changes", 0) > 0:
            warnings.append(f"missing_patch_for_file: {filename}")
        files.append(
            {
                "filename": filename,
                "previous_filename": f.get("previous_filename"),
                "status": f.get("status"),
                "additions": f.get("additions", 0),
                "deletions": f.get("deletions", 0),
                "changes": f.get("changes", 0),
                "patch": patch,
            }
        )

    review_items: list[dict[str, Any]] = []
    for thread in raw_threads:
        review_items.append(_normalize_thread(client, pr_ref.host, thread, warnings))
    for review in raw_reviews:
        normalized = _normalize_review_summary(review)
        if normalized is not None:
            review_items.append(normalized)
    for comment in raw_issue_comments:
        normalized = _normalize_issue_comment(comment)
        if normalized is not None:
            review_items.append(normalized)

    head_sha = pr_meta.get("headRefOid")
    if not head_sha:
        warnings.append("missing_pr_head_sha")

    return {
        "schema_version": SCHEMA_VERSION,
        "pr": {
            "host": pr_ref.host,
            "owner": pr_ref.owner,
            "repo": pr_ref.repo,
            "number": pr_ref.number,
            "url": pr_meta.get("url") or pr_ref.url,
            "title": pr_meta.get("title") or "",
            "state": pr_meta.get("state") or "UNKNOWN",
            "is_draft": bool(pr_meta.get("isDraft", False)),
            "author": _login(pr_meta.get("author")) or "<unknown>",
            "base_ref": pr_meta.get("baseRefName") or "",
            "head_ref": pr_meta.get("headRefName") or "",
            "base_sha": pr_meta.get("baseRefOid"),
            "head_sha": head_sha,
            "updated_at": pr_meta.get("updatedAt"),
            "collected_at": datetime.now(timezone.utc).isoformat(),
        },
        "commits": commits,
        "files": files,
        "review_items": review_items,
        "warnings": warnings,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect GitHub PR review discussions, commits, and files via `gh api`."
    )
    parser.add_argument("pr_url", help="GitHub Pull Request URL (e.g. https://github.com/org/repo/pull/42)")
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Output JSON file path (writes to stdout if omitted).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        result = collect_pr_data(args.pr_url)
    except (ValueError, GhApiError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    encoded = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    else:
        sys.stdout.write(encoded + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
