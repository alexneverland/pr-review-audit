"""Validate verification findings and render the PR Review Audit Markdown report.

Enforces evidence guardrails, sanitizes untrusted GitHub metadata/text,
highlights resolved-but-unfixed findings, and summarizes review freshness.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any


ALLOWED_STATUSES = ("FIXED", "STILL_PRESENT", "PARTIALLY_FIXED", "UNCERTAIN")
_STATUS_MARKER = {
    "FIXED": "🟢",
    "STILL_PRESENT": "🔴",
    "PARTIALLY_FIXED": "🟠",
    "UNCERTAIN": "🟠",
}
_STATUS_RANK = {
    "STILL_PRESENT": 0,
    "PARTIALLY_FIXED": 1,
    "UNCERTAIN": 2,
    "FIXED": 3,
}
_SAFE_URL_RE = re.compile(r"^https?://[^\s<>()]+$")


def sanitize_markdown_text(text: Any) -> str:
    """Escape raw HTML angle brackets in untrusted text while keeping readable prose."""
    if text is None:
        return ""
    value = str(text).replace("\r\n", "\n").replace("\r", "\n").strip()
    return value.replace("<", "&lt;").replace(">", "&gt;")


def sanitize_table_cell(text: Any) -> str:
    """Make untrusted text safe for a single-line GitHub-Flavored Markdown table cell."""
    cleaned = sanitize_markdown_text(text)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.replace("|", "&#124;")


def _safe_markdown_link(label: str, url: Any) -> str:
    safe_label = sanitize_markdown_text(label).replace("[", "\\[").replace("]", "\\]")
    if isinstance(url, str) and _SAFE_URL_RE.fullmatch(url.strip()):
        return f"[{safe_label}]({url.strip()})"
    return safe_label


def validate_and_normalize_findings(
    context: dict[str, Any],
    findings_payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Validate structured findings against audit-context items and apply safety guardrails."""
    items = context.get("items") or []
    items_by_id = {item["id"]: item for item in items if isinstance(item, dict) and item.get("id")}
    raw_findings = findings_payload.get("findings")
    if not isinstance(raw_findings, list):
        raise ValueError("`findings` payload must contain a `findings` list.")

    warnings: list[str] = []
    seen_ids: set[str] = set()
    actionable: list[dict[str, Any]] = []
    non_actionable: list[dict[str, Any]] = []

    for idx, entry in enumerate(raw_findings):
        if not isinstance(entry, dict):
            raise ValueError(f"Finding at index {idx} must be an object.")
        item_id = entry.get("item_id")
        if not isinstance(item_id, str) or item_id not in items_by_id:
            raise ValueError(f"Unknown or missing `item_id` in findings: {item_id!r}")
        if item_id in seen_ids:
            raise ValueError(f"Duplicate finding for `item_id`: {item_id!r}")
        seen_ids.add(item_id)

        item = items_by_id[item_id]
        if entry.get("non_actionable") is True or entry.get("actionable") is False:
            non_actionable.append(
                {
                    "item_id": item_id,
                    "item": item,
                    "reason": sanitize_markdown_text(
                        entry.get("reason") or entry.get("issue_summary") or "Marked non-actionable."
                    ),
                }
            )
            continue

        raw_status = entry.get("status")
        if raw_status not in ALLOWED_STATUSES:
            raise ValueError(
                f"Invalid status {raw_status!r} for {item_id}; expected one of {ALLOWED_STATUSES}."
            )

        evidence = sanitize_markdown_text(entry.get("evidence") or "")
        raw_refs = entry.get("code_references") or []
        if not isinstance(raw_refs, list):
            raise ValueError(f"`code_references` for {item_id} must be a list of strings.")
        code_references = [
            sanitize_markdown_text(ref) for ref in raw_refs if isinstance(ref, str) and ref.strip()
        ]
        issue_summary = sanitize_markdown_text(
            entry.get("issue_summary")
            or ((item.get("comments") or [{}])[0].get("body") or "")
        )

        status = raw_status
        guardrail_notes: list[str] = []
        if status in {"FIXED", "STILL_PRESENT", "PARTIALLY_FIXED"} and (
            not evidence or not code_references
        ):
            missing_parts = []
            if not evidence:
                missing_parts.append("evidence")
            if not code_references:
                missing_parts.append("code_references")
            reason = (
                f"Guardrail downgrade: {status} finding for {item_id} lacked "
                f"{' and '.join(missing_parts)}; downgraded to UNCERTAIN."
            )
            warnings.append(reason)
            guardrail_notes.append(reason)
            status = "UNCERTAIN"

        requires_human = bool(entry.get("requires_human_verification", False)) or status in {
            "STILL_PRESENT",
            "PARTIALLY_FIXED",
            "UNCERTAIN",
        }
        human_reason = sanitize_markdown_text(entry.get("human_verification_reason") or "")
        if not human_reason and requires_human:
            if guardrail_notes:
                human_reason = guardrail_notes[0]
            elif status == "STILL_PRESENT":
                human_reason = "Issue appears unresolved in latest HEAD."
            elif status == "PARTIALLY_FIXED":
                human_reason = "Only part of the review finding was addressed."
            elif status == "UNCERTAIN":
                human_reason = "Insufficient static evidence to confirm fix status."

        resolved_but_unfixed = bool(
            item.get("is_resolved") is True and status in {"STILL_PRESENT", "PARTIALLY_FIXED"}
        )

        actionable.append(
            {
                "item_id": item_id,
                "orig_index": idx,
                "status": status,
                "original_status": raw_status,
                "issue_summary": issue_summary or "(No issue summary provided)",
                "evidence": evidence or "(No verification evidence provided)",
                "code_references": code_references,
                "requires_human_verification": requires_human,
                "human_verification_reason": human_reason,
                "resolved_but_unfixed": resolved_but_unfixed,
                "guardrail_notes": guardrail_notes,
                "item": item,
            }
        )

    for item in items:
        item_id = item.get("id")
        if item_id and item_id not in seen_ids:
            warnings.append(f"missing_finding_for_item: {item_id} defaulted to UNCERTAIN.")
            actionable.append(
                {
                    "item_id": item_id,
                    "orig_index": len(actionable),
                    "status": "UNCERTAIN",
                    "original_status": "UNCERTAIN",
                    "issue_summary": sanitize_markdown_text(
                        ((item.get("comments") or [{}])[0].get("body") or "(No comment body)")
                    ),
                    "evidence": "No verification verdict was supplied for this review item.",
                    "code_references": [],
                    "requires_human_verification": True,
                    "human_verification_reason": "Review item was omitted from verification input.",
                    "resolved_but_unfixed": False,
                    "guardrail_notes": [f"Missing finding for {item_id}"],
                    "item": item,
                }
            )

    actionable.sort(
        key=lambda f: (
            _STATUS_RANK.get(f["status"], 99),
            0 if f["resolved_but_unfixed"] else 1,
            f["orig_index"],
        )
    )
    return actionable, non_actionable, warnings


