"""Playwright browser automation for a personal Naukri job workflow.

The browser runs with a persistent local profile so normal cookies/session state
survive between runs. CAPTCHA, OTP, suspicious-login checks, and unknown
application questions trigger a human takeover rather than an attempted bypass.
"""
from __future__ import annotations

import json
import os
import re
import time
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path
from urllib.parse import quote_plus, urlparse

from .credentials import get_naukri_credentials, interactive_setup
from .db import add_job, find_duplicate, get_job, list_jobs, log_event, review_queue, save_prep, set_status, submitted_count_today, update_job_details
from .scorer import score_job
from .tailoring import fingerprint_job, prepare_application, prep_to_dict

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
except Exception as exc:  # pragma: no cover - helpful import-time error
    sync_playwright = None
    PlaywrightTimeoutError = Exception
    _PLAYWRIGHT_IMPORT_ERROR = exc
else:
    _PLAYWRIGHT_IMPORT_ERROR = None

ROOT = Path(__file__).resolve().parents[1]
BROWSER_DIR = ROOT / ".browser" / "naukri_profile"
ARTIFACT_DIR = ROOT / "outbox" / "browser_artifacts"
BASE_RESUME = ROOT / "data" / "resumes" / "01_General_Cybersecurity_Analyst.pdf"
NAUKRI_HOME = "https://www.naukri.com/"

# Dedicated browser data owned by this agent. This is intentionally separate
# from the user's normal Chrome profile so the agent can run without taking over
# the user's everyday browser.
AGENT_BROWSER_DIR = ROOT / ".browser" / "naukri_agent"
# Keep one dedicated Chrome process owning this profile. Playwright attaches to it
# over CDP instead of trying to launch a second browser against the same profile.
AGENT_CDP_HOST = "127.0.0.1"
AGENT_CDP_PORT = int(os.getenv("NAUKRI_CDP_PORT", "9229"))
AGENT_CDP_ENDPOINT = f"http://{AGENT_CDP_HOST}:{AGENT_CDP_PORT}"

DEFAULT_QUERIES = [
    "Cybersecurity Engineer",
    "Security Engineer",
    "Security Operations Engineer",
    "Endpoint Security Engineer",
    "Vulnerability Management Engineer",
    "IAM Engineer",
    "Identity Engineer",
    "Security Analyst",
]

PREFERRED_LOCATIONS = [
    "Remote", "Kolkata", "Bangalore", "Bengaluru", "Hyderabad", "Secunderabad",
    "Pune", "Mumbai", "Noida", "Gurgaon", "Gurugram", "Delhi NCR", "Ahmedabad",
]

EXTERNAL_HOST_MARKERS = ("naukri.com", "naukri.co.in")
CAPTCHA_WORDS = ("captcha", "i am not a robot", "verify you are human", "security check", "unusual activity")
LOGIN_WORDS = ("login", "sign in", "log in")

class HumanActionRequired(RuntimeError):
    """A browser state requires a human action the agent will not bypass."""


def _cdp_is_available() -> bool:
    try:
        from urllib.request import urlopen
        with urlopen(f"{AGENT_CDP_ENDPOINT}/json/version", timeout=0.8) as response:
            return response.status == 200
    except Exception:
        return False


def setup_agent_browser():
    """Launch one normal Chrome instance for the dedicated Naukri profile.

    The browser is intentionally kept running. Playwright later attaches to this
    exact browser over CDP, so there is never a profile-lock race.
    """
    AGENT_BROWSER_DIR.mkdir(parents=True, exist_ok=True)

    if _cdp_is_available():
        return AGENT_BROWSER_DIR

    url = NAUKRI_HOME
    if os.uname().sysname == "Darwin":
        executable = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if not Path(executable).exists():
            raise RuntimeError(f"Google Chrome executable not found at {executable}.")
        subprocess.Popen([
            executable,
            f"--user-data-dir={AGENT_BROWSER_DIR}",
            f"--remote-debugging-address={AGENT_CDP_HOST}",
            f"--remote-debugging-port={AGENT_CDP_PORT}",
            "--no-first-run",
            "--no-default-browser-check",
            url,
        ])
    else:
        executable = os.getenv("BROWSER_EXECUTABLE_PATH")
        if not executable:
            for candidate in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
                found = __import__("shutil").which(candidate)
                if found:
                    executable = found
                    break
        if not executable:
            raise RuntimeError("Chrome executable not found; set BROWSER_EXECUTABLE_PATH.")
        subprocess.Popen([
            executable,
            f"--user-data-dir={AGENT_BROWSER_DIR}",
            f"--remote-debugging-address={AGENT_CDP_HOST}",
            f"--remote-debugging-port={AGENT_CDP_PORT}",
            "--no-first-run",
            "--no-default-browser-check",
            url,
        ])

    deadline = time.time() + 15
    while time.time() < deadline:
        if _cdp_is_available():
            return AGENT_BROWSER_DIR
        time.sleep(0.25)

    raise RuntimeError(
        f"Dedicated Chrome started but CDP did not become available at {AGENT_CDP_ENDPOINT}."
    )


@dataclass
class FoundJob:
    title: str
    company: str
    location: str
    salary: str
    experience: str
    url: str
    description: str
    score: int | None = None
    recommendation: str | None = None
    job_id: int | None = None

    def to_dict(self):
        return asdict(self)


