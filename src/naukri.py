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
from .db import add_job, find_duplicate, log_event, review_queue, save_prep, set_status, submitted_count_today
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


def setup_agent_browser():
    """Open the dedicated Naukri profile in normal Chrome for one-time Google login."""
    AGENT_BROWSER_DIR.mkdir(parents=True, exist_ok=True)
    url = NAUKRI_HOME
    if os.uname().sysname == "Darwin":
        subprocess.Popen([
            "open", "-na", "Google Chrome", "--args",
            f"--user-data-dir={AGENT_BROWSER_DIR}",
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
            "--no-first-run",
            "--no-default-browser-check",
            url,
        ])
    return AGENT_BROWSER_DIR


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
    @staticmethod
    def _clear_stale_chrome_profile_lock():
        """Remove stale Chrome singleton locks only when no process owns this profile."""
        profile = str(AGENT_BROWSER_DIR)
        try:
            result = subprocess.run(
                ["pgrep", "-f", profile],
                capture_output=True,
                text=True,
                check=False,
            )
            pids = [p.strip() for p in result.stdout.splitlines() if p.strip()]
        except Exception:
            pids = []

        if pids:
            raise RuntimeError(
                "The dedicated Naukri Chrome profile is still in use by process(es) "
                + ", ".join(pids)
                + ". Quit only that dedicated Naukri Chrome window/app, then retry."
            )

        # Chrome writes these singleton files into a user-data-dir. They can be
        # left behind after an abnormal close. Never delete cookies/session data.
        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            path = AGENT_BROWSER_DIR / name
            try:
                if path.exists() or path.is_symlink():
                    path.unlink()
            except OSError:
                pass

    def __init__(self, headless: bool = False, slow_mo: int = 150):
        if sync_playwright is None:
            raise RuntimeError(
                "Playwright is unavailable. Install dependencies with pip install -r requirements.txt."
            ) from _PLAYWRIGHT_IMPORT_ERROR
        self.headless = headless
        self.slow_mo = slow_mo
        self._pw = None
        self.context = None
        self.page = None
        self.browser_user_data_dir = None
        self.browser_profile_dir = None

    def __enter__(self):
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        AGENT_BROWSER_DIR.mkdir(parents=True, exist_ok=True)
        self._clear_stale_chrome_profile_lock()
        self._pw = sync_playwright().start()

        # Use a dedicated persistent Chrome profile. The user can complete
        # Google Sign-In once inside this profile; subsequent runs reuse the
        # persisted Naukri session without touching the user's normal Chrome.
        launch_kwargs = dict(
            headless=self.headless,
            slow_mo=self.slow_mo,
            viewport={"width": 1440, "height": 1000},
            accept_downloads=True,
            channel=os.getenv("BROWSER_CHANNEL", "chrome"),
        )

        executable = os.getenv("BROWSER_EXECUTABLE_PATH")
        if executable:
            launch_kwargs["executable_path"] = executable

        self.context = self._pw.chromium.launch_persistent_context(
            str(AGENT_BROWSER_DIR),
            **launch_kwargs,
        )
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.set_default_timeout(12000)
        return self


    def __exit__(self, exc_type, exc, tb):
        try:
            if self.context:
                try:
                    self.context.close()
                except Exception:
                    # The browser may already have closed; cleanup should remain safe.
                    pass
        finally:
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
        url = self.page.url.lower()
        if "/nlogin/" in url:
            return False
        # Login forms are the strongest unauthenticated signal.
        try:
            if self.page.locator('input[type="password"]').count() > 0:
                return False
        except Exception:
            pass
        text = self._body_text()

        # The public Naukri homepage is also on naukri.com, so the domain alone
        # is NOT evidence that the user is authenticated.
        authenticated_markers = (
            "my naukri",
            "my profile",
            "view profile",
            "logout",
            "sign out",
        )
        if any(marker in text for marker in authenticated_markers):
            return True
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

    def _fill_search_form(self, query: str, location: str) -> bool:
        skill_selectors = [
            'input[placeholder*="Skills" i]',
            'input[placeholder*="Designation" i]',
            'input[placeholder*="Companies" i]',
            'input[placeholder*="search" i]',
        ]
        loc_selectors = [
            'input[placeholder*="Location" i]',
            'input[placeholder*="locality" i]',
        ]
        skill = None
        for sel in skill_selectors:
            try:
                loc = self.page.locator(sel)
                if loc.count():
                    skill = loc.first
                    break
            except Exception:
                pass
        loc_field = None
        for sel in loc_selectors:
            try:
                loc = self.page.locator(sel)
                if loc.count():
                    loc_field = loc.first
                    break
            except Exception:
                pass
        if not skill or not loc_field:
            return False
        skill.fill(query)
        self.page.wait_for_timeout(300)
        try:
            loc_field.fill(location)
        except Exception:
            pass
        self.page.wait_for_timeout(300)
        try:
            self.page.get_by_role("button", name=re.compile(r"^search$", re.I)).first.click()
        except Exception:
            try:
                self.page.locator('button[type="submit"]').first.click()
            except Exception:
                return False
        self.page.wait_for_timeout(1800)
        return True

    def search(self, query: str, location: str = "") -> list[FoundJob]:
        self.ensure_logged_in()
        self.page.goto("https://www.naukri.com/search", wait_until="domcontentloaded")
        self.page.wait_for_timeout(1000)
        searched = self._fill_search_form(query, location)
        if not searched:
            # Current Naukri exposes a search page with these concepts; the form
            # may change over time, so use the public search endpoint as fallback.
            url = f"https://www.naukri.com/search?keyword={quote_plus(query)}"
            if location:
                url += f"&location={quote_plus(location)}"
            self.page.goto(url, wait_until="domcontentloaded")
            self.page.wait_for_timeout(1500)
        if self.needs_human_verification():
            self.wait_for_human("Naukri presented CAPTCHA/security verification during search")
        return self._extract_jobs()

    def _extract_jobs(self) -> list[FoundJob]:
        links = self.page.locator('a[href*="/job-listings/"]')
        count = min(links.count(), 100)
        results: list[FoundJob] = []
        seen = set()
        for i in range(count):
            a = links.nth(i)
            try:
                title = (a.inner_text() or "").strip()
                href = a.get_attribute("href") or ""
            except Exception:
                continue
            if not title or len(title) < 3 or not href or href in seen:
                continue
            seen.add(href)
            if href.startswith("/"):
                href = "https://www.naukri.com" + href
            blob = self._nearby_card_text(a)
            company, location, salary, experience, description = self._parse_card_blob(blob, title)
            results.append(FoundJob(title, company, location, salary, experience, href, description))
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

    def _target_preference_ok(self, job: FoundJob, profile: dict) -> bool:
        targeting = profile.get("targeting", {})
        preferred = [str(x).lower() for x in targeting.get("preferred_locations", [])]
        loc_blob = (job.location or "").lower()
        if loc_blob and preferred and not any(p in loc_blob for p in preferred):
            return False
        salary_blob = (job.salary or "").lower()
        if salary_blob and "not disclosed" not in salary_blob and "confidential" not in salary_blob:
            nums = [float(x.replace(',', '')) for x in re.findall(r"(?:₹\s*)?(\d+(?:\.\d+)?)", salary_blob)]
            if nums:
                max_salary = max(nums)
                # Interpret small figures as LPA when the listing mentions LPA/Lacs/Lakh.
                if any(t in salary_blob for t in ("lpa", "lac", "lakh")) and max_salary < float(targeting.get("minimum_ctc_lpa", 8)):
                    return False
        return True

    def ingest(self, jobs: list[FoundJob]) -> list[FoundJob]:
        added: list[FoundJob] = []
        from .cli import load_profile
        profile = load_profile()
        for job in jobs:
            if not self._target_preference_ok(job, profile):
                continue
            fp = fingerprint_job(job.company, job.title, job.url)
            if find_duplicate(job.company, job.title, job.url, fp):
                continue
            result = score_job(job.title, job.description, job.company)
            # Lightweight location guard: don't lower the score if salary is unknown;
            # just let preference filtering happen before application.
            job.score = result.score
            job.recommendation = result.recommendation
            job.job_id = add_job(job.company, job.title, job.url, job.description, result.score, result.recommendation, fp)
            if result.score >= profile["targeting"]["minimum_match_score_for_review"]:
                prep = prepare_application(job.title, job.description, profile, job.company)
                save_prep(job.job_id, json.dumps(prep_to_dict(prep), ensure_ascii=False))
            added.append(job)
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
