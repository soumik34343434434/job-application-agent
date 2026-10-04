from src.naukri import NaukriAgent


def test_parse_card_blob():
    blob = """Security Engineer\nAcme Security\n2-5 Yrs\n₹10-15 LPA\nBangalore, Bengaluru\nEndpoint security and EDR operations using Microsoft Defender"""
    company, location, salary, experience, description = NaukriAgent._parse_card_blob(blob, "Security Engineer")
    assert company == "Acme Security"
    assert "Bangalore" in location
    assert "LPA" in salary
    assert "2-5" in experience
    assert "Endpoint security" in description


def test_external_host_detection_logic_is_simple():
    from urllib.parse import urlparse
    assert "naukri.com" in urlparse("https://www.naukri.com/job-listings/x").netloc
    assert "naukri.com" not in urlparse("https://careers.example.com/job/x").netloc
