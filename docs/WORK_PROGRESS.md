# Agent Work Progress

This file is a visible, auditable progress log for the Naukri agent build.

## Current cycle
- Goal: prove the end-to-end Naukri workflow before enabling automatic daily applications.
- Current focus: live browser/search reliability and application-form safety.
- Daily automatic application scheduler: DISABLED.

## Completed in recent cycles
- Persistent dedicated Naukri Chrome over CDP.
- Real Naukri job URL extraction and full JD extraction.
- Profile-based job scoring and hard filtering.
- Application queue and 10/day submission cap.
- Sensitive eligibility questions stop auto-submission.
- Required-field guard added before auto-submit.
- Browser session data removed from the Git repository.
- CI concurrency added so stale runs are cancelled.

## Current validation
- Normal unit/integration CI: passing in recent runs.
- Public Naukri smoke probe: passing in recent runs.
- Live Mac Naukri regression: still being validated; latest failures were related to live browser/search-surface state rather than the scoring engine.

## Next gates
1. Live Naukri search is consistently usable.
2. Search -> real listings -> full JD -> scoring passes.
3. Application form can be filled with verified fields.
4. Unknown/sensitive questions block submission.
5. Submission success is positively verified and recorded.
6. Only after all gates pass: enable daily automation, capped at 10/day.

## Rule
No automatic daily applications are enabled until all validation gates above pass.
