# Agent Work Progress

This file is the visible audit trail for the Naukri agent build.

## Current cycle — 2026-10-05
**Goal:** harden the complete Naukri workflow before enabling unattended daily applications.

**Current status:** live local browser work is active on the authenticated dedicated Chrome profile. The latest local fixes are pushed to `main`.

**Working on now:** bounded live discovery + safe application routing.

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
- Discovery is now bounded by `per_query` before expensive full-JD ingestion.
- Live search has a bounded retry for the authenticated header/navigation race.

## Validation status
- Python source compilation: **passing locally**.
- Live authenticated Naukri search: **passing**; real listing URLs returned.
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
