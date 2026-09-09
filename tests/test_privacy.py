from football_analysis.privacy import redact_url, sanitize


def test_redact_url():
    output = redact_url("https://example.com/api?matchId=7&token=secret")
    assert "matchId=7" in output
    assert "secret" not in output


def test_sanitize_nested():
    output = sanitize({"data": {"token": "secret", "odds": 1.75}})
    assert output["data"]["token"] == "[REDACTED]"
    assert output["data"]["odds"] == 1.75

