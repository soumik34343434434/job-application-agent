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
        "incident response",
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
        "cyber security engineer",
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
        "vulnerability management",
        "vulnerability management analyst",
        "vulnerability management specialist",
        "vulnerability management engineer",
        "vulnerability engineer",
        "vulnerability analyst",
        "vulnerability assessment analyst",
        "vulnerability remediation analyst",
        "security vulnerability analyst",
        "vulnerability assessment",
    ],
    "iam_identity": [
        "iam engineer",
        "iam analyst",
        "iam security analyst",
        "identity and access management analyst",
        "identity engineer",
        "identity security analyst",
        "access management analyst",
        "identity and access management",
        "okta engineer",
        "okta administrator",
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
        "cyber security analyst",
        "security analyst",
        "security operations analyst",
        "soc analyst 2",
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
    "customer support": 18,
    "support engineer": 18,
    "fresher": 25,
    "intern": 25,
    "trainee": 20,
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


SPECIALIZED_TITLE_PENALTIES = {
    "l0_l1": (18, [r"\bl0\s*/\s*l1\b", r"\bl0\b", r"\bl1\b"]),
    "service_desk": (18, ["service desk"]),
    "devops": (15, ["devops engineer", "devops"]),
    "site_reliability": (15, ["site reliability engineer", "sre"]),
    "product_engineering": (15, ["product engineer"]),
    "application_security": (12, ["application security", "app security"]),
    "application_admin": (8, ["application admin"]),
    "threat_research": (12, ["threat researcher", "threat research"]),
}

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

    # Single-value experience requirements such as "2 years experience" or
    # "1 year required" are common on detail pages and cards.
    single = re.search(
        r"(?:(?:minimum|at least|required|experience|exp(?:erience)?)\s*(?:of|:)?\s*)"
        r"(\d+(?:\.\d+)?)\s*(?:yrs?|years?)|"
        r"(\d+(?:\.\d+)?)\s*(?:yrs?|years?)\s+(?:of\s+)?experience",
        blob,
    )
    if single:
        value = single.group(1) or single.group(2)
        return float(value)

    return None


def _narrow_role_penalties(title_n: str, blob: str, verified_groups: list[str], role_hits: list[str]):
    """Return modest penalties for tool-only/specialized roles the profile de-prioritizes."""
    penalties = []
    reasons = []
    # Only penalize when the JD is dominated by the narrow tool/domain and lacks
    # broader security evidence; genuine mixed security roles remain strong.
    broad_groups = set(verified_groups) - {"iam_identity", "vulnerability_management"}
    if _has_any(blob, ["okta"]) and role_hits == ["iam_identity"] and not broad_groups:
        penalties.append(("okta_only", 18))
        reasons.append("Role is dominated by Okta/IAM rather than broader security operations.")
    elif _has_any(blob, ["iam", "identity and access management"]) and role_hits == ["iam_identity"] and not broad_groups:
        penalties.append(("iam_only", 12))
        reasons.append("Role is narrowly focused on IAM/identity administration.")
    if _has_any(blob, ["qualys"]) and role_hits == ["vulnerability_management"] and len(verified_groups) <= 1:
        penalties.append(("qualys_only", 10))
        reasons.append("Role is narrowly centered on Qualys rather than broader security work.")
    return penalties, reasons


def _experience_ceiling(blob: str) -> float | None:
    """Extract the highest required experience for explicit ranges/single values."""
    matches = re.findall(
        r"(\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)\s*(?:yrs?|years?)",
        blob,
    )
    if matches:
        return max(float(b) for _, b in matches)

    plus = re.search(r"(\d+(?:\.\d+)?)\s*\+\s*(?:yrs?|years?)", blob)
    if plus:
        # A plus requirement has no finite ceiling, so use the minimum as its
        # effective requirement for the candidate's lower-bound check.
        return float(plus.group(1))

    single = re.search(
        r"(?:(?:minimum|at least|required|experience|exp(?:erience)?)\s*(?:of|:)?\s*)"
        r"(\d+(?:\.\d+)?)\s*(?:yrs?|years?)|"
        r"(\d+(?:\.\d+)?)\s*(?:yrs?|years?)\s+(?:of\s+)?experience",
        blob,
    )
    if single:
        value = single.group(1) or single.group(2)
        return float(value)

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
        # A single exact target-role-family match is strong evidence because the
        # profile is already constrained to security-focused roles.
        score += min(25, 20 + 5 * len(role_hits))
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
        # Two or more verified skill families plus a target role should cross the
        # review/submit boundary only when the evidence is substantial.
        score += min(40, 15 * len(verified_groups))
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

    narrow_penalties, narrow_reasons = _narrow_role_penalties(title_n, blob, verified_groups, role_hits)
    for label, penalty in narrow_penalties:
        score -= penalty
        penalties.append(label)
    reasons.extend(narrow_reasons)

    offensive_hits = [term for term in OFFENSIVE_SECURITY_TERMS if _term_in_blob(term, blob)]
    if offensive_hits:
        score -= 30
        penalties.append("offensive_security")
        reasons.append(
            "Role is strongly penetration-testing/red-team oriented rather than the current security-operations profile."
        )
        # Multiple offensive indicators, or an explicitly offensive title, mean
        # the job is not merely adjacent to the candidate's profile. Do not let
        # unrelated security-tool overlap inflate it back into the application queue.
        title_offensive = _has_any(title_n, OFFENSIVE_SECURITY_TERMS)
        if len(offensive_hits) >= 2 or title_offensive:
            score = min(score, 64)
            penalties.append("offensive_dominant")
            reasons.append("Offensive-security requirements are substantial enough to cap the match below the review queue.")

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

    # Penalize roles whose core designation is materially outside the candidate's
    # security-operations / vulnerability / identity target. This prevents dense
    # security keyword overlap from making DevOps, SRE, Service Desk, Product
    # Engineering, AppSec, or specialist research roles look equivalent to a
    # direct security-engineering vacancy.
    for label, (penalty, terms) in SPECIALIZED_TITLE_PENALTIES.items():
        matched_title_term = any(
            re.search(term, title_n) if term.startswith(r"\b") else _term_in_blob(term, title_n)
            for term in terms
        )
        if matched_title_term:
            score -= penalty
            penalties.append(label)
            reasons.append(f"Title indicates a specialized/non-target role lane: {label.replace('_', ' ')}.")
            break

    # Generic SOC Analyst should not be treated as L2/L3 merely from the title.
    if _term_in_blob("soc analyst", title_n) and _has_any(blob, ["l1", "level 1", "tier 1", "first line"]):
        score -= 18
        penalties.append("soc_l1")

    min_exp = _experience_minimum(blob)
    max_exp = _experience_ceiling(blob)
    if max_exp is not None and max_exp < 2:
        score -= 30
        penalties.append("experience_below_profile")
        reasons.append(
            f"Job's stated experience ceiling is about {max_exp:g} years, below the verified 2+ year profile."
        )
    elif min_exp is not None:
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

    # Compensation fit is a secondary signal: the hard minimum CTC filter remains
    # in NaukriAgent, while this adds preference for the user's 8-16 LPA target band.
    salary_blob = normalize(blob)
    salary_numbers = [
        float(x.replace(",", ""))
        for x in re.findall(r"(?:₹\s*)?(\d+(?:\.\d+)?)", salary_blob)
    ]
    if salary_numbers and _has_any(salary_blob, ["lpa", "lac", "lakh"]):
        low, high = min(salary_numbers), max(salary_numbers)
        if low >= 8 and high <= 16:
            score += 6
            reasons.append("Stated compensation is inside the preferred 8-16 LPA target range.")
        elif high >= 8 and low < 8:
            score += 2
            reasons.append("Stated compensation overlaps the 8+ LPA target.")

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
