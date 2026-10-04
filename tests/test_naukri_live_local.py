import json
from pathlib import Path

import pytest

from src.naukri import NaukriAgent
from src.naukri_inspector import NaukriInspector


@pytest.mark.naukri_live
def test_authenticated_naukri_browser_surface():
    """
    Live regression against the user's own dedicated Chrome session.

    This test is intentionally limited to login/session + search-surface checks.
    It never submits a job application.
    """
    artifact_dir = Path("outbox/live_runner")
    artifact_dir.mkdir(parents=True, exist_ok=True)

    inspector = NaukriInspector()
    try:
        try:
            inspector.connect()
        except RuntimeError as exc:
            pytest.fail(str(exc))

        info = inspector.page_info()
        if "naukri.com" not in (info.get("url") or "").lower():
            pytest.fail(f"Dedicated browser is not currently on Naukri: {info}")

        body = inspector.visible_text().get("text", "")
        lower = body.lower()
        if any(x in lower for x in ("captcha", "security verification", "access denied")):
            inspector.screenshot_to_file = None
            pytest.skip("Naukri is presenting a human/security gate; live runner captured the page state.")

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
        assert not missing, (
            f"Naukri search surface changed. Missing placeholders: {sorted(missing)}; "
            f"observed inputs: {sorted(placeholders)}"
        )

        (artifact_dir / "page.json").write_text(
            json.dumps(info, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    finally:
        inspector.close()
