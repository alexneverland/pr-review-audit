"""Analyze review freshness, track renamed/moved code, and prepare audit context.

Deterministic and read-only. Reads `collected.json` produced by `collect.py`
and optionally fetches read-only compare diffs / HEAD file snapshots via `gh api`
or reads files safely from a local repository directory.
"""

from __future__ import annotations

import argparse
import base64
from functools import lru_cache
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from typing import Any
from urllib.parse import quote

try:
    from .collect import GhApiError, GhRunner, GitHubClient, run_gh_api
except ImportError:
    from collect import GhApiError, GhRunner, GitHubClient, run_gh_api


SCHEMA_VERSION = "1.0"
MAX_FILE_BYTES = 1_000_000
MAX_ANCHORS = 8
MAX_MOVED_CANDIDATES = 5
EXCERPT_RADIUS = 20

_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_PRIORITY_PREFIXES = (
    "def ",
    "async def ",
    "class ",
    "function ",
    "async function ",
    "export ",
    "func ",
    "fn ",
    "pub fn ",
    "const ",
    "let ",
)
_TRIVIAL_COMMENT_PREFIXES = ("//", "#", "/*", "*", "*/", "--", "<!--")


def _short_sha(sha: str | None) -> str | None:
    return sha[:7] if isinstance(sha, str) and sha else None


def _build_rename_map(files: list[dict[str, Any]]) -> dict[str, str]:
    direct: dict[str, str] = {}
    for entry in files:
        prev = entry.get("previous_filename")
        curr = entry.get("filename")
        if isinstance(prev, str) and prev and isinstance(curr, str) and curr and prev != curr:
            direct[prev] = curr

    resolved: dict[str, str] = {}
    for src in direct:
        seen = {src}
        curr = direct[src]
        while curr in direct and curr not in seen:
            seen.add(curr)
            curr = direct[curr]
        resolved[src] = curr
    return resolved


def extract_diff_anchors(diff_hunk: str | None, max_anchors: int = MAX_ANCHORS) -> list[str]:
    """Extract distinctive non-trivial code lines from a GitHub diff hunk."""
    if not isinstance(diff_hunk, str) or not diff_hunk.strip():
        return []

    candidates: list[tuple[int, str]] = []
    seen: set[str] = set()
    for raw_line in diff_hunk.splitlines():
        if raw_line.startswith("@@"):
            continue
        stripped_prefix = raw_line[1:] if raw_line[:1] in {"+", "-", " "} else raw_line
        line = stripped_prefix.strip()
        if len(line) < 12 or line in seen:
            continue
        if line.startswith(_TRIVIAL_COMMENT_PREFIXES):
            continue
        if not any(ch.isalnum() for ch in line):
            continue
        seen.add(line)
        priority = 1 if line.startswith(_PRIORITY_PREFIXES) else 0
        candidates.append((priority, line))

    candidates.sort(key=lambda item: (item[0], len(item[1])), reverse=True)
    return [line for _, line in candidates[:max_anchors]]


def _find_anchor_lines_in_text(text: str, anchors: list[str]) -> tuple[list[str], list[int]]:
    matched: list[str] = []
    line_numbers: list[int] = []
    lines = text.splitlines()
    for anchor in anchors:
        for idx, line in enumerate(lines, start=1):
            if anchor in line:
                matched.append(anchor)
                if idx not in line_numbers:
                    line_numbers.append(idx)
                break
    return matched, sorted(line_numbers)


def _find_anchor_lines_in_patch(patch: str, anchors: list[str]) -> list[str]:
    added_or_context: list[str] = []
    for raw_line in patch.splitlines():
        if raw_line.startswith("@@") or raw_line.startswith("--- ") or raw_line.startswith("+++ "):
            continue
        if raw_line.startswith("-"):
            continue
        cleaned = raw_line[1:] if raw_line[:1] in {"+", " "} else raw_line
        added_or_context.append(cleaned)
    joined = "\n".join(added_or_context)
    return [anchor for anchor in anchors if anchor in joined]


