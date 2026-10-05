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
from difflib import SequenceMatcher
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

# Naukri role discovery is designation-led: each configured role is checked
# individually against autocomplete before its result page is searched.
DEFAULT_QUERIES = [
    "Vulnerability Management",
    "Vulnerability Management Analyst",
    "Vulnerability Analyst",
    "Vulnerability Management Specialist",
    "Vulnerability Management Engineer",
    "Vulnerability Engineer",
    "Vulnerability Assessment Analyst",
    "Vulnerability Remediation Analyst",
    "Security Vulnerability Analyst",
    "Qualys Vulnerability Management",
    "IAM Engineer",
    "IAM Analyst",
    "Identity & Access Management Analyst",
    "Identity Engineer",
    "IAM Security Analyst",
    "Okta Engineer",
    "Okta Administrator",
    "Identity Security Analyst",
    "Access Management Analyst",
    "Identity & Access Management",
    "Cyber Security Engineer",
    "Cybersecurity Analyst",
    "Security Analyst",
    "Security Operations Analyst",
    "Security Operations Engineer",
    "Endpoint Security Analyst",
    "Endpoint Security Engineer",
    "Security Engineer",
    "Incident Response Analyst",
    "SOC Analyst L2 L3",
]

# Human-friendly group names can map to Naukri's actual designation labels.
# For SOC, Naukri currently exposes the L2-style numbered designation.
ROLE_VARIANTS = {
    "SOC Analyst L2 L3": ["SOC Analyst 2"],
}

# Valuable security concepts/technologies that may not exist as exact Naukri
# designation suggestions. These are searched as free-text keywords, then the
# resulting JDs are scored against the candidate profile. They are deliberately
# kept separate from designation validation so an absent designation is not
# mistaken for an absent skill.
SKILL_QUERIES = [
    "Vulnerability Assessment",
    "Vulnerability Remediation",
    "Security Vulnerability",
    "Qualys",
    "IAM",
    "Identity Access Management",
    "Okta",
    "Identity Security",
    "Access Management",
    "Endpoint Security",
    "Incident Response",
    "Microsoft Defender",
    "Carbon Black",
    "ThreatLocker",
    "Mimecast",
    "Netskope",
    "Cloudflare",
    "Cortex XDR",
    "Threat Hunting",
    "Security Automation",
]

# Naukri should receive all preferred locations in the same search field too.
PREFERRED_LOCATIONS = [
    "Remote", "Kolkata", "Bangalore", "Bengaluru", "Hyderabad", "Secunderabad",
    "Pune", "Mumbai", "Noida", "Gurgaon", "Gurugram", "Delhi NCR", "Ahmedabad",
]

# Naukri's current header search supports an experience selector. Start
# searches at 2 years to keep the result set aligned with the verified profile.
SEARCH_EXPERIENCE = "2 years"

EXTERNAL_HOST_MARKERS = ("naukri.com", "naukri.co.in")
CAPTCHA_WORDS = (
    "captcha",
    "recaptcha",
    "i am not a robot",
    "verify you are human",
    "unusual activity",
)
LOGIN_WORDS = ("login", "sign in", "log in")

class HumanActionRequired(RuntimeError):
    """A browser state requires a human action the agent will not bypass."""


class RoleNotRecognized(RuntimeError):
    """Naukri autocomplete does not contain the requested exact role phrase."""


