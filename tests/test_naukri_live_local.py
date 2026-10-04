import json
from pathlib import Path
from urllib.request import urlopen

import pytest

from src.naukri import NaukriAgent
from src.naukri_inspector import AGENT_CDP_ENDPOINT, NaukriInspector
from src.scorer import score_job


@pytest.mark.naukri_live
def test_authenticated_naukri_browser_surface():
    """
    Live regression against the user's own dedicated Chrome session.

    This test is intentionally limited to login/session + search-surface checks.
    It never submits a job application.
    """
    artifact_dir = Path("outbox/live_runner")
    artifact_dir.mkdir(parents=True, exist_ok=True)

    def write_json(name, data):
        (artifact_dir / name).write_text(
            json.dumps(data, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

    def diagnostics(stage, error=None):
        payload = {"stage": stage, "error": str(error) if error else None}
        try:
            with urlopen(f"{AGENT_CDP_ENDPOINT}/json/version", timeout=3) as response:
                payload["cdp_version"] = json.load(response)
        except Exception as exc:
            payload["cdp_version_error"] = str(exc)
        try:
            with urlopen(f"{AGENT_CDP_ENDPOINT}/json/list", timeout=3) as response:
                pages = json.load(response)
                payload["cdp_pages"] = [
                    {
                        "id": item.get("id"),
                        "type": item.get("type"),
                        "title": item.get("title"),
                        "url": item.get("url"),
                    }
                    for item in pages
                ]
        except Exception as exc:
            payload["cdp_pages_error"] = str(exc)

        try:
            if inspector.page is not None:
                payload["page_info"] = inspector.page_info()
                payload["visible_text"] = inspector.visible_text().get("text", "")[:6000]
                inspector.page.screenshot(
                    path=str(artifact_dir / "failure_page.png"),
                    full_page=True,
                )
        except Exception as exc:
            payload["page_capture_error"] = str(exc)

        write_json("failure.json", payload)

    inspector = NaukriInspector()
    try:
        try:
            inspector.connect()
        except Exception as exc:
            diagnostics("connect", exc)
            pytest.fail(f"Could not attach to dedicated Chrome: {exc}")

        try:
            write_json(
                "pages.json",
                [
                    {"title": p.title(), "url": p.url}
                    for p in inspector.context.pages
                ],
            )
        except Exception:
            pass

        info = inspector.page_info()
        if "naukri.com" not in (info.get("url") or "").lower():
            diagnostics("selected_page_not_naukri")
            pytest.fail(
                "Dedicated Chrome is reachable, but the selected tab is not Naukri: "
                f"{info}"
            )

        body = inspector.visible_text().get("text", "")
        lower = body.lower()
        if any(x in lower for x in ("captcha", "security verification", "access denied")):
            inspector.page.screenshot(
                path=str(artifact_dir / "security_gate.png"),
                full_page=True,
            )
            write_json(
                "security_gate.json",
                {"page": info, "text": body[:6000]},
            )
            pytest.skip(
                "Naukri is presenting a human/security gate; live runner captured the page state."
            )

        agent = NaukriAgent(headless=False, slow_mo=0)
        agent._pw = inspector._pw
        agent.browser = inspector.browser
        agent.context = inspector.context
        agent.page = inspector.page
        agent.page.set_default_timeout(12000)

        if not agent.is_logged_in():
            diagnostics("not_authenticated")
            pytest.fail("Dedicated Naukri Chrome is not authenticated.")

        agent.page.goto("https://www.naukri.com/", wait_until="domcontentloaded", timeout=20000)
        agent.page.wait_for_timeout(1200)

        if agent.needs_human_verification():
            agent.page.screenshot(
                path=str(artifact_dir / "security_gate_after_navigation.png"),
                full_page=True,
            )
            pytest.skip("Naukri presented a CAPTCHA/security gate during the live probe.")

        report = agent.inspect_search_control()
        (artifact_dir / "search_surface.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        agent.page.screenshot(path=str(artifact_dir / "search_surface.png"), full_page=True)

        inputs = report.get("inputs", [])
        placeholders = {
            (item.get("placeholder") or "").strip().lower()
            for item in inputs
        }

        expected = {
            "enter keyword / designation / companies",
            "select experience",
            "enter location",
        }
        missing = expected - placeholders
        if missing:
            write_json(
                "search_surface_failure.json",
                {
                    "missing": sorted(missing),
                    "observed_inputs": sorted(placeholders),
                    "report": report,
                },
            )
        assert not missing, (
            f"Naukri search surface changed. Missing placeholders: {sorted(missing)}; "
            f"observed inputs: {sorted(placeholders)}"
        )

        (artifact_dir / "page.json").write_text(
            json.dumps(info, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except pytest.skip.Exception:
        raise
    except Exception as exc:
        diagnostics("unexpected_exception", exc)
        raise
    finally:
        inspector.close()



@pytest.mark.naukri_live
def test_naukri_live_search_extract_and_score():
    """
    End-to-end dry run of the discovery pipeline against the user's own
    authenticated Naukri browser.

    It searches but never applies or submits. It validates:
      search form -> 2 years -> location -> real job-listing extraction ->
      authoritative detail extraction -> profile scoring.
    """
    artifact_dir = Path("outbox/live_runner")
    artifact_dir.mkdir(parents=True, exist_ok=True)

    inspector = NaukriInspector()
    agent = None
    try:
        inspector.connect()

        info = inspector.page_info()
        if "naukri.com" not in (info.get("url") or "").lower():
            pytest.fail(f"Dedicated Chrome is not currently on Naukri: {info}")

        agent = NaukriAgent(headless=False, slow_mo=0)
        agent._pw = inspector._pw
        agent.browser = inspector.browser
        agent.context = inspector.context
        agent.page = inspector.page
        agent.page.set_default_timeout(12000)

        if not agent.is_logged_in():
            pytest.fail("Dedicated Naukri Chrome is not authenticated.")

        agent.page.goto("https://www.naukri.com/", wait_until="domcontentloaded", timeout=20000)
        agent.page.wait_for_timeout(1200)

        if agent.needs_human_verification():
            agent.page.screenshot(path=str(artifact_dir / "dry_run_security_gate.png"), full_page=True)
            pytest.skip("Naukri presented a security gate before the dry-run search.")

        query = "Cybersecurity Engineer"
        location = "Kolkata"

        if not agent._fill_search_form(query, location):
            pytest.fail("Could not fill/submit the live Naukri search form.")

        if agent.needs_human_verification():
            agent.page.screenshot(path=str(artifact_dir / "dry_run_search_gate.png"), full_page=True)
            pytest.skip("Naukri presented a security gate during the dry-run search.")

        agent._set_relevance_sort()
        agent.page.wait_for_timeout(1200)

        if agent.needs_human_verification():
            agent.page.screenshot(path=str(artifact_dir / "dry_run_sort_gate.png"), full_page=True)
            pytest.skip("Naukri presented a security gate after search results loaded.")

        jobs = agent._extract_jobs()
        if not jobs:
            agent.page.screenshot(path=str(artifact_dir / "dry_run_no_jobs.png"), full_page=True)
            pytest.fail("Search returned no real Naukri job-listing URLs.")

        # Prevent _extract_job_detail from waiting indefinitely for a human in CI.
        def no_human_takeover(reason):
            from src.naukri import HumanActionRequired
            raise HumanActionRequired(reason)

        agent.wait_for_human = no_human_takeover

        details = []
        for card_job in jobs[:3]:
            try:
                detail = agent._extract_job_detail(card_job.url)
            except HumanActionRequired:
                pytest.skip("Naukri presented a security gate while opening a job detail page.")
            if not detail:
                continue

            scored = score_job(detail.title, detail.description, detail.company)
            details.append({
                "title": detail.title,
                "company": detail.company,
                "location": detail.location,
                "salary": detail.salary,
                "experience": detail.experience,
                "url": detail.url,
                "description_length": len(detail.description or ""),
                "score": scored.score,
                "recommendation": scored.recommendation,
                "matched": scored.matched,
                "penalties": scored.penalties,
                "reasons": scored.reasons,
            })

        if not details:
            pytest.fail("Found real job links, but could not extract any authoritative job details.")

        (artifact_dir / "dry_run_pipeline.json").write_text(
            json.dumps(
                {
                    "query": query,
                    "location": location,
                    "search_url": agent.page.url,
                    "jobs_found": len(jobs),
                    "jobs": [job.to_dict() for job in jobs[:10]],
                    "details_scored": details,
                    "did_submit_application": False,
                },
                indent=2,
                ensure_ascii=False,
                default=str,
            ),
            encoding="utf-8",
        )
    finally:
        inspector.close()
