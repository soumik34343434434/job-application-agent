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
    """Capture the live Naukri surface without making the regression brittle."""
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

        # This is diagnostic only. The authoritative regression below exercises
        # the real selectors, fills the search form, loads results, opens live
        # job pages, and scores the extracted JDs.
        controls = {
            "keyword": agent.page.locator(
                'input[placeholder="Enter keyword / designation / companies"]'
            ).count(),
            "experience": agent.page.locator(
                'input[placeholder="Select experience"]'
            ).count(),
            "location": agent.page.locator(
                'input[placeholder="Enter location"]'
            ).count(),
        }
        (artifact_dir / "search_controls.json").write_text(
            json.dumps({"controls": controls, "activated": report.get("activated")}, indent=2),
            encoding="utf-8",
        )
        _safe_screenshot(agent.page, artifact_dir / "search_surface.png")

        (artifact_dir / "page.json").write_text(
            json.dumps(info, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except pytest.skip.Exception:
        raise
    except Exception as exc:
        diagnostics("unexpected_exception", exc)
        raise

        # Safe live application-form regression: enter one real Naukri listing,
        # click Apply, fill verified common fields, and stop before submit.
        candidate = next((item for item in details if int(item["score"]) >= 70), details[0])
        from src.db import add_job

        full_detail = agent._extract_job_detail(candidate["url"])
        if full_detail is None:
            pytest.skip("Could not reopen a live job detail for the safe apply-form regression.")

        scored = score_job(full_detail.title, full_detail.description, full_detail.company)
        job_id = add_job(
            full_detail.company,
            full_detail.title,
            full_detail.url,
            full_detail.description,
            scored.score,
            scored.recommendation,
        )
        form_job = {
            "id": job_id,
            "title": full_detail.title,
            "company": full_detail.company,
            "url": full_detail.url,
            "score": scored.score,
        }

        outcome = agent.apply_job(form_job, auto_submit=False)
        if outcome not in {
            "READY_TO_SUBMIT",
            "ALREADY_APPLIED",
            "EXTERNAL_APPLICATION",
            "NEEDS_USER",
        }:
            pytest.fail(f"Unexpected safe application-form outcome: {outcome}")
        assert outcome not in {"SUBMITTED", "SUBMITTED_UNCONFIRMED"}

        (artifact_dir / "safe_apply_form.json").write_text(
            json.dumps(
                {
                    "job": form_job,
                    "outcome": outcome,
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
