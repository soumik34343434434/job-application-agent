import argparse
import os
import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .db import (
    init_db, add_job, list_jobs, find_duplicate, get_job, save_prep,
    set_status, review_queue, submitted_count_today,
    start_agent_run, finish_agent_run, latest_agent_run, last_completed_agent_run,
)
from .scorer import score_job
from .tailoring import prepare_application, prep_to_dict, fingerprint_job
from .credentials import interactive_setup

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "data" / "profile.json"


def load_profile():
    return json.loads(PROFILE.read_text(encoding="utf-8"))


def cmd_score(args):
    result = score_job(args.title, args.description, args.company or "")
    print(json.dumps({"score": result.score, "recommendation": result.recommendation, "matched": result.matched, "penalties": result.penalties, "reasons": result.reasons}, indent=2))


def cmd_add(args):
    desc = args.description
    if args.description_file:
        desc = Path(args.description_file).read_text(encoding="utf-8")
    fp = fingerprint_job(args.company, args.title, args.url)
    dup = find_duplicate(args.company, args.title, args.url, fp)
    if dup:
        print(f"Duplicate detected: existing job #{dup['id']} | {dup['company']} | {dup['title']}")
        return
    result = score_job(args.title, desc, args.company)
    job_id = add_job(args.company, args.title, args.url, desc, result.score, result.recommendation, fp)
    print(f"Added job #{job_id}: {result.score}/100 ({result.recommendation})")
    if result.score >= load_profile()["targeting"]["minimum_match_score_for_review"]:
        prep = prepare_application(args.title, desc, load_profile(), args.company)
        save_prep(job_id, json.dumps(prep_to_dict(prep), ensure_ascii=False))
        print("Application prep created: READY_FOR_REVIEW")


def cmd_prepare(args):
    job = get_job(args.job_id)
    if not job:
        raise SystemExit(f"Job #{args.job_id} not found")
    prep = prepare_application(job["title"], job["description"] or "", load_profile(), job["company"])
    payload = prep_to_dict(prep)
    save_prep(args.job_id, json.dumps(payload, ensure_ascii=False))
    outbox = ROOT / "outbox"
    outbox.mkdir(exist_ok=True)
    packet = {"job_id": args.job_id, "company": job["company"], "title": job["title"], "url": job["url"], "prep": payload}
    (outbox / f"job_{args.job_id}_application_packet.json").write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(packet, ensure_ascii=False, indent=2))


def cmd_list(args):
    for row in list_jobs():
        print(f"#{row['id']} | {row['score'] or 0:>3}/100 | {row['recommendation']:<13} | {row['company']} | {row['title']} | {row['status']}")


def cmd_review(args):
    for row in review_queue():
        print(f"#{row['id']} | {row['score']}/100 | {row['company']} | {row['title']} | {row['status']}")


def cmd_status(args):
    try:
        set_status(args.job_id, args.status, args.note or "", load_profile()["targeting"]["max_daily_applications"])
    except ValueError as e:
        raise SystemExit(str(e))
    print(f"Job #{args.job_id} -> {args.status.upper()}. Submitted today: {submitted_count_today()}")


def cmd_naukri_setup(args):
    print(interactive_setup())


def cmd_naukri_create_account(args):
    from .naukri import NaukriAgent
    with NaukriAgent(headless=False, slow_mo=args.slow_mo) as agent:
        print(agent.create_account())


def cmd_naukri_export_storage_state(args):
    """Export the authenticated Naukri browser state for CI bootstrap."""
    from .naukri import NaukriAgent

    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    with NaukriAgent(headless=False, slow_mo=0) as agent:
        agent.ensure_logged_in(interactive=True)
        agent.context.storage_state(path=str(output))
    print(f"Exported Naukri storage state to {output}")


def cmd_naukri_setup_browser(args):
    from .naukri import setup_agent_browser, AGENT_BROWSER_DIR, AGENT_CDP_ENDPOINT

    print("Opening/checking the dedicated Naukri Chrome.")
    print(f"Profile data: {AGENT_BROWSER_DIR}")
    print(f"Automation endpoint: {AGENT_CDP_ENDPOINT}")
    print()
    print("The dedicated Chrome will stay open.")
    print("Log into Naukri manually there once using Login -> Sign in with Google.")
    print("You do NOT need to close that Chrome for the agent to use it.")
    print("Your normal Chrome profile remains untouched.")
    print()

    try:
        setup_agent_browser()
    except RuntimeError as exc:
        raise SystemExit(str(exc))

    print("Dedicated Naukri Chrome is running.")
    print("Finish the manual login there, then leave the Chrome window open.")
    print("You can now run 'python3 -m src.cli naukri login' or 'scan'.")


