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
- Unit/backtest suite: **23 passed** in the current targeted regression set.
- Live authenticated Naukri session: **passing**; dedicated Chrome reports an authenticated jobseeker session.
- Live Apply-surface classification: **passing** on real Naukri pages. A current OpenLM listing exposes Naukri's visible `#apply-button` and is classified `INTERNAL`; current Riskman, Travarsa, and Bizoforce listings expose `Apply on company site` and are classified `EXTERNAL`.
- Unknown Apply surfaces now fail closed as `APPLY_SURFACE_UNKNOWN` with bounded diagnostics rather than `APPLY_BUTTON_NOT_FOUND`.
- Internal Apply click failures now fail closed as `APPLY_CLICK_FAILED` with diagnostics.
- Sensitive eligibility/work-authorization questions remain hard stops for unattended submission.
- Required-field detection and confirmation-state detection remain covered by synthetic browser regression tests.
- Submitted today during validation: **0**.
- Scheduled profile remains **`auto_submit=false`**.
- Real internal application submission with positive confirmation: **not performed yet**; the live validation intentionally stopped before clicking a real one-click Apply control.

## Next gates
1. Keep live Apply classification stable across more current Naukri listings.
2. Add a dedicated safe form-entry probe for an internal application flow that is known to require a form rather than one-click submission.
3. Verify all required/common fields are populated correctly in that form.
4. Confirm no unknown/sensitive eligibility question is present.
5. Only then enable a controlled real submission test and require positive confirmation before recording `SUBMITTED`.
6. Confirm the database records `SUBMITTED` and the 10/day cap remains enforced.
7. Then enable unattended scheduled applications.


## Audit rule
No daily unattended application scheduling is enabled until the internal submission + positive confirmation gate passes.

The detailed GitHub Actions logs remain the source for CI execution. This file records the higher-level engineering progress and is updated during each work cycle.
