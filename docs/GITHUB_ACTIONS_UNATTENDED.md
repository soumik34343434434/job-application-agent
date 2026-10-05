# GitHub Actions unattended deployment

This workflow runs the existing Naukri scheduler in a fresh Ubuntu runner and persists the two pieces of state the runner cannot keep on disk:

- `runtime/applications.db` — the SQLite application/job history.
- `runtime/naukri.storage-state.enc` — an encrypted Playwright browser session.

The repository is private. The browser session is encrypted with a key stored only in the GitHub Actions secret `NAUKRI_STATE_KEY`.

## One-time bootstrap from the Mac

Do this only after the dedicated Naukri Chrome on the Mac is authenticated.

1. Generate a random encryption key:

```bash
openssl rand -hex 32
```

Save the output somewhere safe. Add it to the repository at:

**Settings → Secrets and variables → Actions → New repository secret**

Name:

```
NAUKRI_STATE_KEY
```

2. Export the authenticated Naukri session:

```bash
cd /Users/sante/.job-application-agent-engineer/repo
source /Users/sante/.job-application-agent-engineer/venv/bin/activate
python3 -m src.cli naukri export-storage-state
```

This creates:

```
runtime/naukri.storage-state.json
```

Do **not** commit that file.

3. Encrypt it using the same key:

```bash
export NAUKRI_STATE_KEY='PASTE_THE_RANDOM_KEY_HERE'

openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt \
  -pass env:NAUKRI_STATE_KEY \
  -in runtime/naukri.storage-state.json \
  -out runtime/naukri.storage-state.enc

rm runtime/naukri.storage-state.json
```

4. Commit only the encrypted state:

```bash
git add runtime/naukri.storage-state.enc
git commit -m "chore: bootstrap encrypted Naukri session"
git push origin main
```

## First workflow run

The workflow is intentionally **prepare-only** while `data/profile.json` has `auto_submit=false`.

Run it manually from GitHub Actions with **workflow_dispatch**.

The first goal is to prove:

- the encrypted session decrypts;
- Naukri accepts the restored session on the GitHub runner;
- the normal scan works;
- the SQLite state is persisted;
- no application is submitted.

Only after the live internal application + positive confirmation gate has passed should live submission be enabled.

## Enabling real unattended submission

Two gates are required:

1. `data/profile.json` must have:

```json
"auto_submit": true
```

2. Repository Actions variable:

```
UNATTENDED_LIVE_SUBMISSION=true
```

The workflow code requires both. If either gate is missing, the scheduler runs in prepare-only mode.

## Session expiry

The agent never automates Google authentication and never bypasses CAPTCHA/OTP/security verification. If the restored Naukri session is no longer valid, the run fails safely rather than attempting a workaround.

At that point, re-authenticate the dedicated Naukri browser on the Mac, re-export the storage state, re-encrypt it, and push the new encrypted file.

## Persistence model

GitHub-hosted runners are ephemeral. The workflow therefore restores state at the start of every run and persists it before the runner is destroyed.

The existing SQLite 72-hour due check remains the source of truth, so the daily GitHub wake-up does not cause an application run until 72 hours have elapsed since the last completed run.
