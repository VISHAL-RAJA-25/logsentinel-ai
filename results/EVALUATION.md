# Evaluation results

Evaluated on **15 injected failure scenarios** of a FastAPI demo app (fresh run of all incidents).

| Metric | Result |
|---|---|
| Correct root cause ranked #1 | 13 / 15 |
| Correct root cause in the top 3 | 14 / 15 |
| Problem category classified correctly | 15 / 15 |
| Patch validated in the sandbox, no review warnings | 12 / 15 |
| Patch passed but flagged for human review | 3 / 15 |
| Patch rejected, none produced, or sandbox unavailable | 0 / 15 |
| Time per investigation (median / 90th percentile) | 25.6 s / 54.0 s |

## Per incident

| ID | Incident | Diagnosis | Category | Patch | Tries | Time |
|---|---|---|---|---|---|---|
| 001 | Login with unknown email crashes | top-1 | ok | validated | 1 | 8.77 s |
| 002 | Signup crashes: query uses a column that does not exist | top-1 | ok | validated | 1 | 5.88 s |
| 003 | Payment crashes: required environment variable is not set | top-1 | ok | needs_review | 3 | 73.99 s |
| 004 | Payment for a non-existent user fails with a database error | missed | ok | validated | 3 | 54.01 s |
| 005 | Normal payments rejected after a bad configuration value | top-1 | ok | validated | 1 | 25.19 s |
| 006 | Signup times out waiting for a database connection | top-1 | ok | validated | 1 | 32.24 s |
| 007 | Signup fails: required package is not installed | top-1 | ok | needs_review | 3 | 34.06 s |
| 008 | Payment crashes: fee rate from config is a string | top-1 | ok | validated | 1 | 4.22 s |
| 009 | Payment fails after saving: cannot write receipt file | top-1 | ok | validated | 1 | 24.91 s |
| 010 | Signup crashes for users with a one-word name | top-1 | ok | validated | 1 | 27.92 s |
| 011 | Payments fail: payment gateway refuses connections | top-3 only | ok | needs_review | 3 | 119.94 s |
| 012 | Signup crashes with maximum recursion depth exceeded | top-1 | ok | validated | 1 | 25.42 s |
| 013 | Every login crashes after password comparison change | top-1 | ok | validated | 1 | 25.58 s |
| 014 | Duplicate signup returns 500 with a unique constraint error | top-1 | ok | validated | 1 | 25.6 s |
| 015 | Payment saved but request fails: raw SQL uses wrong table | top-1 | ok | validated | 1 | 5.34 s |

## Honest limits

- These are **development scenarios**: the classifier rules and prompts were tuned while looking at them, so the numbers are optimistic. Unseen bugs are the real test.
- In every scenario the newest commit is the bug, which makes diagnosis easier than in most real incidents.
- **Validated** means: the bug reproduced in a no-network container, the app's tests passed, the failing request stopped crashing, rule-based checks found no masking tricks, and an AI reviewer agreed. It is not proof of correctness, so a human reviews every patch and it opens only as a draft pull request.
- Some incidents (a missing secret, an uninstalled package, a service that is down) cannot be fixed by editing code. The system flags them for a human instead of faking a fix.
- Diagnosis was graded by an LLM judge against the answer key (keyword matching as a fallback), so the grader itself can be wrong.
- The model's stated confidence is not calibrated: it averaged 91% when right and 90% when wrong.
- Times include waiting for free-tier LLM rate limits, so the median is the fairer figure.
