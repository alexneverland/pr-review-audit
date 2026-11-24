"""Automated tests for PR Review Audit using mocked GitHub CLI responses."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import collect  # noqa: E402
import compare  # noqa: E402
import report  # noqa: E402


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _git(repo_dir: Path, *args: str, stdin: str | None = None) -> str:
    proc = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-C", str(repo_dir), *args],
        capture_output=True,
        input=stdin,
        text=True,
        encoding="utf-8",
        shell=False,
        check=True,
    )
    return proc.stdout.strip()


def _init_git_repo(repo_dir: Path, files: dict[str, str]) -> str:
    repo_dir.mkdir(parents=True, exist_ok=True)
    _git(repo_dir, "init")
    _git(repo_dir, "config", "user.name", "Test User")
    _git(repo_dir, "config", "user.email", "test@example.com")
    for rel_path, content in files.items():
        target = repo_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(repo_dir, "add", ".")
    _git(repo_dir, "commit", "-m", "Initial fixture commit")
    return _git(repo_dir, "rev-parse", "HEAD")


class MockGhRouter:
    """Deterministic fake `gh api` runner routing GraphQL and REST calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.graphql_handlers: list[tuple[str, object]] = []
        self.rest_routes: dict[str, tuple[int, str, str]] = {}

    def on_graphql(self, marker: str, response_or_fn: object) -> None:
        self.graphql_handlers.append((marker, response_or_fn))

    def on_rest(self, path_prefix: str, payload: object, rc: int = 0, stderr: str = "") -> None:
        stdout = payload if isinstance(payload, str) else json.dumps(payload)
        self.rest_routes[path_prefix] = (rc, stdout, stderr)

    def __call__(self, args: list[str], stdin_data: str | None = None) -> tuple[int, str, str]:
        self.calls.append((args, stdin_data))
        if args and args[0] == "graphql":
            body = json.loads(stdin_data or "{}")
            query = body.get("query", "")
            variables = body.get("variables", {})
            for marker, handler in self.graphql_handlers:
                if marker in query:
                    result = handler(variables) if callable(handler) else handler
                    if isinstance(result, tuple):
                        return result
                    return 0, json.dumps(result), ""
            return 1, "", f"Unexpected GraphQL query: {query[:80]}"

        endpoint = args[-1]
        if endpoint in self.rest_routes:
            return self.rest_routes[endpoint]
        for key, resp in self.rest_routes.items():
            if endpoint.startswith(key):
                return resp
        return 1, "", f"404 Not Found: {endpoint}"


def _base_graphql_routes(
    router: MockGhRouter,
    *,
    head_sha: str = "head1111",
    commits: list[dict] | None = None,
    threads: list[dict] | None = None,
    reviews: list[dict] | None = None,
    issue_comments: list[dict] | None = None,
) -> None:
    commits = commits if commits is not None else [
        {
            "commit": {
                "oid": head_sha,
                "committedDate": "2026-11-20T10:00:00Z",
                "messageHeadline": "Latest commit",
                "author": {"name": "Alice", "user": {"login": "alice"}},
            }
        }
    ]
    router.on_graphql(
        "baseRefOid",
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "title": "Harden auth & refactor helpers",
                        "url": "https://github.com/acme/app/pull/42",
                        "state": "OPEN",
                        "isDraft": False,
                        "baseRefName": "main",
                        "headRefName": "feat/auth",
                        "baseRefOid": "base0000",
                        "headRefOid": head_sha,
                        "updatedAt": "2026-11-20T12:00:00Z",
                        "author": {"login": "alice"},
                    }
                }
            }
        },
    )
    router.on_graphql(
        "commits(first: 100",
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "commits": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": commits,
                        }
                    }
                }
            }
        },
    )
    router.on_graphql(
        "reviewThreads(first: 100",
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": threads or [],
                        }
                    }
                }
            }
        },
    )
    router.on_graphql(
        "reviews(first: 100",
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviews": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": reviews or [],
                        }
                    }
                }
            }
        },
    )
    router.on_graphql(
        "pullRequest(number: $number) {\n      comments(first: 100, after: $endCursor)",
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "comments": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": issue_comments or [],
                        }
                    }
                }
            }
        },
    )


class TestUrlParsingAndSecurity(unittest.TestCase):
    def test_valid_pr_urls(self) -> None:
        ref = collect.parse_pr_url("https://github.com/octocat/hello-world/pull/123")
        self.assertEqual(ref.host, "github.com")
        self.assertEqual(ref.owner, "octocat")
        self.assertEqual(ref.repo, "hello-world")
        self.assertEqual(ref.number, 123)

        sub = collect.parse_pr_url("https://ghe.internal.corp/org/my.repo/pull/77/files?w=1#diff")
        self.assertEqual(sub.host, "ghe.internal.corp")
        self.assertEqual(sub.url, "https://ghe.internal.corp/org/my.repo/pull/77")

    def test_rejects_malicious_or_invalid_urls(self) -> None:
        bad_urls = [
            "",
            "-X DELETE",
            "https://github.com/org/repo/pull/1;rm -rf /",
            "https://github.com/org/repo/pull/1 --flag",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "https://user:pass@github.com/org/repo/pull/1",
            "https://github.com/org/repo/issues/1",
            "https://github.com/../repo/pull/1",
            "https://github.com/org/repo/pull/0",
            "https://github.com/org/repo/pull/abc",
        ]
        for bad in bad_urls:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    collect.parse_pr_url(bad)

    def test_repo_dir_blocks_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            secret = Path(tmp) / "secret.txt"
            secret.write_text("TOP_SECRET", encoding="utf-8")

            warnings: list[str] = []
            self.assertIsNone(compare._read_local_repo_file(root, "../secret.txt", warnings))
            self.assertIsNone(compare._read_local_repo_file(root, "/etc/passwd", warnings))
            self.assertTrue(any("unsafe" in w or "traversal" in w for w in warnings))