def _extract_excerpt(
    text: str,
    target_line: int | None,
    anchor_lines: list[int],
    radius: int = EXCERPT_RADIUS,
) -> dict[str, Any]:
    lines = text.splitlines()
    total_lines = len(lines)
    if total_lines == 0:
        return {"start_line": 1, "end_line": 0, "total_lines": 0, "snippet": ""}

    center = target_line if isinstance(target_line, int) and 1 <= target_line <= total_lines else None
    if center is None and anchor_lines:
        center = anchor_lines[0]

    if center is not None:
        start = max(1, center - radius)
        end = min(total_lines, center + radius)
    else:
        start = 1
        end = min(total_lines, 120)

    numbered = [f"{lineno}: {lines[lineno - 1]}" for lineno in range(start, end + 1)]
    return {
        "start_line": start,
        "end_line": end,
        "total_lines": total_lines,
        "truncated": end < total_lines or start > 1,
        "snippet": "\n".join(numbered),
    }


def analyze_freshness(
    item: dict[str, Any],
    pr_data: dict[str, Any],
    compare_files_by_sha: dict[str, list[dict[str, Any]] | None],
    rename_map: dict[str, str],
) -> tuple[dict[str, Any], list[str]]:
    """Determine chronological freshness and whether later commits affected this item."""
    warnings: list[str] = []
    pr = pr_data.get("pr") or {}
    commits = pr_data.get("commits") or []
    files = pr_data.get("files") or []
    head_sha = pr.get("head_sha")

    effective_sha = item.get("commit_sha") or item.get("original_commit_sha")
    if not effective_sha:
        return (
            {
                "status": "UNKNOWN_COMMIT",
                "review_commit_sha": None,
                "head_commit_sha": head_sha,
                "commits_behind": None,
                "affected_by_later_commits": None,
                "later_file_changes": [],
                "reason": "Review item has no associated commit SHA.",
            },
            warnings,
        )

    if head_sha and effective_sha == head_sha:
        return (
            {
                "status": "CURRENT",
                "review_commit_sha": effective_sha,
                "head_commit_sha": head_sha,
                "commits_behind": 0,
                "affected_by_later_commits": False,
                "later_file_changes": [],
                "reason": "Review commit matches current PR HEAD.",
            },
            warnings,
        )

    commit_index: dict[str, int] = {}
    for idx, commit in enumerate(commits):
        sha = commit.get("sha")
        if isinstance(sha, str) and sha:
            commit_index[sha] = idx

    if effective_sha not in commit_index:
        return (
            {
                "status": "COMMIT_NOT_IN_HISTORY",
                "review_commit_sha": effective_sha,
                "head_commit_sha": head_sha,
                "commits_behind": None,
                "affected_by_later_commits": None,
                "later_file_changes": [],
                "reason": (
                    "Review commit SHA is absent from current PR commit history "
                    "(likely force-pushed, rebased, or squashed)."
                ),
            },
            warnings,
        )

    commits_behind = len(commits) - 1 - commit_index[effective_sha]
    if commits_behind == 0:
        return (
            {
                "status": "CURRENT",
                "review_commit_sha": effective_sha,
                "head_commit_sha": head_sha,
                "commits_behind": 0,
                "affected_by_later_commits": False,
                "later_file_changes": [],
                "reason": "Review commit is the latest commit in PR history.",
            },
            warnings,
        )

    item_path = item.get("path")
    renamed_path = rename_map.get(item_path) if isinstance(item_path, str) else None
    target_paths = {p for p in (item_path, renamed_path) if isinstance(p, str) and p}

    compare_entries = compare_files_by_sha.get(effective_sha)
    if effective_sha in compare_files_by_sha and compare_entries is not None:
        if target_paths:
            matches = [
                entry
                for entry in compare_entries
                if entry.get("filename") in target_paths
                or entry.get("previous_filename") in target_paths
            ]
            if matches:
                return (
                    {
                        "status": "OLDER_AFFECTED",
                        "review_commit_sha": effective_sha,
                        "head_commit_sha": head_sha,
                        "commits_behind": commits_behind,
                        "affected_by_later_commits": True,
                        "later_file_changes": [
                            {
                                "filename": m.get("filename"),
                                "previous_filename": m.get("previous_filename"),
                                "status": m.get("status"),
                                "changes": m.get("changes"),
                            }
                            for m in matches
                        ],
                        "reason": (
                            f"{commits_behind} commit(s) landed after {_short_sha(effective_sha)} "
                            f"and modified {', '.join(sorted(target_paths))}."
                        ),
                    },
                    warnings,
                )
            return (
                {
                    "status": "OLDER_UNAFFECTED",
                    "review_commit_sha": effective_sha,
                    "head_commit_sha": head_sha,
                    "commits_behind": commits_behind,
                    "affected_by_later_commits": False,
                    "later_file_changes": [],
                    "reason": (
                        f"{commits_behind} commit(s) landed after {_short_sha(effective_sha)}, "
                        "but none touched the reviewed file."
                    ),
                },
                warnings,
            )

        if compare_entries:
            return (
                {
                    "status": "OLDER_AFFECTED",
                    "review_commit_sha": effective_sha,
                    "head_commit_sha": head_sha,
                    "commits_behind": commits_behind,
                    "affected_by_later_commits": True,
                    "later_file_changes": [
                        {
                            "filename": m.get("filename"),
                            "previous_filename": m.get("previous_filename"),
                            "status": m.get("status"),
                            "changes": m.get("changes"),
                        }
                        for m in compare_entries[:20]
                    ],
                    "reason": (
                        f"{commits_behind} commit(s) landed after {_short_sha(effective_sha)} "
                        f"changing {len(compare_entries)} file(s)."
                    ),
                },
                warnings,
            )
        return (
            {
                "status": "OLDER_UNAFFECTED",
                "review_commit_sha": effective_sha,
                "head_commit_sha": head_sha,
                "commits_behind": commits_behind,
                "affected_by_later_commits": False,
                "later_file_changes": [],
                "reason": (
                    f"{commits_behind} commit(s) landed after {_short_sha(effective_sha)}, "
                    "with no net file changes."
                ),
            },
            warnings,
        )

    # Compare diff unavailable fallback
    warnings.append(f"compare_unavailable_{_short_sha(effective_sha)}")
    pr_paths = {f.get("filename") for f in files} | {
        f.get("previous_filename") for f in files if f.get("previous_filename")
    }
    if item.get("is_outdated"):
        return (
            {
                "status": "OLDER_AFFECTED",
                "review_commit_sha": effective_sha,
                "head_commit_sha": head_sha,
                "commits_behind": commits_behind,
                "affected_by_later_commits": True,
                "later_file_changes": [],
                "reason": (
                    f"{commits_behind} commit(s) behind HEAD; compare diff unavailable, "
                    "but GitHub marks this thread as outdated."
                ),
            },
            warnings,
        )
    if target_paths and (target_paths & pr_paths):
        return (
            {
                "status": "OLDER_AFFECTED",
                "review_commit_sha": effective_sha,
                "head_commit_sha": head_sha,
                "commits_behind": commits_behind,
                "affected_by_later_commits": None,
                "later_file_changes": [],
                "reason": (
                    f"{commits_behind} commit(s) behind HEAD and reviewed file is modified in PR; "
                    "compare diff unavailable so later-commit impact could not be ruled out."
                ),
            },
            warnings,
        )
    return (
        {
            "status": "OLDER_UNAFFECTED",
            "review_commit_sha": effective_sha,
            "head_commit_sha": head_sha,
            "commits_behind": commits_behind,
            "affected_by_later_commits": None,
            "later_file_changes": [],
            "reason": (
                f"{commits_behind} commit(s) behind HEAD; compare diff unavailable."
            ),
        },
        warnings,
    )


