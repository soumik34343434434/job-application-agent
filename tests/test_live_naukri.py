import json
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright


@pytest.mark.live
def test_public_naukri_homepage_search_surface(tmp_path):
    """Probe the public Naukri homepage without logging in or submitting anything."""
    artifact_dir = Path("outbox/live_probe")
    artifact_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        try:
            page.goto("https://www.naukri.com/", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2500)

            body = page.locator("body").inner_text(timeout=5000)
            inputs = page.locator("input")
            visible_inputs = []
            for i in range(min(inputs.count(), 80)):
                item = inputs.nth(i)
                if not item.is_visible():
                    continue
                visible_inputs.append({
                    "placeholder": item.get_attribute("placeholder"),
                    "aria": item.get_attribute("aria-label"),
                    "name": item.get_attribute("name"),
                    "type": item.get_attribute("type"),
                })

            buttons = page.locator("button")
            visible_buttons = []
            for i in range(min(buttons.count(), 60)):
                item = buttons.nth(i)
                if not item.is_visible():
                    continue
                visible_buttons.append({
                    "text": " ".join((item.inner_text() or "").split())[:160],
                    "aria": item.get_attribute("aria-label"),
                    "type": item.get_attribute("type"),
                })

            payload = {
                "url": page.url,
                "title": page.title(),
                "body_start": " ".join(body.split())[:3000],
                "visible_inputs": visible_inputs,
                "visible_buttons": visible_buttons,
            }
            (artifact_dir / "naukri_home_probe.json").write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            page.screenshot(path=str(artifact_dir / "naukri_home_probe.png"), full_page=True)

            access_denied = page.title().strip().lower() == "access denied" or "access denied" in body.lower()
            if access_denied:
                pytest.skip(
                    "Naukri's edge layer denied access from this GitHub-hosted runner; "
                    "the captured artifact is retained for diagnosis."
                )

            assert any(
                (x.get("placeholder") or "").lower() == "enter keyword / designation / companies"
                for x in visible_inputs
            ), f"Expected Naukri keyword search input was not visible. Observed inputs: {visible_inputs}"
        finally:
            browser.close()
