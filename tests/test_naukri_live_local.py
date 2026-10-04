import json
from pathlib import Path
import pytest

from src.naukri import HumanActionRequired, NaukriAgent
from src.scorer import score_job


def _safe_screenshot(page, path):
    try:
        page.screenshot(
            path=str(path),
            full_page=True,
            timeout=4000,
            animations="disabled",
        )
    except Exception as exc:
        Path(path).with_suffix(".screenshot_error.txt").write_text(
            str(exc), encoding="utf-8"
        )


def _bind_diagnostics(agent, artifact_dir):
    def write_json(name, data):
        (artifact_dir / name).write_text(
            json.dumps(data, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

    def capture(stage, error=None):
        payload = {
            "stage": stage,
            "error": str(error) if error else None,
        }
        try:
            if agent.page is not None:
                payload["page_url"] = agent.page.url
                payload["page_title"] = agent.page.title()
                payload["visible_text"] = agent.page.locator("body").inner_text(timeout=3000)[:6000]
                _safe_screenshot(agent.page, artifact_dir / "failure_page.png")
        except Exception as exc:
            payload["capture_error"] = str(exc)
        write_json("failure.json", payload)

    return capture


@pytest.mark.naukri_live
def test_authenticated_naukri_browser_surface():
    """Live Naukri surface check; never submits an application."""
    artifact_dir = Path("outbox/live_runner")
    artifact_dir.mkdir(parents=True, exist_ok=True)

    agent = NaukriAgent(headless=False, slow_mo=0)
    diagnostics = _bind_diagnostics(agent, artifact_dir)

    try:
        agent.__enter__()

        if not agent.is_logged_in():
            diagnostics("not_authenticated")
            pytest.fail("Dedicated Naukri Chrome is not authenticated.")

        info = {
            "url": agent.page.url,
            "title": agent.page.title(),
        }

        if agent.needs_human_verification():
            _safe_screenshot(agent.page, artifact_dir / "security_gate.png")
            (artifact_dir / "security_gate.json").write_text(
                json.dumps(info, indent=2),
                encoding="utf-8",
            )
            pytest.skip("Naukri is presenting a CAPTCHA/security gate.")

        report = agent.inspect_search_control()
        (artifact_dir / "search_surface.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

        # Capture the exact live nodes containing the search launcher text.
        search_dom = agent.page.evaluate(
            """
            () => Array.from(document.querySelectorAll('*'))
              .filter(e => /search jobs here/i.test((e.innerText || '').trim()) && e.offsetParent !== null)
              .slice(0, 20)
              .map(e => ({
                tag: e.tagName,
                text: (e.innerText || '').trim().slice(0, 500),
                aria: e.getAttribute('aria-label'),
                role: e.getAttribute('role'),
                id: e.id || null,
                className: String(e.className || '').slice(0, 1000),
                outerHTML: e.outerHTML.slice(0, 3000)
              }))
            """
        )
        (artifact_dir / "search_dom.json").write_text(
            json.dumps(search_dom, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        _safe_screenshot(agent.page, artifact_dir / "search_surface.png")

        # The inspector's generic input enumeration is intentionally lossy.
        # Validate the exact controls the automation itself uses instead.
        expected_selectors = {
            "keyword": 'input[placeholder="Enter keyword / designation / companies"]',
            "experience": 'input[placeholder="Select experience"]',
            "location": 'input[placeholder="Enter location"]',
        }
        missing = []
        for name, selector in expected_selectors.items():
            try:
                loc = agent.page.locator(selector)
                if not any(
                    loc.nth(i).is_visible() and loc.nth(i).is_enabled()
                    for i in range(loc.count())
                ):
                    missing.append(name)
            except Exception:
                missing.append(name)

        if missing:
            (artifact_dir / "search_surface_failure.json").write_text(
                json.dumps(
                    {
                        "missing_controls": missing,
                        "report": report,
                    },
                    indent=2,
                    ensure_ascii=False,
                    default=str,
                ),
                encoding="utf-8",
            )
        assert not missing, (
            f"Naukri search controls unavailable: {missing}; "
            f"activated={report.get('activated')}"
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
        agent.__exit__(None, None, None)


@pytest.mark.naukri_live
def test_naukri_live_search_extract_and_score():
    """End-to-end dry run: search -> extract -> read full JD -> score."""
    artifact_dir = Path("outbox/live_runner")
    artifact_dir.mkdir(parents=True, exist_ok=True)

    agent = NaukriAgent(headless=False, slow_mo=0)
    try:
        agent.__enter__()

        if not agent.is_logged_in():
            pytest.fail("Dedicated Naukri Chrome is not authenticated.")

        if agent.needs_human_verification():
            _safe_screenshot(agent.page, artifact_dir / "dry_run_security_gate.png")
            pytest.skip("Naukri is presenting a security gate before the dry-run search.")

        query = "Cybersecurity Engineer"
        location = "Kolkata"

        if not agent._fill_search_form(query, location):
            _safe_screenshot(agent.page, artifact_dir / "dry_run_search_failure.png")
            pytest.fail("Could not fill/submit the live Naukri search form.")

        if agent.needs_human_verification():
            _safe_screenshot(agent.page, artifact_dir / "dry_run_search_gate.png")
            pytest.skip("Naukri presented a security gate during the dry-run search.")

        agent._set_relevance_sort()
        agent.page.wait_for_timeout(1200)

        if agent.needs_human_verification():
            _safe_screenshot(agent.page, artifact_dir / "dry_run_sort_gate.png")
            pytest.skip("Naukri presented a security gate after search results loaded.")

        jobs = agent._extract_jobs()
        if not jobs:
            _safe_screenshot(agent.page, artifact_dir / "dry_run_no_jobs.png")
            pytest.fail("Search returned no real Naukri job-listing URLs.")

        def no_human_takeover(reason):
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
            details.append(
                {
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
                }
            )

        if not details:
            pytest.fail("Found real job links, but could not extract authoritative job details.")

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
        agent.__exit__(None, None, None)
