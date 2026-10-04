from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

# These are the candidate's verified tools/domains from the resume.
VERIFIED_SKILL_TERMS = {
    "endpoint_security": [
        "microsoft defender",
        "carbon black",
        "threatlocker",
        "edr",
        "endpoint security",
    ],
    "vulnerability_management": [
        "qualys",
        "qualys vm",
        "vulnerability management",
        "vulnerability assessment",
        "vulnerability remediation",
        "cmdb",
    ],
    "iam_identity": [
        "okta",
        "iam",
        "identity",
        "sso",
        "mfa",
        "jml",
        "access management",
    ],
    "cloud_security": [
        "netskope",
        "cloudflare",
        "cloud security",
    ],
    "email_security": [
        "mimecast",
        "phishing",
        "email security",
    ],
    "security_operations": [
        "security operations",
        "incident investigation",
        "security monitoring",
    ],
    "itsm_operations": [
        "servicenow",
        "sla-driven",
        "security records",
    ],
}

# Broader concepts/platform families are useful for recall, but are deliberately
# worth less than an exact verified resume match.
RELATED_DOMAIN_TERMS = {
    "endpoint_security": [
        "xdr",
        "endpoint detection",
        "endpoint response",
        "endpoint protection",
        "edr",
    ],
    "vulnerability_management": [
        "vulnerability scanning",
        "vulnerability remediation",
        "cvss",
        "asset inventory",
        "patch remediation",
    ],
    "iam_identity": [
        "identity security",
        "identity access",
        "authentication",
        "authorization",
        "privileged access",
    ],
    "cloud_security": [
        "casb",
        "secure web gateway",
        "cloud access security",
        "cloud security posture",
    ],
    "email_security": [
        "email threat",
        "phishing investigation",
        "email protection",
    ],
    "security_operations": [
        "incident response",
        "threat detection",
        "security monitoring",
        "alert investigation",
        "siem",
        "soc",
    ],
}

TARGET_ROLE_FAMILIES = {
    "security_engineering": [
        "cybersecurity engineer",
        "security engineer",
        "security operations engineer",
        "security platform engineer",
    ],
    "endpoint_security": [
        "endpoint security engineer",
        "endpoint security analyst",
        "endpoint engineer",
    ],
    "vulnerability_management": [
        "vulnerability management engineer",
        "vulnerability engineer",
        "vulnerability analyst",
        "vulnerability assessment analyst",
        "vulnerability remediation analyst",
    ],
    "iam_identity": [
        "iam engineer",
        "iam analyst",
        "identity engineer",
        "identity security analyst",
        "access management analyst",
        "okta engineer",
    ],
    "detection_response": [
        "detection engineer",
        "detection and response analyst",
        "incident response analyst",
        "security engineer - incident response",
        "security analyst - incident response",
    ],
    "cloud_security": [
        "cloud security engineer",
        "cloud security analyst",
    ],
    "email_security": [
        "email security analyst",
    ],
    "security_analyst": [
        "cybersecurity analyst",
        "security analyst",
        "security operations analyst",
    ],
}

NEGATIVE_PATTERNS = {
    "grc": 28,
    "governance risk compliance": 28,
    "it auditor": 28,
    "risk & compliance": 28,
    "risk and compliance": 28,
    "technical support": 22,
    "iam support": 24,
    "soc l1": 28,
    "soc level 1": 28,
    "manual tester": 18,
}

OFFENSIVE_SECURITY_TERMS = [
    "penetration testing",
    "penetration test",
    "web application penetration",
    "network vapt",
    "mobile application penetration",
    "red teaming",
    "red team",
    "burp suite",
    "oscp",
    "crtp",
    "crte",
    "crto",
    "offensive security",
]

AUTOMOTIVE_EMBEDDED_TERMS = [
    "automotive",
    "autosar",
    "embedded c",
    "embedded systems",
    "ecu",
    "hsm/she",
    "secure jtag",
    "can protocol",
    "iso/sae 21434",
    "un r155",
    "un r156",
    "functional safety",
]

