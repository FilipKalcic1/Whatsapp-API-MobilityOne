import pytest
from worker import summarize_data, sanitize_log_data

def test_sanitize_log_data_hides_secrets():
    """Provjerava da se lozinke i tokeni maskiraju."""
    data = {
        "user": "marko",
        "password": "secret_password",
        "nested": {"token": "12345", "public": "ok"}
    }
    
    clean = sanitize_log_data(data)
    
    assert clean["user"] == "marko"
    assert clean["password"] == "***MASKED***"
    assert clean["nested"]["token"] == "***MASKED***"
    assert clean["nested"]["public"] == "ok"

def test_summarize_data_truncates_large_input():
    """Provjerava da se ogromni podaci skraćuju."""
    # 1. Ogroman string
    huge_string = "a" * 2000
    summary = summarize_data(huge_string)
    assert len(summary) < 2000
    assert "truncated" in summary

    # 2. Ogromna lista
    huge_list = [i for i in range(100)]
    summary_list = summarize_data(huge_list)
    assert isinstance(summary_list, str)
    assert "List with 100 items" in summary_list

    # 3. Ogroman dict
    huge_dict = {f"key_{i}": i for i in range(100)}
    summary_dict = summarize_data(huge_dict)
    assert summary_dict["info"] == "Large dictionary summarized"
    assert summary_dict["keys_count"] == 100