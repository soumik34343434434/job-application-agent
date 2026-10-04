# Job Application Agent — v0.5 (Naukri browser)

A local-first personal job application assistant for targeted Naukri workflows. It combines the v0.4 matching/preparation engine with a Playwright browser controller that uses a persistent browser profile for Naukri session state.

## Your targeting profile

- Minimum CTC: ₹8 LPA
- Preferred CTC: ₹8–16 LPA
- Current CTC: ₹4.5 LPA
- Notice period: 60 days
- Preferred locations: Remote, Pune, Noida, Mumbai, Kolkata, Hyderabad/Secunderabad, Gurgaon/Gurugram, Delhi NCR, Bangalore/Bengaluru, Ahmedabad
- Relocation: yes for preferred locations
- Target companies: any
- Daily application cap: 10

## v0.5 browser layer

1. Persistent local Chromium/Chrome profile under `.browser/naukri_profile`.
2. Optional OS credential-store integration for Naukri email/username and password.
3. Automatic login when the authenticated session is absent.
4. Search by role + location using Naukri's visible search UI, with a public search-page fallback.
5. Extract Naukri job links and card metadata into the existing SQLite tracker.
6. Score and prepare applications using the v0.4 intelligence layer.
7. Open a job, click the visible Apply control, upload the supplied resume, and fill only verified/common fields.
8. External application sites are detected and handed back to the user instead of being blindly submitted.
9. CAPTCHA, OTP, suspicious-login verification, or unknown eligibility questions trigger a human takeover.
10. `--auto-submit` enables actual submission; without it, forms are prepared but not submitted.
11. Existing duplicate detection and the 10/day submission cap remain enforced.

## First-time setup

```bash
cd job_application_agent
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
python -m playwright install chromium
python -m src.cli init-db
python -m src.cli naukri setup-credentials
```

The `setup-credentials` command uses the OS credential store when available. It does not place the password in `profile.json`, SQLite, the source tree, logs, or the ZIP. On systems without a usable OS keyring, set `NAUKRI_EMAIL` and `NAUKRI_PASSWORD` in the local environment instead.

## Browser/login

```bash
python -m src.cli naukri login
```

If you do not have a Naukri account yet, the assisted registration flow is:

```bash
python -m src.cli naukri create-account
```

It fills stable profile fields from `data/profile.json`, uploads the supplied resume when the form exposes a file input, and hands you the browser for OTP/verification or any fields that changed.

A visible browser opens. If the stored credentials work, the agent completes the ordinary login flow. If Naukri asks for OTP/CAPTCHA/security verification, the browser stays open and waits for you to finish that step.

The same persistent browser profile is reused later, so a still-valid Naukri session can be reused without logging in again. Naukri sessions can expire; in that case the login routine runs again.

## Search + ingest

Single search:

```bash
python -m src.cli naukri search --query "Cybersecurity Engineer" --location "Kolkata"
```

Targeted batch scan:

```bash
python -m src.cli naukri scan --max-queries 6 --per-query 12
```

## Apply from the review queue

Prepare only:

```bash
python -m src.cli naukri apply --limit 10
```

Actually submit eligible Naukri-hosted applications:

```bash
python -m src.cli naukri apply --limit 10 --auto-submit
```

The agent never invents work-authorization/sponsorship answers. Those questions stop the workflow for human input. It also stops on CAPTCHA/security challenges instead of attempting to bypass them.

## Dashboard

```bash
uvicorn src.webapp:app --reload
```

Then open the local URL. The dashboard remains the place to review score, application prep, queue status, and submission count.

## File layout

- `data/profile.json` — verified profile and targeting preferences
- `data/resumes/01_General_Cybersecurity_Analyst.pdf` — supplied resume
- `data/applications.db` — local tracker
- `src/scorer.py` — job fit scoring
- `src/tailoring.py` — application preparation
- `src/db.py` — persistence, duplicates, daily cap and events
- `src/credentials.py` — OS-keyring credential helper
- `src/naukri.py` — persistent browser, login, search, extraction, application flow
- `src/webapp.py` — local dashboard
- `tests/` — regression tests

## Important limitations

Naukri's live DOM and application flows can change. The agent intentionally uses multiple selector strategies and saves screenshots under `outbox/browser_artifacts` when a human takeover is needed. External-company applications are not blindly submitted.

### macOS login test

For the first Naukri login test, run:

```bash
python3 -m src.cli naukri login
```

The login command now keeps Chrome open after authentication. Press Enter in the VS Code terminal when you want the agent to close the browser.