@dataclass
class ScoreResult:
    score: int
    recommendation: str
    matched: List[str]
    penalties: List[str]
    reasons: List[str]


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def _term_in_blob(term: str, blob: str) -> bool:
    pattern = r"(?<!\w)" + re.escape(normalize(term)) + r"(?!\w)"
    return re.search(pattern, blob) is not None


def _has_any(blob: str, terms: list[str]) -> bool:
    return any(_term_in_blob(term, blob) for term in terms)


def _experience_minimum(blob: str) -> float | None:
    matches = re.findall(
        r"(\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)\s*(?:yrs?|years?)",
        blob,
    )
    if matches:
        return min(float(a) for a, _ in matches)

    plus = re.search(r"(\d+(?:\.\d+)?)\s*\+\s*(?:yrs?|years?)", blob)
    if plus:
        return float(plus.group(1))

    return None


def score_job(title: str, description: str, company: str = "") -> ScoreResult:
    title_n = normalize(title)
    blob = normalize(f"{title} {company} {description}")

    # Start from a neutral baseline. Title, role-family, verified tools, and
    # broader security concepts then build the score.
    score = 30
    matched: List[str] = []
    penalties: List[str] = []
    reasons: List[str] = []

    role_hits = []
    for family, terms in TARGET_ROLE_FAMILIES.items():
        if any(_term_in_blob(term, title_n) for term in terms):
            role_hits.append(family)

    if role_hits:
        score += min(25, 15 + 5 * (len(role_hits) - 1))
        matched.extend(role_hits)
        reasons.append("Job title belongs to a target security role family.")

    verified_groups = []
    for group, terms in VERIFIED_SKILL_TERMS.items():
        hits = [term for term in terms if _term_in_blob(term, blob)]
        if hits:
            verified_groups.append(group)
            matched.extend(hits[:4])

    # Exact verified tools/domains carry the most weight.
    if verified_groups:
        score += min(32, 7 * len(verified_groups))
        reasons.append(
            f"JD matches {min(len(verified_groups), 5)} verified security skill areas from the resume."
        )

    related_groups = []
    for group, terms in RELATED_DOMAIN_TERMS.items():
        if any(_term_in_blob(term, blob) for term in terms):
            related_groups.append(group)

    # Related-domain evidence is useful for jobs such as XDR, SIEM, or broader
    # incident-response roles, but it cannot outweigh strong exclusion signals.
    new_related = [g for g in related_groups if g not in verified_groups]
    if new_related:
        score += min(15, 5 * len(new_related))
        reasons.append("JD contains adjacent security-domain concepts transferable from the current security background.")

    if _has_any(blob, OFFENSIVE_SECURITY_TERMS):
        score -= 30
        penalties.append("offensive_security")
        reasons.append(
            "Role is strongly penetration-testing/red-team oriented rather than the current security-operations profile."
        )

    if _has_any(blob, AUTOMOTIVE_EMBEDDED_TERMS):
        score -= 30
        penalties.append("automotive_embedded")
        reasons.append(
            "Role is strongly automotive/embedded-security specialized and that background is not verified."
        )

    for term, penalty in NEGATIVE_PATTERNS.items():
        if _term_in_blob(term, blob):
            score -= penalty
            penalties.append(term)

    min_exp = _experience_minimum(blob)
    if min_exp is not None:
        if min_exp > 2.5:
            score -= 30
            penalties.append("experience_above_profile")
            reasons.append(
                f"Job's stated minimum experience is about {min_exp:g} years, above the verified 2+ year profile."
            )
        elif min_exp <= 2:
            score += 5
            reasons.append("Minimum experience is aligned with the verified 2+ year profile.")

    if not role_hits and not verified_groups and not related_groups:
        score -= 10
        reasons.append("Little evidence of alignment with the candidate's security role families or skill domains.")

    # A title-only security match should never become a HIGH_PRIORITY job.
    if role_hits and not verified_groups and not related_groups:
        score = min(score, 64)
        reasons.append("Relevant title, but insufficient supporting security-domain evidence in the JD.")

    score = max(0, min(100, score))

    if score >= 85:
        recommendation = "HIGH_PRIORITY"
    elif score >= 70:
        recommendation = "REVIEW"
    else:
        recommendation = "SKIP"

    return ScoreResult(score, recommendation, matched, penalties, reasons)