class TestPaginationAndErrors(unittest.TestCase):
    def test_graphql_and_rest_pagination_and_ghe_hostname(self) -> None:
        router = MockGhRouter()
        _base_graphql_routes(router, head_sha="head999")

        def thread_handler(variables: dict) -> dict:
            cursor = variables.get("endCursor")
            if cursor is None:
                return {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                                    "nodes": [
                                        {
                                            "id": "T1",
                                            "isResolved": False,
                                            "isOutdated": False,
                                            "path": "a.py",
                                            "line": 10,
                                            "originalLine": 10,
                                            "diffSide": "RIGHT",
                                            "resolvedBy": None,
                                            "comments": {
                                                "pageInfo": {"hasNextPage": True, "endCursor": "tc1"},
                                                "nodes": [
                                                    {
                                                        "id": "C1",
                                                        "url": "https://ghe.example.com/o/r/pull/5#r1",
                                                        "body": "Fix null check",
                                                        "createdAt": "2026-11-20T09:00:00Z",
                                                        "author": {"login": "rev1"},
                                                        "commit": {"oid": "head999"},
                                                        "originalCommit": {"oid": "head999"},
                                                        "diffHunk": "@@ -8,4 +8,4 @@\n+x = user.name",
                                                    }
                                                ],
                                            },
                                        }
                                    ],
                                }
                            }
                        }
                    }
                }
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [
                                    {
                                        "id": "T2",
                                        "isResolved": True,
                                        "isOutdated": False,
                                        "path": "b.py",
                                        "line": 5,
                                        "originalLine": 5,
                                        "diffSide": "RIGHT",
                                        "resolvedBy": {"login": "alice"},
                                        "comments": {
                                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                                            "nodes": [
                                                {
                                                    "id": "C3",
                                                    "url": "https://ghe.example.com/o/r/pull/5#r3",
                                                    "body": "Looks good after fix",
                                                    "createdAt": "2026-11-20T09:30:00Z",
                                                    "author": {"login": "rev2"},
                                                    "commit": {"oid": "head999"},
                                                    "originalCommit": {"oid": "head999"},
                                                    "diffHunk": "@@ -1,3 +1,3 @@\n+return True",
                                                }
                                            ],
                                        },
                                    }
                                ],
                            }
                        }
                    }
                }
            }

        router.graphql_handlers = [
            (m, h) for m, h in router.graphql_handlers if "reviewThreads" not in m
        ]
        router.on_graphql("reviewThreads(first: 100", thread_handler)
        router.on_graphql(
            "... on PullRequestReviewThread",
            {
                "data": {
                    "node": {
                        "comments": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [
                                {
                                    "id": "C2",
                                    "url": "https://ghe.example.com/o/r/pull/5#r2",
                                    "body": "Follow-up reply",
                                    "createdAt": "2026-11-20T09:10:00Z",
                                    "author": {"login": "alice"},
                                    "commit": {"oid": "head999"},
                                    "originalCommit": {"oid": "head999"},
                                    "diffHunk": "",
                                }
                            ],
                        }
                    }
                }
            },
        )

        page1_files = [{"filename": f"f{i}.py", "status": "modified", "changes": 1, "patch": "+a"} for i in range(100)]
        page2_files = [{"filename": "b.py", "status": "modified", "changes": 1, "patch": "+b"}]
        router.on_rest("repos/o/r/pulls/5/files?per_page=100&page=1", page1_files)
        router.on_rest("repos/o/r/pulls/5/files?per_page=100&page=2", page2_files)

        collected = collect.collect_pr_data("https://ghe.example.com/o/r/pull/5", runner=router)
        self.assertEqual(len(collected["files"]), 101)
        self.assertEqual(len(collected["review_items"]), 2)
        self.assertEqual(len(collected["review_items"][0]["comments"]), 2)
        self.assertTrue(any("--hostname" in args and "ghe.example.com" in args for args, _ in router.calls))

    def test_graphql_and_auth_errors_raise_clean_gh_api_error(self) -> None:
        def auth_fail(_args: list[str], _stdin: str | None = None) -> tuple[int, str, str]:
            return 1, "", "HTTP 401: Bad credentials (not logged into host)"

        with self.assertRaises(collect.GhApiError) as ctx:
            collect.collect_pr_data("https://github.com/acme/private/pull/1", runner=auth_fail)
        self.assertIn("gh auth", str(ctx.exception))

        def gql_err(_args: list[str], _stdin: str | None = None) -> tuple[int, str, str]:
            return 0, json.dumps({"errors": [{"message": "Could not resolve to a PullRequest"}]}), ""

        with self.assertRaises(collect.GhApiError) as ctx2:
            collect.collect_pr_data("https://github.com/acme/private/pull/999", runner=gql_err)
        self.assertIn("Could not resolve", str(ctx2.exception))


