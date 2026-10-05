# Agent Work Progress

This file is the visible audit trail for the Naukri agent build.

## Current cycle — 2026-10-05
**Goal:** harden the complete Naukri workflow before enabling unattended daily applications.

**Current status:** live local browser work is active on the authenticated dedicated Chrome profile. The latest local fixes are pushed to `main`.

**Working on now:** OR-style Naukri retrieval: keep all 30 role intents, canonicalize them with autocomplete, run one Naukri search per role with `2 years` and unrestricted location, union/dedupe the result pool, then enforce preferred locations as an OR in authoritative job details and route candidates through scoring/safe application gates.

## What has been completed
- Persistent dedicated Naukri Chrome over CDP.
- Real Naukri job-listing URL extraction.
- Full live job-description extraction.
- Profile-based scoring and hard filtering.
- Application preparation and queue.
- 10/day submission cap.
- Sensitive eligibility/sponsorship/citizenship/clearance questions block automatic submission.
- Required-field guard blocks submission when required fields remain unanswered.
- Current Naukri collapsed-header search trigger hardened for the live `Search jobs here` button.
- Experience selection hardened around the live `2 years` option.
- External employer-hosted applications are classified as `EXTERNAL_APPLICATION` instead of selector failures.
- Unattended discovery no longer blocks on a human-verification challenge for one query/listing.
- Default discovery checks each of the 30 requested Tier 1/Tier 2 role phrases against Naukri's live autocomplete before searching.
- Harmless designation variants are normalized (for example `Cybersecurity Analyst` -> Naukri `Cyber Security Analyst`; `Identity & Access Management` -> `Identity access management`).
- Unsupported related suggestions are not treated as matches (for example `IAM Engineer` is not accepted from `IAM Support Engineer`).
- The `SOC Analyst L2 L3` group resolves to Naukri's recognized `SOC Analyst 2` variant; generic `SOC Analyst` is not added.
- Preferred-location logic is now OR-based and applied locally from authoritative job details, avoiding the unsupported 30×13 search matrix.
- Location aliases are normalized (`Bangalore`/`Bengaluru`, `Gurgaon`/`Gurugram`, `Secunderabad`/`Hyderabad`, `Delhi / NCR`/`Delhi NCR`) before preferred-location matching.
- Naukri autocomplete refreshes are given a bounded second wait to avoid stale suggestions being mistaken for the next role.
- Non-interactive search routes homepage verification challenges through the safe human-blocker path instead of waiting for terminal input.
- Live search has a bounded retry for the authenticated header/navigation race.
- The scan reuses the authenticated browser session across all role searches rather than re-validating login for every query.
- Scorer backtest coverage now includes strong-role positives, GRC/audit/support/offensive/automotive negatives, fresher/under-experience cases, narrow Okta/Qualys roles, and location-alias edge cases.

## Validation status
- Python source compilation: **passing locally**.
- Unit/backtest suite: **16 passed** in the targeted regression set.
- Live authenticated Naukri search: **passing**; a live `Vulnerability Management` role search returned **20 real job cards**; the new OR-style scan path completed its smoke run without submission.
- Database scorer backtest: **169 stored job rows** recomputed with the improved scorer; **18** scored >=70 and **6** scored >=85 at backtest time.
- Backtest artifact: `outbox/browser_artifacts/model_backtest_report.json`.
- Internal Naukri application-form path: **passing in prepare-only mode**; a real Apply form reached `READY_TO_SUBMIT`.
- External application routing: **passing**; a real employer-hosted listing returned `EXTERNAL_APPLICATION`.
- Submitted today during validation: **0**.
- Real internal `85+` auto-submit with positive confirmation: **not yet proven**.
- Daily unattended application scheduling: **not enabled**.

## Next gates
1. Run a targeted internal Naukri application with a score of at least 85.
2. Verify all required/common fields are populated correctly.
3. Confirm no unknown/sensitive eligibility question is present.
4. Submit once under the existing safety gates and positively detect the success state.
5. Confirm the database records `SUBMITTED` and the 10/day cap remains enforced.
6. Then enable unattended daily autopilot.

## Audit rule
No daily unattended application scheduling is enabled until the internal submission + positive confirmation gate passes.

The detailed GitHub Actions logs remain the source for CI execution. This file records the higher-level engineering progress and is updated during each work cycle.
