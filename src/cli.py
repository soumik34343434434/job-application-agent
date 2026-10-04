import argparse
import json
import re
from pathlib import Path

from .db import (
    init_db, add_job, list_jobs, find_duplicate, get_job, save_prep,
    set_status, review_queue, submitted_count_today,
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
    print("After you finish logging in, leave that Chrome window open anddef cmd_naukri_login(args):
    from .naukri import NaukriAgent, AGENT_BROWSER_DIR, setup_agent_browser

    print("Checking the dedicated Naukri browser profile.")
    print(f"Persistent profile: {AGENT_BROWSER_DIR}")
    print("Your normal Chrome profile remains untouched.")

    agent = NaukriAgent(headless=False, slow_mo=args.slow_mo)
    try:
        agent.__enter__()
    except RuntimeError as exc:
        raise SystemExit(str(exc))

    try:
        agent.open_home()
        if agent.is_logged_in():
            print("Naukri session is authenticated.")
            print("The dedicated Chrome can remain open; Playwright will detach cleanly.")
            return

        print()
        print("Naukri is not logged in in the dedicated profile.")
        print("Use the dedicated Chrome window that is already open.")
        print("Complete Naukri -> Login -> Sign in with Google there.")
        print("Leave that Chrome open; then rerun this command.")
        print()
    finally:
        agent.__exit__(None, None, None)
ef main():
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
    x = ns.add_parser("setup-browser"); x.set_defaults(func=cmd_naukri_setup_browser)
    x = ns.add_parser("login"); x.add_argument("--slow-mo", type=int, default=150); x.set_defaults(func=cmd_naukri_login)
    x = ns.add_parser("search"); x.add_argument("--query", required=True); x.add_argument("--location", default=""); x.add_argument("--slow-mo", type=int, default=150); x.set_defaults(func=cmd_naukri_search)
    x = ns.add_parser("scan"); x.add_argument("--max-queries", type=int, default=6); x.add_argument("--per-query", type=int, default=12); x.add_argument("--slow-mo", type=int, default=150); x.set_defaults(func=cmd_naukri_scan)
    x = ns.add_parser("apply"); x.add_argument("--limit", type=int, default=10); x.add_argument("--auto-submit", action="store_true", help="Actually submit forms; omit for prepare-only mode."); x.add_argument("--slow-mo", type=int, default=150); x.set_defaults(func=cmd_naukri_apply)

    x = ns.add_parser("autopilot"); x.add_argument("--scan-queries", type=int, default=4); x.add_argument("--per-query", type=int, default=12); x.add_argument("--limit", type=int, default=10); x.add_argument("--auto-submit", action="store_true", help="Actually submit forms; omit for prepare-only mode."); x.add_argument("--slow-mo", type=int, default=150); x.set_defaults(func=cmd_naukri_autopilot)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
