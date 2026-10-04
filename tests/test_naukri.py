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
        browser = pw.chromium.launch(headless=True)
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
