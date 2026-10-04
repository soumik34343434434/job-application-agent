import re
from dataclasses import dataclass
from typing import List

POSITIVE_TERMS = {
    "security engineer": 12,
    "cybersecurity engineer": 14,
    "cybersecurity analyst": 12,
    "security analyst": 10,
    "security operations": 12,
    "security operations analyst": 12,
    "security operations engineer": 13,
    "endpoint security": 12,
    "endpoint security engineer": 13,
    "cloud security": 10,
    "cloud security engineer": 11,
    "detection and response": 10,
    "incident response": 8,
    "vulnerability management": 12,
    "vulnerability analyst": 12,
    "vulnerability engineer": 12,
    "vulnerability management engineer": 14,
    "vulnerability assessment": 9,
    "vulnerability remediation": 9,
    "iam analyst": 11,
    "iam engineer": 12,
    "identity engineer": 12,
    "identity security": 11,
    "identity and access management": 8,
    "okta engineer": 11,
    "okta administrator": 7,
    "access management": 8,
    "microsoft defender": 8,
    "carbon black": 6,
    "threatlocker": 5,
    "mimecast": 5,
    "netskope": 6,
    "cloudflare": 8,
    "qualys": 5,
    "servicenow": 2,
    "phishing": 5,
    "edr": 6,
}

NEGATIVE_TERMS = {
    "okta-only": 35,
    "okta only": 35,
    "iam-only": 30,
    "iam only": 30,
    "identity and access management only": 30,
    "vulnerability management only": 28,
    "qualys-only": 25,
    "qualys only": 25,
    "it auditor": 22,
    "grc analyst": 22,
    "risk & compliance analyst": 22,
    "risk and compliance analyst": 22,
    "technology risk analyst": 20,
    "generic it support": 25,
    "technical support": 18,
    "soc l1": 20,
    "soc level 1": 20,
    "iam support engineer": 22,
    "manual tester": 15,
}

@dataclass
class ScoreResult:
    score: int
    recommendation: str
    matched: List[str]
    penalties: List[str]
    reasons: List[str]


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def score_job(title: str, description: str, company: str = "") -> ScoreResult:
    title_n = normalize(title)
    blob = normalize(f"{title} {company} {description}")
    score = 50
    matched: List[str] = []
    penalties: List[str] = []
    reasons: List[str] = []

    # Title gets extra weight because it is the strongest signal for role family.
    for term, weight in POSITIVE_TERMS.items():
        if term in blob:
            bonus = weight + (5 if term in title_n else 0)
            score += bonus
            matched.append(term)

    for term, penalty in NEGATIVE_TERMS.items():
        if term in blob:
            score -= penalty
            penalties.append(term)

    if any(x in title_n for x in ["security engineer", "cybersecurity engineer", "security operations engineer"]):
        score += 5
        reasons.append("Role title aligns with a broader security-engineering direction.")

    if any(x in blob for x in ["endpoint", "edr", "email security", "phishing", "cloud security", "incident response"]):
        score += 5
        reasons.append("Job contains security domains already represented in the candidate profile.")

    if any(x in title_n for x in ["vulnerability", "iam", "identity", "okta"]):
        reasons.append("Role title directly matches one of the current Tier 1 job families.")

    if any(x in title_n for x in ["grc", "auditor", "risk", "compliance"]):
        score -= 8
        reasons.append("Role appears more governance/risk/audit oriented than the current technical target.")

    score = max(0, min(100, score))
    if score >= 85:
        recommendation = "HIGH_PRIORITY"
    elif score >= 70:
        recommendation = "REVIEW"
    else:
        recommendation = "SKIP"

    if penalties:
        reasons.append("Contains one or more excluded or overly specialized role patterns.")
    if not matched:
        reasons.append("Few direct role or skill matches found; manual review recommended before applying.")

    return ScoreResult(score, recommendation, matched, penalties, reasons)