def _format_thread_status(item: dict[str, Any]) -> str:
    is_resolved = item.get("is_resolved")
    if is_resolved is True:
        resolver = item.get("resolved_by")
        suffix = f" (by `@{sanitize_markdown_text(resolver)}`)" if resolver else ""
        outdated = ", Outdated" if item.get("is_outdated") else ""
        return f"Resolved{suffix}{outdated}"
    if is_resolved is False:
        outdated = " (Outdated)" if item.get("is_outdated") else ""
        return f"Unresolved{outdated}"
    return "N/A (not a thread)"


def _format_location(item: dict[str, Any]) -> str:
    loc = item.get("location") or {}
    loc_status = loc.get("status")
    orig_path = loc.get("original_path")
    curr_path = loc.get("current_path")
    moved = item.get("moved_candidates") or []

    if loc_status == "no_path":
        return "No file path (general PR/review comment)"
    if loc_status == "renamed":
        return f"`{sanitize_markdown_text(orig_path)}` -> renamed to `{sanitize_markdown_text(curr_path)}`"
    if loc_status == "deleted":
        if moved:
            top = moved[0]
            return (
                f"`{sanitize_markdown_text(orig_path)}` (deleted; matching code anchors found in "
                f"`{sanitize_markdown_text(top.get('path'))}`)"
            )
        return f"`{sanitize_markdown_text(orig_path)}` (deleted in PR)"
    if moved:
        top = moved[0]
        return (
            f"`{sanitize_markdown_text(curr_path or orig_path)}` "
            f"(possible moved code in `{sanitize_markdown_text(top.get('path'))}`)"
        )
    return f"`{sanitize_markdown_text(curr_path or orig_path)}`"