def cmd_naukri_login(args):
    from .naukri import NaukriAgent, AGENT_BROWSER_DIR

    print("Checking the dedicated Naukri browser profile.")
    print(f"Persistent profile: {AGENT_BROWSER_DIR}")
    print("Your normal Chrome profile remains untouched.")

    try:
        with NaukriAgent(headless=False, slow_mo=args.slow_mo) as agent:
            agent.open_home()
            if agent.is_logged_in():
                print("Naukri session is authenticated.")
                print("Playwright detached successfully; the dedicated Chrome remains open.")
                return

            print()
            print("Naukri is not logged in in the dedicated profile.")
            print("Use the dedicated Chrome window that's already open.")
            print("Complete Naukri -> Login -> Sign in with Google.")
            print("Leave that Chrome open, finish login, then rerun this command.")
    except RuntimeError as exc:
        raise SystemExit(str(exc))


def cmd_naukri_inspect_search(args):
    from .naukri import NaukriAgent
    try:
        with NaukriAgent(headless=False, slow_mo=0) as agent:
            print(json.dumps(agent.inspect_search_control(), indent=2, ensure_ascii=False))
    except RuntimeError as exc:
        raise SystemExit(str(exc))


def cmd_naukri_inspect(args):
    from .naukri import NaukriAgent
    try:
        with NaukriAgent(headless=False, slow_mo=0) as agent:
            print(json.dumps(agent.inspect_current_page(), indent=2, ensure_ascii=False))
    except RuntimeError as exc:
        raise SystemExit(str(exc))


def cmd_naukri_search(args):
    from .naukri import NaukriAgent, print_jobs
    with NaukriAgent(headless=False, slow_mo=args.slow_mo) as agent:
        jobs = agent.search(args.query, args.location or "")
        new_jobs = agent.ingest(jobs)
        print(f"Found {len(jobs)} jobs; ingested {len(new_jobs)} new jobs.")
        print_jobs(new_jobs)


def cmd_naukri_scan(args):
    from .naukri import NaukriAgent, print_jobs
    with NaukriAgent(headless=False, slow_mo=args.slow_mo) as agent:
        jobs = agent.scan_default(max_queries=args.max_queries, per_query=args.per_query)
        print(f"Scanned/ingested {len(jobs)} new jobs.")
        print_jobs(jobs)


def cmd_naukri_enrich(args):
    from .naukri import NaukriAgent, print_jobs

    with NaukriAgent(headless=False, slow_mo=args.slow_mo) as agent:
        jobs = agent.enrich_existing_jobs(limit=args.limit)
        print(f"Refreshed {len(jobs)} existing real Naukri jobs from their live detail pages.")
        print_jobs(jobs)


def cmd_naukri_inspect_api(args):
    from .naukri_inspector import run_server
    print(f"Starting read-only Naukri inspector at http://{args.host}:{args.port}")
    print(f"Docs: http://{args.host}:{args.port}/docs")
    print("The inspector never clicks, fills, submits, or navigates Naukri.")
    run_server(host=args.host, port=args.port)


def cmd_naukri_job_detail(args):
    from .naukri import NaukriAgent
    from .cli import load_profile
    import json as _json

    row = get_job(args.job_id)
    if not row:
        raise SystemExit(f"Job #{args.job_id} not found.")
    if not row["url"] or "/job-listings-" not in row["url"].lower():
        raise SystemExit("The selected record is not a real Naukri job-listing URL.")

    with NaukriAgent(headless=False, slow_mo=args.slow_mo) as agent:
        job = agent._extract_job_detail(row["url"])
        if not job:
            raise SystemExit("Could not extract a complete live job detail page.")
        profile = load_profile()
        result = score_job(job.title, job.description, job.company)

    payload = {
        "job_id": row["id"],
        "url": job.url,
        "title": job.title,
        "company": job.company,
        "location": job.location,
        "salary": job.salary,
        "experience": job.experience,
        "description_length": len(job.description or ""),
        "description": job.description,
        "score": result.score,
        "recommendation": result.recommendation,
        "matched": result.matched,
        "penalties": result.penalties,
        "reasons": result.reasons,
    }
    print(_json.dumps(payload, indent=2, ensure_ascii=False))


def cmd_naukri_apply(args):
    from .naukri import NaukriAgent
    with NaukriAgent(headless=False, slow_mo=args.slow_mo) as agent:
        results = agent.auto_apply_from_queue(limit=args.limit, auto_submit=args.auto_submit)
        print(json.dumps(results, indent=2, ensure_ascii=False))
        print(f"Submitted today: {submitted_count_today()}/{load_profile()['targeting']['max_daily_applications']}")


def _scheduled_run_due(profile):
    schedule = profile.get("targeting", {}).get("application_schedule", {})
    interval_hours = float(schedule.get("interval_hours", 72))
    last = last_completed_agent_run()
    if not last:
        return True, None, interval_hours
    finished = datetime.fromisoformat(last["finished_at"])
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=timezone.utc)
    due_at = finished + timedelta(hours=interval_hours)
    now = datetime.now(timezone.utc)
    return now >= due_at, due_at, interval_hours


