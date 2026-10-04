import hashlib
import json
import re
from dataclasses import dataclass, asdict
from typing import Dict, List

from .scorer import normalize

@dataclass
class ApplicationPrep:
    resume_variant: str
    emphasis: List[str]
    matched_keywords: List[str]
    missing_keywords: List[str]
    suggested_summary: str
    answers: Dict[str, str]
    risk_flags: List[str]

KEYWORD_GROUPS = {
    "endpoint_security": ["microsoft defender", "defender", "carbon black", "threatlocker", "edr", "endpoint security"],
    "vulnerability_management": ["qualys", "vulnerability management", "vulnerability assessment", "remediation", "cvss"],
    "iam_identity": ["okta", "iam", "identity", "sso", "mfa", "jml", "access management"],
    "cloud_security": ["cloud security", "cloudflare", "netskope", "casb", "secure web gateway"],
    "email_security": ["mimecast", "phishing", "email security", "email threat"],
    "incident_response": ["incident response", "incident investigation", "threat investigation", "detection and response", "siem", "soc"],
    "security_engineering": ["security engineering", "security engineer", "cybersecurity engineer", "security operations"],
    "automation": ["automation", "python", "api", "orchestration"],
}

PROFILE_KEYWORDS = {
    "endpoint_security": {"Microsoft Defender", "Carbon Black", "ThreatLocker", "EDR", "endpoint security"},
    "vulnerability_management": {"Qualys VM", "vulnerability management", "CMDB validation", "agent troubleshooting"},
    "iam_identity": {"Okta", "JML", "SSO", "MFA", "identity", "access management"},
    "cloud_security": {"Netskope", "Cloudflare", "cloud security"},
    "email_security": {"Mimecast", "phishing investigations", "phishing reporting"},
    "incident_response": {"incident investigation", "security operations"},
    "security_engineering": {"security engineering", "security operations", "endpoint security"},
    "automation": {"ServiceNow", "Qualys VM"},
}

ROLE_VARIANTS = [
    ("vulnerability", "vulnerability_focus"),
    ("qualys", "vulnerability_focus"),
    ("iam", "identity_focus"),
    ("identity", "identity_focus"),
    ("okta", "identity_focus"),
    ("endpoint", "security_operations_focus"),
    ("edr", "security_operations_focus"),
    ("incident response", "security_operations_focus"),
    ("detection", "security_operations_focus"),
    ("cloud security", "cloud_security_focus"),
    ("cloudflare", "cloud_security_focus"),
    ("email security", "email_security_focus"),
    ("mimecast", "email_security_focus"),
]


def _detect_groups(title: str, description: str) -> List[str]:
    blob = normalize(f"{title} {description}")
    scores = []
    for group, terms in KEYWORD_GROUPS.items():
        hits = sum(1 for term in terms if term in blob)
        if hits:
            scores.append((hits, group))
    scores.sort(reverse=True)
    return [g for _, g in scores]


def _select_variant(title: str, description: str) -> str:
    blob = normalize(f"{title} {description}")
    for term, variant in ROLE_VARIANTS:
        if term in blob:
            return variant
    return "general_security_focus"


def _keywords_for_groups(groups: List[str], description: str):
    blob = normalize(description)
    matched = []
    missing = []
    for group in groups:
        for keyword in PROFILE_KEYWORDS.get(group, set()):
            if keyword.lower() in blob:
                matched.append(keyword)
            else:
                missing.append(keyword)
    # Preserve order while deduplicating.
    return list(dict.fromkeys(matched)), list(dict.fromkeys(missing))


def _summary_for(variant: str, title: str, matched: List[str]) -> str:
    title_clean = title.strip() or "the role"
    skills = ", ".join(matched[:5]) if matched else "security operations"
    return (
        f"Cybersecurity Analyst with 2+ years of hands-on experience at Cognizant across "
        f"security operations, vulnerability management and IAM. For {title_clean}, emphasize "
        f"{skills}, supported by practical experience in enterprise security operations and incident/request workflows."
    )


def prepare_application(title: str, description: str, profile: Dict, company: str = "") -> ApplicationPrep:
    groups = _detect_groups(title, description)
    variant = _select_variant(title, description)
    matched, missing = _keywords_for_groups(groups[:3], description)
    targeting = profile.get("targeting", {})
    answers = {
        "notice_period": f"60 days",
        "current_ctc": "₹4.5 LPA",
        "expected_ctc": "₹8–16 LPA, depending on role scope and total compensation",
        "relocation": "Yes — willing to relocate to the preferred cities listed in my profile.",
        "years_of_experience": "2+ years",
        "work_authorization": "ASK_USER_BEFORE_SUBMITTING",
        "visa_sponsorship": "ASK_USER_BEFORE_SUBMITTING",
    }
    risk_flags = []
    blob = normalize(f"{title} {description}")
    if "python" in blob and "python" not in normalize(json.dumps(profile.get("skills", {}))):
        risk_flags.append("JD asks for Python; profile does not verify production Python experience.")
    if any(term in blob for term in ["5+ years", "4+ years", "3+ years"]):
        risk_flags.append("JD appears to request materially more experience than the candidate currently has; do not claim additional years.")
    if any(term in blob for term in ["citizenship", "security clearance", "us person", "eu work authorization"]):
        risk_flags.append("Jurisdiction-specific eligibility question detected; user confirmation required.")
    return ApplicationPrep(
        resume_variant=variant,
        emphasis=groups[:4],
        matched_keywords=matched,
        missing_keywords=missing[:12],
        suggested_summary=_summary_for(variant, title, matched),
        answers=answers,
        risk_flags=risk_flags,
    )


def prep_to_dict(prep: ApplicationPrep) -> Dict:
    return asdict(prep)


def fingerprint_job(company: str, title: str, url: str = "") -> str:
    raw = "|".join([normalize(company), normalize(title), normalize(url.split("?")[0])])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
