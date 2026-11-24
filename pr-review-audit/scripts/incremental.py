"""Track evidence from a prior audit and conservatively reuse its verdicts."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import PurePosixPath
import re
from typing import Any

try:
    from .collect import GhApiError, GitHubClient, parse_pr_url
except ImportError:
    from collect import GhApiError, GitHubClient, parse_pr_url


STATE_VERSION = 1
_FULL_SHA = re.compile(r"[a-f0-9]{40}")
_LINE_REFERENCE = re.compile(r"(.+):[1-9][0-9]*(?:-[1-9][0-9]*)?")
_REUSABLE_STATUSES = ("FIXED", "STILL_PRESENT", "PARTIALLY_FIXED")
_REVIEW_FIELDS = (
    "source_type", "path", "line", "original_line", "start_line", "original_start_line",
    "diff_side", "commit_sha", "original_commit_sha", "is_resolved", "is_outdated",
    "diff_hunk", "comments", "metadata_warnings",
)


def is_full_sha(value: Any) -> bool:
    return isinstance(value, str) and _FULL_SHA.fullmatch(value) is not None


def _repo_path(value: Any) -> bool:
    return (
        isinstance(value, str) and bool(value.strip()) and "\\" not in value
        and all(ord(char) >= 32 for char in value)
        and not value.startswith("/") and ".." not in value.split("/")
        and value != "." and PurePosixPath(value).as_posix() == value
    )


def review_fingerprint(item: dict[str, Any]) -> str:
    relevant = {field: item.get(field) for field in _REVIEW_FIELDS}
    return sha256(json.dumps(relevant, sort_keys=True).encode("utf-8")).hexdigest()


def collection_incomplete(warnings: list[str]) -> bool:
    markers = (
        "partial_graphql_errors", "graphql_pagination_cap_reached", "graphql_cursor_stalled",
        "rest_pagination_cap_reached", "thread_comments_incomplete",
    )
    return any(w.startswith(markers) for w in warnings)


def reusable_paths(item: dict[str, Any], finding: dict[str, Any]) -> set[str] | None:
    """Require explicit dependency coverage and concrete references for reuse."""
    dependencies = finding.get("dependency_paths")
    refs = finding.get("code_references")
    evidence = finding.get("evidence")
    path = item.get("path")
    if (
        item.get("source_type") != "review_thread" or item.get("metadata_warnings")
        or not item.get("comments") or not _repo_path(path)
        or finding.get("status") not in _REUSABLE_STATUSES
        or not isinstance(evidence, str) or not evidence.strip()
        or finding.get("requires_human_verification")
        or not isinstance(dependencies, list) or not all(_repo_path(p) for p in dependencies)
        or not isinstance(refs, list) or not refs
    ):
        return None
    watched = {path, *dependencies}
    for ref in refs:
        match = _LINE_REFERENCE.fullmatch(ref) if isinstance(ref, str) else None
        if not match or not _repo_path(match[1]):
            return None
        watched.add(match[1])
    return watched


def _changed_paths(
    client: GitHubClient, url: str, previous_head: str, current_head: str
) -> set[str] | None:
    ref = parse_pr_url(url)
    if previous_head == current_head:
        return set()
    endpoint = f"repos/{ref.owner}/{ref.repo}/compare/{previous_head}...{current_head}"
    comparison = client.rest_get_json(ref.host, endpoint)
    if not isinstance(comparison, dict) or comparison.get("status") != "ahead":
        return None
    merge_base = comparison.get("merge_base_commit")
    files = comparison.get("files")
    if (
        not isinstance(merge_base, dict) or merge_base.get("sha") != previous_head
        or not isinstance(files, list) or len(files) >= 300
    ):
        return None
    changed: set[str] = set()
    for file in files:
        if not isinstance(file, dict) or not _repo_path(file.get("filename")):
            return None
        changed.add(file["filename"])
        old_name = file.get("previous_filename")
        if old_name is not None or file.get("status") == "renamed":
            if not _repo_path(old_name):
                return None
            changed.add(old_name)
    return changed


def plan_reaudit(
    pr_data: dict[str, Any], previous: dict[str, Any], client: GitHubClient
) -> dict[str, Any]:
    """Mark eligible unchanged findings, treating missing evidence as a new audit."""
    pr = pr_data.get("pr") or {}
    items = {item["id"]: item for item in pr_data.get("review_items", []) if item.get("id")}
    old_head, new_head = previous.get("head_sha"), pr.get("head_sha")
    plan: dict[str, Any] = {
        "previous_head_sha": old_head if is_full_sha(old_head) else None,
        "reused_findings": {},
        "recheck_item_ids": list(items),
    }
    entries = previous.get("entries")
    if previous.get("state_version") != STATE_VERSION or not isinstance(entries, dict):
        return {**plan, "fallback_reason": "Unsupported prior audit state."}
    if (
        not is_full_sha(old_head) or not is_full_sha(new_head) or not is_full_sha(pr.get("base_sha"))
        or previous.get("base_sha") != pr.get("base_sha") or previous.get("pr_url") != pr.get("url")
    ):
        return {**plan, "fallback_reason": "The PR or its base commit could not be matched."}
    if collection_incomplete(pr_data.get("warnings", [])):
        return {**plan, "fallback_reason": "The current review collection was incomplete."}
    try:
        changed = _changed_paths(client, pr["url"], old_head, new_head)
    except (GhApiError, ValueError, KeyError):
        changed = None
    if changed is None:
        return {**plan, "fallback_reason": "No complete comparison with the prior PR HEAD."}

    reused = plan["reused_findings"]
    for item_id, item in items.items():
        entry = entries.get(item_id)
        if not isinstance(entry, dict) or entry.get("fingerprint") != review_fingerprint(item):
            continue
        finding = entry.get("finding")
        if not isinstance(finding, dict) or finding.get("item_id") != item_id:
            continue
        watched = reusable_paths(item, finding)
        verified_at = entry.get("verified_at_sha")
        if watched and watched.isdisjoint(changed) and is_full_sha(verified_at):
            reused[item_id] = {"finding": finding, "verified_at_sha": verified_at}
    plan["recheck_item_ids"] = [item_id for item_id in items if item_id not in reused]
    return plan
