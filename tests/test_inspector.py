from src.naukri_inspector import _redact_text, _safe_url, _safe_headers, _playwright_locator_hint


def test_redaction_removes_sensitive_values():
    value = "email=user@example.com phone=9876543210 Authorization: Bearer abcdefghijklmnop"
    out = _redact_text(value)
    assert "user@example.com" not in out
    assert "9876543210" not in out
    assert "Bearer abcdefghijklmnop" not in out


def test_safe_url_redacts_secret_query_values():
    out = _safe_url("https://www.naukri.com/path?token=secret123&q=security")
    assert "token=%5BREDACTED%5D" in out
    assert "security" in out
    assert "secret123" not in out


def test_safe_headers_redacts_auth_cookie():
    out = _safe_headers({
        "authorization": "Bearer secret",
        "cookie": "session=secret",
        "content-type": "application/json",
    })
    assert out["authorization"] == "[REDACTED]"
    assert out["cookie"] == "[REDACTED]"
    assert out["content-type"] == "application/json"


def test_playwright_hint_prefers_accessible_role():
    hint = _playwright_locator_hint({
        "tag": "button",
        "role": "button",
        "aria": "Search",
        "text": "Search",
    })
    assert "get_by_role" in hint