class NaukriAgent:
    def __init__(self, headless: bool = False, slow_mo: int = 150):
        if sync_playwright is None:
            raise RuntimeError(
                "Playwright is unavailable. Install dependencies with pip install -r requirements.txt."
            ) from _PLAYWRIGHT_IMPORT_ERROR
        self.headless = headless
        self.slow_mo = slow_mo
        self._pw = None
        self.browser = None
        self.context = None
        self.page = None
        self.browser_user_data_dir = None
        self.browser_profile_dir = None

    def __enter__(self):
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        AGENT_BROWSER_DIR.mkdir(parents=True, exist_ok=True)

        # A single dedicated Chrome owns the profile. We attach to that existing
        # browser through CDP rather than using launch_persistent_context(), which
        # would collide with an already-running Chrome profile.
        setup_agent_browser()
        self._pw = sync_playwright().start()
        # Playwright 1.60+ supports no_defaults for CDP attachment. This keeps
        # Playwright from trying to override the already-running Chrome context,
        # including download behavior, during connection.
        self.browser = self._pw.chromium.connect_over_cdp(
            AGENT_CDP_ENDPOINT,
            no_defaults=True,
            is_local=True,
            slow_mo=self.slow_mo,
        )
        if not self.browser.contexts:
            raise RuntimeError("Connected to dedicated Chrome, but no browser context is available.")

        self.context = self.browser.contexts[0]

        # CDP often exposes an initial about:blank tab before the actual Naukri
        # tab. Never assume pages[0] is the page the user is looking at.
        pages = list(self.context.pages)
        naukri_pages = []
        for page in pages:
            try:
                if "naukri.com" in (page.url or "").lower():
                    naukri_pages.append(page)
            except Exception:
                continue

        if naukri_pages:
            self.page = naukri_pages[-1]
        else:
            # Reuse an existing blank tab when possible, then navigate it to
            # Naukri. This prevents the diagnostic and search commands from ever
            # operating against about:blank.
            blank_pages = []
            for page in pages:
                try:
                    if (page.url or "").lower() in ("about:blank", ""):
                        blank_pages.append(page)
                except Exception:
                    continue
            self.page = blank_pages[-1] if blank_pages else self.context.new_page()
            self.page.goto(NAUKRI_HOME, wait_until="domcontentloaded")
            self.page.wait_for_timeout(1200)

        self.page.set_default_timeout(12000)
        return self

    def __exit__(self, exc_type, exc, tb):
        # Detach Playwright only. The dedicated Chrome stays running and keeps
        # owning the profile; this removes the need for manual close/reopen steps.
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass


    def _artifact(self, label: str):
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", label)[:80]
        ts = time.strftime("%Y%m%d-%H%M%S")
        path = ARTIFACT_DIR / f"{ts}-{safe}.png"
        try:
            self.page.screenshot(path=str(path), full_page=True)
        except Exception:
            pass
        return path

    def _body_text(self) -> str:
        try:
            return self.page.locator("body").inner_text(timeout=5000).lower()
        except Exception:
            return ""

    def needs_human_verification(self) -> bool:
        text = self._body_text()
        return any(word in text for word in CAPTCHA_WORDS)

    def wait_for_human(self, reason: str):
        artifact = self._artifact(reason.replace(" ", "_"))
        print(f"\nHuman action required: {reason}")
        print(f"Browser is open. Screenshot: {artifact}")
        print("Complete the step in the browser, then press Enter here to resume.")
        input()

    def is_logged_in(self) -> bool:
        """Use an authenticated jobseeker route as the primary session check."""
        try:
            current_url = self.page.url
            self.page.goto(
                "https://www.naukri.com/mnjuser/profile",
                wait_until="domcontentloaded",
                timeout=15000,
            )
            self.page.wait_for_timeout(1000)

            url = self.page.url.lower()
            text = self._body_text()

            # A redirect to Naukri login is the strongest unauthenticated signal.
            if "/nlogin/" in url or "/login" in url and "naukri.com" in url:
                return False

            # Naukri's jobseeker area is under /mnjuser; reaching it without a
            # login redirect is stronger evidence than generic homepage labels.
            if "/mnjuser/" in url:
                return True

            authenticated_markers = (
                "my naukri",
                "my profile",
                "view profile",
                "logout",
                "sign out",
            )
            return any(marker in text for marker in authenticated_markers)
        except Exception:
            return False


    def open_home(self):
        self.page.goto(NAUKRI_HOME, wait_until="domcontentloaded")
        self.page.wait_for_timeout(1500)
        if self.needs_human_verification():
            self.wait_for_human("Naukri presented a CAPTCHA/security verification")

    def _click_login(self):
        candidates = [
            self.page.get_by_role("link", name=re.compile(r"^(login|sign in|log in)$", re.I)),
            self.page.get_by_role("button", name=re.compile(r"^(login|sign in|log in)$", re.I)),
            self.page.get_by_text(re.compile(r"^(login|sign in|log in)$", re.I)),
        ]
        for locator in candidates:
            try:
                if locator.count():
                    locator.first.click()
                    self.page.wait_for_timeout(1000)
                    return True
            except Exception:
                continue
        return False

    def _click_google_login(self) -> bool:
        """Click Naukri's Google sign-in control without handling Google credentials."""
        candidates = [
            self.page.get_by_role("button", name=re.compile(r"(continue|sign).*google|google", re.I)),
            self.page.get_by_role("link", name=re.compile(r"(continue|sign).*google|google", re.I)),
            self.page.get_by_text(re.compile(r"(continue|sign).*google|google", re.I)),
            self.page.locator('[aria-label*="google" i]'),
            self.page.locator('button:has-text("Google")'),
            self.page.locator('a:has-text("Google")'),
        ]
        for locator in candidates:
            try:
                if locator.count():
                    locator.first.scroll_into_view_if_needed()
                    locator.first.click()
                    self.page.wait_for_timeout(1200)
                    return True
            except Exception:
                continue
        return False

    def ensure_logged_in(self):
        self.open_home()
        if self.is_logged_in():
            return

        raise RuntimeError(
            "Naukri is not logged in in the dedicated agent profile. "
            "Run 'python3 -m src.cli naukri setup-browser' once, complete "
            "Sign in with Google manually in that normal Chrome window, close it, "
            "then run the agent again. The automation runner never attempts "
            "to automate Google authentication."
        )

    def create_account(self):
        """Best-effort assisted Naukri jobseeker registration.

        Registration forms change frequently, so this fills only stable/basic
        fields and then hands over for any verification or missing fields.
        """
        self.open_home()
        if self.is_logged_in():
            return "ALREADY_LOGGED_IN"

        # Open registration from the visible site navigation when possible.
        for locator in [
            self.page.get_by_role("link", name=re.compile(r"register|sign up|create account", re.I)),
            self.page.get_by_role("button", name=re.compile(r"register|sign up|create account", re.I)),
            self.page.get_by_text(re.compile(r"register now|register|sign up", re.I)),
        ]:
            try:
                if locator.count():
                    locator.first.click()
                    self.page.wait_for_timeout(1000)
                    break
            except Exception:
                continue

        profile = __import__("json").loads((ROOT / "data" / "profile.json").read_text(encoding="utf-8"))
        username, _ = get_naukri_credentials()
        if not username:
            username = input("Email to register with Naukri: ").strip()
        password = os.getenv("NAUKRI_PASSWORD")
        if not password:
            import getpass
            password = getpass.getpass("New Naukri password (hidden): ")

        self._fill_input_by_label(["full name", "name"], profile.get("name", ""))
        self._fill_input_by_label(["email", "email id"], username)
        self._fill_input_by_label(["mobile", "phone"], profile.get("phone", ""))
        self._fill_input_by_label(["location", "city"], profile.get("location", "Kolkata"))
        self._fill_input_by_label(["password", "create password"], password)
        self._fill_input_by_label(["confirm password", "re-enter password"], password)
        self._fill_input_by_label(["current company"], "Cognizant Technology Solutions")
        self._fill_input_by_label(["current designation", "designation"], "Cybersecurity Analyst")
        self._fill_input_by_label(["experience", "total experience"], "2")

        file_inputs = self.page.locator('input[type="file"]')
        if file_inputs.count() and BASE_RESUME.exists():
            try:
                file_inputs.first.set_input_files(str(BASE_RESUME))
            except Exception:
                pass

        # Click an obvious register/create control; if the form has extra fields,
        # the human takeover handles them in the real browser.
        clicked = False
        for locator in [
            self.page.get_by_role("button", name=re.compile(r"register|create account|sign up", re.I)),
            self.page.get_by_text(re.compile(r"register|create account|sign up", re.I)),
        ]:
            try:
                if locator.count():
                    locator.last.click()
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            self.wait_for_human("Naukri registration form needs manual completion")
        self.page.wait_for_timeout(1500)
        if self.needs_human_verification():
            self.wait_for_human("Naukri account registration requires CAPTCHA/security verification")
        if not self.is_logged_in():
            self.wait_for_human("Complete Naukri OTP/email/mobile verification or remaining registration fields")
        if self.is_logged_in():
            try:
                from .credentials import save_naukri_credentials
                save_naukri_credentials(username, password)
            except Exception:
                pass
            return "ACCOUNT_CREATED_OR_CONFIRMED"
        return "REGISTRATION_NEEDS_ATTENTION"

    def activate_header_search(self) -> bool:
        """Open the current authenticated Naukri header search control."""
        candidates = [
            self.page.get_by_text(re.compile(r"^search jobs here$", re.I)),
            self.page.get_by_role("button", name=re.compile(r"search jobs here|search", re.I)),
            self.page.locator('[aria-label*="search jobs here" i]'),
            self.page.locator('[placeholder*="search jobs here" i]'),
        ]
        for loc in candidates:
            try:
                for i in range(loc.count()):
                    candidate = loc.nth(i)
                    if candidate.is_visible() and candidate.is_enabled():
                        candidate.click()
                        self.page.wait_for_timeout(700)
                        return True
            except Exception:
                continue
        return False

    def inspect_search_control(self) -> dict:
        """Inspect the current header search control and its opened state."""
        result = {
            "before_url": self.page.url,
            "activated": self.activate_header_search(),
            "url": self.page.url,
            "textboxes": [],
            "inputs": [],
            "search_related_elements": [],
        }
        try:
            textboxes = self.page.get_by_role("textbox")
            for i in range(min(textboxes.count(), 40)):
                el = textboxes.nth(i)
                if el.is_visible():
                    result["textboxes"].append({
                        "placeholder": el.get_attribute("placeholder"),
                        "aria": el.get_attribute("aria-label"),
                        "role": el.get_attribute("role"),
                        "tag": el.evaluate("(e) => e.tagName"),
                    })
        except Exception:
            pass

        try:
            inputs = self.page.locator("input")
            for i in range(min(inputs.count(), 60)):
                el = inputs.nth(i)
                if el.is_visible():
                    result["inputs"].append({
                        "placeholder": el.get_attribute("placeholder"),
                        "aria": el.get_attribute("aria-label"),
                        "type": el.get_attribute("type"),
                        "name": el.get_attribute("name"),
                    })
        except Exception:
            pass

        # Capture nearby DOM for anything containing the search label so we can
        # implement against the actual current UI rather than legacy selectors.
        try:
            loc = self.page.get_by_text(re.compile(r"search jobs here|search", re.I))
            for i in range(min(loc.count(), 20)):
                el = loc.nth(i)
                if not el.is_visible():
                    continue
                result["search_related_elements"].append(
                    el.evaluate(
                        """(e) => ({
                          tag: e.tagName,
                          text: (e.innerText || '').trim().slice(0, 200),
                          aria: e.getAttribute('aria-label'),
                          role: e.getAttribute('role'),
                          placeholder: e.getAttribute('placeholder'),
                          className: String(e.className || '').slice(0, 500),
                          parent: e.parentElement ? {
                            tag: e.parentElement.tagName,
                            className: String(e.parentElement.className || '').slice(0, 500),
                            role: e.parentElement.getAttribute('role')
                          } : null
                        })"""
                    )
                )
        except Exception:
            pass

        result["screenshot"] = str(self._artifact("naukri_search_control_inspect"))
        return result

    def _first_visible_editable(self, selectors: list[str]):
        """Return the first visible, enabled text control matching any selector."""
        for selector in selectors:
            try:
                locs = self.page.locator(selector)
                count = locs.count()
                for i in range(count):
                    loc = locs.nth(i)
                    if loc.is_visible() and loc.is_enabled():
                        return loc
            except Exception:
                continue
        return None

    def _fill_search_form(self, query: str, location: str) -> bool:
        # Current authenticated Naukri homepage: expand "Search jobs here",
        # then fill the three visible search controls reported by inspect-search.
        self.activate_header_search()
        self.page.wait_for_timeout(500)

        try:
            keyword = self.page.locator(
                'input[placeholder="Enter keyword / designation / companies"]'
            )
            keyword = next(
                (
                    keyword.nth(i)
                    for i in range(keyword.count())
                    if keyword.nth(i).is_visible() and keyword.nth(i).is_enabled()
                ),
                None,
            )
        except Exception:
            keyword = None

        try:
            location_field = self.page.locator('input[placeholder="Enter location"]')
            location_field = next(
                (
                    location_field.nth(i)
                    for i in range(location_field.count())
                    if location_field.nth(i).is_visible() and location_field.nth(i).is_enabled()
                ),
                None,
            )
        except Exception:
            location_field = None

        if not keyword or not location_field:
            return False

        try:
            keyword.fill(query)
            self.page.wait_for_timeout(400)
            location_field.fill(location)
            self.page.wait_for_timeout(800)
        except Exception:
            return False

        # Choose the current location suggestion, if presented.
        try:
            suggestions = self.page.get_by_text(re.compile(rf"^{re.escape(location)}$", re.I))
            for i in range(suggestions.count()):
                candidate = suggestions.nth(i)
                if candidate.is_visible() and candidate.is_enabled():
                    candidate.click()
                    break
        except Exception:
            pass

        # The inspected UI exposes a visible Search button.
        try:
            buttons = self.page.locator("button.nI-gNb-sb__icon-wrapper")
            for i in range(buttons.count()):
                candidate = buttons.nth(i)
                if candidate.is_visible() and candidate.is_enabled():
                    candidate.click()
                    self.page.wait_for_timeout(3500)
                    return True
        except Exception:
            pass

        try:
            search = self.page.get_by_role("button", name=re.compile(r"^search$", re.I))
            for i in range(search.count()):
                candidate = search.nth(i)
                if candidate.is_visible() and candidate.is_enabled():
                    candidate.click()
                    self.page.wait_for_timeout(3500)
                    return True
        except Exception:
            pass

        try:
            keyword.press("Enter")
            self.page.wait_for_timeout(3500)
            return True
        except Exception:
            return False


    def _set_relevance_sort(self) -> bool:
        """Prefer Relevance sorting when Naukri exposes the sorting control."""
        candidates = [
            self.page.get_by_text(re.compile(r"^personalized$", re.I)),
            self.page.get_by_text(re.compile(r"^sort by:\s*personalized$", re.I)),
        ]
        for loc in candidates:
            try:
                for i in range(loc.count()):
                    item = loc.nth(i)
                    if not item.is_visible():
                        continue
                    item.click()
                    self.page.wait_for_timeout(400)
                    options = [
                        self.page.get_by_text(re.compile(r"^relevance$", re.I)),
                        self.page.get_by_role("option", name=re.compile(r"relevance", re.I)),
                    ]
                    for opt in options:
                        for j in range(opt.count()):
                            candidate = opt.nth(j)
                            if candidate.is_visible():
                                candidate.click()
                                self.page.wait_for_timeout(800)
                                return True
            except Exception:
                continue
        return False

    def search(self, query: str, location: str = "") -> list[FoundJob]:
        self.ensure_logged_in()
        self.page.goto(NAUKRI_HOME, wait_until="domcontentloaded")
        self.page.wait_for_timeout(1200)

        searched = self._fill_search_form(query, location)

        if not searched:
            raise RuntimeError(
                "Could not use the current Naukri header search UI. "
                "Open Naukri homepage and run 'naukri inspect-search' for diagnostics."
            )

        self._set_relevance_sort()

        if self.needs_human_verification():
            self.wait_for_human("Naukri presented CAPTCHA/security verification during search")
        return self._extract_jobs()


    def _extract_jobs(self) -> list[FoundJob]:
        """Extract only real Naukri job-listing URLs from the result page."""
        results: list[FoundJob] = []
        seen = set()

        anchors = self.page.locator(
            'a[href*="/job-listings-"], a[href*="/job-listings/"]'
        )
        try:
            count = min(anchors.count(), 120)
        except Exception:
            count = 0

        for i in range(count):
            a = anchors.nth(i)
            try:
                if not a.is_visible():
                    continue
                href = a.get_attribute("href") or ""
                title = re.sub(r"\s+", " ", (a.inner_text() or "")).strip()
            except Exception:
                continue

            if not href:
                continue
            if href.startswith("/"):
                href = "https://www.naukri.com" + href

            host = urlparse(href).netloc.lower()
            if host and not self._host_is_naukri(host):
                continue
            if "/job-listings-" not in href.lower() and "/job-listings/" not in href.lower():
                continue

            key = href.split("?")[0]
            if key in seen:
                continue

            blob = self._nearby_card_text(a)
            if not title or len(title) < 3:
                lines = [x.strip() for x in blob.splitlines() if x.strip()]
                for line in lines[:10]:
                    if 4 <= len(line) <= 140:
                        title = re.sub(r"\s+", " ", line)
                        break

            if not title:
                continue

            seen.add(key)
            company, location, salary, experience, description = self._parse_card_blob(
                blob, title
            )
            results.append(
                FoundJob(
                    title,
                    company,
                    location,
                    salary,
                    experience,
                    href,
                    description,
                )
            )

        return results


    def _nearby_card_text(self, anchor) -> str:
        js = """
        (el) => {
          let n = el;
          for (let i = 0; i < 7 && n; i++, n = n.parentElement) {
            const cls = String(n.className || '').toLowerCase();
            if (cls.includes('jobtuple') || cls.includes('job-tuple') || n.tagName === 'ARTICLE') {
              return n.innerText || '';
            }
          }
          return el.parentElement?.parentElement?.innerText || el.innerText || '';
        }
        """
        try:
            return anchor.evaluate(js)
        except Exception:
            try:
                return anchor.inner_text()
            except Exception:
                return ""

    @staticmethod
    def _parse_card_blob(blob: str, title: str):
        lines = [re.sub(r"\s+", " ", x).strip() for x in blob.splitlines() if x.strip()]
        company = ""
        location = ""
        salary = ""
        experience = ""
        for line in lines:
            if line == title:
                continue
            if not company and len(line) <= 120 and not re.search(r"\b(?:yrs?|years?)\b", line, re.I) and "₹" not in line and "lpa" not in line.lower():
                company = line
                continue
            if not experience and re.search(r"\b\d+\s*[-–]\s*\d+\s*(?:yrs?|years?)\b", line, re.I):
                experience = line
                continue
            if not salary and ("₹" in line or "lpa" in line.lower() or "not disclosed" in line.lower()):
                salary = line
                continue
            if not location and ("," in line or "remote" in line.lower()):
                location = line
        description = " ".join(lines[:18])
        return company or "Unknown", location, salary, experience, description

    @staticmethod
    def _clean_text(value: str) -> str:
        return re.sub(r"\s+", " ", (value or "")).strip()

    def _first_text(self, selectors: list[str]) -> str:
        for selector in selectors:
            try:
                loc = self.page.locator(selector)
                for i in range(min(loc.count(), 10)):
                    item = loc.nth(i)
                    if item.is_visible():
                        text = self._clean_text(item.inner_text())
                        if text:
                            return text
            except Exception:
                continue
        return ""

    def _extract_labeled_value(self, labels: list[str], body: str) -> str:
        lines = [self._clean_text(x) for x in body.splitlines() if self._clean_text(x)]
        for i, line in enumerate(lines):
            low = line.lower()
            for label in labels:
                if label in low:
                    # Handle "Experience: 2-5 Yrs" and similar one-line forms.
                    parts = re.split(r":\s*", line, maxsplit=1)
                    if len(parts) == 2 and parts[1].strip():
                        return parts[1].strip()
                    # Otherwise use the immediately following non-empty line.
                    if i + 1 < len(lines):
                        nxt = lines[i + 1]
                        if nxt and len(nxt) <= 100:
                            return nxt
        return ""

    def _extract_job_detail(self, url: str) -> FoundJob | None:
        """Open one real Naukri listing and extract authoritative job-page data."""
        canonical = (url or "").split("?")[0]
        if not canonical:
            return None

        try:
            self.page.goto(canonical, wait_until="domcontentloaded", timeout=20000)
            self.page.wait_for_timeout(1400)

            if self.needs_human_verification():
                self.wait_for_human("Naukri presented CAPTCHA/security verification on a job detail page")

            page_url = self.page.url or canonical
            if "/nlogin/" in page_url.lower() or "/login" in page_url.lower():
                raise RuntimeError("Naukri login session expired while opening a job detail page.")

            body = self.page.locator("body").inner_text(timeout=7000)
            body_clean = self._clean_text(body)

            title = self._first_text([
                "h1.jd-header-title",
                "h1[class*='jd-header-title']",
                "h1[class*='job-title']",
                "h1",
            ])

            company = self._first_text([
                "div[class*='jd-header-comp-name'] a",
                "div[class*='jd-header-comp-name']",
                "a[class*='comp-name']",
                "[class*='company-name'] a",
                "[class*='company-name']",
            ])

            location = self._first_text([
                "span[class*='location']",
                "span[class*='loc']",
                "[class*='location']",
                "[class*='loc']",
            ])

            # Prefer structured metadata parsed from the visible page text. Some
            # Naukri DOM containers combine "experience" and "salary" in one node.
            salary = ""
            experience = ""
            experience_match = re.search(
                r"(?i)years?\s+of\s+experience\s*:\s*(\d+(?:\.\d+)?\s*(?:-|–|to)\s*\d+(?:\.\d+)?\s*(?:yrs?|years?)|\d+(?:\.\d+)?\s*\+\s*(?:yrs?|years?))",
                body,
            )
            if experience_match:
                experience = self._clean_text(experience_match.group(1))

            salary_match = re.search(
                r"(?i)(?:salary|ctc|compensation)\s*:\s*([^\n|]{2,80})",
                body,
            )
            if salary_match:
                salary = self._clean_text(salary_match.group(1))
            elif re.search(r"(?i)\bnot\s+disclosed\b", body):
                salary = "Not Disclosed"

            description = self._first_text([
                "section[class*='job-desc']",
                "div[class*='job-desc']",
                "section[class*='jd-desc']",
                "div[class*='jd-desc']",
                "[class*='jobDescription']",
                "[class*='job-description']",
            ])

            # Fallbacks from the visible page text when a CSS class changes.
            if not company:
                company = self._extract_labeled_value(["company"], body)
            if not location:
                location = self._extract_labeled_value(["location"], body)
            if not experience:
                experience_match = re.search(
                    r"(?i)(?:experience|years?\s+of\s+experience)\s*:\s*(\d+(?:\.\d+)?\s*(?:-|–|to)\s*\d+(?:\.\d+)?\s*(?:yrs?|years?)|\d+(?:\.\d+)?\s*\+\s*(?:yrs?|years?))",
                    body,
                )
                if experience_match:
                    experience = self._clean_text(experience_match.group(1))

            # Some Naukri postings express the requirement as prose, e.g.
            # "minimum of 2+ years" instead of a labeled metadata field.
            if not experience:
                experience_match = re.search(
                    r"(?i)(?:minimum\s+of|at\s+least)\s+(\d+(?:\.\d+)?\s*\+\s*(?:yrs?|years?)|\d+(?:\.\d+)?\s*(?:-|–|to)\s*\d+(?:\.\d+)?\s*(?:yrs?|years?))",
                    body,
                )
                if experience_match:
                    experience = self._clean_text(experience_match.group(1))
            if not salary:
                salary_match = re.search(
                    r"(?i)(?:salary|ctc|compensation)\s*:\s*([^\n|]{2,80})",
                    body,
                )
                if salary_match:
                    salary = self._clean_text(salary_match.group(1))
                elif re.search(r"(?i)\bnot\s+disclosed\b", body):
                    salary = "Not Disclosed"

            # Naukri detail pages normally expose a dedicated description block.
            # Avoid storing the entire page (navigation, recommendations, etc.).
            if len(description) < 120:
                headings = self.page.get_by_text(
                    re.compile(r"^job description$|^about the job$|^roles? and responsibilities$",
                                re.I)
                )
                for i in range(min(headings.count(), 8)):
                    try:
                        heading = headings.nth(i)
                        if not heading.is_visible():
                            continue
                        candidate = heading.evaluate(
                            """(e) => {
                                let n = e.parentElement;
                                for (let i = 0; i < 5 && n; i++, n = n.parentElement) {
                                    const t = (n.innerText || '').trim();
                                    if (t.length >= 250) return t;
                                }
                                return '';
                            }"""
                        )
                        candidate = self._clean_text(candidate)
                        if len(candidate) > len(description):
                            description = candidate
                    except Exception:
                        continue

            # Last-resort metadata extraction from the visible page.
            if not location:
                m = re.search(r"(?i)(?:location|locations?)\s*[:\-]?\s*([^|\n]{2,80})", body)
                if m:
                    location = self._clean_text(m.group(1))
            if not experience:
                m = re.search(
                    r"(?i)(?:experience|experience required)\s*[:\-]?\s*(\d+(?:\.\d+)?\s*(?:-|–|to)\s*\d+(?:\.\d+)?\s*(?:yrs?|years?)|\d+(?:\.\d+)?\s*\+?\s*(?:yrs?|years?))",
                    body,
                )
                if m:
                    experience = self._clean_text(m.group(1))
            if not salary:
                m = re.search(
                    r"(?i)(₹\s*[^|\n]{1,60}?(?:lpa|lac|lakh)|\d+(?:\.\d+)?\s*(?:-\s*\d+(?:\.\d+)?)?\s*(?:lpa|lac|lakh)|not disclosed|confidential)",
                    body,
                )
                if m:
                    salary = self._clean_text(m.group(1))

            if not title or not company or len(description) < 120:
                return None

            return FoundJob(
                title=title,
                company=company,
                location=location,
                salary=salary,
                experience=experience,
                url=canonical,
                description=description,
            )
        except HumanActionRequired:
            raise
        except RuntimeError:
            raise
        except Exception:
            return None

    def enrich_existing_jobs(self, limit: int = 30) -> list[FoundJob]:
        """Refresh existing real Naukri rows from their live job pages."""
        from .cli import load_profile

        profile = load_profile()
        refreshed: list[FoundJob] = []
        rows = [
            row for row in list_jobs()
            if row["url"]
            and "/job-listings-" in row["url"].lower()
            and row["status"] not in ("SUBMITTED", "ALREADY_APPLIED")
        ]

        for row in rows[:limit]:
            detail = self._extract_job_detail(row["url"])
            if not detail:
                continue

            # Always calculate a live-JD score first, even when a hard targeting
            # rule later rejects the job. This keeps the database informative
            # instead of leaving rejected rows at the old score or at 0/NEW.
            result = score_job(detail.title, detail.description, detail.company)
            detail.score = result.score
            detail.recommendation = result.recommendation
            detail.job_id = row["id"]

            target_ok = self._target_preference_ok(detail, profile)
            if not target_ok:
                detail.score = min(detail.score, 69)
                detail.recommendation = "SKIP"

            update_job_details(
                row["id"],
                detail.company,
                detail.title,
                detail.url,
                detail.description,
                detail.score,
                detail.recommendation,
            )

            if target_ok and detail.score >= profile["targeting"]["minimum_match_score_for_review"]:
                prep = prepare_application(detail.title, detail.description, profile, detail.company)
                save_prep(row["id"], json.dumps(prep_to_dict(prep), ensure_ascii=False))
            else:
                reason = (
                    "Re-scored from full live job description but failed hard targeting filters."
                    if not target_ok
                    else "Re-scored from full live job description."
                )
                set_status(row["id"], detail.recommendation or "SKIP", reason)
            refreshed.append(detail)

        return refreshed

    def _target_preference_ok(self, job: FoundJob, profile: dict) -> bool:
        targeting = profile.get("targeting", {})
        preferred = [str(x).lower() for x in targeting.get("preferred_locations", [])]

        loc_blob = (job.location or "").lower()
        if loc_blob and preferred and not any(p in loc_blob for p in preferred):
            return False

        # Candidate has 2+ years. Do not queue jobs whose stated minimum is
        # materially above that level.
        exp_blob = (job.experience or "").lower()
        exp_range = re.search(
            r"(\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)\s*(?:yrs?|years?)",
            exp_blob,
        )
        if exp_range and float(exp_range.group(1)) > 2.5:
            return False

        if not exp_range:
            exp_single = re.search(r"(\d+(?:\.\d+)?)\s*\+?\s*(?:yrs?|years?)", exp_blob)
            if exp_single and float(exp_single.group(1)) > 2.5:
                return False

        salary_blob = (job.salary or "").lower()
        if salary_blob and "not disclosed" not in salary_blob and "confidential" not in salary_blob:
            nums = [
                float(x.replace(",", ""))
                for x in re.findall(r"(?:₹\s*)?(\d+(?:\.\d+)?)", salary_blob)
            ]
            if nums and any(t in salary_blob for t in ("lpa", "lac", "lakh")):
                if max(nums) < float(targeting.get("minimum_ctc_lpa", 8)):
                    return False

        return True


    def ingest(self, jobs: list[FoundJob]) -> list[FoundJob]:
        added: list[FoundJob] = []
        from .cli import load_profile

        profile = load_profile()
        for card_job in jobs:
            # URL identity is checked before opening the detail page so repeated
            # searches do not waste time refreshing the same listing.
            canonical = card_job.url.split("?")[0].rstrip("/")
            if not canonical or ("/job-listings-" not in canonical.lower() and "/job-listings/" not in canonical.lower()):
                continue

            fp = fingerprint_job("", "", canonical)
            duplicate = find_duplicate(card_job.company, card_job.title, canonical, fp)
            if duplicate:
                continue

            detail = self._extract_job_detail(canonical)
            if not detail:
                continue

            if not self._target_preference_ok(detail, profile):
                continue

            fp = fingerprint_job(detail.company, detail.title, detail.url)
            duplicate = find_duplicate(detail.company, detail.title, detail.url, fp)
            if duplicate:
                continue

            result = score_job(detail.title, detail.description, detail.company)
            detail.score = result.score
            detail.recommendation = result.recommendation
            detail.job_id = add_job(
                detail.company,
                detail.title,
                detail.url,
                detail.description,
                result.score,
                result.recommendation,
                fp,
            )

            if result.score >= profile["targeting"]["minimum_match_score_for_review"]:
                prep = prepare_application(detail.title, detail.description, profile, detail.company)
                save_prep(detail.job_id, json.dumps(prep_to_dict(prep), ensure_ascii=False))

            added.append(detail)

        return added

    def scan_default(self, max_queries: int = 6, per_query: int = 12) -> list[FoundJob]:
        all_found: list[FoundJob] = []
        seen = set()
        locations = PREFERRED_LOCATIONS[:4]
        for query in DEFAULT_QUERIES[:max_queries]:
            for location in locations:
                for job in self.search(query, location):
                    if job.url not in seen:
                        seen.add(job.url)
                        all_found.append(job)
                if len(all_found) >= max_queries * per_query:
                    break
            if len(all_found) >= max_queries * per_query:
                break
        return self.ingest(all_found)

    def _all_contexts(self):
        """Return the top page plus any live iframes used by Naukri's apply UI."""
        contexts = [self.page]
        try:
            for frame in self.page.frames:
                if frame is not self.page.main_frame:
                    contexts.append(frame)
        except Exception:
            pass
        return contexts

    def _all_context_text(self) -> str:
        parts = []
        for context in self._all_contexts():
            try:
                parts.append(context.locator("body").inner_text(timeout=2500))
            except Exception:
                continue
        return "\n".join(parts).lower()

    def _human_blocker(self, reason: str, interactive: bool):
        artifact = self._artifact(reason.replace(" ", "_"))
        if interactive:
            self.wait_for_human(reason)
            return
        raise HumanActionRequired(f"{reason}. Screenshot: {artifact}")

    def _verification_guard(self, reason: str, interactive: bool):
        if self.needs_human_verification():
            self._human_blocker(reason, interactive)

    def _safe_click_apply(self) -> bool:
        patterns = [
            r"^apply$",
            r"apply now",
            r"apply on naukri",
            r"quick apply",
            r"apply for this job",
        ]
        existing_pages = list(self.context.pages) if self.context else [self.page]

        for context in self._all_contexts():
            for pat in patterns:
                locators = [
                    context.get_by_role("button", name=re.compile(pat, re.I)),
                    context.get_by_role("link", name=re.compile(pat, re.I)),
                    context.get_by_text(re.compile(pat, re.I)),
                ]
                for loc in locators:
                    try:
                        if loc.count():
                            loc.first.scroll_into_view_if_needed()
                            loc.first.click()
                            self.page.wait_for_timeout(1500)
                            if self.context:
                                current_pages = self.context.pages
                                if len(current_pages) > len(existing_pages):
                                    self.page = current_pages[-1]
                                    self.page.set_default_timeout(12000)
                            return True
                    except Exception:
                        continue
        return False

    def _host_is_naukri(self, host: str) -> bool:
        host = host.lower().split(":")[0]
        return any(host == marker or host.endswith("." + marker) for marker in EXTERNAL_HOST_MARKERS)

    def _is_internal(self) -> bool:
        """Allow auto-submit only when the active application remains on Naukri."""
        try:
            hosts = {urlparse(self.page.url).netloc.lower()}
            for frame in self.page.frames:
                src = frame.url or ""
                if src and not src.startswith(("about:", "data:")):
                    hosts.add(urlparse(src).netloc.lower())
            hosts.discard("")
            return bool(hosts) and all(self._host_is_naukri(host) for host in hosts)
        except Exception:
            return False

    def _fill_input_by_label(self, labels: list[str], value: str) -> bool:
        for context in self._all_contexts():
            for label in labels:
                try:
                    loc = context.get_by_label(re.compile(label, re.I), exact=False)
                    if loc.count():
                        loc.first.fill(value)
                        return True
                except Exception:
                    pass
        return False

    def _fill_select_by_label(self, labels: list[str], values: list[str]) -> bool:
        for context in self._all_contexts():
            for label in labels:
                try:
                    loc = context.get_by_label(re.compile(label, re.I), exact=False)
                    if loc.count():
                        for value in values:
                            try:
                                loc.first.select_option(label=value)
                                return True
                            except Exception:
                                pass
                except Exception:
                    pass
        return False

    def _fill_common_fields(self, profile: dict, resume_path: Path):
        self._fill_input_by_label(["full name", "candidate name", "name"], profile.get("name", ""))
        self._fill_input_by_label(["email", "email id", "email address"], profile.get("email", ""))
        self._fill_input_by_label(["mobile", "mobile number", "phone", "phone number"], profile.get("phone", ""))

        for context in self._all_contexts():
            file_inputs = context.locator('input[type="file"]')
            try:
                if file_inputs.count() and resume_path.exists():
                    file_inputs.first.set_input_files(str(resume_path))
                    break
            except Exception:
                pass

        text = self._all_context_text()

        if re.search(r"notice period|availability", text, re.I):
            self._fill_input_by_label(["notice period", "availability"], "60 days")
            self._fill_select_by_label(
                ["notice period", "availability"],
                ["60 days", "60 Days", "2 Months", "2 month", "2 Months or less"],
            )

        if re.search(r"current.*ctc|current.*salary", text, re.I):
            self._fill_input_by_label(["current ctc", "current salary", "current compensation"], "4.5 LPA")

        if re.search(r"expected.*ctc|expected.*salary|expected.*compensation", text, re.I):
            # Keep this conservative: only fill a free-text expected-compensation field.
            self._fill_input_by_label(
                ["expected ctc", "expected salary", "expected compensation"],
                "8-16 LPA",
            )

        if re.search(r"years.*experience|experience.*years|total experience", text, re.I):
            self._fill_input_by_label(
                ["total experience", "years of experience", "experience"],
                "2+ years",
            )

        if re.search(r"relocat", text, re.I):
            for context in self._all_contexts():
                for answer in ("Yes", "YES"):
                    try:
                        choice = context.get_by_label(re.compile(r"^yes$", re.I), exact=True)
                        if choice.count():
                            choice.first.check()
                            break
                    except Exception:
                        continue

    def _unknown_sensitive_questions(self) -> list[str]:
        body = self._all_context_text()
        flags = []
        if re.search(
            r"work authorization|authorized to work|legally authorized|right to work|visa sponsorship|sponsorship",
            body,
            re.I,
        ):
            flags.append("Work authorization / visa sponsorship question")
        if re.search(r"citizenship|passport|security clearance|us person|government clearance", body, re.I):
            flags.append("Eligibility/citizenship/clearance question")
        return flags

    def _application_success_detected(self) -> bool:
        body = self._all_context_text()
        success_markers = (
            "application sent",
            "applied successfully",
            "application submitted",
            "you have applied",
            "application has been sent",
        )
        return any(marker in body for marker in success_markers)

    def _already_applied_detected(self) -> bool:
        body = self._all_context_text()
        return any(
            marker in body
            for marker in (
                "already applied",
                "you have already applied",
                "you already applied",
            )
        )

    def apply_job(self, job: dict, auto_submit: bool = False, resume_path: Path = BASE_RESUME) -> str:
        from .cli import load_profile

        profile = load_profile()
        targeting = profile.get("targeting", {})
        daily_cap = int(targeting.get("max_daily_applications", 10))

        if submitted_count_today() >= daily_cap:
            return "DAILY_CAP_REACHED"

        self.ensure_logged_in()
        self.page.goto(job["url"], wait_until="domcontentloaded")
        self.page.wait_for_timeout(1200)
        self._verification_guard(
            "Naukri presented CAPTCHA/security verification on the job page",
            interactive=not auto_submit,
        )

        if self._already_applied_detected():
            set_status(job["id"], "ALREADY_APPLIED", "Naukri indicates this application was already submitted.", daily_cap)
            return "ALREADY_APPLIED"

        if not self._safe_click_apply():
            return "APPLY_BUTTON_NOT_FOUND"

        self.page.wait_for_timeout(1200)
        self._verification_guard(
            "Naukri presented CAPTCHA/security verification during apply",
            interactive=not auto_submit,
        )

        if self._already_applied_detected():
            set_status(job["id"], "ALREADY_APPLIED", "Naukri indicates this application was already submitted.", daily_cap)
            return "ALREADY_APPLIED"

        if not self._is_internal():
            log_event(job["id"], "EXTERNAL_APPLICATION", "Apply flow left Naukri; automatic submission disabled.")
            set_status(
                job["id"],
                "EXTERNAL",
                "External application site detected; this job is not auto-submitted.",
                daily_cap,
            )
            return "EXTERNAL_APPLICATION"

        self._fill_common_fields(profile, resume_path)

        flags = self._unknown_sensitive_questions()
        if flags:
            log_event(job["id"], "USER_ACTION_REQUIRED", "; ".join(flags))
            if auto_submit:
                artifact = self._artifact("sensitive_application_questions")
                set_status(
                    job["id"],
                    "NEEDS_USER",
                    f"Sensitive eligibility question detected: {'; '.join(flags)}. Screenshot: {artifact}",
                    daily_cap,
                )
                return "NEEDS_USER"
            self.wait_for_human(
                "Please answer the application questions shown in the browser: " + "; ".join(flags)
            )

        if not auto_submit:
            set_status(job["id"], "APPROVED", "Application form prepared; auto-submit disabled.", daily_cap)
            return "READY_TO_SUBMIT"

        # Final safety: refuse to submit while a known sensitive/eligibility question remains.
        remaining = self._unknown_sensitive_questions()
        if remaining:
            set_status(
                job["id"],
                "NEEDS_USER",
                "Sensitive eligibility question remains unanswered; not submitted.",
                daily_cap,
            )
            return "NEEDS_USER"

        submit_patterns = [
            r"^submit$",
            r"submit application",
            r"send application",
            r"apply$",
            r"finish",
            r"continue",
        ]
        submitted = False

        for context in self._all_contexts():
            for pat in submit_patterns:
                for loc in [
                    context.get_by_role("button", name=re.compile(pat, re.I)),
                    context.get_by_role("link", name=re.compile(pat, re.I)),
                ]:
                    try:
                        if loc.count():
                            loc.last.scroll_into_view_if_needed()
                            loc.last.click()
                            submitted = True
                            self.page.wait_for_timeout(1500)
                            break
                    except Exception:
                        continue
                if submitted:
                    break
            if submitted:
                break

        if not submitted:
            artifact = self._artifact("submit_button_not_found")
            set_status(
                job["id"],
                "NEEDS_USER",
                f"Could not find a submit control. Screenshot: {artifact}",
                daily_cap,
            )
            return "SUBMIT_BUTTON_NOT_FOUND"

        self._verification_guard(
            "Naukri presented CAPTCHA/security verification after submit",
            interactive=False,
        )

        if self._application_success_detected():
            set_status(job["id"], "SUBMITTED", "Naukri application submitted by the browser agent.", daily_cap)
            return "SUBMITTED"

        # A submit click without confirmation is not safe to label as success.
        artifact = self._artifact("submission_confirmation_missing")
        log_event(job["id"], "SUBMISSION_UNCONFIRMED", f"Submit control clicked; confirmation not detected. Screenshot: {artifact}")
        set_status(
            job["id"],
            "NEEDS_USER",
            f"Submit control clicked but success was not confirmed. Screenshot: {artifact}",
            daily_cap,
        )
        return "SUBMITTED_UNCONFIRMED"

    def auto_apply_from_queue(self, limit: int = 10, auto_submit: bool = False) -> list[dict]:
        from .cli import load_profile

        profile = load_profile()
        targeting = profile.get("targeting", {})
        daily_cap = int(targeting.get("max_daily_applications", 10))
        min_submit_score = int(targeting.get("minimum_match_score_for_submit_recommendation", 85))

        results = []
        for row in review_queue():
            if len(results) >= limit or submitted_count_today() >= daily_cap:
                break

            score = int(row["score"] or 0)
            if auto_submit and score < min_submit_score:
                continue

            try:
                outcome = self.apply_job(dict(row), auto_submit=auto_submit)
            except HumanActionRequired as exc:
                note = str(exc)
                set_status(row["id"], "NEEDS_USER", note, daily_cap)
                outcome = "NEEDS_USER"
            except RuntimeError as exc:
                # A missing/expired login is a run-level failure; do not hammer Naukri
                # with ten identical login attempts.
                if "not logged in" in str(exc).lower():
                    raise
                note = f"Browser error: {exc}"
                log_event(row["id"], "APPLICATION_ERROR", note)
                set_status(row["id"], "ERROR", note, daily_cap)
                outcome = "ERROR"
            except Exception as exc:
                note = f"Unexpected application error: {exc}"
                log_event(row["id"], "APPLICATION_ERROR", note)
                set_status(row["id"], "ERROR", note, daily_cap)
                outcome = "ERROR"

            results.append(
                {
                    "job_id": row["id"],
                    "title": row["title"],
                    "company": row["company"],
                    "score": score,
                    "outcome": outcome,
                }
            )

        return results


def print_jobs(jobs: list[FoundJob]):
    for job in jobs:
        print(f"{job.score or 0:>3}/100 | {job.recommendation or 'NEW':<13} | {job.company[:30]:30} | {job.title[:60]} | {job.url}")
