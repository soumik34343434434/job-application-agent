"""Bridge to an already-running Chrome using the official Playwright browser extension.

This does not launch or take ownership of Chrome. It attaches to an existing tab/profile
through playwright-cli --extension, so the user's normal browser session/cookies remain
in place. Detaching leaves Chrome running.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
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
        return self.run(
            "attach",
            "--extension=chrome",
            f"--profile-dir-name={self.profile_dir_name}",
        )

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
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Could not parse browser state: {raw}") from exc

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
