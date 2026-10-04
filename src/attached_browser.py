"""Bridge to an already-running Chrome using Chrome DevTools Protocol (CDP).

This does not launch or take ownership of Chrome. It attaches to an existing tab/profile
through playwright-cli --cdp=chrome, so the user's normal browser session/cookies remain
in place. Detaching leaves Chrome running.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class AttachedChrome:
    def __init__(self, session: str = "naukri", profile_dir_name: str = "Default"):
        self.session = session
        self.profile_dir_name = profile_dir_name
        self.cli = self._resolve_cli()

    @staticmethod
    def _resolve_cli() -> list[str]:
        configured = os.getenv("PLAYWRIGHT_CLI_BIN")
        if configured:
            return [configured]
        found = shutil.which("playwright-cli")
        if found:
            return [found]
        # Official CLI docs support using npx when the package is not installed globally.
        return ["npx", "-y", "@playwright/cli@latest"]

    def run(self, *args: str, raw: bool = False, check: bool = True) -> str:
        cmd = [*self.cli]
        if raw:
            cmd.append("--raw")
        cmd.extend([f"--session={self.session}", *args])
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            text=True,
            capture_output=True,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        if check and proc.returncode != 0:
            raise RuntimeError(output.strip() or f"playwright-cli failed: {cmd}")
        return output.strip()

    def version(self) -> str:
        proc = subprocess.run(
            [*self.cli, "--version"],
            cwd=str(ROOT),
            text=True,
            capture_output=True,
        )
        if proc.returncode:
            raise RuntimeError((proc.stdout or "") + (proc.stderr or ""))
        return proc.stdout.strip()

    def attach(self) -> str:
        # Extension mode attaches to the currently running Chrome instance and
        # its existing tabs/profile. There is no separate Playwright profile.
        return self.run("attach", "--cdp=chrome")

    def select_naukri_tab(self) -> str:
        tabs = self.run("tab-list")
        # Example output contains: "- 2: [Naukri](https://www.naukri.com/...)"
        matches = re.findall(r"(?m)^\s*-\s*(\d+):.*?https?://(?:www\.)?naukri\.com", tabs, re.I)
        if matches:
            return self.run("tab-select", matches[0])
        # No Naukri tab: create one in the same authenticated browser.
        return self.run("tab-new", "https://www.naukri.com/")

    def detach(self) -> str:
        return self.run("detach", check=False)

    def snapshot(self, filename: str | None = None) -> str:
        args = ["snapshot"]
        if filename:
            args.append(f"--filename={filename}")
        return self.run(*args)

    def goto(self, url: str) -> str:
        return self.run("goto", url)

    def click(self, target: str) -> str:
        return self.run("click", target)

    def fill(self, target: str, value: str) -> str:
        return self.run("fill", target, value)

    def upload(self, path: str) -> str:
        return self.run("upload", path)

    def find(self, text: str) -> str:
        return self.run("find", text)

    def screenshot(self, filename: str | None = None) -> str:
        args = ["screenshot"]
        if filename:
            args.append(f"--filename={filename}")
        return self.run(*args)

    def eval(self, expression: str) -> str:
        return self.run("eval", expression, raw=True)

    def page_info(self) -> dict:
        raw = self.eval(
            "() => JSON.stringify({url: location.href, title: document.title, "
            "text: (document.body?.innerText || '').slice(0, 12000)})"
        )
        value = raw.strip()
        try:
            parsed = json.loads(value)
            # playwright-cli --raw can return a JSON string containing the
            # evaluated JSON payload. Decode one additional layer when needed.
            if isinstance(parsed, str):
                parsed = json.loads(parsed)
            if not isinstance(parsed, dict):
                raise ValueError(f"Expected object, got {type(parsed).__name__}")
            return parsed
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            raise RuntimeError(f"Could not parse browser state: {raw}") from exc

    def click_google_sign_in(self) -> bool:
        expr = """() => {
          const els = [...document.querySelectorAll('button,a,[role="button"]')];
          const el = els.find(e => /google/i.test((e.innerText || '') + ' ' + (e.getAttribute('aria-label') || '')));
          if (!el) return false;
          el.click();
          return true;
        }"""
        return self.eval(expr) == "true"

    def is_naukri_logged_in(self) -> bool:
        info = self.page_info()
        url = str(info.get("url", "")).lower()
        text = str(info.get("text", "")).lower()
        if "naukri.com" not in url:
            return False
        if any(marker in text for marker in (
            "my naukri", "my profile", "view profile", "logout", "sign out"
        )):
            return True
        # Explicit login UI means unauthenticated.
        if any(marker in text for marker in (
            "login", "sign in", "create account", "register now"
        )):
            return False
        return False
