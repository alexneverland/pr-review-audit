# PR Review Audit Report

| Field | Value |
| --- | --- |
| Pull Request | [acme/payments#184](https://github.com/acme/payments/pull/184) — Enforce webhook signature checks and idempotency keys |
| State | `OPEN` |
| Branches | `feat/webhook-hardening` -> `main` |
| Latest Commit (HEAD) | `9e41d0c8a71b2e3345a8b7c6d5e4f3a2b1c0d9e8` (`9e41d0c`) |
| Audit Mode | Read-only static verification (tests and repo scripts not executed) |
| Actionable Findings Audited | 4 |

## Executive Summary

| Classification | Count |
| --- | ---: |
| `FIXED` | 1 |
| `STILL_PRESENT` | 1 |
| `PARTIALLY_FIXED` | 1 |
| `UNCERTAIN` | 1 |
| **Total Actionable** | **4** |
| Non-Actionable Items Excluded | 1 |

| Verdict | Finding | Code reference |
| --- | --- | --- |
| 🔴 `STILL_PRESENT` | The signature check still uses a comparison that can reveal timing information. | src/webhooks/verify.py:48 |
| 🟠 `PARTIALLY_FIXED` | Missing TTL expiration and payload-conflict handling for idempotency keys. | src/webhooks/idempotency.py:29-37 |
| 🟠 `UNCERTAIN` | The Redis migration's eviction policy still needs verification in staging. | — |
| 🟢 `FIXED` | Timestamps older than five minutes are rejected. | src/webhooks/legacy_adapter.py:21-24 |

> **WARNING — Resolved Threads Still Unfixed:** 1 review thread(s) marked as resolved on GitHub still exhibit unfixed or only partially fixed issues in the latest commit:
>
> - **STILL_PRESENT** ([PRRT_kwDOExample1](https://github.com/acme/payments/pull/184#discussion_r1001), `src/webhooks/verify.py`): Non-constant-time `==` comparison used on HMAC signature; timing side-channel remains after refactor.

## Review Freshness Warnings

### Demonstrably Affected by Subsequent Commits (`OLDER_AFFECTED`)

Reviews performed before later commits that touched the same file or PR scope:

- [PRRT_kwDOExample1](https://github.com/acme/payments/pull/184#discussion_r1001) (`src/webhooks/verify.py`, review commit `3a11b2c`): 2 commit(s) landed after `3a11b2c` and modified `src/webhooks/verify.py`.
- [PRRT_kwDOExample2](https://github.com/acme/payments/pull/184#discussion_r1002) (`src/webhooks/idempotency.py`, review commit `3a11b2c`): 2 commit(s) landed after `3a11b2c` and modified `src/webhooks/idempotency.py`.

### Missing Review Commit Metadata (`UNKNOWN_COMMIT`)

Reviews lacking commit SHA metadata in GitHub API responses:

- [IC_kwDOExample4](https://github.com/acme/payments/pull/184#issuecomment-2004) (`general`, review commit `none`): Review item has no associated commit SHA.

### Chronologically Older, Reviewed File Untouched (`OLDER_UNAFFECTED`)

Reviews performed on earlier commits where subsequent commits did not modify the reviewed file:

- [PRRT_kwDOExample3](https://github.com/acme/payments/pull/184#discussion_r1003) (`src/legacy_signature.py`, review commit `1c09f44`): 3 commit(s) landed after `1c09f44`, but none touched the reviewed file.

## Detailed Findings

### 1. [STILL_PRESENT] `src/webhooks/verify.py`

> **Resolved-Thread Discrepancy:** This thread is marked **Resolved** on GitHub, but verification against the latest code indicates the issue is **STILL_PRESENT**.

- **GitHub Reference:** [GitHub Comment](https://github.com/acme/payments/pull/184#discussion_r1001) (ID: `PRRT_kwDOExample1`, Source: `review_thread`)
- **Reviewer:** `@sec-reviewer`
- **GitHub Thread Status:** Resolved (by `@pr-author`), Outdated
- **Review Freshness:** `OLDER_AFFECTED` — 2 commit(s) landed after `3a11b2c` and modified `src/webhooks/verify.py`.
- **Code Location:** `src/webhooks/verify.py`
- **Original Issue:** Non-constant-time `==` comparison used on HMAC signature; timing side-channel remains after refactor.
- **Code References:** `src/webhooks/verify.py:48`
- **Verification Evidence:** Later commit `7f82a10` moved signature parsing into `verify_hmac_header` and stripped whitespace, causing GitHub to mark the thread outdated, and the author marked it resolved. However, line 48 in `HEAD` (`9e41d0c`) still compares `expected_sig == provided_sig` using `==` instead of `hmac.compare_digest`.

### 2. [PARTIALLY_FIXED] `src/webhooks/idempotency.py`

- **GitHub Reference:** [GitHub Comment](https://github.com/acme/payments/pull/184#discussion_r1002) (ID: `PRRT_kwDOExample2`, Source: `review_thread`)
- **Reviewer:** `@backend-lead`
- **GitHub Thread Status:** Unresolved
- **Review Freshness:** `OLDER_AFFECTED` — 2 commit(s) landed after `3a11b2c` and modified `src/webhooks/idempotency.py`.
- **Code Location:** `src/webhooks/idempotency.py`
- **Original Issue:** Missing TTL expiration on stored idempotency keys and missing conflict error when payload hash differs for the same key.
- **Code References:** `src/webhooks/idempotency.py:29-37`, `src/webhooks/idempotency.py:44`
- **Verification Evidence:** Lines 29–37 now set `ttl_seconds=86400` when calling `store.set(...)`, fixing the unbounded retention issue. However, line 44 still returns the cached response immediately when the key exists without comparing the incoming request payload digest against `record.payload_sha256`.

### 3. [UNCERTAIN] `issue_comment`

- **GitHub Reference:** [GitHub Comment](https://github.com/acme/payments/pull/184#issuecomment-2004) (ID: `IC_kwDOExample4`, Source: `issue_comment`)
- **Reviewer:** `@ops-reviewer`
- **GitHub Thread Status:** N/A (not a thread)
- **Review Freshness:** `UNKNOWN_COMMIT` — Review item has no associated commit SHA.
- **Code Location:** No file path (general PR/review comment)
- **Original Issue:** Please verify the Redis migration sets `maxmemory-policy` in staging before merging.
- **Code References:** None verified
- **Verification Evidence:** External staging Redis configuration cannot be verified from repository PR diffs alone, and the issue comment carries no commit SHA or target file path.

### 4. [FIXED] `src/legacy_signature.py`

- **GitHub Reference:** [GitHub Comment](https://github.com/acme/payments/pull/184#discussion_r1003) (ID: `PRRT_kwDOExample3`, Source: `review_thread`)
- **Reviewer:** `@sec-reviewer`
- **GitHub Thread Status:** Resolved (by `@pr-author`), Outdated
- **Review Freshness:** `OLDER_UNAFFECTED` — 3 commit(s) landed after `1c09f44`, but none touched the reviewed file.
- **Code Location:** `src/legacy_signature.py` -> renamed to `src/webhooks/legacy_adapter.py`
- **Original Issue:** Reject timestamps older than 5 minutes (`300s`) to prevent replay attacks.
- **Code References:** `src/webhooks/legacy_adapter.py:21-24`
- **Verification Evidence:** File was renamed from `src/legacy_signature.py` to `src/webhooks/legacy_adapter.py`. In `HEAD`, lines 21–24 compute `abs(now_ts - event_ts) > 300` and raise `ReplayWindowError` before signature verification proceeds.

## Findings Requiring Human Verification

- [ ] **[STILL_PRESENT]** [PRRT_kwDOExample1](https://github.com/acme/payments/pull/184#discussion_r1001) (`src/webhooks/verify.py`): Replace `==` with `hmac.compare_digest` even though the GitHub thread was marked resolved.
- [ ] **[PARTIALLY_FIXED]** [PRRT_kwDOExample2](https://github.com/acme/payments/pull/184#discussion_r1002) (`src/webhooks/idempotency.py`): Payload digest mismatch handling on reused idempotency keys is still missing.
- [ ] **[UNCERTAIN]** [IC_kwDOExample4](https://github.com/acme/payments/pull/184#issuecomment-2004) (`general`): Confirm external staging Redis `maxmemory-policy` configuration out-of-band.

## Audit Limitations & Metadata Warnings

- **Static Verification Only:** No repository code, test suites, or build scripts were executed during this audit; runtime behavior and test coverage are unverified unless separately inspected.
- IC_kwDOExample4: missing_commit_sha
