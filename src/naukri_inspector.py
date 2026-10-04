"""Read-only live Naukri browser introspection and OpenAI architecture analysis.

This module attaches to the already-running dedicated Naukri Chrome over CDP.
It never launches a browser, navigates, clicks, fills, submits, or mutates the
Naukri session. It only observes the current page/DOM and records sanitized
network metadata after the inspector starts.

Run the local API with:
    python3 -m src.cli naukri inspect-api

Then inspect:
    http://127.0.0.1:8765/docs
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from fastapi import FastAPI, Header, HTTPException, Query, Response
from pydantic import BaseModel

from .naukri import AGENT_CDP_ENDPOINT, _cdp_is_available

try:
    from playwright.sync_api import sync_playwright
except Exception as exc:  # pragma: no cover
    sync_playwright = None
    _PLAYWRIGHT_IMPORT_ERROR = exc
else:
    _PLAYWRIGHT_IMPORT_ERROR = None

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOST = os.getenv("NAUKRI_INSPECTOR_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.getenv("NAUKRI_INSPECTOR_PORT", "8765"))
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "")
MAX_BODY_TEXT = int(os.getenv("NAUKRI_INSPECTOR_MAX_TEXT", "30000"))
MAX_DOM_TEXT = int(os.getenv("NAUKRI_INSPECTOR_MAX_DOM", "60000"))
MAX_NETWORK_EVENTS = int(os.getenv("NAUKRI_INSPECTOR_MAX_NETWORK", "500"))

_SECRET_QUERY_KEYS = re.compile(
    r"(token|auth|authorization|cookie|session|password|passwd|secret|api[_-]?key|otp|code|csrf|xsrf|jwt)",
    re.I,
)
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\s().-]{8,}\d)(?!\d)")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")
_LONG_SECRET_RE = re.compile(r"\b[A-Za-z0-9_-]{40,}\b")


def _redact_text(value: str, limit: int | None = None) -> str:
    text = value or ""
    text = _EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    text = _PHONE_RE.sub("[REDACTED_PHONE]", text)
    text = _BEARER_RE.sub("Bearer [REDACTED_TOKEN]", text)
    text = _JWT_RE.sub("[REDACTED_JWT]", text)
    text = re.sub(r"(?i)(authorization\s*[:=]\s*)[^,;\n]+", r"\1[REDACTED]", text)
    if limit is not None:
        return text[:limit]
    return text


def _safe_url(url: str) -> str:
    try:
        parsed = urlparse(url or "")
        pairs = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            safe_value = "[REDACTED]" if _SECRET_QUERY_KEYS.search(key) else _redact_text(value, 300)
            pairs.append((key, safe_value))
        return urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                parsed.params,
                urlencode(pairs),
                "",
            )
        )
    except Exception:
        return _redact_text(url, 2000)


def _safe_headers(headers: dict[str, str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in (headers or {}).items():
        low = key.lower()
        if low in {"authorization", "cookie", "set-cookie", "proxy-authorization"} or _SECRET_QUERY_KEYS.search(low):
            out[key] = "[REDACTED]"
        else:
            out[key] = _redact_text(value, 300)
    return out


def _playwright_locator_hint(element: dict[str, Any]) -> str:
    role = element.get("role")
    name = (element.get("aria") or element.get("text") or "").strip()
    placeholder = (element.get("placeholder") or "").strip()
    if role and name:
        return f'get_by_role("{role}", name={name!r})'
    if placeholder:
        return f'locator(\'input[placeholder={placeholder!r}]\')'
    tag = element.get("tag")
    input_name = (element.get("name") or "").strip()
    if tag == "input" and input_name:
        return f'locator(\'input[name={input_name!r}]\')'
    return tag or "element"


class NetworkRecorder:
    def __init__(self, max_events: int = MAX_NETWORK_EVENTS):
        self.events = deque(maxlen=max_events)
        self._lock = threading.Lock()

    def clear(self) -> None:
        with self._lock:
            self.events.clear()

    def record_request(self, request) -> None:
        try:
            item = {
                "timestamp": time.time(),
                "kind": "request",
                "method": request.method,
                "url": _safe_url(request.url),
                "resource_type": request.resource_type,
                "headers": _safe_headers(request.headers),
            }
            with self._lock:
                self.events.append(item)
        except Exception:
            pass

    def record_response(self, response) -> None:
        try:
            item = {
                "timestamp": time.time(),
                "kind": "response",
                "status": response.status,
                "url": _safe_url(response.url),
                "resource_type": response.request.resource_type,
                "headers": _safe_headers(response.headers),
            }
            with self._lock:
                self.events.append(item)
        except Exception:
            pass

    def snapshot(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.events)[-max(1, min(limit, MAX_NETWORK_EVENTS)) :]


class NaukriInspector:
    """Attach to the existing Naukri Chrome and read only."""

    def __init__(self):
        if sync_playwright is None:
            raise RuntimeError(
                "Playwright is unavailable. Install dependencies with "
                "pip install -r requirements.txt."
            ) from _PLAYWRIGHT_IMPORT_ERROR

        self._pw = None
        self.browser = None
        self.context = None
        self.page = None
        self.recorder = NetworkRecorder()
        self._listeners_bound = False

    def connect(self) -> None:
        if not _cdp_is_available():
            raise RuntimeError(
                f"Dedicated Naukri Chrome is not running at {AGENT_CDP_ENDPOINT}. "
                "Run 'python3 -m src.cli naukri setup-browser' first."
            )

        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.connect_over_cdp(
            AGENT_CDP_ENDPOINT,
            no_defaults=True,
            is_local=True,
            slow_mo=0,
        )
        if not self.browser.contexts:
            raise RuntimeError("CDP is reachable but no browser context is available.")

        self.context = self.browser.contexts[0]
        self._bind_network_listeners()

        pages = list(self.context.pages)
        naukri_pages = []
        for page in pages:
            try:
                if "naukri.com" in (page.url or "").lower():
                    naukri_pages.append(page)
            except Exception:
                pass

        self.page = naukri_pages[-1] if naukri_pages else (pages[-1] if pages else None)
        if self.page is None:
            raise RuntimeError("No browser tab is available for inspection.")

    def close(self) -> None:
        # Detach only. Never close or mutate the user's browser.
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
        self._pw = None
        self.browser = None
        self.context = None
        self.page = None

    def _bind_network_listeners(self) -> None:
        if self._listeners_bound or not self.context:
            return
        for page in list(self.context.pages):
            self._bind_page(page)
        try:
            self.context.on("page", self._bind_page)
        except Exception:
            pass
        self._listeners_bound = True

    def _bind_page(self, page) -> None:
        try:
            page.on("request", self.recorder.record_request)
            page.on("response", self.recorder.record_response)
        except Exception:
            pass

    def _require_page(self):
        if self.page is None:
            self.connect()
        if self.page is None:
            raise RuntimeError("No page available.")

        # Refresh the selected page in case the user opened a new Naukri tab
        # while the inspector was already running.
        try:
            current = [p for p in self.context.pages if "naukri.com" in (p.url or "").lower()]
            if current:
                self.page = current[-1]
        except Exception:
            pass
        return self.page

    def page_info(self) -> dict[str, Any]:
        page = self._require_page()
        return {
            "url": _safe_url(page.url),
            "title": _redact_text(page.title(), 500),
            "ready_state": page.evaluate("document.readyState"),
            "viewport": page.viewport_size,
            "timestamp": time.time(),
        }

    def visible_text(self, max_chars: int = MAX_BODY_TEXT) -> dict[str, Any]:
        page = self._require_page()
        text = page.locator("body").inner_text(timeout=7000)
        return {
            **self.page_info(),
            "text": _redact_text(text, max_chars),
        }

    def dom(self, max_chars: int = MAX_DOM_TEXT) -> dict[str, Any]:
        page = self._require_page()
        html = page.locator("body").evaluate("(e) => e.outerHTML")
        return {
            **self.page_info(),
            "dom": _redact_text(html, max_chars),
            "truncated": len(html) > max_chars,
        }

    def elements(self, limit: int = 250) -> dict[str, Any]:
        page = self._require_page()
        script = """
        (limit) => {
          const selectors = 'button,a,input,textarea,select,[role="button"],[role="option"],[role="combobox"],[contenteditable="true"]';
          return Array.from(document.querySelectorAll(selectors))
            .filter(e => {
              const r = e.getBoundingClientRect();
              const s = getComputedStyle(e);
              return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
            })
            .slice(0, limit)
            .map(e => ({
              tag: e.tagName.toLowerCase(),
              role: e.getAttribute('role'),
              aria: e.getAttribute('aria-label'),
              name: e.getAttribute('name'),
              placeholder: e.getAttribute('placeholder'),
              type: e.getAttribute('type'),
              text: (e.innerText || e.value || '').trim().slice(0, 180),
              href: e.tagName === 'A' ? e.getAttribute('href') : null,
              disabled: !!e.disabled,
              checked: !!e.checked,
              selector_ids: {
                id: e.id || null,
                data_testid: e.getAttribute('data-testid'),
                data_qa: e.getAttribute('data-qa'),
              }
            }));
        }
        """
        raw = page.evaluate(script, max(1, min(limit, 500)))
        out = []
        for item in raw:
            cleaned = dict(item)
            for key in ("aria", "name", "placeholder", "text"):
                cleaned[key] = _redact_text(cleaned.get(key) or "", 300)
            if cleaned.get("href"):
                cleaned["href"] = _safe_url(cleaned["href"])
            ids = cleaned.get("selector_ids") or {}
            cleaned["selector_ids"] = {
                k: _redact_text(v or "", 200) for k, v in ids.items()
            }
            cleaned["playwright_hint"] = _playwright_locator_hint(cleaned)
            out.append(cleaned)
        return {**self.page_info(), "count": len(out), "elements": out}

    def forms(self) -> dict[str, Any]:
        page = self._require_page()
        forms = page.evaluate(
            """
            () => Array.from(document.querySelectorAll('form')).map((form, index) => ({
              index,
              action: form.getAttribute('action'),
              method: form.getAttribute('method'),
              controls: Array.from(form.querySelectorAll('input,textarea,select,button')).map(e => ({
                tag: e.tagName.toLowerCase(),
                type: e.getAttribute('type'),
                name: e.getAttribute('name'),
                placeholder: e.getAttribute('placeholder'),
                aria: e.getAttribute('aria-label'),
                required: !!e.required,
                disabled: !!e.disabled
              }))
            }))
            """
        )
        for form in forms:
            form["action"] = _safe_url(form.get("action") or "")
            for control in form.get("controls", []):
                for key in ("name", "placeholder", "aria"):
                    control[key] = _redact_text(control.get(key) or "", 300)
        return {**self.page_info(), "forms": forms}

    def links(self, limit: int = 300) -> dict[str, Any]:
        page = self._require_page()
        links = page.evaluate(
            """
            (limit) => Array.from(document.querySelectorAll('a[href]'))
              .slice(0, limit)
              .map(a => ({
                text: (a.innerText || a.textContent || '').trim().slice(0, 200),
                href: a.href,
                aria: a.getAttribute('aria-label'),
                rel: a.getAttribute('rel')
              }))
            """,
            max(1, min(limit, 1000)),
        )
        out = []
        for link in links:
            out.append({
                "text": _redact_text(link.get("text") or "", 300),
                "href": _safe_url(link.get("href") or ""),
                "aria": _redact_text(link.get("aria") or "", 300),
                "rel": link.get("rel") or "",
            })
        return {**self.page_info(), "count": len(out), "links": out}

    def scripts(self) -> dict[str, Any]:
        page = self._require_page()
        scripts = page.evaluate(
            "() => Array.from(document.scripts).map(s => s.src).filter(Boolean)"
        )
        clean = sorted({_safe_url(x) for x in scripts})[:250]
        return {**self.page_info(), "scripts": clean}

    def screenshot(self) -> bytes:
        page = self._require_page()
        return page.screenshot(type="png", full_page=True)

    def snapshot(self, network_limit: int = 150) -> dict[str, Any]:
        return {
            "page": self.page_info(),
            "visible_text": self.visible_text()["text"],
            "elements": self.elements(250)["elements"],
            "forms": self.forms()["forms"],
            "job_links": [
                item
                for item in self.links(500)["links"]
                if "/job-listings-" in item["href"].lower()
                or "/job-listings/" in item["href"].lower()
            ][:150],
            "scripts": self.scripts()["scripts"],
            "network": self.recorder.snapshot(network_limit),
            "read_only": True,
            "generated_at": time.time(),
        }


class AnalyzeRequest(BaseModel):
    question: str = (
        "Understand the current Naukri page and its browser/network architecture. "
        "Identify robust Playwright selectors and the safest next implementation "
        "changes for the job-application agent."
    )


def build_app() -> FastAPI:
    app = FastAPI(
        title="Naukri Read-Only Inspector",
        version="1.0.0",
        description="Local read-only introspection bridge for the dedicated Naukri browser.",
    )
    inspector = NaukriInspector()
    app.state.inspector = inspector

    def authorize(token: str | None) -> None:
        expected = os.getenv("NAUKRI_INSPECTOR_TOKEN")
        if expected and token != expected:
            raise HTTPException(status_code=401, detail="Invalid inspector token.")

    def ensure_connected() -> NaukriInspector:
        try:
            if inspector.page is None:
                inspector.connect()
            return inspector
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/health")
    def health(x_naukri_inspector_token: str | None = Header(default=None)):
        authorize(x_naukri_inspector_token)
        return {
            "ok": True,
            "read_only": True,
            "cdp": AGENT_CDP_ENDPOINT,
            "connected": inspector.page is not None,
        }

    @app.get("/page")
    def page_info(x_naukri_inspector_token: str | None = Header(default=None)):
        authorize(x_naukri_inspector_token)
        return ensure_connected().page_info()

    @app.get("/text")
    def page_text(
        max_chars: int = Query(MAX_BODY_TEXT, ge=1000, le=100000),
        x_naukri_inspector_token: str | None = Header(default=None),
    ):
        authorize(x_naukri_inspector_token)
        return ensure_connected().visible_text(max_chars)

    @app.get("/dom")
    def page_dom(
        max_chars: int = Query(MAX_DOM_TEXT, ge=2000, le=200000),
        x_naukri_inspector_token: str | None = Header(default=None),
    ):
        authorize(x_naukri_inspector_token)
        return ensure_connected().dom(max_chars)

    @app.get("/elements")
    def page_elements(
        limit: int = Query(250, ge=1, le=500),
        x_naukri_inspector_token: str | None = Header(default=None),
    ):
        authorize(x_naukri_inspector_token)
        return ensure_connected().elements(limit)

    @app.get("/forms")
    def page_forms(x_naukri_inspector_token: str | None = Header(default=None)):
        authorize(x_naukri_inspector_token)
        return ensure_connected().forms()

    @app.get("/links")
    def page_links(
        limit: int = Query(300, ge=1, le=1000),
        x_naukri_inspector_token: str | None = Header(default=None),
    ):
        authorize(x_naukri_inspector_token)
        return ensure_connected().links(limit)

    @app.get("/scripts")
    def page_scripts(x_naukri_inspector_token: str | None = Header(default=None)):
        authorize(x_naukri_inspector_token)
        return ensure_connected().scripts()

    @app.get("/network")
    def network(
        limit: int = Query(150, ge=1, le=500),
        clear: bool = False,
        x_naukri_inspector_token: str | None = Header(default=None),
    ):
        authorize(x_naukri_inspector_token)
        obj = ensure_connected()
        payload = {"events": obj.recorder.snapshot(limit)}
        if clear:
            obj.recorder.clear()
        return payload

    @app.post("/network/clear")
    def clear_network(x_naukri_inspector_token: str | None = Header(default=None)):
        authorize(x_naukri_inspector_token)
        ensure_connected().recorder.clear()
        return {"cleared": True}

    @app.get("/snapshot")
    def snapshot(
        network_limit: int = Query(150, ge=1, le=500),
        x_naukri_inspector_token: str | None = Header(default=None),
    ):
        authorize(x_naukri_inspector_token)
        return ensure_connected().snapshot(network_limit)

    @app.get("/screenshot")
    def screenshot(x_naukri_inspector_token: str | None = Header(default=None)):
        authorize(x_naukri_inspector_token)
        image = ensure_connected().screenshot()
        return Response(content=image, media_type="image/png")

    @app.post("/analyze")
    def analyze(
        request: AnalyzeRequest,
        x_naukri_inspector_token: str | None = Header(default=None),
    ):
        authorize(x_naukri_inspector_token)
        obj = ensure_connected()
        snapshot = obj.snapshot(network_limit=120)
        try:
            from openai import OpenAI
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail="OpenAI SDK is not installed. Run pip install -r requirements.txt.",
            ) from exc

        if not os.getenv("OPENAI_API_KEY"):
            raise HTTPException(
                status_code=500,
                detail="OPENAI_API_KEY is not set in the local environment.",
            )

        payload = json.dumps(snapshot, ensure_ascii=False)
        max_chars = int(os.getenv("OPENAI_INSPECTOR_INPUT_CHARS", "95000"))
        payload = payload[:max_chars]

        prompt = (
            "You are the architecture/reliability engineer for a personal Naukri "
            "browser automation project. You are receiving READ-ONLY telemetry from "
            "the user's own authenticated browser. Do not propose bypassing CAPTCHA, "
            "anti-bot systems, login controls, or application eligibility checks. "
            "Use only the supplied telemetry; do not invent selectors or endpoints.\n\n"
            f"User question:\n{request.question}\n\n"
            "Live telemetry (already sanitized):\n"
            f"{payload}\n\n"
            "Return concise sections: current_page_model, stable_selectors, "
            "network_observations, recommended_playwright_changes, reliability_risks, "
            "next_manual_observation. Make selectors concrete and explain why they "
            "are more stable than CSS classes. Distinguish observed facts from "
            "inference."
        )

        try:
            client = OpenAI()
            response = client.responses.create(
                model=OPENAI_MODEL,
                input=[{"role": "user", "content": prompt}],
            )
            return {
                "model": OPENAI_MODEL,
                "analysis": response.output_text,
                "telemetry_chars": len(payload),
                "read_only": True,
            }
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"OpenAI analysis failed: {exc}") from exc

    return app


app = build_app()


def run_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run_server()