def cmd_naukri_schedule_status(args):
    profile = load_profile()
    schedule = profile.get("targeting", {}).get("application_schedule", {})
    latest = latest_agent_run()
    due, due_at, interval_hours = _scheduled_run_due(profile)
    payload = {
        "enabled": bool(schedule.get("enabled", True)),
        "applications_per_run": int(schedule.get("applications_per_run", 5)),
        "interval_hours": interval_hours,
        "auto_submit": bool(schedule.get("auto_submit", False)),
        "due_now": due,
        "next_due_at": due_at.isoformat() if due_at else "NOW",
        "latest_run": dict(latest) if latest else None,
        "submitted_today": submitted_count_today(),
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def cmd_naukri_scheduled_run(args):
    from .naukri import NaukriAgent

    profile = load_profile()
    targeting = profile.get("targeting", {})
    schedule = targeting.get("application_schedule", {})
    if not bool(schedule.get("enabled", True)) and not args.force:
        print("Scheduled application run is disabled in profile.json.")
        return

    due, due_at, _ = _scheduled_run_due(profile)
    if not due and not args.force:
        print(json.dumps({
            "status": "NOT_DUE",
            "next_due_at": due_at.isoformat() if due_at else None,
            "submitted_today": submitted_count_today(),
        }, indent=2, ensure_ascii=False))
        return

    latest = latest_agent_run()
    if latest and latest["status"] == "RUNNING":
        print(json.dumps({
            "status": "RUN_ALREADY_RUNNING",
            "run_id": latest["id"],
            "started_at": latest["started_at"],
        }, indent=2, ensure_ascii=False))
        return

    limit = min(
        int(args.limit or schedule.get("applications_per_run", 5)),
        int(targeting.get("max_daily_applications", 10)),
    )
    # One run key per minute prevents duplicate scheduler launches from doing
    # two identical application batches while still allowing a failed run to retry.
    run_key = "scheduled-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M")
    claimed = start_agent_run(run_key, limit)
    if claimed is None:
        print(json.dumps({"status": "DUPLICATE_RUN_IGNORED", "run_key": run_key}, indent=2))
        return

    auto_submit = bool(schedule.get("auto_submit", False))
    if os.getenv("GITHUB_ACTIONS") and auto_submit:
        # Second independent safety gate for unattended live submissions.
        # The profile alone can never enable real submissions in CI.
        auto_submit = os.getenv("UNATTENDED_LIVE_SUBMISSION", "").lower() == "true"
    results = []
    run_status = "COMPLETED"
    try:
        print(json.dumps({
            "run_id": claimed["id"],
            "run_key": run_key,
            "mode": "AUTO_SUBMIT" if auto_submit else "DRY_RUN_PREPARE_ONLY",
            "limit": limit,
        }, indent=2), flush=True)
        with NaukriAgent(headless=bool(os.getenv("GITHUB_ACTIONS")) or bool(os.getenv("NAUKRI_STORAGE_STATE_PATH")), slow_mo=args.slow_mo) as agent:
            jobs = agent.scan_default(max_queries=args.scan_queries, per_query=args.per_query)
            if getattr(agent, "_last_scan_error", None):
                raise RuntimeError(f"Naukri scan failed: {agent._last_scan_error}")
            results = agent.auto_apply_from_queue(limit=limit, auto_submit=auto_submit)
        submitted = sum(1 for item in results if item.get("outcome") == "SUBMITTED")
        finish_agent_run(run_key, run_status, json.dumps({"scanned_added": len(jobs), "results": results}, ensure_ascii=False), submitted)
        print(json.dumps({
            "status": run_status,
            "scanned_added": len(jobs),
            "application_results": results,
            "submitted_this_run": submitted,
            "submitted_today": submitted_count_today(),
            "next_run": "approximately 72 hours after this run completes",
        }, indent=2, ensure_ascii=False))
    except Exception as exc:
        run_status = "FAILED"
        finish_agent_run(run_key, run_status, json.dumps({"error": str(exc), "partial_results": results}, ensure_ascii=False), sum(1 for item in results if item.get("outcome") == "SUBMITTED"))
        raise


def cmd_naukri_autopilot(args):
    from .naukri import NaukriAgent
    with NaukriAgent(headless=False, slow_mo=args.slow_mo) as agent:
        jobs = agent.scan_default(max_queries=args.scan_queries, per_query=args.per_query)
        print(f"Scan complete. Added {len(jobs)} new target jobs.")
        results = agent.auto_apply_from_queue(
            limit=min(args.limit, load_profile()["targeting"]["max_daily_applications"]),
            auto_submit=args.auto_submit,
        )
        print(json.dumps(
            {
                "scanned_added": len(jobs),
                "application_results": results,
                "submitted_today": submitted_count_today(),
            },
            indent=2,
            ensure_ascii=False,
        ))