class TestAuditScenarios(unittest.TestCase):
    def test_scenario_1_resolved_and_genuinely_fixed(self) -> None:
        router = MockGhRouter()
        _base_graphql_routes(
            router,
            head_sha="sha_head",
            threads=[
                {
                    "id": "T_FIXED",
                    "isResolved": True,
                    "isOutdated": False,
                    "path": "src/auth.py",
                    "line": 12,
                    "originalLine": 12,
                    "diffSide": "RIGHT",
                    "resolvedBy": {"login": "alice"},
                    "comments": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            {
                                "id": "C1",
                                "url": "https://github.com/acme/app/pull/42#discussion_r1",
                                "body": "Use hmac.compare_digest instead of ==.",
                                "createdAt": "2026-11-20T10:00:00Z",
                                "author": {"login": "bob"},
                                "commit": {"oid": "sha_head"},
                                "originalCommit": {"oid": "sha_head"},
                                "diffHunk": "@@ -10,3 +10,3 @@\n+return sig == expected",
                            }
                        ],
                    },
                }
            ],
        )
        router.on_rest(
            "repos/acme/app/pulls/42/files?per_page=100&page=1",
            [{"filename": "src/auth.py", "status": "modified", "changes": 2, "patch": "+hmac.compare_digest"}],
        )
        fixed_src = "import hmac\n\ndef verify(sig, expected):\n" + "\n" * 8 + "    return hmac.compare_digest(sig, expected)\n"
        router.on_rest(
            "repos/acme/app/contents/src/auth.py?ref=sha_head",
            {"type": "file", "encoding": "base64", "size": len(fixed_src), "content": _b64(fixed_src)},
        )

        collected = collect.collect_pr_data("https://github.com/acme/app/pull/42", runner=router)
        context = compare.build_audit_context(collected, fetch_diffs=True, fetch_files=True, runner=router)
        self.assertEqual(context["items"][0]["freshness"]["status"], "CURRENT")
        self.assertIn("hmac.compare_digest", context["items"][0]["current_code_excerpt"]["snippet"])

        md = report.render_markdown_report(
            context,
            {
                "findings": [
                    {
                        "item_id": "T_FIXED",
                        "status": "FIXED",
                        "issue_summary": "Use constant-time comparison for signatures.",
                        "evidence": "Line 12 in HEAD now calls `hmac.compare_digest(sig, expected)`.",
                        "code_references": ["src/auth.py:12"],
                    }
                ]
            },
        )
        self.assertIn("### 1. [FIXED] `src/auth.py`", md)
        self.assertNotIn("Resolved Threads Still Unfixed", md)

    def test_scenario_2_resolved_but_issue_still_present(self) -> None:
        router = MockGhRouter()
        _base_graphql_routes(
            router,
            head_sha="sha_head",
            threads=[
                {
                    "id": "T_FALSE_RESOLVED",
                    "isResolved": True,
                    "isOutdated": True,
                    "path": "src/auth.py",
                    "line": None,
                    "originalLine": 10,
                    "diffSide": "RIGHT",
                    "resolvedBy": {"login": "alice"},
                    "comments": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            {
                                "id": "C2",
                                "url": "https://github.com/acme/app/pull/42#discussion_r2",
                                "body": "Token comparison uses == instead of hmac.compare_digest.",
                                "createdAt": "2026-11-20T10:00:00Z",
                                "author": {"login": "sec-bot"},
                                "commit": {"oid": "sha_head"},
                                "originalCommit": {"oid": "sha_head"},
                                "diffHunk": "@@ -8,3 +8,3 @@\n+    return sig == expected",
                            }
                        ],
                    },
                }
            ],
        )
        router.on_rest(
            "repos/acme/app/pulls/42/files?per_page=100&page=1",
            [{"filename": "src/auth.py", "status": "modified", "changes": 4, "patch": "+    return sig.strip() == expected"}],
        )
        still_buggy = "def verify(sig, expected):\n    sig = sig.strip()\n    return sig == expected\n"
        router.on_rest(
            "repos/acme/app/contents/src/auth.py?ref=sha_head",
            {"type": "file", "encoding": "base64", "size": len(still_buggy), "content": _b64(still_buggy)},
        )

        collected = collect.collect_pr_data("https://github.com/acme/app/pull/42", runner=router)
        context = compare.build_audit_context(collected, fetch_files=True, runner=router)
        md = report.render_markdown_report(
            context,
            {
                "findings": [
                    {
                        "item_id": "T_FALSE_RESOLVED",
                        "status": "STILL_PRESENT",
                        "issue_summary": "Token comparison uses `==` instead of constant-time comparison.",
                        "evidence": "Author added `.strip()` and marked thread resolved, but line 3 in HEAD still uses `sig == expected`.",
                        "code_references": ["src/auth.py:3"],
                    }
                ]
            },
        )
        self.assertIn("WARNING — Resolved Threads Still Unfixed", md)
        self.assertIn("Resolved-Thread Discrepancy", md)
        self.assertIn("### 1. [STILL_PRESENT] `src/auth.py`", md)
        self.assertIn("src/auth.py:3", md)

    def test_scenario_3_partially_fixed(self) -> None:
        context = {
            "pr": {"owner": "acme", "repo": "app", "number": 42, "head_sha": "abcdef1", "title": "Fix cache"},
            "items": [
                {
                    "id": "T_PARTIAL",
                    "source_type": "review_thread",
                    "url": "https://github.com/acme/app/pull/42#discussion_r3",
                    "author": "reviewer",
                    "path": "src/cache.py",
                    "is_resolved": False,
                    "is_outdated": False,
                    "freshness": {"status": "CURRENT", "reason": "Matches HEAD."},
                    "location": {"status": "modified", "original_path": "src/cache.py", "current_path": "src/cache.py"},
                }
            ],
            "warnings": [],
        }
        md = report.render_markdown_report(
            context,
            {
                "findings": [
                    {
                        "item_id": "T_PARTIAL",
                        "status": "PARTIALLY_FIXED",
                        "issue_summary": "Add TTL and payload hash check.",
                        "evidence": "TTL added on line 20, but payload hash validation is still absent.",
                        "code_references": ["src/cache.py:20", "src/cache.py:31"],
                    }
                ]
            },
        )
        self.assertIn("| `PARTIALLY_FIXED` | 1 |", md)
        self.assertIn("## Findings Requiring Human Verification", md)
        self.assertIn("**[PARTIALLY_FIXED]**", md)

    def test_scenario_4_review_freshness_older_unaffected_affected_and_rebased(self) -> None:
        router = MockGhRouter()
        _base_graphql_routes(
            router,
            head_sha="3333cccc3333cccc3333cccc3333cccc3333cccc",
            commits=[
                {"commit": {"oid": "1111aaaa1111aaaa1111aaaa1111aaaa1111aaaa", "committedDate": "2026-11-20T09:00:00Z", "messageHeadline": "Init", "author": {"user": {"login": "alice"}}}},
                {"commit": {"oid": "2222bbbb2222bbbb2222bbbb2222bbbb2222bbbb", "committedDate": "2026-11-20T10:00:00Z", "messageHeadline": "Touch a.py", "author": {"user": {"login": "alice"}}}},
                {"commit": {"oid": "3333cccc3333cccc3333cccc3333cccc3333cccc", "committedDate": "2026-11-20T11:00:00Z", "messageHeadline": "Touch docs", "author": {"user": {"login": "alice"}}}},
            ],
            threads=[
                {
                    "id": "T_AFFECTED",
                    "isResolved": False,
                    "isOutdated": False,
                    "path": "a.py",
                    "line": 4,
                    "originalLine": 4,
                    "diffSide": "RIGHT",
                    "comments": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [{"id": "CA", "url": "https://github.com/acme/app/pull/42#r1", "body": "Check a.py", "createdAt": "2026-11-20T09:05:00Z", "author": {"login": "rev"}, "commit": {"oid": "1111aaaa1111aaaa1111aaaa1111aaaa1111aaaa"}, "originalCommit": {"oid": "1111aaaa1111aaaa1111aaaa1111aaaa1111aaaa"}, "diffHunk": ""}]},
                },
                {
                    "id": "T_UNAFFECTED",
                    "isResolved": False,
                    "isOutdated": False,
                    "path": "b.py",
                    "line": 8,
                    "originalLine": 8,
                    "diffSide": "RIGHT",
                    "comments": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [{"id": "CB", "url": "https://github.com/acme/app/pull/42#r2", "body": "Check b.py", "createdAt": "2026-11-20T10:05:00Z", "author": {"login": "rev"}, "commit": {"oid": "2222bbbb2222bbbb2222bbbb2222bbbb2222bbbb"}, "originalCommit": {"oid": "2222bbbb2222bbbb2222bbbb2222bbbb2222bbbb"}, "diffHunk": ""}]},
                },
                {
                    "id": "T_REBASED",
                    "isResolved": False,
                    "isOutdated": True,
                    "path": "b.py",
                    "line": 8,
                    "originalLine": 8,
                    "diffSide": "RIGHT",
                    "comments": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [{"id": "CR", "url": "https://github.com/acme/app/pull/42#r3", "body": "Old force-pushed review", "createdAt": "2026-11-20T08:00:00Z", "author": {"login": "rev"}, "commit": {"oid": "deadbeef1234deadbeef1234deadbeef1234dead"}, "originalCommit": {"oid": "deadbeef1234deadbeef1234deadbeef1234dead"}, "diffHunk": ""}]},
                },
            ],
        )
        router.on_rest(
            "repos/acme/app/pulls/42/files?per_page=100&page=1",
            [
                {"filename": "a.py", "status": "modified", "changes": 1, "patch": "+x"},
                {"filename": "b.py", "status": "modified", "changes": 1, "patch": "+y"},
                {"filename": "README.md", "status": "modified", "changes": 1, "patch": "+doc"},
            ],
        )
        router.on_rest(
            "repos/acme/app/compare/1111aaaa1111aaaa1111aaaa1111aaaa1111aaaa...3333cccc3333cccc3333cccc3333cccc3333cccc",
            {"files": [{"filename": "a.py", "status": "modified", "changes": 3}, {"filename": "README.md", "status": "modified", "changes": 1}]},
        )
        router.on_rest(
            "repos/acme/app/compare/2222bbbb2222bbbb2222bbbb2222bbbb2222bbbb...3333cccc3333cccc3333cccc3333cccc3333cccc",
            {"files": [{"filename": "README.md", "status": "modified", "changes": 1}]},
        )

        collected = collect.collect_pr_data("https://github.com/acme/app/pull/42", runner=router)
        context = compare.build_audit_context(collected, fetch_diffs=True, runner=router)
        by_id = {item["id"]: item["freshness"] for item in context["items"]}

        self.assertEqual(by_id["T_AFFECTED"]["status"], "OLDER_AFFECTED")
        self.assertEqual(by_id["T_AFFECTED"]["commits_behind"], 2)
        self.assertTrue(by_id["T_AFFECTED"]["affected_by_later_commits"])

        self.assertEqual(by_id["T_UNAFFECTED"]["status"], "OLDER_UNAFFECTED")
        self.assertEqual(by_id["T_UNAFFECTED"]["commits_behind"], 1)
        self.assertFalse(by_id["T_UNAFFECTED"]["affected_by_later_commits"])

        self.assertEqual(by_id["T_REBASED"]["status"], "COMMIT_NOT_IN_HISTORY")
        self.assertIsNone(by_id["T_REBASED"]["commits_behind"])

    def test_scenario_5_renamed_file_and_cross_file_moved_code(self) -> None:
        router = MockGhRouter()
        _base_graphql_routes(
            router,
            head_sha="head_move",
            threads=[
                {
                    "id": "T_RENAMED",
                    "isResolved": False,
                    "isOutdated": True,
                    "path": "src/old_name.py",
                    "line": None,
                    "originalLine": 5,
                    "diffSide": "RIGHT",
                    "comments": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [{"id": "C1", "url": "https://github.com/acme/app/pull/42#r1", "body": "Check timeout", "createdAt": "2026-11-20T10:00:00Z", "author": {"login": "rev"}, "commit": {"oid": "head_move"}, "originalCommit": {"oid": "head_move"}, "diffHunk": "@@ -1,3 +1,3 @@\n+def connect_gateway(timeout=30):"}]},
                },
                {
                    "id": "T_EXTRACTED",
                    "isResolved": False,
                    "isOutdated": True,
                    "path": "src/legacy_module.py",
                    "line": None,
                    "originalLine": 12,
                    "diffSide": "RIGHT",
                    "comments": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [{"id": "C2", "url": "https://github.com/acme/app/pull/42#r2", "body": "Validate JWT issuer", "createdAt": "2026-11-20T10:00:00Z", "author": {"login": "rev"}, "commit": {"oid": "head_move"}, "originalCommit": {"oid": "head_move"}, "diffHunk": "@@ -10,4 +10,4 @@\n+def decode_session_token(raw_jwt, secret_key):\n+    return jwt.decode(raw_jwt, secret_key)"}]},
                },
            ],
        )
        router.on_rest(
            "repos/acme/app/pulls/42/files?per_page=100&page=1",
            [
                {
                    "filename": "src/new_name.py",
                    "previous_filename": "src/old_name.py",
                    "status": "renamed",
                    "changes": 2,
                    "patch": "@@ -1,2 +1,2 @@\n+def connect_gateway(timeout=5):",
                },
                {
                    "filename": "src/legacy_module.py",
                    "status": "removed",
                    "changes": 10,
                    "patch": None,
                },
                {
                    "filename": "src/helpers/token.py",
                    "status": "added",
                    "changes": 5,
                    "patch": "@@ -0,0 +1,4 @@\n+def decode_session_token(raw_jwt, secret_key):\n+    return jwt.decode(raw_jwt, secret_key, algorithms=['HS256'])",
                },
            ],
        )
        new_name_src = "def connect_gateway(timeout=5):\n    return timeout\n"
        token_src = "import jwt\n\ndef decode_session_token(raw_jwt, secret_key):\n    return jwt.decode(raw_jwt, secret_key, algorithms=['HS256'])\n"
        router.on_rest(
            "repos/acme/app/contents/src/new_name.py?ref=head_move",
            {"type": "file", "encoding": "base64", "size": len(new_name_src), "content": _b64(new_name_src)},
        )
        router.on_rest(
            "repos/acme/app/contents/src/helpers/token.py?ref=head_move",
            {"type": "file", "encoding": "base64", "size": len(token_src), "content": _b64(token_src)},
        )

        collected = collect.collect_pr_data("https://github.com/acme/app/pull/42", runner=router)
        context = compare.build_audit_context(collected, fetch_files=True, runner=router)
        items = {item["id"]: item for item in context["items"]}

        self.assertEqual(items["T_RENAMED"]["location"]["status"], "renamed")
        self.assertEqual(items["T_RENAMED"]["location"]["current_path"], "src/new_name.py")
        self.assertIn("timeout=5", items["T_RENAMED"]["current_code_excerpt"]["snippet"])

        self.assertEqual(items["T_EXTRACTED"]["location"]["status"], "deleted")
        self.assertTrue(items["T_EXTRACTED"]["moved_candidates"])
        self.assertEqual(items["T_EXTRACTED"]["moved_candidates"][0]["path"], "src/helpers/token.py")
        self.assertTrue(items["T_EXTRACTED"]["moved_code_excerpts"])
        self.assertEqual(items["T_EXTRACTED"]["moved_code_excerpts"][0]["path"], "src/helpers/token.py")

    def test_scenario_6_missing_or_incomplete_metadata(self) -> None:
        router = MockGhRouter()
        _base_graphql_routes(
            router,
            head_sha="head_meta",
            threads=[
                {
                    "id": "T_INCOMPLETE",
                    "isResolved": False,
                    "isOutdated": True,
                    "path": "src/deleted.py",
                    "line": None,
                    "originalLine": None,
                    "diffSide": None,
                    "resolvedBy": None,
                    "comments": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            {
                                "id": "C_INC",
                                "url": "https://github.com/acme/app/pull/42#r9",
                                "body": "Handle missing key.",
                                "createdAt": "2026-11-20T10:00:00Z",
                                "author": None,
                                "commit": None,
                                "originalCommit": None,
                                "diffHunk": "",
                            }
                        ],
                    },
                }
            ],
            issue_comments=[
                {
                    "id": "IC_1",
                    "url": "https://github.com/acme/app/pull/42#issuecomment-1",
                    "body": "Please double-check deployment env vars.",
                    "createdAt": "2026-11-20T11:00:00Z",
                    "author": None,
                }
            ],
        )
        router.on_rest(
            "repos/acme/app/pulls/42/files?per_page=100&page=1",
            [{"filename": "src/deleted.py", "status": "removed", "changes": 10, "patch": None}],
        )

        collected = collect.collect_pr_data("https://github.com/acme/app/pull/42", runner=router)
        context = compare.build_audit_context(collected, fetch_diffs=True, fetch_files=True, runner=router)
        by_id = {item["id"]: item for item in context["items"]}

        self.assertIn("missing_author", by_id["T_INCOMPLETE"]["metadata_warnings"])
        self.assertIn("missing_commit_sha", by_id["T_INCOMPLETE"]["metadata_warnings"])
        self.assertIn("missing_line", by_id["T_INCOMPLETE"]["metadata_warnings"])
        self.assertEqual(by_id["T_INCOMPLETE"]["freshness"]["status"], "UNKNOWN_COMMIT")
        self.assertEqual(by_id["IC_1"]["freshness"]["status"], "UNKNOWN_COMMIT")

        md = report.render_markdown_report(
            context,
            {
                "findings": [
                    {
                        "item_id": "T_INCOMPLETE",
                        "status": "UNCERTAIN",
                        "issue_summary": "Handle missing key.",
                        "evidence": "File was deleted and metadata is incomplete.",
                        "code_references": [],
                    },
                    {
                        "item_id": "IC_1",
                        "status": "UNCERTAIN",
                        "issue_summary": "Verify deployment env vars.",
                        "evidence": "External deployment environment cannot be verified statically.",
                        "code_references": [],
                    },
                ]
            },
        )
        self.assertIn("| `UNCERTAIN` | 2 |", md)
        self.assertIn("T_INCOMPLETE: missing_commit_sha", md)

    def test_report_guardrails_and_sanitization(self) -> None:
        context = {
            "pr": {
                "owner": "acme",
                "repo": "app",
                "number": 42,
                "url": "https://github.com/acme/app/pull/42",
                "head_sha": "abc1234",
                "title": "Fix | bug <script>alert(1)</script>",
            },
            "items": [
                {
                    "id": "T1",
                    "source_type": "review_thread",
                    "url": "https://github.com/acme/app/pull/42#r1",
                    "author": "bad<actor>",
                    "path": "src/app.py",
                    "is_resolved": True,
                    "is_outdated": False,
                    "freshness": {"status": "CURRENT", "reason": "Current."},
                    "location": {"status": "modified", "original_path": "src/app.py", "current_path": "src/app.py"},
                    "comments": [{"body": "Fix <img src=x onerror=alert(1)>"}],
                }
            ],
            "warnings": [],
        }

        # FIXED without code_references must be downgraded to UNCERTAIN
        md = report.render_markdown_report(
            context,
            {
                "findings": [
                    {
                        "item_id": "T1",
                        "status": "FIXED",
                        "issue_summary": "Sanitize input <script>evil()</script> | pipe",
                        "evidence": "Looks fixed.",
                        "code_references": [],
                    }
                ]
            },
        )
        self.assertIn("| `UNCERTAIN` | 1 |", md)
        self.assertIn("| Verdict | Finding | Code reference |", md)
        self.assertIn("| `UNCERTAIN` | Sanitize input &lt;script&gt;evil()&lt;/script&gt; &#124; pipe | — |", md)
        self.assertIn("[UNCERTAIN] `src/app.py`", md)
        self.assertIn("Guardrail downgrade", md)
        self.assertNotIn("<script>", md)
        self.assertIn("&lt;script&gt;", md)

        with self.assertRaises(ValueError):
            report.validate_and_normalize_findings(
                context,
                {
                    "findings": [
                        {
                            "item_id": "T1",
                            "status": "BOGUS",
                            "evidence": "x",
                            "code_references": ["src/app.py:1"],
                        }
                    ]
                },
            )

    def test_cli_end_to_end_compare_with_repo_dir_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            repo_dir = tmp_dir / "repo"
            head_sha = _init_git_repo(
                repo_dir,
                {"src/verify.py": "def check(a, b):\n    return a == b  # still buggy\n"},
            )

            collected_path = tmp_dir / "collected.json"
            collected_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "pr": {
                            "host": "github.com",
                            "owner": "acme",
                            "repo": "app",
                            "number": 99,
                            "url": "https://github.com/acme/app/pull/99",
                            "title": "CLI smoke test",
                            "state": "OPEN",
                            "base_ref": "main",
                            "head_ref": "fix/auth",
                            "head_sha": head_sha,
                        },
                        "commits": [{"sha": head_sha, "short_sha": head_sha[:7]}],
                        "files": [
                            {
                                "filename": "src/verify.py",
                                "status": "modified",
                                "changes": 1,
                                "patch": "@@ -1,2 +1,2 @@\n+    return a == b",
                            }
                        ],
                        "review_items": [
                            {
                                "id": "T_CLI",
                                "source_type": "review_thread",
                                "url": "https://github.com/acme/app/pull/99#r1",
                                "author": "reviewer",
                                "path": "src/verify.py",
                                "line": 2,
                                "original_line": 2,
                                "commit_sha": head_sha,
                                "original_commit_sha": head_sha,
                                "is_resolved": True,
                                "is_outdated": False,
                                "resolved_by": "author",
                                "diff_hunk": "@@ -1,2 +1,2 @@\n+    return a == b",
                                "comments": [{"body": "Use constant-time comparison"}],
                                "metadata_warnings": [],
                            }
                        ],
                        "warnings": [],
                    }
                ),
                encoding="utf-8",
            )

            context_path = tmp_dir / "audit-context.json"
            rc_compare = compare.main(
                [str(collected_path), "--repo-dir", str(repo_dir), "--output", str(context_path)]
            )
            self.assertEqual(rc_compare, 0)
            context_data = json.loads(context_path.read_text(encoding="utf-8"))
            self.assertIn("a == b", context_data["items"][0]["current_code_excerpt"]["snippet"])

            findings_path = tmp_dir / "findings.json"
            findings_path.write_text(
                json.dumps(
                    {
                        "findings": [
                            {
                                "item_id": "T_CLI",
                                "status": "STILL_PRESENT",
                                "issue_summary": "Constant-time comparison missing.",
                                "evidence": "Line 2 in HEAD still uses `a == b`.",
                                "code_references": ["src/verify.py:2"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            report_path = tmp_dir / "report.md"
            rc_report = report.main(
                [
                    "--context",
                    str(context_path),
                    "--findings",
                    str(findings_path),
                    "--output",
                    str(report_path),
                ]
            )
            self.assertEqual(rc_report, 0)
            rendered = report_path.read_text(encoding="utf-8")
            self.assertIn("WARNING — Resolved Threads Still Unfixed", rendered)
            self.assertIn("src/verify.py:2", rendered)

    def test_codex_issue_comment_extracts_commit_sha(self) -> None:
        codex_sha = "77bb53aa82b03ba0007cb6c6a335322e78886bde"
        head_sha = "88cc64bb93c14cb1118dc7d7b446433f89997cef"
        router = MockGhRouter()
        _base_graphql_routes(
            router,
            head_sha=head_sha,
            commits=[
                {
                    "commit": {
                        "oid": codex_sha,
                        "committedDate": "2026-11-23T10:00:00Z",
                        "messageHeadline": "Reviewed by Codex",
                        "author": {"user": {"login": "alice"}},
                    }
                },
                {
                    "commit": {
                        "oid": head_sha,
                        "committedDate": "2026-11-23T11:00:00Z",
                        "messageHeadline": "Follow-up fix",
                        "author": {"user": {"login": "alice"}},
                    }
                },
            ],
            issue_comments=[
                {
                    "id": "IC_CODEX",
                    "url": "https://github.com/acme/app/pull/42#issuecomment-500",
                    "body": (
                        "<!-- codex-pull-request-review-summary -->\n"
                        f'<!-- codex-review-state:v1 {{"code_review":{{"commit_sha":"{codex_sha}","has_any_findings":false}}}} -->\n'
                        "Codex Review: Didn't find any major issues."
                    ),
                    "createdAt": "2026-11-23T10:05:00Z",
                    "author": {"login": "chatgpt-codex-connector"},
                }
            ],
        )
        router.on_rest(
            "repos/acme/app/pulls/42/files?per_page=100&page=1",
            [{"filename": "src/auth.py", "status": "modified", "changes": 1, "patch": "+x"}],
        )
        router.on_rest(
            f"repos/acme/app/compare/{codex_sha}...{head_sha}",
            {"files": [{"filename": "src/auth.py", "status": "modified", "changes": 1}]},
        )

        collected = collect.collect_pr_data("https://github.com/acme/app/pull/42", runner=router)
        self.assertEqual(len(collected["review_items"]), 1)
        codex_item = collected["review_items"][0]
        self.assertEqual(codex_item["commit_sha"], codex_sha)
        self.assertEqual(codex_item["comments"][0]["commit_sha"], codex_sha)
        self.assertNotIn("missing_commit_sha", codex_item["metadata_warnings"])

        context = compare.build_audit_context(collected, fetch_diffs=True, runner=router)
        self.assertEqual(context["items"][0]["freshness"]["status"], "OLDER_AFFECTED")
        self.assertEqual(context["items"][0]["freshness"]["commits_behind"], 1)

    def test_repo_dir_rejects_non_git_directory_and_fetch_files_takes_precedence(self) -> None:
        head_sha = "1234567890abcdef1234567890abcdef12345678"
        pr_data = {
            "schema_version": "1.0",
            "pr": {
                "host": "github.com",
                "owner": "acme",
                "repo": "app",
                "number": 7,
                "url": "https://github.com/acme/app/pull/7",
                "title": "Repo dir guard",
                "state": "OPEN",
                "head_sha": head_sha,
            },
            "commits": [{"sha": head_sha, "short_sha": head_sha[:7]}],
            "files": [{"filename": "src/verify.py", "status": "modified", "changes": 1, "patch": "+pass"}],
            "review_items": [
                {
                    "id": "T_NONGIT",
                    "source_type": "review_thread",
                    "url": "https://github.com/acme/app/pull/7#r1",
                    "author": "rev",
                    "path": "src/verify.py",
                    "line": 1,
                    "original_line": 1,
                    "commit_sha": head_sha,
                    "original_commit_sha": head_sha,
                    "is_resolved": False,
                    "is_outdated": False,
                    "diff_hunk": "",
                    "comments": [{"body": "Check verify.py"}],
                    "metadata_warnings": [],
                }
            ],
            "warnings": [],
        }

        with tempfile.TemporaryDirectory() as tmp:
            plain_dir = Path(tmp) / "plain"
            (plain_dir / "src").mkdir(parents=True)
            (plain_dir / "src" / "verify.py").write_text("LOCAL_FORGED_CONTENT\n", encoding="utf-8")

            # Non-git directory alone must be rejected
            ctx_plain = compare.build_audit_context(pr_data, repo_dir=plain_dir)
            self.assertIn("repo_dir_not_a_git_repository", ctx_plain["warnings"])
            self.assertIsNone(ctx_plain["items"][0]["current_code_excerpt"])

            # With --fetch-files enabled, GitHub HEAD content wins even if --repo-dir is also passed
            router = MockGhRouter()
            gh_src = "GITHUB_HEAD_CONTENT\n"
            router.on_rest(
                f"repos/acme/app/contents/src/verify.py?ref={head_sha}",
                {"type": "file", "encoding": "base64", "size": len(gh_src), "content": _b64(gh_src)},
            )
            ctx_both = compare.build_audit_context(
                pr_data, fetch_files=True, repo_dir=plain_dir, runner=router
            )
            snippet = ctx_both["items"][0]["current_code_excerpt"]["snippet"]
            self.assertIn("GITHUB_HEAD_CONTENT", snippet)
            self.assertNotIn("LOCAL_FORGED_CONTENT", snippet)

    def test_repo_dir_rejects_head_mismatch_and_dirty_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp) / "repo"
            local_head = _init_git_repo(
                repo_dir,
                {"src/verify.py": "CLEAN_COMMITTED_LINE\n"},
            )
            other_sha = "ffffffffffffffffffffffffffffffffffffffff"
            base_pr = {
                "schema_version": "1.0",
                "pr": {
                    "host": "github.com",
                    "owner": "acme",
                    "repo": "app",
                    "number": 8,
                    "url": "https://github.com/acme/app/pull/8",
                    "title": "HEAD mismatch & dirty file",
                    "state": "OPEN",
                    "head_sha": other_sha,
                },
                "commits": [{"sha": other_sha, "short_sha": other_sha[:7]}],
                "files": [{"filename": "src/verify.py", "status": "modified", "changes": 1, "patch": "+x"}],
                "review_items": [
                    {
                        "id": "T_MISMATCH",
                        "source_type": "review_thread",
                        "url": "https://github.com/acme/app/pull/8#r1",
                        "author": "rev",
                        "path": "src/verify.py",
                        "line": 1,
                        "original_line": 1,
                        "commit_sha": other_sha,
                        "original_commit_sha": other_sha,
                        "is_resolved": False,
                        "is_outdated": False,
                        "diff_hunk": "",
                        "comments": [{"body": "Check"}],
                        "metadata_warnings": [],
                    }
                ],
                "warnings": [],
            }

            # Mismatched HEAD SHA must be rejected
            ctx_mismatch = compare.build_audit_context(base_pr, repo_dir=repo_dir)
            self.assertTrue(any(w.startswith("repo_dir_head_mismatch:") for w in ctx_mismatch["warnings"]))
            self.assertIsNone(ctx_mismatch["items"][0]["current_code_excerpt"])

            # Matching HEAD SHA with a dirty working-tree edit must read committed HEAD blob, not dirty file
            base_pr["pr"]["head_sha"] = local_head
            base_pr["commits"] = [{"sha": local_head, "short_sha": local_head[:7]}]
            base_pr["review_items"][0]["commit_sha"] = local_head
            base_pr["review_items"][0]["original_commit_sha"] = local_head
            (repo_dir / "src" / "verify.py").write_text("DIRTY_UNCOMMITTED_EDIT\n", encoding="utf-8")

            ctx_dirty = compare.build_audit_context(base_pr, repo_dir=repo_dir)
            snippet = ctx_dirty["items"][0]["current_code_excerpt"]["snippet"]
            self.assertIn("CLEAN_COMMITTED_LINE", snippet)
            self.assertNotIn("DIRTY_UNCOMMITTED_EDIT", snippet)

    def test_repo_dir_does_not_execute_configured_git_clean_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            repo_dir = tmp_dir / "repo"
            marker_file = tmp_dir / "pwned.txt"
            filter_script = tmp_dir / "evil_filter.py"
            filter_script.write_text(
                "import pathlib, sys\n"
                f"pathlib.Path({marker_file.as_posix()!r}).write_text('executed', encoding='utf-8')\n"
                "sys.stdout.write(sys.stdin.read())\n",
                encoding="utf-8",
            )

            head_sha = _init_git_repo(
                repo_dir,
                {
                    ".gitattributes": "*.py filter=evil\n",
                    "src/verify.py": "COMMITTED_SAFE\n",
                },
            )
            filter_cmd = f'"{Path(sys.executable).as_posix()}" "{filter_script.as_posix()}"'
            _git(repo_dir, "config", "filter.evil.clean", filter_cmd)

            # Same-length working-tree edit that would trigger clean filter under `git status`
            (repo_dir / "src" / "verify.py").write_text("DIRTY_EDIT_123\n", encoding="utf-8")

            pr_data = {
                "schema_version": "1.0",
                "pr": {
                    "host": "github.com",
                    "owner": "acme",
                    "repo": "app",
                    "number": 9,
                    "url": "https://github.com/acme/app/pull/9",
                    "title": "Clean filter safety",
                    "state": "OPEN",
                    "head_sha": head_sha,
                },
                "commits": [{"sha": head_sha, "short_sha": head_sha[:7]}],
                "files": [{"filename": "src/verify.py", "status": "modified", "changes": 1, "patch": "+x"}],
                "review_items": [
                    {
                        "id": "T_FILTER",
                        "source_type": "review_thread",
                        "url": "https://github.com/acme/app/pull/9#r1",
                        "author": "rev",
                        "path": "src/verify.py",
                        "line": 1,
                        "original_line": 1,
                        "commit_sha": head_sha,
                        "original_commit_sha": head_sha,
                        "is_resolved": False,
                        "is_outdated": False,
                        "diff_hunk": "",
                        "comments": [{"body": "Check"}],
                        "metadata_warnings": [],
                    }
                ],
                "warnings": [],
            }

            ctx = compare.build_audit_context(pr_data, repo_dir=repo_dir)
            self.assertFalse(marker_file.exists(), "Git clean filter must never be executed.")
            snippet = ctx["items"][0]["current_code_excerpt"]["snippet"]
            self.assertIn("COMMITTED_SAFE", snippet)
            self.assertNotIn("DIRTY_EDIT_123", snippet)

    def test_repo_dir_disables_git_replace_objects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp) / "repo"
            head_sha = _init_git_repo(repo_dir, {"src/verify.py": "COMMITTED_ORIGINAL\n"})
            orig_blob = _git(repo_dir, "rev-parse", f"{head_sha}:src/verify.py")

            fake_file = Path(tmp) / "fake.py"
            fake_file.write_text("REPLACED_LOCAL_FORGERY\n", encoding="utf-8")
            fake_blob = _git(repo_dir, "hash-object", "-w", str(fake_file))
            _git(repo_dir, "replace", orig_blob, fake_blob)

            pr_data = {
                "schema_version": "1.0",
                "pr": {
                    "host": "github.com",
                    "owner": "acme",
                    "repo": "app",
                    "number": 10,
                    "url": "https://github.com/acme/app/pull/10",
                    "title": "Replace ref safety",
                    "state": "OPEN",
                    "head_sha": head_sha,
                },
                "commits": [{"sha": head_sha, "short_sha": head_sha[:7]}],
                "files": [{"filename": "src/verify.py", "status": "modified", "changes": 1, "patch": "+x"}],
                "review_items": [
                    {
                        "id": "T_REPLACE",
                        "source_type": "review_thread",
                        "url": "https://github.com/acme/app/pull/10#r1",
                        "author": "rev",
                        "path": "src/verify.py",
                        "line": 1,
                        "original_line": 1,
                        "commit_sha": head_sha,
                        "original_commit_sha": head_sha,
                        "is_resolved": False,
                        "is_outdated": False,
                        "diff_hunk": "",
                        "comments": [{"body": "Check"}],
                        "metadata_warnings": [],
                    }
                ],
                "warnings": [],
            }

            ctx = compare.build_audit_context(pr_data, repo_dir=repo_dir)
            snippet = ctx["items"][0]["current_code_excerpt"]["snippet"]
            self.assertIn("COMMITTED_ORIGINAL", snippet)
            self.assertNotIn("REPLACED_LOCAL_FORGERY", snippet)

    def test_repo_dir_blocks_lazy_fetch_and_promisor_uploadpack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            repo_dir = tmp_dir / "repo"
            upstream_dir = tmp_dir / "upstream"
            _init_git_repo(upstream_dir, {"src/verify.py": "REMOTE_COMMITTED\n"})
            head_sha = _init_git_repo(repo_dir, {"src/verify.py": "REMOTE_COMMITTED\n"})
            blob_oid = _git(repo_dir, "rev-parse", f"{head_sha}:src/verify.py")

            marker_file = tmp_dir / "lazy_pwned.txt"
            uploadpack_script = tmp_dir / "evil_uploadpack.py"
            uploadpack_script.write_text(
                "import pathlib\n"
                f"pathlib.Path({marker_file.as_posix()!r}).write_text('pwned', encoding='utf-8')\n",
                encoding="utf-8",
            )
            uploadpack_cmd = f'"{Path(sys.executable).as_posix()}" "{uploadpack_script.as_posix()}"'
            _git(repo_dir, "config", "core.repositoryformatversion", "1")
            _git(repo_dir, "config", "extensions.partialclone", "origin")
            _git(repo_dir, "config", "remote.origin.url", upstream_dir.as_posix())
            _git(repo_dir, "config", "remote.origin.promisor", "true")
            _git(repo_dir, "config", "remote.origin.uploadpack", uploadpack_cmd)

            obj_path = repo_dir / ".git" / "objects" / blob_oid[:2] / blob_oid[2:]
            os.chmod(obj_path, 0o644)
            obj_path.unlink()

            pr_data = {
                "schema_version": "1.0",
                "pr": {
                    "host": "github.com",
                    "owner": "acme",
                    "repo": "app",
                    "number": 11,
                    "url": "https://github.com/acme/app/pull/11",
                    "title": "Partial clone safety",
                    "state": "OPEN",
                    "head_sha": head_sha,
                },
                "commits": [{"sha": head_sha, "short_sha": head_sha[:7]}],
                "files": [{"filename": "src/verify.py", "status": "modified", "changes": 1, "patch": "+x"}],
                "review_items": [
                    {
                        "id": "T_PARTIAL",
                        "source_type": "review_thread",
                        "url": "https://github.com/acme/app/pull/11#r1",
                        "author": "rev",
                        "path": "src/verify.py",
                        "line": 1,
                        "original_line": 1,
                        "commit_sha": head_sha,
                        "original_commit_sha": head_sha,
                        "is_resolved": False,
                        "is_outdated": False,
                        "diff_hunk": "",
                        "comments": [{"body": "Check"}],
                        "metadata_warnings": [],
                    }
                ],
                "warnings": [],
            }

            ctx = compare.build_audit_context(pr_data, repo_dir=repo_dir)
            self.assertFalse(marker_file.exists(), "Promisor lazy-fetch must never run.")
            self.assertIsNone(ctx["items"][0]["current_code_excerpt"])
            self.assertTrue(
                any(
                    "local_file_unreadable (src/verify.py)" in w
                    or "repo_dir_partial_clone_unsupported_without_no_lazy_fetch" in w
                    for w in ctx["warnings"]
                )
            )

            # Simulate Git < 2.48 (no `--no-lazy-fetch` flag): partial clone must still be blocked
            with mock.patch.object(compare, "_git_supports_no_lazy_fetch", return_value=False):
                ctx_legacy_partial = compare.build_audit_context(pr_data, repo_dir=repo_dir)
                self.assertFalse(marker_file.exists(), "Legacy Git path must not trigger uploadpack.")
                self.assertIsNone(ctx_legacy_partial["items"][0]["current_code_excerpt"])
                self.assertIn(
                    "repo_dir_partial_clone_unsupported_without_no_lazy_fetch",
                    ctx_legacy_partial["warnings"],
                )

    def test_repo_dir_works_on_older_git_without_no_lazy_fetch_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp) / "repo"
            head_sha = _init_git_repo(repo_dir, {"src/verify.py": "LEGACY_GIT_OK\n"})
            pr_data = {
                "schema_version": "1.0",
                "pr": {
                    "host": "github.com",
                    "owner": "acme",
                    "repo": "app",
                    "number": 12,
                    "url": "https://github.com/acme/app/pull/12",
                    "title": "Git 2.43 compatibility",
                    "state": "OPEN",
                    "head_sha": head_sha,
                },
                "commits": [{"sha": head_sha, "short_sha": head_sha[:7]}],
                "files": [{"filename": "src/verify.py", "status": "modified", "changes": 1, "patch": "+x"}],
                "review_items": [
                    {
                        "id": "T_LEGACY",
                        "source_type": "review_thread",
                        "url": "https://github.com/acme/app/pull/12#r1",
                        "author": "rev",
                        "path": "src/verify.py",
                        "line": 1,
                        "original_line": 1,
                        "commit_sha": head_sha,
                        "original_commit_sha": head_sha,
                        "is_resolved": False,
                        "is_outdated": False,
                        "diff_hunk": "",
                        "comments": [{"body": "Check"}],
                        "metadata_warnings": [],
                    }
                ],
                "warnings": [],
            }

            with mock.patch.object(compare, "_git_supports_no_lazy_fetch", return_value=False):
                ctx = compare.build_audit_context(pr_data, repo_dir=repo_dir)
                snippet = ctx["items"][0]["current_code_excerpt"]["snippet"]
                self.assertIn("LEGACY_GIT_OK", snippet)

    def test_repo_dir_blocks_bare_promisor_without_equals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp) / "repo"
            head_sha = _init_git_repo(repo_dir, {"src/verify.py": "COMMITTED\n"})
            cfg_path = repo_dir / ".git" / "config"
            cfg_text = cfg_path.read_text(encoding="utf-8")
            cfg_path.write_text(
                cfg_text + '\n[remote "origin"]\n\turl = ssh://example.com/repo.git\n\tpromisor\n',
                encoding="utf-8",
            )

            pr_data = {
                "schema_version": "1.0",
                "pr": {
                    "host": "github.com",
                    "owner": "acme",
                    "repo": "app",
                    "number": 13,
                    "url": "https://github.com/acme/app/pull/13",
                    "title": "Bare promisor safety",
                    "state": "OPEN",
                    "head_sha": head_sha,
                },
                "commits": [{"sha": head_sha, "short_sha": head_sha[:7]}],
                "files": [{"filename": "src/verify.py", "status": "modified", "changes": 1, "patch": "+x"}],
                "review_items": [
                    {
                        "id": "T_BARE_PROMISOR",
                        "source_type": "review_thread",
                        "url": "https://github.com/acme/app/pull/13#r1",
                        "author": "rev",
                        "path": "src/verify.py",
                        "line": 1,
                        "original_line": 1,
                        "commit_sha": head_sha,
                        "original_commit_sha": head_sha,
                        "is_resolved": False,
                        "is_outdated": False,
                        "diff_hunk": "",
                        "comments": [{"body": "Check"}],
                        "metadata_warnings": [],
                    }
                ],
                "warnings": [],
            }

            with mock.patch.object(compare, "_git_supports_no_lazy_fetch", return_value=False):
                self.assertTrue(compare._is_partial_or_promisor_repo(repo_dir))
                ctx = compare.build_audit_context(pr_data, repo_dir=repo_dir)
                self.assertIsNone(ctx["items"][0]["current_code_excerpt"])
                self.assertIn(
                    "repo_dir_partial_clone_unsupported_without_no_lazy_fetch",
                    ctx["warnings"],
                )

    def test_repo_dir_blocks_whitespace_partialclone_remote_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            repo_dir = tmp_dir / "repo"
            upstream_dir = tmp_dir / "upstream"
            _init_git_repo(upstream_dir, {"src/verify.py": "COMMITTED\n"})
            head_sha = _init_git_repo(repo_dir, {"src/verify.py": "COMMITTED\n"})
            blob_oid = _git(repo_dir, "rev-parse", f"{head_sha}:src/verify.py")

            marker_file = tmp_dir / "ws_pwned.txt"
            uploadpack_script = tmp_dir / "evil_ws_uploadpack.py"
            uploadpack_script.write_text(
                "import pathlib\n"
                f"pathlib.Path({marker_file.as_posix()!r}).write_text('pwned', encoding='utf-8')\n",
                encoding="utf-8",
            )
            uploadpack_cmd = f'"{Path(sys.executable).as_posix()}" "{uploadpack_script.as_posix()}"'
            _git(repo_dir, "config", "core.repositoryformatversion", "1")
            _git(repo_dir, "config", "extensions.partialclone", " ")
            _git(repo_dir, "config", "protocol.file.allow", "always")
            _git(repo_dir, "config", "remote. .url", upstream_dir.as_posix())
            _git(repo_dir, "config", "remote. .promisor", "true")
            _git(repo_dir, "config", "remote. .uploadpack", uploadpack_cmd)

            obj_path = repo_dir / ".git" / "objects" / blob_oid[:2] / blob_oid[2:]
            os.chmod(obj_path, 0o644)
            obj_path.unlink()

            pr_data = {
                "schema_version": "1.0",
                "pr": {
                    "host": "github.com",
                    "owner": "acme",
                    "repo": "app",
                    "number": 14,
                    "url": "https://github.com/acme/app/pull/14",
                    "title": "Whitespace partialclone safety",
                    "state": "OPEN",
                    "head_sha": head_sha,
                },
                "commits": [{"sha": head_sha, "short_sha": head_sha[:7]}],
                "files": [{"filename": "src/verify.py", "status": "modified", "changes": 1, "patch": "+x"}],
                "review_items": [
                    {
                        "id": "T_WS_PARTIAL",
                        "source_type": "review_thread",
                        "url": "https://github.com/acme/app/pull/14#r1",
                        "author": "rev",
                        "path": "src/verify.py",
                        "line": 1,
                        "original_line": 1,
                        "commit_sha": head_sha,
                        "original_commit_sha": head_sha,
                        "is_resolved": False,
                        "is_outdated": False,
                        "diff_hunk": "",
                        "comments": [{"body": "Check"}],
                        "metadata_warnings": [],
                    }
                ],
                "warnings": [],
            }

            with mock.patch.object(compare, "_git_supports_no_lazy_fetch", return_value=False):
                self.assertTrue(compare._is_partial_or_promisor_repo(repo_dir))
                ctx = compare.build_audit_context(pr_data, repo_dir=repo_dir)
                self.assertFalse(marker_file.exists(), "Whitespace partialclone must not trigger uploadpack.")
                self.assertIsNone(ctx["items"][0]["current_code_excerpt"])
                self.assertIn(
                    "repo_dir_partial_clone_unsupported_without_no_lazy_fetch",
                    ctx["warnings"],
                )

    def test_repo_dir_pins_verified_commit_sha_against_mid_audit_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp) / "repo"
            head_1 = _init_git_repo(repo_dir, {"src/verify.py": "COMMIT_ONE_CONTENT\n"})
            (repo_dir / "src" / "verify.py").write_text("COMMIT_TWO_CONTENT\n", encoding="utf-8")
            _git(repo_dir, "add", "src/verify.py")
            _git(repo_dir, "commit", "-m", "Second commit")
            head_2 = _git(repo_dir, "rev-parse", "HEAD")

            _git(repo_dir, "checkout", head_1)
            warnings: list[str] = []
            verified_sha = compare._verify_local_repo_head(repo_dir, head_1, warnings)
            self.assertEqual(verified_sha, head_1)

            # Simulate another process switching branches/commits after verification
            _git(repo_dir, "checkout", head_2)
            content = compare._read_local_repo_file(
                repo_dir, "src/verify.py", warnings, commit_sha=verified_sha
            )
            self.assertEqual(content, "COMMIT_ONE_CONTENT\n")
            self.assertEqual(warnings, [])

    def test_repo_dir_ignores_worktree_symlink_when_reading_pinned_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            repo_dir = root / "repo"
            rel_path = "src/verify.py"
            head_sha = _init_git_repo(repo_dir, {rel_path: "COMMITTED_CONTENT\n"})
            resolve = Path.resolve

            def resolve_worktree_path(path: Path, *args, **kwargs) -> Path:
                if path == repo_dir / rel_path:
                    return root / "outside" / "verify.py"
                return resolve(path, *args, **kwargs)

            collected = {
                "pr": {"head_sha": head_sha},
                "files": [{"filename": rel_path}],
                "review_items": [{"id": "T1", "path": rel_path, "line": 1}],
            }
            # Emulate a worktree symlink without requiring Windows symlink privileges.
            with mock.patch.object(Path, "resolve", autospec=True, side_effect=resolve_worktree_path):
                context = compare.build_audit_context(collected, repo_dir=repo_dir)

            excerpt = context["items"][0]["current_code_excerpt"]
            self.assertEqual(excerpt["snippet"], "1: COMMITTED_CONTENT")
            self.assertEqual(context["warnings"], [])

    def test_repo_dir_reads_drive_prefixed_path_from_git_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp) / "repo"
            parent = _init_git_repo(repo_dir, {"src/verify.py": "COMMITTED_CONTENT\n"})
            subtree = _git(repo_dir, "rev-parse", f"{parent}^{{tree}}")
            # Build a tree object directly because Windows cannot check out a directory named C:.
            root_tree = _git(repo_dir, "mktree", "-z", stdin=f"040000 tree {subtree}\tC:\0")
            head_sha = _git(repo_dir, "commit-tree", root_tree, "-p", parent, "-m", "Nested tree")
            _git(repo_dir, "update-ref", "HEAD", head_sha, parent)

            rel_path = "C:/src/verify.py"
            collected = {
                "pr": {"head_sha": head_sha},
                "files": [{"filename": rel_path}],
                "review_items": [{"id": "T1", "path": rel_path, "line": 1}],
            }
            context = compare.build_audit_context(collected, repo_dir=repo_dir)

            self.assertEqual(context["warnings"], [])
            self.assertEqual(
                context["items"][0]["current_code_excerpt"]["snippet"], "1: COMMITTED_CONTENT"
            )

    def test_partially_fixed_without_evidence_or_refs_downgraded_to_uncertain(self) -> None:
        context = {
            "pr": {
                "owner": "acme",
                "repo": "app",
                "number": 42,
                "url": "https://github.com/acme/app/pull/42",
                "head_sha": "abc1234",
                "title": "Partial guardrail",
            },
            "items": [
                {
                    "id": "T_P1",
                    "source_type": "review_thread",
                    "url": "https://github.com/acme/app/pull/42#r1",
                    "author": "reviewer",
                    "path": "src/cache.py",
                    "is_resolved": True,
                    "is_outdated": False,
                    "freshness": {"status": "CURRENT", "reason": "Current."},
                    "location": {"status": "modified", "original_path": "src/cache.py", "current_path": "src/cache.py"},
                },
                {
                    "id": "T_P2",
                    "source_type": "review_thread",
                    "url": "https://github.com/acme/app/pull/42#r2",
                    "author": "reviewer",
                    "path": "src/auth.py",
                    "is_resolved": True,
                    "is_outdated": False,
                    "freshness": {"status": "CURRENT", "reason": "Current."},
                    "location": {"status": "modified", "original_path": "src/auth.py", "current_path": "src/auth.py"},
                },
            ],
            "warnings": [],
        }

        md = report.render_markdown_report(
            context,
            {
                "findings": [
                    {
                        "item_id": "T_P1",
                        "status": "PARTIALLY_FIXED",
                        "issue_summary": "Add TTL and hash check.",
                        "evidence": "TTL added.",
                        "code_references": [],
                    },
                    {
                        "item_id": "T_P2",
                        "status": "PARTIALLY_FIXED",
                        "issue_summary": "Check null token.",
                        "evidence": "",
                        "code_references": ["src/auth.py:10"],
                    },
                ]
            },
        )
        self.assertIn("| `UNCERTAIN` | 2 |", md)
        self.assertIn("| `PARTIALLY_FIXED` | 0 |", md)
        self.assertIn("Guardrail downgrade: PARTIALLY_FIXED finding for T_P1 lacked code_references", md)
        self.assertIn("Guardrail downgrade: PARTIALLY_FIXED finding for T_P2 lacked evidence", md)
        self.assertNotIn("WARNING — Resolved Threads Still Unfixed", md)


if __name__ == "__main__":
    unittest.main()
