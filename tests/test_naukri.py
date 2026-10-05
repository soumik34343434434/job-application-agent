from src.naukri import NaukriAgent


def test_parse_card_blob():
    blob = """Security Engineer\nAcme Security\n2-5 Yrs\n₹10-15 LPA\nBangalore, Bengaluru\nEndpoint security and EDR operations using Microsoft Defender"""
    company, location, salary, experience, description = NaukriAgent._parse_card_blob(blob, "Security Engineer")
    assert company == "Acme Security"
    assert "Bangalore" in location
    assert "LPA" in salary
    assert "2-5" in experience
    assert "Endpoint security" in description


def test_external_host_detection_logic_is_simple():
    from urllib.parse import urlparse
    assert "naukri.com" in urlparse("https://www.naukri.com/job-listings/x").netloc
    assert "naukri.com" not in urlparse("https://careers.example.com/job/x").netloc


def test_naukri_apply_confirmation_is_submission_success():
    from playwright.sync_api import sync_playwright

    html = '<main><h1>Applied to "Security Operations Analyst"</h1></main>'
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page()
        page.set_content(html)
        agent = NaukriAgent(headless=True, slow_mo=0)
        agent.page = page
        agent.context = page.context
        assert agent._application_success_detected() is True
        browser.close()


def test_application_required_field_detection():
    """Required-field guard works on a representative Naukri-like form."""
    from playwright.sync_api import sync_playwright
    from src.naukri import NaukriAgent

    html = """
    <form>
      <label>Full name <input aria-label="Full name" required></label>
      <label>Email <input aria-label="Email" required></label>
      <label>Mobile <input aria-label="Mobile" required></label>
      <label>Notice period <input aria-label="Notice period" required></label>
      <label>Expected CTC <input aria-label="Expected CTC" required></label>
      <label>Work authorization <input aria-label="Work authorization" required></label>
    </form>
    """
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page()
        page.set_content(html)
        agent = NaukriAgent(headless=True, slow_mo=0)
        agent.page = page
        agent._fill_common_fields(
            {
                "name": "Test User",
                "email": "test@example.invalid",
                "phone": "0000000000",
            },
            resume_path=__import__("pathlib").Path("/nonexistent/resume.pdf"),
        )
        missing = agent._missing_required_fields()
        assert "work authorization" in [x.lower() for x in missing]
        assert "full name" not in [x.lower() for x in missing]
        assert "email" not in [x.lower() for x in missing]
        assert "mobile" not in [x.lower() for x in missing]
        browser.close()


def test_application_required_field_detection():
    """Required-field guard works on a representative Naukri-like form."""
    from pathlib import Path
    from playwright.sync_api import sync_playwright
    from src.naukri import NaukriAgent

    html = """
    <form>
      <label>Full name <input aria-label="Full name" required></label>
      <label>Email <input aria-label="Email" required></label>
      <label>Mobile <input aria-label="Mobile" required></label>
      <label>Notice period <input aria-label="Notice period" required></label>
      <label>Expected CTC <input aria-label="Expected CTC" required></label>
      <label>Work authorization <input aria-label="Work authorization" required></label>
    </form>
    """
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page()
        page.set_content(html)
        agent = NaukriAgent(headless=True, slow_mo=0)
        agent.page = page
        agent._fill_common_fields(
            {
                "name": "Test User",
                "email": "test@example.invalid",
                "phone": "0000000000",
            },
            resume_path=Path("/nonexistent/resume.pdf"),
        )
        missing = agent._missing_required_fields()
        lowered = [x.lower() for x in missing]
        assert "work authorization" in lowered
        assert "full name" not in lowered
        assert "email" not in lowered
        assert "mobile" not in lowered
        browser.close()



def _agent_for_html(page, html):
    page.set_content(html)
    agent = NaukriAgent(headless=True, slow_mo=0)
    agent.page = page
    agent.context = page.context
    return agent


def test_apply_surface_classifies_internal_control_without_clicking():
    from playwright.sync_api import sync_playwright

    html = '<main><button aria-label="Apply">Apply</button></main>'
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page()
        agent = _agent_for_html(page, html)
        assert agent._apply_surface_state() == "INTERNAL"
        browser.close()


def test_apply_surface_classifies_external_flow():
    from playwright.sync_api import sync_playwright

    html = '<main><a href="https://careers.example.com/job/1">Apply on company site</a></main>'
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page()
        agent = _agent_for_html(page, html)
        assert agent._apply_surface_state() == "EXTERNAL"
        browser.close()


def test_apply_surface_classifies_already_applied_before_click():
    from playwright.sync_api import sync_playwright

    html = '<main><h1>You have already applied</h1><button>Apply</button></main>'
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page()
        agent = _agent_for_html(page, html)
        assert agent._apply_surface_state() == "ALREADY_APPLIED"
        browser.close()


def test_apply_surface_unknown_never_clicks_unrecognized_control():
    from playwright.sync_api import sync_playwright

    html = '<main><button aria-label="Save job">Save</button></main>'
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page()
        agent = _agent_for_html(page, html)
        assert agent._apply_surface_state() == "UNKNOWN"
        assert agent._safe_click_apply() is False
        browser.close()


def test_naukri_password_login_fills_and_submits(monkeypatch):
    from src import naukri as nk

    class FakeLocator:
        def __init__(self, text=""):
            self.text = text
            self.filled = None
            self.clicked = False

        def count(self):
            return 1

        def nth(self, index):
            return self

        @property
        def first(self):
            return self

        def is_visible(self):
            return True

        def is_enabled(self):
            return True

        def fill(self, value):
            self.filled = value

        def scroll_into_view_if_needed(self):
            pass

        def click(self, timeout=None):
            self.clicked = True

    class FakePage:
        def __init__(self):
            self.url = "https://www.naukri.com/nlogin/login"
            self.title = lambda: "Login"
            self.username = FakeLocator()
            self.password = FakeLocator()
            self.submit = FakeLocator()

        def goto(self, url, **kwargs):
            self.url = url

        def wait_for_timeout(self, ms):
            pass

        def locator(self, selector):
            if "password" in selector.lower():
                return self.password
            if "submit" in selector.lower():
                return self.submit
            if "email" in selector.lower() or "username" in selector.lower() or "mobile" in selector.lower() or "type=\"text\"" in selector.lower():
                return self.username
            return FakeLocator()

        def get_by_role(self, role, name=None):
            if role == "button":
                return self.submit
            return FakeLocator()

    agent = NaukriAgent(headless=True, slow_mo=0)
    agent.page = FakePage()
    monkeypatch.setattr(nk, "get_naukri_credentials", lambda: ("user@example.invalid", "test-password"))
    monkeypatch.setattr(agent, "needs_human_verification", lambda: False)
    monkeypatch.setattr(agent, "is_logged_in", lambda: True)

    assert agent._login_with_password(interactive=False) is True
    assert agent.page.username.filled == "user@example.invalid"
    assert agent.page.password.filled == "test-password"
    assert agent.page.submit.clicked is True