def track_file_location(
    item: dict[str, Any],
    files: list[dict[str, Any]],
    file_contents_by_path: dict[str, str],
    rename_map: dict[str, str],
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any] | None]:
    """Track current file location, moved-code candidates, and code excerpt."""
    original_path = item.get("path")
    file_by_name = {f.get("filename"): f for f in files if f.get("filename")}
    anchors = extract_diff_anchors(item.get("diff_hunk"))

    if not original_path:
        return (
            {
                "status": "no_path",
                "original_path": None,
                "current_path": None,
                "anchors_extracted": anchors,
            },
            [],
            None,
        )

    current_path: str | None = original_path
    location_status = "not_in_pr_files"
    if original_path in rename_map:
        current_path = rename_map[original_path]
        location_status = "renamed"
    elif original_path in file_by_name:
        pr_status = file_by_name[original_path].get("status")
        if pr_status == "removed":
            location_status = "deleted"
            current_path = None
        else:
            location_status = pr_status or "present"
            current_path = original_path
    elif original_path in file_contents_by_path:
        location_status = "present_unmodified"
        current_path = original_path

    current_excerpt: dict[str, Any] | None = None
    anchors_found_in_current = False
    if current_path and current_path in file_contents_by_path:
        text = file_contents_by_path[current_path]
        matched_in_curr, matched_lines = _find_anchor_lines_in_text(text, anchors)
        anchors_found_in_current = bool(matched_in_curr)
        target_line = item.get("line") or item.get("original_line")
        current_excerpt = _extract_excerpt(text, target_line, matched_lines)
        current_excerpt["path"] = current_path
        current_excerpt["matched_anchors"] = matched_in_curr

    should_search_moved = (
        bool(anchors)
        and (
            location_status in {"deleted", "not_in_pr_files"}
            or bool(item.get("is_outdated"))
            or (current_path in file_contents_by_path and not anchors_found_in_current)
        )
    )

    moved_candidates: list[dict[str, Any]] = []
    if should_search_moved:
        excluded = {p for p in (original_path, current_path) if p}
        candidate_map: dict[str, dict[str, Any]] = {}

        for f in files:
            fname = f.get("filename")
            patch = f.get("patch")
            if not fname or fname in excluded or f.get("status") == "removed" or not patch:
                continue
            matched = _find_anchor_lines_in_patch(patch, anchors)
            if matched:
                candidate_map[fname] = {
                    "path": fname,
                    "source": "pr_patch",
                    "matched_anchors": matched,
                    "match_score": round(len(matched) / len(anchors), 3),
                    "line_numbers": [],
                }

        for fpath, text in file_contents_by_path.items():
            if fpath in excluded:
                continue
            matched, line_nums = _find_anchor_lines_in_text(text, anchors)
            if matched:
                candidate_map[fpath] = {
                    "path": fpath,
                    "source": "head_file",
                    "matched_anchors": matched,
                    "match_score": round(len(matched) / len(anchors), 3),
                    "line_numbers": line_nums,
                }

        moved_candidates = sorted(
            candidate_map.values(),
            key=lambda c: (len(c["matched_anchors"]), c["match_score"], c["path"]),
            reverse=True,
        )[:MAX_MOVED_CANDIDATES]

    return (
        {
            "status": location_status,
            "original_path": original_path,
            "current_path": current_path,
            "anchors_extracted": anchors,
        },
        moved_candidates,
        current_excerpt,
    )


