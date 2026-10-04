import json
from pathlib import Path
import tempfile

from src.scorer import score_job
from src.tailoring import prepare_application, fingerprint_job

PROFILE = json.loads((Path(__file__).parents[1] / 'data' / 'profile.json').read_text())


def test_strong_vulnerability_role():
    r = score_job('Vulnerability Management Engineer', 'Qualys VM, remediation, vulnerability assessment, security operations', 'Example Co')
    assert r.score >= 85, f"score={r.score} penalties={r.penalties} matched={r.matched} reasons={r.reasons}"


def test_grc_is_low():
    r = score_job('GRC Analyst', 'risk and compliance, IT audit, controls testing', 'Example Co')
    assert r.score < 70


def test_prep_never_invents_authorization():
    p = prepare_application('Security Engineer', 'Endpoint security, EDR, incident response', PROFILE, 'Example Co')
    assert p.answers['work_authorization'] == 'ASK_USER_BEFORE_SUBMITTING'
    assert p.answers['visa_sponsorship'] == 'ASK_USER_BEFORE_SUBMITTING'


def test_fingerprint_stable_for_same_job():
    a = fingerprint_job('Acme', 'Security Engineer', 'https://example.com/job/1?utm=abc')
    b = fingerprint_job('Acme', 'Security Engineer', 'https://example.com/job/1?utm=def')
    assert a == b
