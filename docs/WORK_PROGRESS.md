# Agent Work Progress

This file is the visible audit trail for the Naukri agent build.

## Current cycle — 2026-10-05
**Goal:** harden the complete Naukri workflow before enabling unattended daily applications.

**Current status:** live local browser work is active on the authenticated dedicated Chrome profile. The latest local fixes are pushed to `main`.

**Working on now:** designation-aware Naukri discovery: check each Tier 1/Tier 2 role against live autocomplete, use Naukri's canonical designation when a harmless naming variant exists, search all preferred locations together, then route candidates through safe application gates.

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
- Default discovery sends all 13 preferred locations together instead of looping city-by-city.
- Multi-location search preserves the full location string instead of selecting the first suggestion.
- Naukri autocomplete refreshes are given a bounded second wait to avoid stale suggestions being mistaken for the next role.
- Non-interactive search routes homepage verification challenges through the safe human-blocker path instead of waiting for terminal input.
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
