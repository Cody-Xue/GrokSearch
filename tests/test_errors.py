import os

import httpx
import pytest

from grok_search import errors


def test_redact_removes_keys_and_tokens(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-SECRETVALUE123")
    text = errors.redact("Authorization: Bearer abcdefghijkl token=xyz123456 key tvly-SECRETVALUE123 fc-abcdef123456")
    for secret in ("abcdefghijkl", "xyz123456", "SECRETVALUE", "abcdef123456"):
        assert secret not in text


@pytest.mark.parametrize("status, body, category", [
    (401, {"detail": {"error": "Unauthorized: missing or invalid API key."}}, "auth"),
    (403, {"detail": {"error": "[403] URL is not supported"}}, "bad_request"),
    (402, {"success": False, "error": "Insufficient credits"}, "quota"),
    (432, {"detail": {"error": "This request exceeds your plan's set usage limit."}}, "quota"),
    (429, {}, "rate_limit"),
    (502, {"error": {"message": "bad gateway"}}, "upstream_error"),
])
def test_http_status_classification(status, body, category):
    request = httpx.Request("POST", "https://provider.example.test/x")
    response = httpx.Response(status, json=body, request=request)
    error = errors.classify_exception("p", httpx.HTTPStatusError("x", request=request, response=response))
    assert (error.category, error.status) == (category, status)
    assert error.retryable == (category in ("rate_limit", "upstream_error"))


def test_proxy_environment_problem_is_named(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "localhost,[::1]")
    error = errors.classify_exception("p", httpx.InvalidURL("Invalid port: ':1]'"))
    assert error.category == "proxy_config" and "NO_PROXY" in error.detail
    assert errors.classify_exception("p", httpx.ReadTimeout("slow")).category == "timeout"
    assert errors.classify_exception("p", httpx.ConnectError("refused")).category == "network"


def test_sanitized_no_proxy_lets_httpx_build_clients(monkeypatch):
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,::1,[::1],[::1]:8080,fe80::/10,10.0.0.0/8")
    with pytest.raises(httpx.InvalidURL):
        httpx.Client()
    assert errors.sanitize_no_proxy_env() == ["NO_PROXY"]
    assert os.environ["NO_PROXY"] == "localhost,127.0.0.1,::1,fe80::,10.0.0.0/8"
    httpx.Client().close()