def main():
    init_db()
    p = argparse.ArgumentParser(description="Targeted job application assistant")
    sub = p.add_subparsers(required=True)

    x = sub.add_parser("init-db"); x.set_defaults(func=lambda a: print("Database initialized."))
    x = sub.add_parser("score"); x.add_argument("--title", required=True); x.add_argument("--description", required=True); x.add_argument("--company"); x.set_defaults(func=cmd_score)
    x = sub.add_parser("add-job"); x.add_argument("--company", required=True); x.add_argument("--title", required=True); x.add_argument("--url", required=True); x.add_argument("--description", default=""); x.add_argument("--description-file"); x.set_defaults(func=cmd_add)
    x = sub.add_parser("prepare"); x.add_argument("--job-id", type=int, required=True); x.set_defaults(func=cmd_prepare)
    x = sub.add_parser("list"); x.set_defaults(func=cmd_list)
    x = sub.add_parser("review-queue"); x.set_defaults(func=cmd_review)
    x = sub.add_parser("status"); x.add_argument("--job-id", type=int, required=True); x.add_argument("--status", required=True); x.add_argument("--note"); x.set_defaults(func=cmd_status)

    naukri = sub.add_parser("naukri")
    ns = naukri.add_subparsers(required=True)
    x = ns.add_parser("setup-credentials"); x.set_defaults(func=cmd_naukri_setup)
    x = ns.add_parser("create-account"); x.add_argument("--slow-mo", type=int, default=150); x.set_defaults(func=cmd_naukri_create_account)
    x = ns.add_parser("export-storage-state"); x.add_argument("--output", default="runtime/naukri.storage-state.json"); x.set_defaults(func=cmd_naukri_export_storage_state)
    x = ns.add_parser("setup-browser"); x.set_defaults(func=cmd_naukri_setup_browser)
    x = ns.add_parser("login"); x.add_argument("--slow-mo", type=int, default=150); x.set_defaults(func=cmd_naukri_login)
    x = ns.add_parser("inspect"); x.set_defaults(func=cmd_naukri_inspect)
    x = ns.add_parser("inspect-search"); x.set_defaults(func=cmd_naukri_inspect_search)
    x = ns.add_parser("search"); x.add_argument("--query", required=True); x.add_argument("--location", default=""); x.add_argument("--slow-mo", type=int, default=150); x.set_defaults(func=cmd_naukri_search)
    x = ns.add_parser("scan"); x.add_argument("--max-queries", type=int, default=30); x.add_argument("--per-query", type=int, default=12); x.add_argument("--slow-mo", type=int, default=150); x.set_defaults(func=cmd_naukri_scan)
    x = ns.add_parser("apply"); x.add_argument("--limit", type=int, default=10); x.add_argument("--auto-submit", action="store_true", help="Actually submit forms; omit for prepare-only mode."); x.add_argument("--slow-mo", type=int, default=150); x.set_defaults(func=cmd_naukri_apply)
    x = ns.add_parser("enrich"); x.add_argument("--limit", type=int, default=30); x.add_argument("--slow-mo", type=int, default=150); x.set_defaults(func=cmd_naukri_enrich)
    x = ns.add_parser("inspect-api");
    x.add_argument("--host", default="127.0.0.1");
    x.add_argument("--port", type=int, default=8765);
    x.set_defaults(func=cmd_naukri_inspect_api)
    x = ns.add_parser("job-detail"); x.add_argument("--job-id", type=int, required=True); x.add_argument("--slow-mo", type=int, default=150); x.set_defaults(func=cmd_naukri_job_detail)

    x = ns.add_parser("autopilot"); x.add_argument("--scan-queries", type=int, default=30); x.add_argument("--per-query", type=int, default=12); x.add_argument("--limit", type=int, default=10); x.add_argument("--auto-submit", action="store_true", help="Actually submit forms; omit for prepare-only mode."); x.add_argument("--slow-mo", type=int, default=150); x.set_defaults(func=cmd_naukri_autopilot)
    x = ns.add_parser("schedule-status"); x.set_defaults(func=cmd_naukri_schedule_status)
    x = ns.add_parser("scheduled-run")
    x.add_argument("--scan-queries", type=int, default=30)
    x.add_argument("--per-query", type=int, default=12)
    x.add_argument("--limit", type=int, default=None)
    x.add_argument("--force", action="store_true", help="Run immediately even when the 72-hour interval is not due.")
    x.add_argument("--slow-mo", type=int, default=0)
    x.set_defaults(func=cmd_naukri_scheduled_run)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
