from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

# The candidate's verified experience is 2+ years and the strongest technical
# signals in the stored profile are endpoint security, vulnerability management,
# IAM/Okta, cloud/email security and security operations.
PROFILE_SKILL_TERMS = [
    "microsoft defender",
    "defender",
    "carbon black",
    "threatlocker",
    "edr",
    "endpoint security",
    "qualys",
    "vulnerability management",
    "vulnerability assessment",
    "vulnerability remediation",
    "cmdb",
    "okta",
    "iam",
    "identity",
    "sso",
    "mfa",
    "jml",
    "access management",
    "netskope",
    "cloudflare",
    "mimecast",
    "phishing",
    "email security",
    "incident response",
    "incident investigation",
    "security operations",
    "servicenow",
]

STRONG_TITLE_PATTERNS = [
    "cybersecurity engineer",
    "security engineer",
    "security operations engineer",
    "endpoint security engineer",
    "vulnerability engineer",
    "vulnerability management engineer",
    "iam engineer",
    "identity engineer",
]

NEGATIVE_PATTERNS = {
    "grc": 25,
    "governance risk compliance": 25,
    "it auditor": 25,
    "audit": 18,
    "risk & compliance": 25,
    "risk and compliance": 25,
    "technical support": 20,
    "iam support": 20,
    "soc l1": 25,
    "soc level 1": 25,
    "manual tester": 12,
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


def _has_any(blob: str, terms: list[str]) -> bool:
    return any(term in blob for term in terms)


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

    # Start conservatively. A title match alone must not produce a high score.
    score = 35
    matched: List[str] = []
    penalties: List[str] = []
    reasons: List[str] = []

    title_hits = [term for term in STRONG_TITLE_PATTERNS if term in title_n]
    if title_hits:
        score += 22
        matched.extend(title_hits)
        reasons.append("Role title aligns with the candidate's technical security target.")

    # Distinct, verified profile-skill overlaps are the main evidence of fit.
    skill_hits = []
    for term in PROFILE_SKILL_TERMS:
        if term in blob and term not in skill_hits:
            skill_hits.append(term)
    if skill_hits:
        score += min(30, 5 * len(skill_hits))
        matched.extend(skill_hits[:12])
        reasons.append(f"Job overlaps with {min(len(skill_hits), 6)} verified profile skill areas.")

    # Penalize strongly specialized domains where the stored profile does not
    # provide evidence of the required background.
    if _has_any(blob, OFFENSIVE_SECURITY_TERMS):
        score -= 28
        penalties.append("offensive_security")
        reasons.append("Role is strongly penetration-testing/red-team oriented rather than the candidate's primary security-operations profile.")

    if _has_any(blob, AUTOMOTIVE_EMBEDDED_TERMS):
        score -= 28
        penalties.append("automotive_embedded")
        reasons.append("Role is strongly automotive/embedded-security specialized and the profile does not verify that background.")

    for term, penalty in NEGATIVE_PATTERNS.items():
        if term in blob:
            score -= penalty
            penalties.append(term)

    min_exp = _experience_minimum(blob)
    if min_exp is not None and min_exp > 2.5:
        score -= 30
        penalties.append("experience_above_profile")
        reasons.append(f"Job's stated minimum experience is about {min_exp:g} years, above the verified 2+ year profile.")

    # General security wording without concrete profile-skill overlap should not
    # by itself qualify as a high-priority job.
    if title_hits and not skill_hits:
        score = min(score, 64)
        reasons.append("Title is relevant, but the JD does not show enough overlap with verified tools/domains.")

    score = max(0, min(100, score))
    if score >= 85:
        recommendation = "HIGH_PRIORITY"
    elif score >= 70:
        recommendation = "REVIEW"
    else:
        recommendation = "SKIP"

    if not matched:
        reasons.append("Few verified profile matches were found in the title/JD.")

    return ScoreResult(score, recommendation, matched, penalties, reasons)
