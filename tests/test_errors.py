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


def test_redact_removes_secrets_before_truncating(monkeypatch):
    secret = "g2a_FAKEc0ffee1234567890abcdef123456"
    monkeypatch.setenv("GROK_API_KEY", secret + "\n")  # configured values are stripped like the key that is sent
    text = errors.redact("x" * 280 + " " + secret, 300)
    assert "g2a_FAKE" not in text and text.endswith("***")


@pytest.mark.parametrize("raw, leaked", [
    ("upstream said xai-AbCdEf0123456789xyz", "AbCdEf0123456789"),
    ('{"Authorization": "Basic dXNlcjpwYXNzd29yZA=="}', "dXNlcjpwYXNzd29yZA"),
    ('{"authorization":"g2a_secretvalue99"}', "g2a_secretvalue99"),
    ("GET https://user:hunter22@proxy.example.test/v1?x=1", "hunter22"),
    ("GET https://api.example.test/v1/models?key=AIzaSyFAKE0123456789", "AIzaSyFAKE0123456789"),
])
def test_redact_covers_other_key_formats(raw, leaked):
    assert leaked not in errors.redact(raw)


def test_redact_keeps_the_bearer_scheme_readable():
    assert errors.redact("Authorization: Bearer abcdefghijkl") == "Authorization: Bearer ***"
    assert errors.redact('{"authorization": "Bearer abcdefghijkl"}') == '{"authorization": "Bearer ***"}'


CF_PAGE = """<!DOCTYPE html>
<!--[if lt IE 7]> <html class="no-js ie6 oldie" lang="en-US"> <![endif]-->
<head><title>tavily.example.test | 524: A timeout occurred</title></head>
<body><a href="https://www.cloudflare.com/5xx-error-landing">cloudflare.com</a></body>"""


@pytest.mark.parametrize("status, body, headers, expected", [
    (524, CF_PAGE, {"content-type": "text/html", "server": "cloudflare"}, "Cloudflare 524: origin timed out (no response within ~100 s)"),
    (521, "error code: 521", {}, "Cloudflare 521: origin refused the connection (gateway down)"),
    (502, "<html><head><title>502 Bad Gateway</title></head><body>nginx</body></html>", {"content-type": "text/html"}, "502 Bad Gateway"),
    (500, "<html><body>no title</body></html>", {"content-type": "text/html"}, "HTML error page"),
])
def test_html_and_cloudflare_error_bodies_are_summarised(status, body, headers, expected):
    assert errors.summarize_error_body(body, status=status, content_type=headers.get("content-type", ""), server=headers.get("server", "")) == expected
    request = httpx.Request("POST", "https://provider.example.test/x")
    response = httpx.Response(status, text=body, headers=headers, request=request)
    assert errors.response_error_message(response) == expected


@pytest.mark.parametrize("status, category", [(522, "timeout"), (524, "timeout"), (520, "upstream_error"), (530, "upstream_error")])
def test_cloudflare_timeouts_are_classified_as_timeouts(status, category):
    request = httpx.Request("POST", "https://provider.example.test/x")
    response = httpx.Response(status, text=f"error code: {status}", request=request)
    error = errors.classify_exception("tavily", httpx.HTTPStatusError("x", request=request, response=response))
    assert error.category == category and error.detail.startswith(f"Cloudflare {status}")


@pytest.mark.parametrize("exc, text", [
    (httpx.ConnectError(""), "proxy tunnel closed or upstream unreachable (ConnectError)"),
    (httpx.ReadTimeout(""), "no data received within the read timeout (ReadTimeout)"),
    (httpx.RemoteProtocolError(""), "upstream closed the connection mid-response (RemoteProtocolError)"),
    (httpx.ConnectError("[Errno 111] Connection refused"), "ConnectError: [Errno 111] Connection refused"),
])
def test_transport_errors_never_collapse_to_a_bare_class_name(exc, text):
    assert errors.describe_transport_error(exc) == text
    assert errors.classify_exception("p", exc).detail == text