def _fetch_compare_diffs(
    client: GitHubClient,
    pr_data: dict[str, Any],
    warnings: list[str],
) -> dict[str, list[dict[str, Any]] | None]:
    pr = pr_data.get("pr") or {}
    host = pr.get("host") or "github.com"
    owner = pr.get("owner")
    repo = pr.get("repo")
    head_sha = pr.get("head_sha")
    if not (owner and repo and head_sha and _SHA_RE.fullmatch(str(head_sha))):
        return {}

    commit_shas = {c.get("sha") for c in (pr_data.get("commits") or []) if c.get("sha")}
    needed_shas: list[str] = []
    for item in pr_data.get("review_items") or []:
        sha = item.get("commit_sha") or item.get("original_commit_sha")
        if (
            isinstance(sha, str)
            and sha != head_sha
            and sha in commit_shas
            and _SHA_RE.fullmatch(sha)
            and sha not in needed_shas
        ):
            needed_shas.append(sha)

    result: dict[str, list[dict[str, Any]] | None] = {}
    owner_q = quote(str(owner), safe="")
    repo_q = quote(str(repo), safe="")
    for sha in needed_shas:
        endpoint = f"repos/{owner_q}/{repo_q}/compare/{sha}...{head_sha}"
        try:
            payload = client.rest_get_json(host, endpoint)
        except GhApiError as exc:
            warnings.append(f"compare_fetch_failed_{_short_sha(sha)}: {exc}")
            result[sha] = None
            continue
        if not isinstance(payload, dict):
            result[sha] = None
            continue
        changed_files = payload.get("files") or []
        if len(changed_files) >= 300:
            warnings.append(
                f"compare_files_possibly_truncated_{_short_sha(sha)}: {len(changed_files)} files returned."
            )
        result[sha] = [f for f in changed_files if isinstance(f, dict)]
    return result


