## Summary

<!-- Describe what changed and why. -->

## Checks

- [ ] `python -B -m py_compile pr-review-audit/scripts/__init__.py pr-review-audit/scripts/collect.py pr-review-audit/scripts/compare.py pr-review-audit/scripts/report.py pr-review-audit/tests/test_pr_review_audit.py`
- [ ] `python -B -m unittest discover -s pr-review-audit/tests -v`
- [ ] `git diff --check`

## Safety & Portability Checklist

- [ ] Python 3.11+ standard library only (no external dependencies added)
- [ ] Read-only `gh api` usage preserved (`shell=False`, no untrusted script execution)
- [ ] Untrusted PR/repo data boundaries and `UNCERTAIN` guardrails preserved
