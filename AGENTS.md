# PR Review Audit

The portable standalone skill lives in `pr-review-audit/` (`SKILL.md`, `scripts/`, `references/`, `tests/`, and `examples/`). It must remain self-contained so copying `pr-review-audit/` into a Codex CLI or Claude Code skills directory works without extra setup.

## Review priorities

- Keep all helper scripts (`collect.py`, `compare.py`, `report.py`) on the Python 3.11+ standard library only, with zero external pip dependencies.
- Keep all GitHub interactions strictly read-only (`gh api` GraphQL queries and REST `GET` requests invoked with `shell=False` and GraphQL `--input -`). Never execute untrusted repository scripts, test suites, or binaries, and never modify target code or post GitHub comments without explicit user approval.
- Preserve strict input validation and sanitization: URL/host/owner/repo validation in `collect.py`, path-traversal guards for `--repo-dir` and `HEAD` paths in `compare.py`, HTML/Markdown-table sanitization and evidence downgrade guardrails in `report.py`.
- Never equate a modified line, an outdated thread, or a resolved GitHub thread (`is_resolved`) with a fixed issue. Preserve `UNCERTAIN` whenever static evidence is missing or runtime execution would be required.
- Add or update mocked tests in `pr-review-audit/tests/test_pr_review_audit.py` for any parser, freshness, location-tracking, or reporting behavior change, and keep `README.md` and `SKILL.md` synchronized with CLI flags.

## Checks

```sh
python -B -m py_compile pr-review-audit/scripts/__init__.py pr-review-audit/scripts/collect.py pr-review-audit/scripts/compare.py pr-review-audit/scripts/report.py pr-review-audit/tests/test_pr_review_audit.py
python -B -m unittest discover -s pr-review-audit/tests -v
python -B pr-review-audit/scripts/collect.py --help
python -B pr-review-audit/scripts/compare.py --help
python -B pr-review-audit/scripts/report.py --help
git diff --check
```