def _validate_rel_posix_path(path: str) -> str | None:
    if not path or path.startswith("/") or "\\" in path or "\x00" in path:
        return None
    posix = PurePosixPath(path)
    if posix.is_absolute() or ".." in posix.parts:
        return None
    return posix.as_posix()


def _fetch_file_via_gh(
    client: GitHubClient,
    host: str,
    owner: str,
    repo: str,
    ref: str,
    path: str,
    warnings: list[str],
) -> str | None:
    clean_path = _validate_rel_posix_path(path)
    if clean_path is None:
        warnings.append(f"skipped_unsafe_path: {path!r}")
        return None
    endpoint = (
        f"repos/{quote(owner, safe='')}/{quote(repo, safe='')}/contents/"
        f"{quote(clean_path, safe='/')}?ref={quote(ref, safe='')}"
    )
    try:
        payload = client.rest_get_json(host, endpoint)
    except GhApiError as exc:
        warnings.append(f"file_fetch_failed ({clean_path}): {exc}")
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("type") != "file":
        warnings.append(f"file_fetch_non_file ({clean_path}): type={payload.get('type')}")
        return None
    size = payload.get("size") or 0
    if size > MAX_FILE_BYTES:
        warnings.append(f"file_too_large ({clean_path}): {size} bytes exceeds {MAX_FILE_BYTES}.")
        return None
    encoded = payload.get("content")
    if not isinstance(encoded, str) or payload.get("encoding") != "base64":
        warnings.append(f"file_content_missing_or_unsupported_encoding ({clean_path}).")
        return None
    try:
        raw_bytes = base64.b64decode(encoded, validate=False)
        return raw_bytes.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        warnings.append(f"file_binary_or_non_utf8 ({clean_path}).")
        return None


@lru_cache(maxsize=1)
def _git_supports_no_lazy_fetch() -> bool:
    """Return True if the installed `git` binary supports the `--no-lazy-fetch` flag."""
    try:
        completed = subprocess.run(
            ["git", "--no-lazy-fetch", "--version"],
            capture_output=True,
            shell=False,
            check=False,
        )
    except OSError:
        return False
    return completed.returncode == 0


def _run_git_readonly_bytes(repo_root: Path, git_args: list[str]) -> tuple[int, bytes, bytes]:
    """Run a read-only `git` command inside `repo_root` without filters, lazy fetch, or replace refs."""
    cmd = ["git"]
    if _git_supports_no_lazy_fetch():
        cmd.append("--no-lazy-fetch")
    cmd.extend(
        [
            "--no-replace-objects",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "protocol.allow=never",
            "-c",
            "protocol.file.allow=never",
            "-c",
            "protocol.ext.allow=never",
            "-c",
            "protocol.ssh.allow=never",
            "-c",
            "protocol.http.allow=never",
            "-c",
            "protocol.https.allow=never",
            "-c",
            "protocol.git.allow=never",
            "-c",
            "core.sshCommand=false",
            "-C",
            str(repo_root),
            *git_args,
        ]
    )
    env = os.environ.copy()
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            env=env,
            shell=False,
            check=False,
        )
    except OSError as exc:
        return 1, b"", str(exc).encode("utf-8", errors="replace")
    return completed.returncode, completed.stdout, completed.stderr


