import json
from pathlib import Path

from src.naukri import FoundJob, NaukriAgent
from src.scorer import score_job


BACKTESTS = [
    ("strong_vm", "Vulnerability Management Engineer", "Qualys VM, CVE triage, vulnerability assessment, remediation, security operations", 70),
    ("strong_secops", "Cybersecurity Analyst", "Microsoft Defender, Carbon Black, incident response, phishing investigations, SIEM", 85),
    ("strong_ir", "Incident Response Analyst", "incident response, threat detection, alert investigation, SIEM", 70),
    ("strong_endpoint", "Endpoint Security Engineer", "Microsoft Defender, Carbon Black, EDR, endpoint detection and response", 70),
    ("strong_cloud", "Cloud Security Engineer", "Netskope, CASB, Cloudflare, cloud security posture", 70),
    ("strong_email", "Email Security Analyst", "Mimecast, phishing investigation, email protection", 70),
    ("mixed_security", "Security Engineer", "Okta, Qualys, Microsoft Defender, Cloudflare, incident response", 90),
    ("weak_sales", "Security Sales Engineer", "selling cybersecurity products, account management, sales targets", 0),
    ("grc", "GRC Analyst", "risk and compliance, IT audit, controls testing, governance", 0),
    ("auditor", "IT Auditor", "IT audit, SOX, controls, compliance", 0),
    ("support", "IAM Support Engineer", "Okta support, password resets, user tickets", 0),
    ("offensive", "Security Engineer", "penetration testing, Burp Suite, OSCP, red team", 0),
    ("automotive", "Cybersecurity Engineer - Automotive", "AUTOSAR, ECU, CAN, ISO/SAE 21434", 0),
    ("generic_security", "Security Analyst", "responsible for security activities", 0),
    ("over_experience", "Cybersecurity Analyst", "Microsoft Defender, incident response, SIEM, 5-8 years", 0),
    ("narrow_okta", "Okta Administrator", "Okta user lifecycle, SSO, MFA", 0),
    ("narrow_qualys", "Qualys Administrator", "Qualys scanning and reporting", 0),
    ("network", "Network Security Engineer", "firewalls, routing, VPN, Palo Alto", 0),
    ("appsec", "Application Security Engineer", "SAST, DAST, code review, secure SDLC", 0),
    ("research", "Security Researcher", "malware research, exploit development, reverse engineering", 0),
]


def test_security_scorer_backtest():
    for name, title, description, expected_min in BACKTESTS:
        result = score_job(title, description, "Example Co")
        assert result.score >= expected_min, (name, result.score, result.reasons)


def test_narrow_target_roles_are_deprioritized():
    assert score_job("Okta Administrator", "Okta user lifecycle, SSO, MFA", "Example Co").score < 70
    assert score_job("Qualys Administrator", "Qualys scanning and reporting", "Example Co").score < 70


def test_preferred_location_aliases_are_or_matched():
    profile = json.loads((Path(__file__).parents[1] / "data" / "profile.json").read_text())
    agent = NaukriAgent.__new__(NaukriAgent)
    for location in ["Delhi / NCR", "Secunderabad", "Hyderabad", "Bengaluru", "Gurugram", "Remote US EST Shift"]:
        job = FoundJob("Security Analyst", "Example Co", location, "Not Disclosed", "2-5 Yrs", "https://www.naukri.com/job-listings-test", "incident response")
        assert agent._target_preference_ok(job, profile), location


def test_non_preferred_location_is_rejected():
    profile = json.loads((Path(__file__).parents[1] / "data" / "profile.json").read_text())
    agent = NaukriAgent.__new__(NaukriAgent)
    job = FoundJob("Security Analyst", "Example Co", "Chennai", "Not Disclosed", "2-5 Yrs", "https://www.naukri.com/job-listings-test", "incident response")
    assert not agent._target_preference_ok(job, profile)


def test_role_searches_are_union_not_intersection():
    agent = NaukriAgent.__new__(NaukriAgent)
    calls = []
    accepted = []
    agent.page = None
    agent.ensure_logged_in = lambda interactive=False: None
    agent._fill_search_shell = lambda: True
    agent._collect_autocomplete_mappings = lambda values, lane, ensure_shell=False: [
        {"requested": v, "selected": v, "suggestions": [v], "status": "SELECTED"} for v in values
    ]

    def fake_search(query, location="", interactive=False, validate_role=True, ensure_login=True):
        calls.append((query, location, validate_role))
        return [
            FoundJob("Security Analyst", "Example Co", "Kolkata", "Not Disclosed", "2-5 Yrs", f"https://www.naukri.com/job-listings-{query.replace(' ', '-')}", "incident response")
        ]

    agent.search = fake_search
    agent.ingest = lambda jobs: accepted.extend(jobs) or jobs
    result = agent.scan_default(max_queries=3, per_query=1)

    assert len(calls) == 3
    assert all(location == "" for _, location, _ in calls)
    assert len(result) == 3
    assert len(accepted) == 3


def test_salary_is_scored_only_from_dedicated_field():
    no_salary = score_job("Cybersecurity Analyst", "2-4 years experience; SIEM, EDR, incident response", "Example Co")
    in_band = score_job("Cybersecurity Analyst", "2-4 years experience; SIEM, EDR, incident response", "Example Co", "₹8-12 LPA")
    below_band_text_only = score_job("Cybersecurity Analyst", "2-4 years experience; SIEM, EDR, incident response; 6 LPA", "Example Co")
    assert in_band.score == no_salary.score + 6
    assert below_band_text_only.score == no_salary.score
