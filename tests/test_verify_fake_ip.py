"""Hosts behind a fake-ip resolver (Clash/mihomo): GROK_VERIFY_FAKE_IP=doh|skip|allow."""
import httpx
import pytest

from grok_search import verify
from grok_search.config import config

PUBLIC_IP = "93.184.216.34"
DOH_HOST = "cloudflare-dns.com"


@pytest.fixture(autouse=True)
def _fresh_state():
    verify._DOH_CACHE.clear()


@pytest.fixture(autouse=True)
def dns(monkeypatch):
    table: dict = {}

    async def resolve(host, port):
        value = table.get(host, [PUBLIC_IP])
        if isinstance(value, Exception):
            raise value
        return list(value)

    monkeypatch.setattr(verify, "resolve_host", resolve)
    return table


def mock_verify_client(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(verify.httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))


def doh_answer(addresses, status=0):
    answers = [{"name": "x", "type": 28 if ":" in a else 1, "TTL": 60, "data": a} for a in addresses]
    return {"Status": status, "Answer": answers}


def fake_ip_site(dns, doh_records, hits, doh_status=200):
    """clash.example.test resolves locally to a fake-ip address; DoH knows the real records."""
    dns["clash.example.test"] = ["198.18.0.7"]

    async def handler(request):
        hits.append(f"{request.url.host}{request.url.path}" + (f"?{request.url.params['type']}" if request.url.host == DOH_HOST else ""))
        if request.url.host == DOH_HOST:
            assert request.headers["accept"] == "application/dns-json" and request.url.params["name"] == "clash.example.test"
            if doh_status != 200:
                return httpx.Response(doh_status, text="busy")
            wanted = [a for a in doh_records.get("addresses", []) if (":" in a) == (request.url.params["type"] == "AAAA")]
            return httpx.Response(200, json=doh_answer(wanted, doh_records.get("status", 0)))
        return httpx.Response(200, text="<title>Behind fake-ip</title>")
    return handler


@pytest.mark.asyncio
async def test_doh_mode_checks_real_addresses_then_fetches(monkeypatch, dns):
    hits = []
    mock_verify_client(monkeypatch, fake_ip_site(dns, {"addresses": [PUBLIC_IP, "2606:4700::1"]}, hits))
    sources = [{"url": "https://clash.example.test/a"}, {"url": "https://clash.example.test/b"}]
    result = await verify.verify_answer("x", sources, check_urls=True, timeout_s=5, fake_ip="doh")
    assert [u["title"] for u in result["urls"]] == ["Behind fake-ip", "Behind fake-ip"]
    assert sorted(h for h in hits if h.startswith(DOH_HOST)) == [f"{DOH_HOST}/dns-query?A", f"{DOH_HOST}/dns-query?AAAA"]  # cached per host


@pytest.mark.asyncio
async def test_doh_mode_refuses_names_whose_real_address_is_private(monkeypatch, dns):
    hits = []
    mock_verify_client(monkeypatch, fake_ip_site(dns, {"addresses": ["10.0.0.5"]}, hits))
    result = await verify.verify_answer("x", [{"url": "https://clash.example.test/admin"}], check_urls=True, timeout_s=5, fake_ip="doh")
    assert not any(h.startswith("clash.example.test") for h in hits) and result["urls"] == []
    assert result["skipped"][0]["reason"] == "blocked: clash.example.test resolves to 10.0.0.5 (DNS-over-HTTPS), which is not a public address"


@pytest.mark.asyncio
async def test_doh_mode_nxdomain_is_unresolved(monkeypatch, dns):
    hits = []
    mock_verify_client(monkeypatch, fake_ip_site(dns, {"status": 3}, hits))
    result = await verify.verify_answer("x", [{"url": "https://clash.example.test/"}], check_urls=True, timeout_s=5, fake_ip="doh")
    assert "NXDOMAIN" in result["unresolved"][0]["reason"] and result["lookup_failed"] == []
    assert not any(h.startswith("clash.example.test") for h in hits)


@pytest.mark.asyncio
async def test_doh_failure_is_lookup_failed(monkeypatch, dns):
    hits = []
    mock_verify_client(monkeypatch, fake_ip_site(dns, {}, hits, doh_status=503))
    result = await verify.verify_answer("x", [{"url": "https://clash.example.test/"}], check_urls=True, timeout_s=5, fake_ip="doh")
    assert result["unresolved"] == [] and "DNS-over-HTTPS HTTP 503" in result["lookup_failed"][0]["reason"]
    assert not any(h.startswith("clash.example.test") for h in hits)


@pytest.mark.asyncio
async def test_doh_mode_checks_redirect_targets_too(monkeypatch, dns):
    dns["clash.example.test"] = ["198.18.0.7"]
    hits = []

    async def handler(request):
        hits.append(request.url.host)
        if request.url.host == DOH_HOST:
            return httpx.Response(200, json=doh_answer(["192.168.1.1"] if request.url.params["type"] == "A" else []))
        return httpx.Response(302, headers={"Location": "https://clash.example.test/router"})

    mock_verify_client(monkeypatch, handler)
    result = await verify.verify_answer("x", [{"url": "https://public.example.test/r"}], check_urls=True, timeout_s=5, fake_ip="doh")
    assert "clash.example.test" not in hits
    assert result["skipped"][0]["reason"].startswith("blocked: redirect target clash.example.test resolves to 192.168.1.1 (DNS-over-HTTPS)")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode, fetched", [("allow", True), ("skip", False)])
async def test_allow_and_skip_modes_do_not_use_doh(monkeypatch, dns, mode, fetched):
    hits = []
    mock_verify_client(monkeypatch, fake_ip_site(dns, {"addresses": [PUBLIC_IP]}, hits))
    result = await verify.verify_answer("x", [{"url": "https://clash.example.test/"}], check_urls=True, timeout_s=5, fake_ip=mode)
    assert not any(h.startswith(DOH_HOST) for h in hits)
    assert bool(result["urls"]) is fetched
    if not fetched:
        assert "fake-ip" in result["skipped"][0]["reason"]


@pytest.mark.asyncio
async def test_custom_doh_url(monkeypatch, dns):
    dns["clash.example.test"] = ["198.18.0.7"]
    seen = []

    async def handler(request):
        seen.append(str(request.url.copy_with(params=None)))
        if request.url.host == "dns.google":
            return httpx.Response(200, json=doh_answer([PUBLIC_IP] if request.url.params["type"] == "A" else []))
        return httpx.Response(200, text="<title>ok</title>")

    mock_verify_client(monkeypatch, handler)
    result = await verify.verify_answer("x", [{"url": "https://clash.example.test/"}], check_urls=True, timeout_s=5,
                                        fake_ip="doh", doh_url="https://dns.google/resolve")
    assert "https://dns.google/resolve" in seen and result["urls"][0]["title"] == "ok"


def test_fake_ip_settings(monkeypatch):
    monkeypatch.delenv("GROK_VERIFY_FAKE_IP", raising=False)
    monkeypatch.delenv("GROK_VERIFY_DOH_URL", raising=False)
    assert config.verify_fake_ip == "doh" and config.verify_doh_url == "https://cloudflare-dns.com/dns-query"
    monkeypatch.setenv("GROK_VERIFY_FAKE_IP", " SKIP ")
    assert config.verify_fake_ip == "skip"
    monkeypatch.setenv("GROK_VERIFY_FAKE_IP", "sometimes")
    assert config.verify_fake_ip == "doh"
    monkeypatch.setenv("GROK_VERIFY_DOH_URL", "https://dns.google/resolve")
    assert config.verify_doh_url == "https://dns.google/resolve"