class AutocompleteUnavailable(RuntimeError):
    """Naukri autocomplete did not return usable suggestions for this attempt."""


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
        self._last_role_recognized = None
        self._autocomplete_available = None
        self._session_ready = False
        self._last_scan_error = None

    def __enter__(self):
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

        # GitHub Actions cannot safely reuse the macOS Chrome profile because
        # Chrome protects cookies with the local OS keychain. In CI we instead
        # restore a Playwright storage-state JSON containing only the Naukri web
        # session, then save the refreshed state back to the same file on exit.
        storage_state_path = os.getenv("NAUKRI_STORAGE_STATE_PATH")
        if storage_state_path:
            state_path = Path(storage_state_path).expanduser()
            if not state_path.exists():
                raise RuntimeError(
                    f"Naukri storage state not found at {state_path}. "
                    "Bootstrap the authenticated state on the Mac first."
                )
            self._ci_storage_state_path = state_path
            self._pw = sync_playwright().start()
            self.browser = self._pw.chromium.launch(
                headless=self.headless,
                slow_mo=self.slow_mo,
            )
            self.context = self.browser.new_context(
                storage_state=str(state_path),
                accept_downloads=True,
            )
            self.page = self.context.new_page()
            self.page.goto(NAUKRI_HOME, wait_until="domcontentloaded")
            self.page.wait_for_timeout(1200)
            self.page.set_default_timeout(12000)
            return self

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
        # In CI, persist the refreshed Naukri storage state before closing the
        # ephemeral browser. The workflow encrypts this file immediately after
        # the run and removes the plaintext copy.
        state_path = getattr(self, "_ci_storage_state_path", None)
        if state_path is not None and self.context is not None and self._session_ready:
            try:
                state_path.parent.mkdir(parents=True, exist_ok=True)
                self.context.storage_state(path=str(state_path))
            except Exception as exc:
                print(f"Warning: could not persist Naukri storage state: {exc}", flush=True)

        try:
            if getattr(self, "browser", None) is not None and state_path is not None:
                self.browser.close()
        except Exception:
            pass

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
        """Verify an authenticated Naukri session from the jobseeker homepage.

        Do not treat a /mnjuser URL alone as proof of authentication: cloud
        browsers can receive an error document while the URL remains unchanged.
        """
        try:
            current_url = (self.page.url or "").lower()
            if "naukri.com" not in current_url or "mnjuser/homepage" not in current_url:
                self.page.goto(
                    NAUKRI_HOME,
                    wait_until="domcontentloaded",
                    timeout=15000,
                )
                self.page.wait_for_timeout(1000)

            url = (self.page.url or "").lower()
            text = self._body_text()

            # Explicit login/registration routes mean unauthenticated.
            if "/nlogin/" in url or ("/login" in url and "naukri.com" in url):
                return False

            # Browser/network error pages must never count as authentication.
            error_markers = (
                "this site can’t be reached",
                "this site can't be reached",
                "err_http2_protocol_error",
                "err_connection",
                "temporarily down",
                "webpage might be temporarily down",
            )
            if any(marker in text for marker in error_markers):
                return False

            authenticated_markers = (
                "my naukri",
                "my profile",
                "view profile",
                "logout",
                "sign out",
                "profile completeness",
            )
            if "mnjuser/homepage" in url and any(marker in text for marker in authenticated_markers):
                return True

            # A generic public homepage without authenticated markers is not enough.
            return False
        except Exception:
            return False

    def open_home(self, interactive: bool = True):
        try:
            self.page.goto(NAUKRI_HOME, wait_until="domcontentloaded", timeout=15000)
        except Exception as exc:
            # Naukri occasionally aborts a homepage navigation while its
            # authenticated shell redirects/hydrates. If the browser is still
            # on Naukri, treat that abort as a transient navigation race.
            if "ERR_ABORTED" not in str(exc) or "naukri.com" not in (self.page.url or "").lower():
                raise
        self.page.wait_for_timeout(1500)
        if self.needs_human_verification():
            self._human_blocker(
                "Naukri presented a CAPTCHA/security verification",
                interactive=interactive,
            )

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

    def ensure_logged_in(self, interactive: bool | None = None):
        if interactive is None:
            interactive = not bool(os.getenv("GITHUB_ACTIONS"))

        # Reuse the current authenticated Naukri page when possible. Repeated
        # SPA homepage navigations during a multi-search scan can leave the CDP
        # session waiting even though the search controls are already mounted.
        current = (self.page.url or "").lower()
        if "naukri.com" not in current:
            self.open_home(interactive=interactive)
        elif self.needs_human_verification():
            self._human_blocker(
                "Naukri presented a CAPTCHA/security verification",
                interactive=interactive,
            )
        if self.is_logged_in():
            self._session_ready = True
            return

        self._session_ready = False
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
        # On the authenticated homepage the search form is already present in
        # the DOM, but collapsed behind .nI-gNb-sb__main. The searchAppWrapper
        # class belongs to the unrelated "Search appearances" section.
        candidates = [
            # Current authenticated Naukri homepage exposes the collapsed
            # search as a dedicated expand button. Prefer this over the older
            # wrapper/span selectors because the span itself is not the click
            # target in the current build.
            self.page.locator("button.nI-gNb-sb__expand"),
            self.page.get_by_role("button", name=re.compile(r"^search jobs here$", re.I)),
            self.page.locator('[aria-label="Search jobs here"]'),
            self.page.locator("div.nI-gNb-sb__main"),
            self.page.locator(".nI-gNb-sb__placeholder"),
            self.page.get_by_text(re.compile(r"^search jobs here$", re.I)),
            self.page.get_by_role("button", name=re.compile(r"^search$", re.I)),
        ]

        def search_controls_visible() -> bool:
            try:
                selectors = [
                    'input[placeholder="Enter keyword / designation / companies"]',
                    'input[placeholder="Select experience"]',
                    'input[placeholder="Enter location"]',
                ]
                for selector in selectors:
                    loc = self.page.locator(selector)
                    if not any(
                        loc.nth(i).is_visible() and loc.nth(i).is_enabled()
                        for i in range(loc.count())
                    ):
                        return False
                return True
            except Exception:
                return False

        # Fast path for the current authenticated Naukri header. Wait briefly
        # for the expand button because the header is hydrated after the page
        # shell appears. Then trigger its React handler directly so animation
        # stability cannot make the search surface flaky.
        try:
            expand = self.page.locator(
                'button.nI-gNb-sb__expand[aria-label="Search jobs here"]'
            ).first
            expand.wait_for(state="attached", timeout=5000)
            expand.wait_for(state="visible", timeout=5000)
            if expand.is_enabled():
                expand.evaluate("(e) => e.click()")
                self.page.wait_for_timeout(700)
                if search_controls_visible():
                    return True
        except Exception:
            pass

        for loc in candidates:
            try:
                for i in range(min(loc.count(), 20)):
                    candidate = loc.nth(i)
                    if not candidate.is_enabled():
                        continue

                    candidate.scroll_into_view_if_needed()
                    box = candidate.bounding_box()
                    if not box:
                        continue

                    # Prefer a normal Playwright click, then the actual center
                    # of the live wrapper, then a DOM click for React handlers.
                    clicked = False
                    try:
                        candidate.click(timeout=5000)
                        clicked = True
                    except Exception:
                        pass

                    if not clicked:
                        try:
                            self.page.mouse.click(
                                box["x"] + box["width"] / 2,
                                box["y"] + box["height"] / 2,
                            )
                            clicked = True
                        except Exception:
                            pass

                    if not clicked:
                        try:
                            candidate.evaluate("(e) => e.click()")
                        except Exception:
                            continue

                    self.page.wait_for_timeout(500)
                    if search_controls_visible():
                        return True

                    # Some Naukri builds attach the click handler one level
                    # above/below the text node. Try the wrapper itself.
                    try:
                        wrapper = candidate.locator("xpath=..")
                        if wrapper.count() and wrapper.first.is_visible():
                            wrapper.first.click(timeout=5000)
                            self.page.wait_for_timeout(500)
                            if search_controls_visible():
                                return True
                    except Exception:
                        pass
            except Exception:
                continue

        return search_controls_visible()

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

    @staticmethod
    def _normalize_role_phrase(value: str) -> str:
        """Normalize harmless designation spelling/punctuation variants."""
        text = (value or "").casefold().replace("&", " and ")
        text = re.sub(r"\bcybersecurity\b", "cyber security", text)
        text = re.sub(r"\band\b", " ", text)
        return re.sub(r"[^a-z0-9]+", " ", text).strip()

    def _keyword_autocomplete_options(self, query: str) -> list[str]:
        """Read Naukri's live keyword/designation autocomplete options."""
        try:
            options = self.page.locator(
                'div.nI-gNb-sugg .tuple-wrap .opt[title]'
            )
            values = []
            target = re.sub(r"\s+", " ", query).strip().casefold()
            for i in range(min(options.count(), 30)):
                item = options.nth(i)
                if not item.is_visible():
                    continue
                value = self._clean_text(item.get_attribute("title") or item.inner_text())
                if value:
                    values.append(value)
            return values
        except Exception:
            return []

    @staticmethod
    def _autocomplete_similarity(query: str, candidate: str, lane: str = "role") -> float:
        """Rank a live Naukri suggestion against the requested role/location."""
        q_raw = query.casefold().strip()
        c_raw = candidate.casefold().strip()
        q = re.sub(r"\bl(\d)\b", r"\1", q_raw)
        c = re.sub(r"\bl(\d)\b", r"\1", c_raw)
        q = re.sub(r"[^a-z0-9]+", " ", q).strip()
        c = re.sub(r"[^a-z0-9]+", " ", c).strip()
        if not q or not c:
            return 0.0

        q_tokens = q.split()
        c_tokens = c.split()
        q_set = set(q_tokens)
        c_set = set(c_tokens)

        # Canonical location aliases Naukri uses in autocomplete.
        location_aliases = {
            "bangalore": {"bangalore", "bengaluru"},
            "bengaluru": {"bangalore", "bengaluru"},
            "gurgaon": {"gurgaon", "gurugram"},
            "gurugram": {"gurgaon", "gurugram"},
            "secunderabad": {"secunderabad", "hyderabad"},
            "delhi ncr": {"delhi", "ncr"},
            "remote": {"remote"},
        }
        if lane == "location":
            q_location = q.replace("/", " ").strip()
            if q_location in location_aliases:
                aliases = location_aliases[q_location]
                if c_set.issubset(aliases) or c_set.intersection(aliases):
                    score = 0.94 if c_set.issubset(aliases) else 0.82
                else:
                    score = 0.0
            else:
                overlap = sum(1 for token in q_tokens if token in c_set)
                coverage = overlap / max(1, len(q_set))
                score = 0.62 * coverage + 0.28 * SequenceMatcher(None, q, c).ratio()
            # Do not select geographic expansions such as Bangalore Rural when
            # the user asked for the city itself.
            qualifiers = {"rural", "greater", "suburban", "district", "industrial", "area"}
            if c_set.intersection(qualifiers):
                score -= 0.28
            if q_set.intersection({"bangalore", "bengaluru"}) and "bengaluru" in c_set:
                score += 0.10
            if q_set.intersection({"gurgaon", "gurugram"}) and "gurugram" in c_set:
                score += 0.10
            return max(0.0, min(1.0, score))

        if q == c:
            return 1.0

        overlap = sum(1 for token in q_tokens if token in c_set)
        coverage = overlap / max(1, len(q_set))
        extra_penalty = max(0, len(c_set) - len(q_set))
        sequence = SequenceMatcher(None, q, c).ratio()
        score = 0.54 * coverage + 0.30 * sequence - 0.03 * extra_penalty

        role_suffixes = {
            "engineer": {"engineer", "engineering"},
            "analyst": {"analyst", "analysis"},
            "specialist": {"specialist"},
            "administrator": {"administrator", "admin"},
            "manager": {"manager", "management"},
            "management": {"management", "manager"},
            "consultant": {"consultant"},
            "developer": {"developer"},
            "architect": {"architect"},
        }
        title_tokens = set().union(*role_suffixes.values())

        if lane == "role" and q_tokens:
            requested_suffix = q_tokens[-1]
            requested_suffix_set = role_suffixes.get(requested_suffix, set())
            candidate_role_tokens = c_set.intersection(title_tokens)

            if requested_suffix_set:
                if candidate_role_tokens.intersection(requested_suffix_set):
                    score += 0.30
                elif candidate_role_tokens:
                    # A different real designation is preferable to a skill noun,
                    # but still below a designation with the requested suffix.
                    score += 0.06
                    score -= 0.07
                else:
                    score -= 0.08

            # Prefer exact security-product terms over misleading near-spellings.
            brand_terms = {"qualys", "okta", "cloudflare", "netskope", "mimecast"}
            for brand in brand_terms:
                if brand in q_set:
                    if brand in c_set:
                        # Exact product-name suggestions beat lexical neighbors such
                        # as Quality for Qualys or generic companies for Okta.
                        score += 0.42
                    else:
                        score -= 0.16

            # Support/tools/developer suggestions are weaker when the requested
            # role did not ask for them.
            if "support" in c_set and "support" not in q_set:
                score -= 0.14
            if "tools" in c_set and "tools" not in q_set:
                score -= 0.14
            if "developer" in c_set and "developer" not in q_set:
                score -= 0.16
            if "security" in q_set and "security" in c_set:
                score += 0.08

            # For engineering targets, an actual security designation (Analyst,
            # Engineer, Specialist, etc.) is preferable to a bare skill noun.
            if lane == "role" and q_tokens and q_tokens[-1] == "engineer":
                if "analyst" in c_set and "engineer" not in c_set:
                    score += 0.04
                if "management" in c_set and "analyst" not in c_set and "engineer" not in c_set:
                    score -= 0.02

        return max(0.0, min(1.0, score))

    def _autocomplete_items(self):
        """Return visible autocomplete items from the active Naukri search field."""
        selectors = [
            'div.nI-gNb-sugg .tuple-wrap .opt[title]',
            'div.nI-gNb-sugg [role="option"]',
            '[role="listbox"] [role="option"]',
        ]
        seen = set()
        items = []
        for selector in selectors:
            try:
                loc = self.page.locator(selector)
                for i in range(min(loc.count(), 40)):
                    item = loc.nth(i)
                    if not item.is_visible() or not item.is_enabled():
                        continue
                    value = self._clean_text(item.get_attribute("title") or item.inner_text())
                    if not value or value.casefold() in seen:
                        continue
                    seen.add(value.casefold())
                    items.append((value, item))
            except Exception:
                continue
        return items

    def _select_best_autocomplete(self, requested: str, lane: str = "role") -> tuple[str | None, list[str]]:
        """Select the most relevant live Naukri autocomplete suggestion."""
        target = self._normalize_role_phrase(requested)
        candidates = self._autocomplete_items()
        if not candidates:
            return None, []

        ranked = sorted(
            candidates,
            key=lambda pair: self._autocomplete_similarity(requested, pair[0], lane=lane),
            reverse=True,
        )
        suggestions = [value for value, _ in ranked[:8]]
        selected_value, selected_item = ranked[0]
        score = self._autocomplete_similarity(requested, selected_value, lane=lane)

        # Do not accept a completely unrelated company/person suggestion. For a
        # role query require at least one meaningful token overlap; skill/location
        # lanes can use a broader threshold because their terms are often shorter.
        if lane == "role":
            q_tokens = set(target.split())
            c_tokens = set(self._normalize_role_phrase(selected_value).split())
            if not q_tokens.intersection(c_tokens) or score < 0.34:
                return None, suggestions

        try:
            selected_item.click(timeout=2500)
            self.page.wait_for_timeout(250)
            return selected_value, suggestions
        except Exception:
            return None, suggestions

    def _append_comma_delimiter(self, field) -> None:
        """Append a comma after an autocomplete-selected text value when Naukri keeps it in the input."""
        try:
            value = (field.input_value() or "").rstrip()
            if value and not value.endswith(","):
                field.press("End")
                field.type(", ")
        except Exception:
            # Some Naukri builds convert the selected value into a chip and clear
            # the input. In that UI there is no delimiter to type; the chip itself
            # is the committed selection.
            pass

    def _build_autocomplete_field(self, field, values: list[str], lane: str) -> list[dict]:
        """Type each requested value, inspect suggestions, select best match, and delimit."""
        report = []
        for index, requested in enumerate(values, start=1):
            field.fill(requested)
            self.page.wait_for_timeout(750)
            selected, suggestions = self._select_best_autocomplete(requested, lane=lane)
            entry = {
                "index": index,
                "requested": requested,
                "suggestions": suggestions,
                "selected": selected,
                "status": "SELECTED" if selected else "NO_RELEVANT_SUGGESTION",
            }
            report.append(entry)
            print(
                f"[search-builder] {lane} {index}/{len(values)} requested={requested!r} "
                f"suggestions={suggestions[:6]!r} selected={selected!r}",
                flush=True,
            )
            if selected:
                self._append_comma_delimiter(field)
            else:
                # Keep the original text so the user gets the requested search
                # term even when Naukri provides no useful autocomplete match.
                try:
                    field.fill(requested)
                    self._append_comma_delimiter(field)
                except Exception:
                    pass
        return report

    def _collect_autocomplete_mappings(
        self,
        values: list[str],
        lane: str,
        ensure_shell: bool = True,
    ) -> list[dict]:
        """Check each target independently and record the best Naukri suggestion."""
        if ensure_shell and not self._fill_search_shell():
            raise RuntimeError(f"Could not open Naukri search shell for {lane} autocomplete discovery.")
        field_selector = (
            'input[placeholder="Enter keyword / designation / companies"]'
            if lane == "role"
            else 'input[placeholder="Enter location"]'
        )
        field = self._first_visible_editable([field_selector])
        if not field:
            raise RuntimeError(f"Naukri {lane} search field is unavailable.")

        report = []
        for index, requested in enumerate(values, start=1):
            field.fill("")
            field.fill(requested)
            self.page.wait_for_timeout(800)
            selected, suggestions = self._select_best_autocomplete(requested, lane=lane)
            report.append({
                "index": index,
                "requested": requested,
                "suggestions": suggestions,
                "selected": selected,
                "status": "SELECTED" if selected else "NO_RELEVANT_SUGGESTION",
            })
            print(
                f"[autocomplete] {lane} {index}/{len(values)} requested={requested!r} "
                f"suggestions={suggestions[:6]!r} selected={selected!r}",
                flush=True,
            )
            # Reset the input before the next independent check. This is critical:
            # Naukri's suggestion service searches the current term, not a prior
            # comma-separated list.
            try:
                field.fill("")
            except Exception:
                pass
        return report

    def _build_target_search_form(self) -> dict:
        """Build/search diagnostics without relying on unsupported comma semantics."""
        if not self._fill_search_shell():
            raise RuntimeError("Could not open the Naukri search form.")

        keyword = self._first_visible_editable([
            'input[placeholder="Enter keyword / designation / companies"]'
        ])
        experience = self._first_visible_editable([
            'input[placeholder="Select experience"]'
        ])
        location = self._first_visible_editable([
            'input[placeholder="Enter location"]'
        ])
        if not keyword or not experience or not location:
            raise RuntimeError("Naukri search controls are not available.")

        print("[search-builder] building 30 role terms", flush=True)
        role_report = self._build_autocomplete_field(keyword, DEFAULT_QUERIES, lane="role")

        print("[search-builder] selecting experience=2 years", flush=True)
        if not self._select_two_years(experience):
            raise RuntimeError("Could not select/verify Naukri experience = 2 years.")

        print(f"[search-builder] building {len(PREFERRED_LOCATIONS)} location terms", flush=True)
        location_report = self._build_autocomplete_field(location, PREFERRED_LOCATIONS, lane="location")

        # The last delimiter is unnecessary at submit time and can confuse some
        # Naukri builds. Remove it only from a plain input; chips are left alone.
        for field in (keyword, location):
            try:
                value = (field.input_value() or "")
                if value.endswith(", "):
                    field.fill(value[:-2])
            except Exception:
                pass

        return {
            "roles": role_report,
            "experience": SEARCH_EXPERIENCE,
            "locations": location_report,
            "selected_roles": [x["selected"] for x in role_report if x["selected"]],
            "selected_locations": [x["selected"] for x in location_report if x["selected"]],
        }

    def _fill_search_shell(self) -> bool:
        """Open the authenticated Naukri header search without filling values."""
        try:
            current = (self.page.url or "").lower()
            if "naukri.com/mnjuser/homepage" not in current:
                self.page.goto(NAUKRI_HOME, wait_until="domcontentloaded", timeout=15000)
                self.page.wait_for_timeout(1200)
            return self.activate_header_search()
        except Exception:
            return False

    def _select_exact_keyword_suggestion(self, query: str) -> bool:
        """Require Naukri to recognize the exact role phrase before searching."""
        target = re.sub(r"\s+", " ", query).strip().casefold()
        if not target:
            return False

        target = self._normalize_role_phrase(query)
        for attempt in range(2):
            try:
                if attempt:
                    self.page.wait_for_timeout(900)
                options = self.page.locator(
                    'div.nI-gNb-sugg .tuple-wrap .opt[title]'
                )
                for i in range(min(options.count(), 30)):
                    item = options.nth(i)
                    if not item.is_visible() or not item.is_enabled():
                        continue
                    title = self._clean_text(item.get_attribute("title") or item.inner_text())
                    normalized = self._normalize_role_phrase(title)
                    if normalized == target:
                        item.click()
                        self.page.wait_for_timeout(250)
                        return True
            except Exception:
                continue
        return False

    def role_is_recognized(self, query: str) -> tuple[bool, list[str]]:
        """Check whether the exact role exists in Naukri autocomplete."""
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
            if keyword is None:
                return False, []
            keyword.fill(query)
            self.page.wait_for_timeout(1000)
            options = self._keyword_autocomplete_options(query)
            target = self._normalize_role_phrase(query)
            recognized = any(
                self._normalize_role_phrase(option) == target
                for option in options
            )

            # Naukri's suggestion API can lag behind the keystroke. If the
            # current dropdown still appears unrelated, give it one bounded
            # refresh window before declaring the role absent.
            if not recognized:
                self.page.wait_for_timeout(1000)
                options = self._keyword_autocomplete_options(query)
                recognized = any(
                    self._normalize_role_phrase(option) == target
                    for option in options
                )
            return recognized, options
        except Exception:
            return False, []

    def _select_two_years(self, experience_field) -> bool:
        """Use the live Naukri dropdown to select exactly 2 years.

        Use a DOM click for the option instead of Playwright's navigation-aware
        click. Naukri's SPA occasionally causes that click to wait on an unrelated
        navigation, which previously made otherwise-valid searches time out.
        """
        if experience_field is None:
            return False
        try:
            experience_field.evaluate("(e) => e.click()")
            self.page.wait_for_timeout(450)
            option = self.page.get_by_text("2 years", exact=True).first
            if not option.is_visible() or not option.is_enabled():
                return False
            option.evaluate("(e) => e.click()")
            # The exact visible option is authoritative; Naukri often stores the
            # selected value in React state rather than in the hidden input value.
            self.page.wait_for_timeout(250)
            return True
        except Exception:
            return False

    def _fill_search_form(
        self,
        query: str,
        location: str,
        validate_role: bool = True,
    ) -> bool:
        # The live authenticated homepage mounts the search form when its
        # expand button is clicked. Keep this path deliberately simple.
        try:
            current = (self.page.url or "").lower()
            if "naukri.com/mnjuser/homepage" not in current:
                self.page.goto(NAUKRI_HOME, wait_until="domcontentloaded", timeout=15000)
                self.page.wait_for_timeout(1200)
            self.activate_header_search()
            self.page.wait_for_timeout(500)
        except Exception:
            return False

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
            experience_field = self.page.locator('input[placeholder="Select experience"]')
            experience_field = next(
                (
                    experience_field.nth(i)
                    for i in range(experience_field.count())
                    if experience_field.nth(i).is_visible() and experience_field.nth(i).is_enabled()
                ),
                None,
            )
        except Exception:
            experience_field = None

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
            self._last_role_recognized = None
            self._autocomplete_available = None
            return False

        self._last_role_recognized = None
        self._autocomplete_available = None
        experience_selected = False
        try:
            # Set the experience filter before typing the long combined role query
            # so Naukri's keyword autocomplete cannot interfere with the dropdown.
            experience_selected = self._select_two_years(experience_field)
            if not experience_selected:
                raise RuntimeError("Could not select/verify Naukri experience = 2 years.")

            keyword.fill(query)
            self.page.wait_for_timeout(1000)

            # Role searches use Naukri autocomplete as the source of truth.
            # Skill searches intentionally bypass designation validation because
            # the term may be a technology/concept rather than a job title.
            if validate_role:
                options = self._keyword_autocomplete_options(query)
                if not options:
                    self.page.wait_for_timeout(1200)
                    options = self._keyword_autocomplete_options(query)
                self._autocomplete_available = bool(options)
                if not options:
                    # This means Naukri did not return a usable dropdown at all;
                    # do not misclassify the role as nonexistent.
                    self._last_role_recognized = None
                    return False
                if not self._select_exact_keyword_suggestion(query):
                    self._last_role_recognized = False
                    return False
                self._last_role_recognized = True

            self.page.wait_for_timeout(250)

            # Select 2 years in the current Naukri experience control.
            # Naukri renders this control as a dropdown; do not fall back to
            # typing/pressing Enter because that can select "Fresher".
            if experience_field and not experience_selected:
                # The live dropdown input can be visually unstable during the
                # header animation. Trigger its React handler directly first.
                try:
                    experience_field.evaluate("(e) => e.click()")
                except Exception:
                    experience_field.click(timeout=2500)
                self.page.wait_for_timeout(500)

                selected = False
                target = re.compile(r"^2\s*years?$", re.I)

                # Current Naukri renders the option text directly in a
                # single visible span. Resolve the exact option first instead
                # of scanning every <li> on the page.
                try:
                    exact = self.page.get_by_text(target, exact=True)
                    for i in range(min(exact.count(), 5)):
                        candidate = exact.nth(i)
                        if candidate.is_visible() and candidate.is_enabled():
                            candidate.click(timeout=2500)
                            self.page.wait_for_timeout(300)
                            selected = True
                            break
                except Exception:
                    selected = False

                if not selected:
                    # Keep the keyboard fallback deterministic: the current
                    # Naukri order is Fresher, 1 year, 2 years.
                    try:
                        experience_field.press("ArrowDown")
                        experience_field.press("ArrowDown")
                        experience_field.press("Enter")
                        self.page.wait_for_timeout(300)
                        selected = True
                    except Exception:
                        selected = False

                # Verify the visible control no longer says Fresher. If the
                # selection could not be verified, fail the search rather than
                # silently searching with the wrong experience.
                try:
                    current_value = re.sub(
                        r"\s+",
                        " ",
                        (experience_field.input_value() or "").strip(),
                    )
                except Exception:
                    current_value = ""

                try:
                    control_text = re.sub(
                        r"\s+",
                        " ",
                        (experience_field.inner_text() or "").strip(),
                    )
                except Exception:
                    control_text = ""

                selected_blob = f"{current_value} {control_text}".strip()
                if "fresher" in selected_blob.lower():
                    # One retry using exact visible text, because an earlier
                    # keyboard fallback may have landed on the first item.
                    try:
                        experience_field.click()
                        self.page.wait_for_timeout(250)
                        exact = self.page.get_by_text(target)
                        for i in range(min(exact.count(), 20)):
                            item = exact.nth(i)
                            if item.is_visible() and item.is_enabled():
                                item.click()
                                self.page.wait_for_timeout(300)
                                break
                    except Exception:
                        pass

            if experience_field is not None:
                # Never proceed if the dropdown visibly settled on Fresher.
                try:
                    settled = re.sub(r"\s+", " ", (experience_field.input_value() or "").strip())
                except Exception:
                    settled = ""
                if "fresher" in settled.lower():
                    raise RuntimeError("Naukri experience selector settled on Fresher instead of 2 years.")
            location_field.fill(location)
            self.page.wait_for_timeout(800)
        except Exception:
            return False

        # For a comma-separated multi-location search, keep the full value in
        # the location field. Selecting the first suggestion would collapse the
        # search back to one city and defeat the combined-location strategy.
        if "," not in location:
            try:
                suggestions = self.page.get_by_text(re.compile(rf"^{re.escape(location)}$", re.I))
                for i in range(suggestions.count()):
                    candidate = suggestions.nth(i)
                    if candidate.is_visible() and candidate.is_enabled():
                        candidate.click()
                        break
            except Exception:
                pass

        # The inspected UI exposes a visible Search button. Prefer the DOM
        # click because the header can still be transitioning between states.
        try:
            buttons = self.page.locator("button.nI-gNb-sb__icon-wrapper")
            for i in range(buttons.count()):
                candidate = buttons.nth(i)
                if candidate.is_enabled():
                    candidate.evaluate("(e) => e.click()")
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

    def search(
        self,
        query: str,
        location: str = "",
        interactive: bool = True,
        validate_role: bool = True,
        ensure_login: bool = True,
    ) -> list[FoundJob]:
        if ensure_login and not getattr(self, "_session_ready", False):
            self.ensure_logged_in(interactive=interactive)
        # _fill_search_form performs the single bounded homepage navigation.
        # Avoid the previous duplicate goto() sequence, which made repeated role
        # searches unnecessarily sensitive to Naukri's SPA hydration state.
        searched = self._fill_search_form(query, location, validate_role=validate_role)

        if validate_role and not searched:
            if self._autocomplete_available is False:
                raise AutocompleteUnavailable(
                    f"Naukri autocomplete returned no usable suggestions for: {query}"
                )
            if self._last_role_recognized is False:
                raise RoleNotRecognized(
                    f"Naukri autocomplete has no exact role suggestion for: {query}"
                )

        if not searched:
            # The authenticated header is hydrated asynchronously. Give the
            # live page one bounded retry before failing the query.
            try:
                self.page.goto(NAUKRI_HOME, wait_until="domcontentloaded", timeout=15000)
                self.page.wait_for_timeout(1800)
            except Exception:
                pass
            searched = self._fill_search_form(
                query,
                location,
                validate_role=validate_role,
            )

        if not searched:
            raise RuntimeError(
                "Could not use the current Naukri header search UI. "
                "Open Naukri homepage and run 'naukri inspect-search' for diagnostics."
            )

        self._set_relevance_sort()

        if self.needs_human_verification():
            self._human_blocker(
                "Naukri presented CAPTCHA/security verification during search",
                interactive=interactive,
            )
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

            parsed_href = urlparse(href)
            host = parsed_href.netloc.lower()
            if host and not self._host_is_naukri(host):
                continue
            # Social-share anchors can contain a real job-listing URL inside
            # their query string. Only the canonical path itself qualifies as
            # a job listing; never let query parameters turn share links into
            # false-positive jobs.
            canonical_path = (parsed_href.path or "").lower()
            if "/job-listings-" not in canonical_path and "/job-listings/" not in canonical_path:
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

    def _extract_job_detail(
        self,
        url: str,
        interactive: bool = True,
    ) -> FoundJob | None:
        """Open one real Naukri listing and extract authoritative job-page data."""
        canonical = (url or "").split("?")[0]
        if not canonical:
            return None

        try:
            self.page.goto(canonical, wait_until="domcontentloaded", timeout=20000)
            self.page.wait_for_timeout(1400)

            if self.needs_human_verification():
                self._human_blocker(
                    "Naukri presented CAPTCHA/security verification on a job detail page",
                    interactive=interactive,
                )

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
            result = score_job(detail.title, detail.description, detail.company, detail.salary)
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
                detail.salary,
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

    @staticmethod
    def _minimum_required_experience(job: FoundJob):
        """Extract the clearest minimum experience signal from the listing."""
        # Prefer Naukri's structured experience field.
        candidates = [
            job.experience or "",
            # Naukri URLs commonly contain e.g. "8-to-13-years".
            job.url or "",
        ]
        for text in candidates:
            blob = (text or "").lower()
            m = re.search(
                r"(\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)\s*(?:yrs?|years?)",
                blob,
            )
            if m:
                return float(m.group(1))

            m = re.search(
                r"(\d+(?:\.\d+)?)\s*\+\s*(?:yrs?|years?)",
                blob,
            )
            if m:
                return float(m.group(1))

        # Finally check explicit minimum/at-least wording in the JD.
        blob = (job.description or "").lower()
        m = re.search(
            r"(?:minimum\s+of|at\s+least|minimum)\s+(\d+(?:\.\d+)?)\s*\+?\s*(?:yrs?|years?)",
            blob,
        )
        if m:
            return float(m.group(1))

        return None

    @staticmethod
    def _normalize_location_for_matching(value: str) -> str:
        """Normalize punctuation/aliases so preferred-location OR matching is robust."""
        value = re.sub(r"[\/,&|]+", " ", (value or "").lower())
        value = re.sub(r"[.()]+", " ", value)
        value = re.sub(r"\s+", " ", value).strip()
        aliases = {
            "bangalore": "bengaluru",
            "gurgaon": "gurugram",
            "secunderabad": "hyderabad",
            "delhi ncr": "delhi ncr",
            "delhi  ncr": "delhi ncr",
        }
        for source, target in aliases.items():
            value = re.sub(rf"(?<!\w){re.escape(source)}(?!\w)", target, value)
        return value

    def _target_preference_ok(self, job: FoundJob, profile: dict) -> bool:
        targeting = profile.get("targeting", {})
        preferred = [
            self._normalize_location_for_matching(str(x))
            for x in targeting.get("preferred_locations", [])
        ]

        loc_blob = self._normalize_location_for_matching(job.location or "")
        if preferred and not loc_blob:
            # A preferred-location constraint should never silently accept a job
            # whose authoritative detail page provides no location evidence.
            return False
        if loc_blob and preferred and not any(
            re.search(rf"(?<!\w){re.escape(p)}(?!\w)", loc_blob) for p in preferred if p
        ):
            return False

        # Candidate has 2+ years. Use the structured field/URL first and only
        # fall back to explicit "minimum/at least" wording in the JD.
        min_exp = self._minimum_required_experience(job)
        if min_exp is not None and min_exp > 2.5:
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

            try:
                detail = self._extract_job_detail(canonical, interactive=False)
            except HumanActionRequired:
                # A CAPTCHA/security gate on one listing should not stall a
                # bulk discovery run. Application execution remains fail-safe
                # and can still stop for human action on the selected job.
                continue
            if not detail:
                continue

            if not self._target_preference_ok(detail, profile):
                result = score_job(detail.title, detail.description, detail.company, detail.salary)
                detail.score = min(result.score, 69)
                detail.recommendation = "SKIP"
                # Keep the record for auditability, but never create application prep.
                fp = fingerprint_job(detail.company, detail.title, detail.url)
                duplicate = find_duplicate(detail.company, detail.title, detail.url, fp)
                if not duplicate:
                    detail.job_id = add_job(
                        detail.company,
                        detail.title,
                        detail.url,
                        detail.description,
                        detail.score,
                        "SKIP",
                        fp,
                        detail.salary,
                    )
                    added.append(detail)
                continue

            fp = fingerprint_job(detail.company, detail.title, detail.url)
            duplicate = find_duplicate(detail.company, detail.title, detail.url, fp)
            if duplicate:
                continue

            result = score_job(detail.title, detail.description, detail.company, detail.salary)
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
                detail.salary,
            )

            if result.score >= profile["targeting"]["minimum_match_score_for_review"]:
                prep = prepare_application(detail.title, detail.description, profile, detail.company)
                save_prep(detail.job_id, json.dumps(prep_to_dict(prep), ensure_ascii=False))

            added.append(detail)

        return added

    def scan_default(self, max_queries: int = 30, per_query: int = 20) -> list[FoundJob]:
        """Run OR-style role retrieval, then apply location/role scoring locally.

        Naukri's keyword box does not reliably implement a large comma-separated
        OR expression. Each configured role intent therefore gets its own search
        with the experience selector set to 2 years and no location restriction.
        The union of those result sets implements the OR across roles.

        Preferred locations remain an OR condition, but they are enforced locally
        from authoritative job detail pages. This avoids the 30 x 13 search matrix.
        Duplicate result URLs are removed before deep JD analysis.
        """
        from .cli import load_profile

        profile = load_profile()
        role_count = min(max(max_queries, 0), len(DEFAULT_QUERIES))
        roles = DEFAULT_QUERIES[:role_count]
        if per_query < 1:
            raise ValueError("per_query must be >= 1")

        print(f"[scan] ROLE_SEARCHES={len(roles)}", flush=True)
        print("[scan] LOCATION_RETRIEVAL=unrestricted; LOCAL_LOCATION_LOGIC=OR", flush=True)
        print(f"[scan] EXPERIENCE_FILTER={SEARCH_EXPERIENCE}", flush=True)

        try:
            self.ensure_logged_in(interactive=False)
        except RuntimeError as exc:
            self._last_scan_error = f"AUTH_ERROR: {exc}"
            print(f"[scan] AUTH_ERROR: {exc}", flush=True)
            return []
        print("[scan] AUTHENTICATED_SESSION=READY", flush=True)

        # Canonicalize roles/locations first so the browser still follows the
        # user's autocomplete workflow and keeps an auditable mapping artifact.
        report_path = ARTIFACT_DIR / "latest_search_autocomplete_report.json"
        role_report = None
        location_report = None

        # Reuse a complete, previously verified autocomplete mapping when one
        # exists. This prevents a transient Naukri dropdown failure from blocking
        # the actual 30-role retrieval run; autocomplete is a discovery artifact,
        # not a prerequisite for every individual search.
        try:
            if report_path.exists():
                cached = json.loads(report_path.read_text(encoding="utf-8"))
                if (
                    len(cached.get("roles", [])) >= role_count
                    and len(cached.get("locations", [])) == len(PREFERRED_LOCATIONS)
                ):
                    role_report = cached["roles"][:role_count]
                    location_report = cached["locations"]
                    print(f"[scan] AUTOCOMPLETE_CACHE=USING {report_path}", flush=True)
        except Exception:
            role_report = None
            location_report = None

        if role_report is None or location_report is None:
            try:
                if not self._fill_search_shell():
                    raise RuntimeError("Could not open Naukri search shell for autocomplete discovery.")

                role_report = self._collect_autocomplete_mappings(
                    roles,
                    "role",
                    ensure_shell=False,
                )
                location_report = self._collect_autocomplete_mappings(
                    PREFERRED_LOCATIONS,
                    "location",
                    ensure_shell=False,
                )

                report = {
                    "roles": role_report,
                    "locations": location_report,
                    "experience": SEARCH_EXPERIENCE,
                    "search_strategy": (
                        "one Naukri search per role; 2 years; unrestricted retrieval location; "
                        "preferred locations are OR-filtered locally; dedupe + JD scoring"
                    ),
                }
                report_path.write_text(
                    json.dumps(report, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                print(f"[scan] AUTOCOMPLETE_REPORT={report_path}", flush=True)
            except HumanActionRequired:
                print("[scan] SKIP_VERIFICATION: autocomplete discovery", flush=True)
                return []
            except RuntimeError as exc:
                self._last_scan_error = f"SEARCH_ERROR: {exc}"
                print(f"[scan] SEARCH_ERROR: {exc}", flush=True)
                return []

        found_by_url: dict[str, FoundJob] = {}
        search_log = []

        # One actual Naukri retrieval per role. Location is intentionally blank:
        # preferred locations are an OR condition handled after the JD is opened.
        for index, (requested, mapping) in enumerate(zip(roles, role_report), start=1):
            query = mapping.get("selected") or requested
            validate_role = mapping.get("selected") is not None
            print(
                f"[scan] ROLE_SEARCH {index}/{len(roles)} requested={requested!r} "
                f"query={query!r} location=<unrestricted>",
                flush=True,
            )
            try:
                jobs = self.search(
                    query=query,
                    location="",
                    interactive=False,
                    ensure_login=False,
                    # Autocomplete was already resolved above (or loaded from
                    # the verified mapping artifact). Do not re-open the dropdown
                    # for every search; that creates unnecessary SPA races.
                    validate_role=False,
                )
                limited = jobs[:per_query]
                new_count = 0
                for job in limited:
                    key = job.url.split("?")[0].rstrip("/").lower()
                    if not key or key in found_by_url:
                        continue
                    found_by_url[key] = job
                    new_count += 1

                search_log.append({
                    "index": index,
                    "requested": requested,
                    "query": query,
                    "result_cards": len(jobs),
                    "retained_cards": len(limited),
                    "new_unique_jobs": new_count,
                })
                print(
                    f"[scan] ROLE_SEARCH_RESULT {index}/{len(roles)} "
                    f"cards={len(jobs)} retained={len(limited)} new_unique={new_count}",
                    flush=True,
                )
            except HumanActionRequired as exc:
                search_log.append({
                    "index": index,
                    "requested": requested,
                    "query": query,
                    "error": type(exc).__name__,
                })
                print(f"[scan] ROLE_SEARCH_STOP: {exc}", flush=True)
                print("[scan] SKIP_VERIFICATION: stopping role retrieval safely", flush=True)
                return []
            except (AutocompleteUnavailable, RoleNotRecognized) as exc:
                search_log.append({
                    "index": index,
                    "requested": requested,
                    "query": query,
                    "error": type(exc).__name__,
                })
                print(f"[scan] ROLE_SEARCH_SKIP {index}/{len(roles)}: {exc}", flush=True)
                continue
            except RuntimeError as exc:
                search_log.append({
                    "index": index,
                    "requested": requested,
                    "query": query,
                    "error": str(exc),
                })
                print(f"[scan] ROLE_SEARCH_ERROR {index}/{len(roles)}: {exc}", flush=True)
                continue

        found_jobs = list(found_by_url.values())

        scan_artifact = ARTIFACT_DIR / "latest_or_search_report.json"
        try:
            scan_artifact.write_text(
                json.dumps(
                    {
                        "experience": SEARCH_EXPERIENCE,
                        "role_count": len(roles),
                        "location_logic": "OR (local authoritative-detail filter)",
                        "retrieval_location": "unrestricted",
                        "unique_result_count": len(found_jobs),
                        "searches": search_log,
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            print(f"[scan] SEARCH_REPORT={scan_artifact}", flush=True)
        except Exception:
            pass

        print(f"[scan] RESULT_POOL={len(found_jobs)} unique real job cards", flush=True)
        print(
            "[scan] FILTERING=role OR + location OR + experience + CTC + JD/resume skills + dedupe + score",
            flush=True,
        )

        # ingest() opens authoritative detail pages and applies preferred-location
        # OR matching, experience/CTC hard filters, scoring and deduplication.
        added = self.ingest(found_jobs)

        # Materialize the calibrated application-review queue in score order.
        # This is preparation only: no application action is performed here.
        review_threshold = profile["targeting"]["minimum_match_score_for_review"]
        submit_threshold = profile["targeting"]["minimum_match_score_for_submit_recommendation"]
        review_jobs = [job for job in added if (job.score or 0) >= review_threshold]
        review_jobs.sort(key=lambda job: (job.score or 0), reverse=True)
        queue_path = ARTIFACT_DIR / "calibrated_application_queue.json"
        try:
            queue_items = []
            for rank, job in enumerate(review_jobs, 1):
                queue_items.append({
                    "rank": rank,
                    "job_id": job.job_id,
                    "company": job.company,
                    "title": job.title,
                    "score": job.score,
                    "recommendation": job.recommendation,
                    "url": job.url,
                })
            queue_path.write_text(
                json.dumps({
                    "queue": "calibrated_application_review_queue",
                    "total_prepared": len(queue_items),
                    "daily_cap": profile["targeting"]["max_daily_applications"],
                    "today_batch": queue_items[:profile["targeting"]["max_daily_applications"]],
                    "remaining": queue_items[profile["targeting"]["max_daily_applications"]:],
                    "auto_submit_enabled": False,
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"[scan] APPLICATION_QUEUE={queue_path} total={len(queue_items)} today_batch={min(len(queue_items), profile['targeting']['max_daily_applications'])}", flush=True)
        except Exception as exc:
            print(f"[scan] APPLICATION_QUEUE_ERROR={exc}", flush=True)

        reviewable = sum(1 for job in added if (job.score or 0) >= review_threshold)
        high_priority = sum(1 for job in added if (job.score or 0) >= submit_threshold)
        print(f"[scan] EVALUATED_NEW_JOBS={len(added)}", flush=True)
        print(f"[scan] REVIEWABLE_TARGETS={reviewable} HIGH_PRIORITY_TARGETS={high_priority}", flush=True)
        return added

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

    def _apply_surface_state(self) -> str:
        """Classify the visible job-page Apply surface without clicking.

        Returns INTERNAL, EXTERNAL, ALREADY_APPLIED, CAPTCHA, or UNKNOWN.
        A selector miss therefore remains distinguishable from an employer
        redirect and from a security challenge.
        """
        if self.needs_human_verification():
            return "CAPTCHA"
        if self._already_applied_detected():
            return "ALREADY_APPLIED"

        body = self._all_context_text()
        external_re = re.compile(
            r"apply\s+(?:on|at)\s+(?:the\s+)?(?:company|employer)\s+site|"
            r"apply\s+on\s+(?:their|company)\s+website",
            re.I,
        )
        if external_re.search(body):
            return "EXTERNAL"

        apply_re = re.compile(
            r"^(?:apply|apply now|quick apply|apply for this job)$",
            re.I,
        )
        for context in self._all_contexts():
            try:
                candidates = [
                    context.get_by_role("button", name=apply_re),
                    context.get_by_role("link", name=apply_re),
                    context.locator('[role="button"]').filter(has_text=apply_re),
                    context.locator("button, a").filter(has_text=apply_re),
                ]
                for loc in candidates:
                    for index in range(min(loc.count(), 20)):
                        candidate = loc.nth(index)
                        try:
                            if candidate.is_visible() and candidate.is_enabled():
                                return "INTERNAL"
                        except Exception:
                            continue
            except Exception:
                continue

        fallback = (
            'button[id*="apply" i], a[id*="apply" i], '
            'button[class*="apply" i], a[class*="apply" i], '
            '[role="button"][id*="apply" i], [role="button"][class*="apply" i], '
            '[data-test*="apply" i], [data-qa*="apply" i], '
            '[data-testid*="apply" i], [class*="apply-button" i], '
            '[class*="applyButton" i]'
        )
        for context in self._all_contexts():
            try:
                loc = context.locator(fallback)
                for index in range(min(loc.count(), 20)):
                    candidate = loc.nth(index)
                    try:
                        text = " ".join((candidate.inner_text() or "").split()).lower()
                        if candidate.is_visible() and candidate.is_enabled() and (
                            text == "apply" or "apply" in text
                        ):
                            return "INTERNAL"
                    except Exception:
                        continue
            except Exception:
                continue

        return "UNKNOWN"

    def _apply_surface_diagnostics(self) -> dict:
        """Return bounded diagnostics for a failed/ambiguous Apply surface."""
        diagnostics = {
            "url": self.page.url if self.page else "",
            "state": self._apply_surface_state(),
            "title": "",
            "visible_apply_controls": [],
        }
        try:
            diagnostics["title"] = self.page.title()
        except Exception:
            pass
        applyish = re.compile(r"apply|quick apply", re.I)
        for context in self._all_contexts():
            try:
                loc = context.locator("button, a, [role='button']")
                for index in range(min(loc.count(), 100)):
                    item = loc.nth(index)
                    if not item.is_visible():
                        continue
                    txt = " ".join((item.inner_text() or "").split())
                    aria = item.get_attribute("aria-label") or ""
                    if applyish.search(txt) or applyish.search(aria):
                        diagnostics["visible_apply_controls"].append({
                            "tag": (item.evaluate("(e) => e.tagName") or "").lower(),
                            "text": txt[:160],
                            "aria": aria[:160],
                            "id": (item.get_attribute("id") or "")[:120],
                            "class": (item.get_attribute("class") or "")[:160],
                        })
            except Exception:
                continue
        diagnostics["visible_apply_controls"] = diagnostics["visible_apply_controls"][:20]
        return diagnostics

    def _safe_click_apply(self) -> bool:
        """Click only a classifier-approved internal Naukri Apply control."""
        if self._apply_surface_state() != "INTERNAL":
            return False

        apply_re = re.compile(
            r"^(?:apply|apply now|quick apply|apply for this job)$",
            re.I,
        )
        for context in self._all_contexts():
            locators = [
                context.get_by_role("button", name=apply_re),
                context.get_by_role("link", name=apply_re),
                context.locator('[role="button"]').filter(has_text=apply_re),
                context.locator("button, a").filter(has_text=apply_re),
            ]
            for loc in locators:
                for index in range(min(loc.count(), 20)):
                    candidate = loc.nth(index)
                    try:
                        if not candidate.is_visible() or not candidate.is_enabled():
                            continue
                        candidate.scroll_into_view_if_needed()
                        candidate.click(timeout=6000)
                        self.page.wait_for_timeout(1500)
                        pages = list(self.context.pages) if self.context else [self.page]
                        naukri_pages = [
                            page for page in pages
                            if "naukri.com" in (page.url or "").lower()
                        ]
                        if naukri_pages:
                            self.page = naukri_pages[-1]
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

    def _missing_required_fields(self) -> list[str]:
        """Find visible required form controls that still need an answer."""
        missing = []
        seen = set()
        for context in self._all_contexts():
            try:
                controls = context.locator(
                    'input[required], textarea[required], select[required], '
                    '[contenteditable="true"][aria-required="true"]'
                )
                for i in range(min(controls.count(), 100)):
                    control = controls.nth(i)
                    try:
                        if not control.is_visible() or not control.is_enabled():
                            continue
                        tag = (control.evaluate("(e) => e.tagName") or "").lower()
                        typ = (control.get_attribute("type") or "").lower()
                        if typ in {"hidden", "submit", "button", "reset", "file"}:
                            continue

                        answered = False
                        if typ in {"checkbox", "radio"}:
                            answered = control.is_checked()
                        elif tag == "select":
                            answered = bool((control.input_value() or "").strip())
                        else:
                            answered = bool((control.input_value() or "").strip())
                            if not answered:
                                try:
                                    answered = bool((control.inner_text() or "").strip())
                                except Exception:
                                    pass

                        if answered:
                            continue

                        label = (
                            control.get_attribute("aria-label")
                            or control.get_attribute("placeholder")
                            or control.get_attribute("name")
                            or tag
                        )
                        label = re.sub(r"\s+", " ", label).strip()
                        if label and label not in seen:
                            seen.add(label)
                            missing.append(label)
                    except Exception:
                        continue
            except Exception:
                continue
        return missing

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
            "applied to \"",
            "applied to '",
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

        # On the current Naukri flow, the visible Apply button can itself
        # submit a one-click application and redirect to /myapply/saveApply.
        # Therefore prepare-only mode must NEVER click it.
        if not auto_submit:
            external_apply = self.page.get_by_text(
                re.compile(r"apply\s+(?:on|at)\s+(?:the\s+)?(?:company|employer)\s+site", re.I)
            )
            if external_apply.count() and any(external_apply.nth(i).is_visible() for i in range(min(5, external_apply.count()))):
                log_event(job["id"], "EXTERNAL_APPLICATION", "Naukri exposes an employer-hosted application flow.")
                set_status(
                    job["id"],
                    "EXTERNAL",
                    "External application site detected; automatic submission disabled.",
                    daily_cap,
                )
                return "EXTERNAL_APPLICATION"
            set_status(job["id"], "APPROVED", "Application target verified; Apply click intentionally not performed in prepare-only mode.", daily_cap)
            return "READY_TO_APPLY"

        surface = self._apply_surface_state()
        if surface == "CAPTCHA":
            artifact = self._artifact("apply_security_gate")
            set_status(
                job["id"],
                "NEEDS_USER",
                f"Naukri presented a security verification on the Apply surface. Screenshot: {artifact}",
                daily_cap,
            )
            return "NEEDS_USER"
        if surface == "ALREADY_APPLIED":
            set_status(job["id"], "ALREADY_APPLIED", "Naukri indicates this application was already submitted.", daily_cap)
            return "ALREADY_APPLIED"
        if surface == "EXTERNAL":
            log_event(job["id"], "EXTERNAL_APPLICATION", "Naukri exposes an employer-hosted application flow.")
            set_status(
                job["id"],
                "EXTERNAL",
                "External application site detected; automatic submission disabled.",
                daily_cap,
            )
            return "EXTERNAL_APPLICATION"
        if surface != "INTERNAL":
            diagnostics = self._apply_surface_diagnostics()
            log_event(job["id"], "APPLY_SURFACE_UNKNOWN", json.dumps(diagnostics, ensure_ascii=False))
            artifact = self._artifact("apply_surface_unknown")
            set_status(
                job["id"],
                "NEEDS_USER",
                f"Could not positively identify the internal Naukri Apply control. Screenshot: {artifact}",
                daily_cap,
            )
            return "APPLY_SURFACE_UNKNOWN"

        if not self._safe_click_apply():
            diagnostics = self._apply_surface_diagnostics()
            log_event(job["id"], "APPLY_CLICK_FAILED", json.dumps(diagnostics, ensure_ascii=False))
            artifact = self._artifact("apply_click_failed")
            set_status(
                job["id"],
                "NEEDS_USER",
                f"Internal Apply control was identified but could not be clicked. Screenshot: {artifact}",
                daily_cap,
            )
            return "APPLY_CLICK_FAILED"
        self.page.wait_for_timeout(1500)
        self._verification_guard(
            "Naukri presented CAPTCHA/security verification during apply",
            interactive=False,
        )

        # One-click Naukri applications land on the Apply Confirmation page.
        # Treat that confirmation as the authoritative submission result and do
        # not attempt a second submit.
        if self._application_success_detected():
            set_status(job["id"], "SUBMITTED", "Naukri Apply click reached an application confirmation page.", daily_cap)
            return "SUBMITTED"

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

        # Never submit a form with an unanswered required field. This catches
        # ordinary custom application questions as well as fields that Naukri
        # changes without warning.
        missing_required = self._missing_required_fields()
        if missing_required:
            note = "Required application fields still need answers: " + "; ".join(missing_required[:12])
            artifact = self._artifact("required_application_fields")
            set_status(
                job["id"],
                "NEEDS_USER",
                f"{note}. Screenshot: {artifact}",
                daily_cap,
            )
            return "NEEDS_USER"

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
