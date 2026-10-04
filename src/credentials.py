"""Local credential helpers.

Uses the operating system keyring when available. No credentials are written into
project JSON, SQLite, logs, screenshots, or source files.
"""
from __future__ import annotations

import getpass
import os

SERVICE = "job-application-agent"
USERNAME_KEY = "naukri.username"
PASSWORD_KEY = "naukri.password"


def _keyring():
    try:
        import keyring
        return keyring
    except ImportError as exc:
        raise RuntimeError("keyring package is not installed") from exc


def save_naukri_credentials(username: str, password: str) -> None:
    kr = _keyring()
    kr.set_password(SERVICE, USERNAME_KEY, username)
    kr.set_password(SERVICE, PASSWORD_KEY, password)


def get_naukri_credentials() -> tuple[str | None, str | None]:
    username = os.getenv("NAUKRI_EMAIL") or os.getenv("NAUKRI_USERNAME")
    password = os.getenv("NAUKRI_PASSWORD")
    try:
        kr = _keyring()
        username = username or kr.get_password(SERVICE, USERNAME_KEY)
        password = password or kr.get_password(SERVICE, PASSWORD_KEY)
    except Exception:
        # Environment variables remain a supported fallback for machines without
        # a configured OS keyring.
        pass
    return username, password


def interactive_setup() -> str:
    username = input("Naukri email/username: ").strip()
    if not username:
        raise ValueError("Naukri email/username is required")
    password = getpass.getpass("Naukri password (hidden): ")
    if not password:
        raise ValueError("Naukri password is required")
    try:
        save_naukri_credentials(username, password)
        return "Saved Naukri credentials to the OS credential store."
    except Exception as exc:
        return (
            "Could not use the OS credential store. Credentials were NOT saved. "
            f"Set NAUKRI_EMAIL/NAUKRI_PASSWORD in your local environment instead. Error: {exc}"
        )