def render_markdown_report(
    context: dict[str, Any],
    findings_payload: dict[str, Any],
) -> str:
    """Render a deterministic, evidence-based Markdown PR review audit report."""
    actionable, non_actionable, validation_warnings = validate_and_normalize_findings(
        context, findings_payload
    )

    pr = context.get("pr") or {}
    head_sha = pr.get("head_sha") or "<unknown>"
    short_head = head_sha[:7] if isinstance(head_sha, str) and head_sha != "<unknown>" else head_sha
    repo_slug = f"{pr.get('owner', '?')}/{pr.get('repo', '?')}"
    pr_number = pr.get("number", "?")
    pr_title = sanitize_table_cell(pr.get("title") or f"PR #{pr_number}")
    pr_link = _safe_markdown_link(f"{repo_slug}#{pr_number}", pr.get("url"))

    counts = {status: 0 for status in ALLOWED_STATUSES}
    for f in actionable:
        counts[f["status"]] += 1

    resolved_unfixed = [f for f in actionable if f["resolved_but_unfixed"]]

    lines: list[str] = [
        "# PR Review Audit Report",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Pull Request | {pr_link} — {pr_title} |",
        f"| State | `{sanitize_table_cell(pr.get('state') or 'UNKNOWN')}` |",
        f"| Branches | `{sanitize_table_cell(pr.get('head_ref') or '?')}` -> `{sanitize_table_cell(pr.get('base_ref') or '?')}` |",
        f"| Latest Commit (HEAD) | `{sanitize_table_cell(head_sha)}` (`{sanitize_table_cell(short_head)}`) |",
        f"| Audit Mode | Read-only static verification (tests and repo scripts not executed) |",
        f"| Actionable Findings Audited | {len(actionable)} |",
        "",
        "## Executive Summary",
        "",
        "| Classification | Count |",
        "| --- | ---: |",
        f"| `FIXED` | {counts['FIXED']} |",
        f"| `STILL_PRESENT` | {counts['STILL_PRESENT']} |",
        f"| `PARTIALLY_FIXED` | {counts['PARTIALLY_FIXED']} |",
        f"| `UNCERTAIN` | {counts['UNCERTAIN']} |",
        f"| **Total Actionable** | **{len(actionable)}** |",
    ]

    if non_actionable:
        lines.append(f"| Non-Actionable Items Excluded | {len(non_actionable)} |")

    lines.append("")

    if actionable:
        lines.extend([
            "| Verdict | Finding | Code reference |",
            "| --- | --- | --- |",
        ])
        for finding in actionable:
            status = finding["status"]
            summary = sanitize_table_cell(finding["issue_summary"])
            reference = sanitize_table_cell(next(iter(finding["code_references"]), ""))
            lines.append(f"| {_STATUS_MARKER[status]} `{status}` | {summary} | {reference or '—'} |")
        lines.append("")

    if resolved_unfixed:
        lines.extend(
            [
                "> **WARNING — Resolved Threads Still Unfixed:** "
                f"{len(resolved_unfixed)} review thread(s) marked as resolved on GitHub "
                "still exhibit unfixed or only partially fixed issues in the latest commit:",
                ">",
            ]
        )
        for rf in resolved_unfixed:
            item = rf["item"]
            ref_link = _safe_markdown_link(rf["item_id"], item.get("url"))
            lines.append(
                f"> - **{rf['status']}** ({ref_link}, `{sanitize_markdown_text(item.get('path') or 'general')}`): "
                f"{rf['issue_summary']}"
            )
        lines.append("")

    # Review Freshness Warnings
    lines.extend(["## Review Freshness Warnings", ""])
    freshness_groups: dict[str, list[dict[str, Any]]] = {
        "OLDER_AFFECTED": [],
        "OLDER_UNAFFECTED": [],
        "COMMIT_NOT_IN_HISTORY": [],
        "UNKNOWN_COMMIT": [],
    }
    for item in context.get("items") or []:
        f_info = item.get("freshness") or {}
        f_status = f_info.get("status")
        if f_status in freshness_groups:
            freshness_groups[f_status].append(item)

    has_freshness_warnings = any(freshness_groups.values())
    if not has_freshness_warnings:
        lines.append(
            f"All collected reviews reference the latest PR HEAD commit (`{sanitize_markdown_text(short_head)}`)."
        )
        lines.append("")
    else:
        group_descriptions = [
            (
                "OLDER_AFFECTED",
                "Demonstrably Affected by Subsequent Commits (`OLDER_AFFECTED`)",
                "Reviews performed before later commits that touched the same file or PR scope:",
            ),
            (
                "COMMIT_NOT_IN_HISTORY",
                "Review Commit Absent From Current History (`COMMIT_NOT_IN_HISTORY`)",
                "Reviews referencing commits no longer present in PR history (likely force-push or rebase):",
            ),
            (
                "UNKNOWN_COMMIT",
                "Missing Review Commit Metadata (`UNKNOWN_COMMIT`)",
                "Reviews lacking commit SHA metadata in GitHub API responses:",
            ),
            (
                "OLDER_UNAFFECTED",
                "Chronologically Older, Reviewed File Untouched (`OLDER_UNAFFECTED`)",
                "Reviews performed on earlier commits where subsequent commits did not modify the reviewed file:",
            ),
        ]
        for key, heading, intro in group_descriptions:
            group_items = freshness_groups[key]
            if not group_items:
                continue
            lines.extend([f"### {heading}", "", intro, ""])
            for item in group_items:
                f_info = item.get("freshness") or {}
                ref_link = _safe_markdown_link(item.get("id") or "item", item.get("url"))
                rev_sha = f_info.get("review_commit_sha")
                rev_short = rev_sha[:7] if isinstance(rev_sha, str) and rev_sha else "none"
                lines.append(
                    f"- {ref_link} (`{sanitize_markdown_text(item.get('path') or 'general')}`, "
                    f"review commit `{sanitize_markdown_text(rev_short)}`): "
                    f"{sanitize_markdown_text(f_info.get('reason') or '')}"
                )
            lines.append("")

    # Detailed Findings
    lines.extend(["## Detailed Findings", ""])
    if not actionable:
        lines.append("No actionable review findings were identified.")
        lines.append("")
    else:
        for index, finding in enumerate(actionable, start=1):
            item = finding["item"]
            f_info = item.get("freshness") or {}
            title_loc = sanitize_markdown_text(item.get("path") or item.get("source_type") or "PR")
            lines.append(f"### {index}. [{finding['status']}] `{title_loc}`")
            lines.append("")
            if finding["resolved_but_unfixed"]:
                lines.append(
                    "> **Resolved-Thread Discrepancy:** This thread is marked **Resolved** on GitHub, "
                    "but verification against the latest code indicates the issue is **"
                    f"{finding['status']}**."
                )
                lines.append("")

            ref_link = _safe_markdown_link("GitHub Comment", item.get("url"))
            lines.extend(
                [
                    f"- **GitHub Reference:** {ref_link} (ID: `{sanitize_markdown_text(finding['item_id'])}`, Source: `{sanitize_markdown_text(item.get('source_type'))}`)",
                    f"- **Reviewer:** `@{sanitize_markdown_text(item.get('author') or '<unknown>')}`",
                    f"- **GitHub Thread Status:** {_format_thread_status(item)}",
                    f"- **Review Freshness:** `{sanitize_markdown_text(f_info.get('status') or 'UNKNOWN')}` — {sanitize_markdown_text(f_info.get('reason') or 'No freshness detail.')}",
                    f"- **Code Location:** {_format_location(item)}",
                    f"- **Original Issue:** {finding['issue_summary']}",
                ]
            )

            if finding["code_references"]:
                refs_fmt = ", ".join(f"`{ref}`" for ref in finding["code_references"])
                lines.append(f"- **Code References:** {refs_fmt}")
            else:
                lines.append("- **Code References:** None verified")

            lines.append(f"- **Verification Evidence:** {finding['evidence']}")
            if finding["guardrail_notes"]:
                lines.append(
                    f"- **Guardrail Note:** {'; '.join(finding['guardrail_notes'])}"
                )
            lines.append("")

    # Findings Requiring Human Verification
    lines.extend(["## Findings Requiring Human Verification", ""])
    human_items = [f for f in actionable if f["requires_human_verification"]]
    if not human_items:
        lines.append("No actionable findings require additional human verification.")
        lines.append("")
    else:
        for f in human_items:
            item = f["item"]
            ref_link = _safe_markdown_link(f["item_id"], item.get("url"))
            loc = sanitize_markdown_text(item.get("path") or "general")
            lines.append(
                f"- [ ] **[{f['status']}]** {ref_link} (`{loc}`): {f['human_verification_reason']}"
            )
        lines.append("")

    # Audit Limitations & Metadata Warnings
    lines.extend(["## Audit Limitations & Metadata Warnings", ""])
    all_warnings: list[str] = []
    for w in context.get("warnings") or []:
        all_warnings.append(sanitize_markdown_text(w))
    for item in context.get("items") or []:
        for mw in item.get("metadata_warnings") or []:
            all_warnings.append(f"{item.get('id')}: {sanitize_markdown_text(mw)}")
        for cw in item.get("comparison_warnings") or []:
            all_warnings.append(f"{item.get('id')}: {sanitize_markdown_text(cw)}")
    for vw in validation_warnings:
        all_warnings.append(sanitize_markdown_text(vw))
    for note in findings_payload.get("audit_notes") or []:
        all_warnings.append(sanitize_markdown_text(note))

    deduped_warnings = list(dict.fromkeys(w for w in all_warnings if w))
    lines.append(
        "- **Static Verification Only:** No repository code, test suites, or build scripts were executed during this audit; runtime behavior and test coverage are unverified unless separately inspected."
    )
    if deduped_warnings:
        for w in deduped_warnings:
            lines.append(f"- {w}")
    else:
        lines.append("- No metadata truncation or API collection warnings occurred.")
    lines.append("")

    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render a Markdown PR Review Audit report from audit-context.json and findings.json."
    )
    parser.add_argument("--context", required=True, type=Path, help="Path to audit-context.json")
    parser.add_argument("--findings", required=True, type=Path, help="Path to findings.json")
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Output Markdown file path (writes to stdout if omitted).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        context = json.loads(args.context.read_text(encoding="utf-8"))
        findings_payload = json.loads(args.findings.read_text(encoding="utf-8"))
        if not isinstance(context, dict) or not isinstance(findings_payload, dict):
            raise ValueError("Both --context and --findings JSON files must have object roots.")
        markdown = render_markdown_report(context, findings_payload)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(markdown, encoding="utf-8")
    else:
        sys.stdout.write(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
