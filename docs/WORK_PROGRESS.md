# Agent Work Progress

This file is the visible audit trail for the Naukri agent build.

## Current cycle — 2026-10-05
**Goal:** prove the complete Naukri workflow before enabling automatic applications.

**Current status:** latest validation commit `665631ee59faf85384c6163d12a35f9918ecbdf7` is queued in GitHub Actions (run #37229667766).

**Working on now:** live Naukri browser reliability + safe application-form handling.

## What has been completed
- Persistent dedicated Naukri Chrome over CDP.
- Real Naukri job-listing URL extraction.
- Full live job-description extraction.
- Profile-based scoring and hard filtering.
- Application preparation and queue.
- 10/day submission cap.
- Sensitive eligibility/sponsorship/citizenship/clearance questions block automatic submission.
- Required-field guard blocks submission when required fields remain unanswered.
- Browser session/runtime data removed from GitHub.
- CI concurrency added so stale runs are cancelled during rapid iteration.

## Validation status
- Normal test suite: passing in recent runs.
- Public Naukri smoke probe: passing in recent runs.
- Live Mac Naukri regression: **not yet fully passing**.
- Discovery pipeline has previously reached: **search -> real job URLs -> full JD -> scoring**.
- Application submission has **not** been declared production-ready yet.

## Next gates
1. Live Naukri search works consistently.
2. Search -> listings -> full JD -> scoring passes.
3. Application form filling works with verified data.
4. Unknown/sensitive questions stop safely.
5. Submission success is positively verified and recorded.
6. Only then enable daily automation, capped at 10/day.

## Audit rule
No automatic daily applications are enabled until every gate above passes.

The detailed GitHub Actions logs are the source for test execution. This file records the higher-level engineering progress and is updated during each work cycle.
