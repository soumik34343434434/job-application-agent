#!/usr/bin/env python3
import json
import os
import subprocess
import time
from datetime import datetime

REPO = "soumik34343434434/job-application-agent"
INTERVAL = int(os.getenv("WATCH_INTERVAL", "5"))


def gh_api(path):
    p = subprocess.run(
        ["gh", "api", path],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or "gh api failed")
    return json.loads(p.stdout)


def stage(title):
    t = (title or "").lower()
    if "application" in t or "submit" in t:
        return "Validating safe application-form behavior."
    if "search" in t or "naukri" in t:
        return "Validating the Naukri browser/search workflow."
    if "scheduler" in t or "daily" in t:
        return "Scheduler work; daily auto-apply remains disabled until validation passes."
    return "Running regression/engineering validation."


def main():
    print("Naukri Agent — LIVE STATUS")
    print(f"Repository: {REPO}")
    print(f"Polling every {INTERVAL}s. Press Ctrl+C to stop.\n")

    try:
        subprocess.run(["gh", "auth", "status"], check=True, capture_output=True, text=True)
    except Exception:
        print("GitHub CLI is not authenticated. Run: gh auth login")
        raise SystemExit(1)

    last_state = None

    while True:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            commit = gh_api(f"repos/{REPO}/commits/main")
            runs = gh_api(f"repos/{REPO}/actions/runs?per_page=1").get("workflow_runs", [])
            run = runs[0] if runs else {}

            sha = commit.get("sha", "")
            message = commit.get("commit", {}).get("message", "").splitlines()[0] if commit else ""
            run_id = run.get("id", "")
            status = run.get("status", "")
            conclusion = run.get("conclusion") or ""
            title = run.get("display_title", "")
            run_sha = run.get("head_sha", "")

            state = (sha, run_id, status, conclusion, run_sha)
            if state != last_state:
                print(f"[{now}]")
                print(f"  Current code: {sha}")
                print(f"  Latest commit: {message}")
                print(f"  GitHub Actions: run {run_id} — {status}" + (f", {conclusion}" if conclusion else ""))
                print(f"  Change being tested: {title}")
                print(f"  Agent stage: {stage(title)}")

                if status == "in_progress":
                    print("  Result: test is running on the Mac runner.")
                elif status == "queued" or status == "pending":
                    print("  Result: waiting for the Mac runner.")
                elif status == "completed" and conclusion == "success":
                    print("  Result: validation passed.")
                elif status == "completed":
                    print("  Result: validation failed; the next fix targets the failure.")
                print()
                last_state = state

        except Exception as exc:
            print(f"[{now}] Waiting: {exc}")

        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