def _run_git_readonly(repo_root: Path, git_args: list[str]) -> tuple[int, str, str]:
    """Run a read-only `git` command and decode UTF-8 text output."""
    rc, stdout, stderr = _run_git_readonly_bytes(repo_root, git_args)
    return (
        rc,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


def _is_partial_or_promisor_repo(repo_root: Path) -> bool:
    """Detect partial-clone / promisor configuration from Git config without fetching objects."""
    rc, stdout, _ = _run_git_readonly(repo_root, ["config", "--list", "--null"])
    if rc != 0:
        return True
    for entry in stdout.split("\x00"):
        if not entry:
            continue
        if "\n" in entry:
            key, val = entry.split("\n", 1)
            has_val = True
        else:
            key, val = entry, ""
            has_val = False
        key_l = key.strip().lower()
        val_l = val.strip().lower()
        if key_l == "extensions.partialclone":
            return True
        if key_l.startswith("remote.") and key_l.endswith(".promisor"):
            if not has_val or val_l not in {"false", "0", "no", "off", ""}:
                return True
    return False


def _verify_local_repo_head(
    repo_root: Path,
    head_sha: str | None,
    warnings: list[str],
) -> str | None:
    """Verify `--repo-dir` is a Git repository root checked out at PR HEAD and return full SHA."""
    if not head_sha or not _SHA_RE.fullmatch(str(head_sha)):
        warnings.append("repo_dir_skipped_missing_pr_head_sha")
        return None

    rc, stdout, _ = _run_git_readonly(repo_root, ["rev-parse", "--is-inside-work-tree"])
    if rc != 0 or stdout.strip().lower() != "true":
        warnings.append("repo_dir_not_a_git_repository")
        return None

    rc_top, stdout_top, _ = _run_git_readonly(repo_root, ["rev-parse", "--show-toplevel"])
    if rc_top != 0 or not stdout_top.strip():
        warnings.append("repo_dir_not_a_git_repository")
        return None
    try:
        if Path(stdout_top.strip()).resolve() != repo_root:
            warnings.append("repo_dir_not_git_root")
            return None
    except OSError:
        warnings.append("repo_dir_not_a_git_repository")
        return None

    rc_head, stdout_head, _ = _run_git_readonly(repo_root, ["rev-parse", "HEAD"])
    local_head = stdout_head.strip().lower()
    if rc_head != 0 or not _SHA_RE.fullmatch(local_head):
        warnings.append("repo_dir_missing_head_commit")
        return None

    pr_head = str(head_sha).strip().lower()
    if not (local_head == pr_head or local_head.startswith(pr_head)):
        warnings.append(f"repo_dir_head_mismatch: local={local_head[:7]} pr={pr_head[:7]}")
        return None

    if not _git_supports_no_lazy_fetch() and _is_partial_or_promisor_repo(repo_root):
        warnings.append("repo_dir_partial_clone_unsupported_without_no_lazy_fetch")
        return None

    return local_head


def _read_local_repo_file(
    repo_root: Path,
    rel_path: str,
    warnings: list[str],
    *,
    commit_sha: str | None = None,
) -> str | None:
    """Read a committed regular blob at the pinned commit SHA (or HEAD) without Git filters."""
    clean_path = _validate_rel_posix_path(rel_path)
    if clean_path is None:
        warnings.append(f"skipped_unsafe_local_path: {rel_path!r}")
        return None
    candidate = (repo_root / clean_path).resolve()
    if not candidate.is_relative_to(repo_root):
        warnings.append(f"blocked_path_traversal: {rel_path!r}")
        return None
    if commit_sha is not None:
        if not _SHA_RE.fullmatch(commit_sha):
            warnings.append(f"skipped_invalid_commit_sha: {commit_sha!r}")
            return None
        tree_ref = commit_sha
    else:
        tree_ref = "HEAD"
    if not _git_supports_no_lazy_fetch() and _is_partial_or_promisor_repo(repo_root):
        warnings.append("repo_dir_partial_clone_unsupported_without_no_lazy_fetch")
        return None

    rc_tree, stdout_tree, _ = _run_git_readonly(
        repo_root, ["ls-tree", tree_ref, "--", clean_path]
    )
    if rc_tree != 0:
        warnings.append(f"local_file_unreadable ({clean_path}): failed to inspect commit tree.")
        return None
    if not stdout_tree.strip():
        return None

    line = stdout_tree.splitlines()[0].strip()
    if "\t" not in line:
        return None
    meta, _tab_path = line.split("\t", 1)
    parts = meta.split()
    if len(parts) != 3:
        return None
    mode, obj_type, oid = parts
    if not _SHA_RE.fullmatch(oid):
        return None
    if mode not in {"100644", "100755"} or obj_type != "blob":
        warnings.append(f"repo_dir_non_regular_file ({clean_path})")
        return None

    rc_size, stdout_size, _ = _run_git_readonly(repo_root, ["cat-file", "-s", oid])
    if rc_size != 0:
        warnings.append(f"local_file_unreadable ({clean_path}): failed to inspect blob size.")
        return None
    try:
        size = int(stdout_size.strip())
    except ValueError:
        warnings.append(f"local_file_unreadable ({clean_path}): invalid blob size.")
        return None
    if size > MAX_FILE_BYTES:
        warnings.append(
            f"local_file_too_large ({clean_path}): {size} bytes exceeds {MAX_FILE_BYTES}."
        )
        return None

    rc_blob, stdout_blob, _ = _run_git_readonly_bytes(repo_root, ["cat-file", "blob", oid])
    if rc_blob != 0:
        warnings.append(f"local_file_unreadable ({clean_path}): git cat-file failed.")
        return None
    if len(stdout_blob) > MAX_FILE_BYTES:
        warnings.append(
            f"local_file_too_large ({clean_path}): {len(stdout_blob)} bytes exceeds {MAX_FILE_BYTES}."
        )
        return None
    try:
        return stdout_blob.decode("utf-8")
    except UnicodeDecodeError as exc:
        warnings.append(f"local_file_unreadable ({clean_path}): {exc}")
        return None


def _collect_target_paths(
    pr_data: dict[str, Any],
    rename_map: dict[str, str],
) -> list[str]:
    files = pr_data.get("files") or []
    deleted_paths = {f.get("filename") for f in files if f.get("status") == "removed"}
    targets: list[str] = []

    def add(p: str | None) -> None:
        if isinstance(p, str) and p and p not in deleted_paths and p not in targets:
            targets.append(p)

    for item in pr_data.get("review_items") or []:
        path = item.get("path")
        if not path:
            continue
        resolved = rename_map.get(path, path)
        add(resolved)

        anchors = extract_diff_anchors(item.get("diff_hunk"))
        if anchors and (path in deleted_paths or item.get("is_outdated")):
            for f in files:
                fname = f.get("filename")
                patch = f.get("patch")
                if fname and fname != resolved and f.get("status") != "removed" and patch:
                    if _find_anchor_lines_in_patch(patch, anchors):
                        add(fname)
    return targets


def build_audit_context(
    pr_data: dict[str, Any],
    *,
    compare_files_by_sha: dict[str, list[dict[str, Any]] | None] | None = None,
    file_contents_by_path: dict[str, str] | None = None,
    fetch_diffs: bool = False,
    fetch_files: bool = False,
    repo_dir: Path | None = None,
    runner: GhRunner = run_gh_api,
) -> dict[str, Any]:
    """Enrich normalized PR data with freshness, rename/move tracking, and code excerpts."""
    warnings: list[str] = list(pr_data.get("warnings") or [])
    files = pr_data.get("files") or []
    rename_map = _build_rename_map(files)

    resolved_compare = dict(compare_files_by_sha or {})
    resolved_contents = dict(file_contents_by_path or {})
    client = GitHubClient(runner=runner)

    if fetch_diffs:
        fetched = _fetch_compare_diffs(client, pr_data, warnings)
        for sha, data in fetched.items():
            resolved_compare.setdefault(sha, data)

    if fetch_files:
        pr = pr_data.get("pr") or {}
        host = pr.get("host") or "github.com"
        owner = pr.get("owner")
        repo = pr.get("repo")
        head_sha = pr.get("head_sha")
        if owner and repo and head_sha:
            for path in _collect_target_paths(pr_data, rename_map):
                if path not in resolved_contents:
                    text = _fetch_file_via_gh(
                        client, str(host), str(owner), str(repo), str(head_sha), path, warnings
                    )
                    if text is not None:
                        resolved_contents[path] = text

    if repo_dir is not None:
        repo_root = repo_dir.resolve(strict=True)
        head_sha = (pr_data.get("pr") or {}).get("head_sha")
        verified_head = _verify_local_repo_head(repo_root, head_sha, warnings)
        if verified_head is not None:
            for path in _collect_target_paths(pr_data, rename_map):
                if path not in resolved_contents:
                    text = _read_local_repo_file(
                        repo_root, path, warnings, commit_sha=verified_head
                    )
                    if text is not None:
                        resolved_contents[path] = text

    enriched_items: list[dict[str, Any]] = []
    freshness_counts: dict[str, int] = {}
    resolved_count = 0
    unresolved_count = 0

    for item in pr_data.get("review_items") or []:
        freshness, item_compare_warnings = analyze_freshness(
            item, pr_data, resolved_compare, rename_map
        )
        location, moved_candidates, excerpt = track_file_location(
            item, files, resolved_contents, rename_map
        )
        moved_excerpts: list[dict[str, Any]] = []
        if location["status"] in {"deleted", "not_in_pr_files"} or not excerpt:
            for cand in moved_candidates[:2]:
                cpath = cand["path"]
                if cpath in resolved_contents:
                    cand_excerpt = _extract_excerpt(
                        resolved_contents[cpath], None, cand.get("line_numbers") or []
                    )
                    cand_excerpt["path"] = cpath
                    cand_excerpt["matched_anchors"] = cand["matched_anchors"]
                    moved_excerpts.append(cand_excerpt)

        f_status = freshness["status"]
        freshness_counts[f_status] = freshness_counts.get(f_status, 0) + 1
        if item.get("is_resolved") is True:
            resolved_count += 1
        elif item.get("is_resolved") is False:
            unresolved_count += 1

        enriched_items.append(
            {
                **item,
                "freshness": freshness,
                "location": location,
                "current_code_excerpt": excerpt,
                "moved_candidates": moved_candidates,
                "moved_code_excerpts": moved_excerpts,
                "comparison_warnings": item_compare_warnings,
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "pr": pr_data.get("pr") or {},
        "commits": pr_data.get("commits") or [],
        "files": files,
        "rename_map": rename_map,
        "items": enriched_items,
        "summary_stats": {
            "total_review_items": len(enriched_items),
            "resolved_threads": resolved_count,
            "unresolved_threads": unresolved_count,
            "freshness_counts": freshness_counts,
        },
        "warnings": sorted(set(warnings)),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare collected PR reviews against latest PR HEAD and locate affected code."
    )
    parser.add_argument("collected_json", type=Path, help="Path to JSON generated by collect.py.")
    parser.add_argument(
        "--fetch-diffs",
        action="store_true",
        help="Fetch GitHub compare diffs between review commits and HEAD via `gh api`.",
    )
    parser.add_argument(
        "--fetch-files",
        action="store_true",
        help="Fetch current file snapshots at PR HEAD via `gh api`.",
    )
    parser.add_argument(
        "--repo-dir",
        type=Path,
        default=None,
        help="Optional local repository root for read-only file lookup.",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Output audit-context JSON file path (writes to stdout if omitted).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        pr_data = json.loads(args.collected_json.read_text(encoding="utf-8"))
        if not isinstance(pr_data, dict):
            raise ValueError("Collected JSON root must be an object.")
        if args.repo_dir is not None and not args.repo_dir.is_dir():
            raise ValueError(f"--repo-dir does not exist or is not a directory: {args.repo_dir}")
        context = build_audit_context(
            pr_data,
            fetch_diffs=args.fetch_diffs,
            fetch_files=args.fetch_files,
            repo_dir=args.repo_dir,
        )
    except (OSError, ValueError, json.JSONDecodeError, GhApiError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    encoded = json.dumps(context, indent=2, ensure_ascii=False)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    else:
        sys.stdout.write(encoded + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
