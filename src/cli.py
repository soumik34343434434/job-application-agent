import argparse
import json
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


def cmd_naukri_login(args):
    from .attached_browser import AttachedChrome

    browser = AttachedChrome(session="naukri", profile_dir_name="Default")
    print("Attaching to your already-running Chrome via the Playwright Extension...")
    print("Your existing Chrome profile, cookies, and logged-in sessions are reused.")
    print(browser.attach())
    print(browser.select_naukri_tab())

    info = browser.page_info()
    if "naukri.com" not in str(info.get("url", "")).lower():
        browser.goto("https://www.naukri.com/")
        info = browser.page_info()

    if browser.is_naukri_logged_in():
        print("Naukri session is already authenticated.")
    else:
        print("Naukri is not authenticated. Trying Google Sign-In in the existing Chrome tab...")
        if browser.click_google_sign_in():
            print("Google Sign-In clicked.")
            print("Complete Google account selection / 2-step verification in Chrome if Google asks.")
        else:
            print("Could not find a Google Sign-In control automatically.")
            print("Open the Naukri login page in the attached Chrome tab and complete it manually.")

        print("Waiting up to 60 seconds for the Naukri session to become authenticated...")
        import time
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                if browser.is_naukri_logged_in():
                    print("Naukri session is authenticated.")
                    break
            except Exception:
                pass
            time.sleep(1)

    info = browser.page_info()
    print(json.dumps({
        "url": info.get("url"),
        "title": info.get("title"),
        "naukri_logged_in": browser.is_naukri_logged_in(),
    }, indent=2, ensure_ascii=False))
    print("\nChrome stays open. The agent only detaches from it when this command ends.")


def cmd_naukri_scan(args):
    from .naukri import NaukriAgent, print_jobs
    with NaukriAgent(headless=False, slow_mo=args.slow_mo) as agent:
        jobs = agent.scan_default(max_queries=args.max_queries, per_query=args.per_query)
        print(f"Scanned/ingested {len(jobs)} new jobs.")
        print_jobs(jobs)


def cmd_naukri_search(args):
    from .naukri import NaukriAgent, print_jobs
    with NaukriAgent(headless=False, slow_mo=args.slow_mo) as agent:
        jobs = agent.search(args.query, args.location or "")
        new_jobs = agent.ingest(jobs)
        print(f"Found {len(jobs)} jobs; ingested {len(new_jobs)} new jobs.")
        print_jobs(new_jobs)


def cmd_naukri_apply(args):
    from .naukri import NaukriAgent
    with NaukriAgent(headless=False, slow_mo=args.slow_mo) as agent:
        results = agent.auto_apply_from_queue(limit=args.limit, auto_submit=args.auto_submit)
        print(json.dumps(results, indent=2, ensure_ascii=False))
        print(f"Submitted today: {submitted_count_today()}/{load_profile()['targeting']['max_daily_applications']}")




def cmd_naukri_autopilot(args):
    from .naukri import NaukriAgent
    with NaukriAgent(headless=False, slow_mo=args.slow_mo) as agent:
        jobs = agent.scan_default(max_queries=args.scan_queries, per_query=args.per_query)
        print(f"Scan complete. Added {len(jobs)} new target jobs.")
        results = agent.auto_apply_from_queue(limit=min(args.limit, load_profile()["targeting"]["max_daily_applications"]), auto_submit=args.auto_submit)
        print(json.dumps({"scanned_added": len(jobs), "application_results": results, "submitted_today": submitted_count_today()}, indent=2, ensure_ascii=False))

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
    x = ns.add_parser("login"); x.add_argument("--slow-mo", type=int, default=150); x.set_defaults(func=cmd_naukri_login)
    x = ns.add_parser("search"); x.add_argument("--query", required=True); x.add_argument("--location", default=""); x.add_argument("--slow-mo", type=int, default=150); x.set_defaults(func=cmd_naukri_search)
    x = ns.add_parser("scan"); x.add_argument("--max-queries", type=int, default=6); x.add_argument("--per-query", type=int, default=12); x.add_argument("--slow-mo", type=int, default=150); x.set_defaults(func=cmd_naukri_scan)
    x = ns.add_parser("apply"); x.add_argument("--limit", type=int, default=10); x.add_argument("--auto-submit", action="store_true", help="Actually submit forms; omit for prepare-only mode."); x.add_argument("--slow-mo", type=int, default=150); x.set_defaults(func=cmd_naukri_apply)

    x = ns.add_parser("autopilot"); x.add_argument("--scan-queries", type=int, default=4); x.add_argument("--per-query", type=int, default=12); x.add_argument("--limit", type=int, default=10); x.add_argument("--auto-submit", action="store_true", help="Actually submit forms; omit for prepare-only mode."); x.add_argument("--slow-mo", type=int, default=150); x.set_defaults(func=cmd_naukri_autopilot)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
