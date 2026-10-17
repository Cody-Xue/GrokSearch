"""Registry lookups of the verification layer: what is "unresolved" (the registry said no) and what is
"lookup_failed" (no answer), Crossref etiquette, DOIs outside Crossref and the process-wide arXiv gate."""
import asyncio
import socket
import time

import httpx
import pytest

from grok_search import verify
from grok_search.errors import classify_exception

PUBLIC_IP = "93.184.216.34"
SPEC_DEC = "Fast Inference from Transformers via Speculative Decoding"


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    monkeypatch.setattr(verify, "ARXIV_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(verify, "ARXIV_RETRY_DELAY_S", 0.0)
    monkeypatch.setattr(verify, "CROSSREF_MIN_INTERVAL_S", 0.0)
    verify._ARXIV_CACHE.clear()
    verify._arxiv_gate.update(loop=None, lock=None, last_start=0.0)
    verify._crossref_gate.update(loop=None)


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


def atom(*entries):
    body = "".join(f"<entry><id>http://arxiv.org/abs/{idv}v1</id><title>{title}</title></entry>" for idv, title in entries)
    return f'<feed xmlns="http://www.w3.org/2005/Atom">{body}</feed>'


def crossref_record(title):
    return httpx.Response(200, json={"message": {"title": [title], "container-title": ["Nature"], "type": "journal-article",
                                                 "issued": {"date-parts": [[2021, 7, 15]]}}})


def outcomes(result):
    found = {("arxiv", x["id"]): "found" for x in result["arxiv"]}
    found.update({("doi", x["doi"]): "found" for x in result["doi"]})
    found.update({("url", x["url"]): "found" for x in result["urls"]})
    for name in ("unresolved", "lookup_failed", "skipped"):
        found.update({(x["kind"], x["value"]): name for x in result[name]})
    return found


def reasons(result, name):
    return {x["value"]: x["reason"] for x in result[name]}


# ---------------------------------------------------------------- Crossref
@pytest.mark.asyncio
@pytest.mark.parametrize("status, requests", [(429, 2), (503, 2), (500, 1)])
async def test_crossref_failure_is_lookup_failed_not_not_found(monkeypatch, status, requests):
    calls = []

    async def handler(request):
        calls.append(request.url.host)
        return httpx.Response(status, headers={"Retry-After": "0"}, text="busy")

    mock_verify_client(monkeypatch, handler)
    result = await verify.verify_answer("AlphaFold2: DOI 10.1038/s41586-021-03819-2", [], check_urls=False, timeout_s=5)
    assert result["unresolved"] == []
    assert reasons(result, "lookup_failed")["10.1038/s41586-021-03819-2"].startswith(f"Crossref HTTP {status}")
    assert result["summary"]["lookup_failed"] == 1 and result["summary"]["unresolved"] == 0
    assert calls == ["api.crossref.org"] * requests  # one retry after Retry-After on 429/503 only


@pytest.mark.asyncio
async def test_crossref_429_is_retried_after_retry_after(monkeypatch):
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return crossref_record("Highly accurate protein structure prediction with AlphaFold")

    mock_verify_client(monkeypatch, handler)
    result = await verify.verify_answer("DOI 10.1038/s41586-021-03819-2", [], check_urls=False, timeout_s=5)
    assert [x["doi"] for x in result["doi"]] == ["10.1038/s41586-021-03819-2"] and calls["n"] == 2


@pytest.mark.asyncio
async def test_crossref_long_retry_after_starts_a_shared_cooldown(monkeypatch):
    calls = []

    async def handler(request):
        calls.append(str(request.url))
        return httpx.Response(429, headers={"Retry-After": "30"})

    mock_verify_client(monkeypatch, handler)
    started = time.monotonic()
    result = await verify.verify_answer("10.1038/nature14539 and 10.1038/nature16961", [], check_urls=False, timeout_s=5)
    assert time.monotonic() - started < 2
    assert len(calls) == 1  # the second DOI is not sent while Crossref cools down
    failed = reasons(result, "lookup_failed")
    assert set(failed) == {"10.1038/nature14539", "10.1038/nature16961"} and result["unresolved"] == []
    assert any("cooling down" in reason for reason in failed.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("mailto, limit", [("", 1), ("me@example.test", 3)])
async def test_crossref_concurrency_is_process_wide(monkeypatch, mailto, limit):
    state = {"now": 0, "max": 0}

    async def handler(request):
        state["now"] += 1
        state["max"] = max(state["max"], state["now"])
        await asyncio.sleep(0.03)
        state["now"] -= 1
        return crossref_record("T")

    mock_verify_client(monkeypatch, handler)
    answers = [" ".join(f"10.1234/a{i}-{j}" for j in range(4)) for i in range(3)]
    results = await asyncio.gather(*(verify.verify_answer(a, [], check_urls=False, timeout_s=5, mailto=mailto) for a in answers))
    assert all(len(r["doi"]) == 4 for r in results)
    assert state["max"] == limit


# ------------------------------------------------------------ DOIs outside Crossref
def registry_handler(handle, datacite=None, calls=None):
    async def handler(request):
        if calls is not None:
            calls.append(request.url.host)
        if request.url.host == "api.crossref.org":
            return httpx.Response(404, text="Resource not found.")
        if request.url.host == "doi.org":
            assert request.url.path.startswith("/api/handles/")
            return handle
        if request.url.host == "api.datacite.org":
            return datacite or httpx.Response(404, json={"errors": [{"status": "404"}]})
        return httpx.Response(500)
    return handler


@pytest.mark.asyncio
async def test_datacite_doi_resolves_with_its_title(monkeypatch):
    handle = httpx.Response(200, json={"responseCode": 1, "handle": "10.5281/zenodo.3509134"})
    datacite = httpx.Response(200, json={"data": {"attributes": {
        "titles": [{"title": "Some Dataset"}], "publisher": "Zenodo", "publicationYear": 2019,
        "types": {"resourceTypeGeneral": "Dataset"}}}})
    mock_verify_client(monkeypatch, registry_handler(handle, datacite))
    result = await verify.verify_answer("data: 10.5281/zenodo.3509134", [], check_urls=False, timeout_s=5)
    assert result["doi"] == [{"doi": "10.5281/zenodo.3509134", "title": "Some Dataset", "container": "Zenodo",
                              "type": "Dataset", "issued": "2019", "registry": "datacite"}]
    assert result["unresolved"] == [] and result["lookup_failed"] == []


@pytest.mark.asyncio
async def test_doi_registered_with_another_agency_exists_without_title(monkeypatch):
    handle = httpx.Response(200, json={"responseCode": 1, "handle": "10.3969/j.issn.1000-0054.2020.01.001"})
    mock_verify_client(monkeypatch, registry_handler(handle))
    result = await verify.verify_answer("万方 DOI：10.3969/j.issn.1000-0054.2020.01.001。", [], check_urls=False, timeout_s=5)
    assert [(x["doi"], x["registry"], x["title"]) for x in result["doi"]] == [("10.3969/j.issn.1000-0054.2020.01.001", "doi.org", "")]
    assert result["unresolved"] == []


@pytest.mark.asyncio
async def test_doi_unknown_to_the_handle_registry_is_unresolved(monkeypatch):
    calls = []
    handle = httpx.Response(404, json={"responseCode": 100, "handle": "10.1016/S0140-6736"})
    mock_verify_client(monkeypatch, registry_handler(handle, calls=calls))
    result = await verify.verify_answer("10.1016/S0140-6736", [], check_urls=False, timeout_s=5)
    assert reasons(result, "unresolved") == {"10.1016/S0140-6736": "not found"}
    assert calls == ["api.crossref.org", "doi.org"]


@pytest.mark.asyncio
async def test_failed_handle_lookup_is_lookup_failed(monkeypatch):
    mock_verify_client(monkeypatch, registry_handler(httpx.Response(503, text="busy")))
    result = await verify.verify_answer("10.1234/maybe", [], check_urls=False, timeout_s=5)
    assert result["unresolved"] == []
    assert reasons(result, "lookup_failed") == {"10.1234/maybe": "not in Crossref; doi.org handle lookup HTTP 503"}


# ---------------------------------------------------------------- arXiv gate
@pytest.mark.asyncio
async def test_arxiv_uses_one_connection_and_spaces_after_completion(monkeypatch):
    monkeypatch.setattr(verify, "ARXIV_MIN_INTERVAL_S", 0.05)
    state = {"now": 0, "max": 0}
    spans = []

    async def handler(request):
        state["now"] += 1
        state["max"] = max(state["max"], state["now"])
        start = time.monotonic()
        await asyncio.sleep(0.08)
        spans.append((start, time.monotonic()))
        state["now"] -= 1
        return httpx.Response(200, text=atom())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await asyncio.gather(*(verify.arxiv_get(client, {"id_list": f"26{i:02d}.00001"}) for i in range(1, 4)))
    assert state["max"] == 1
    gaps = [b[0] - a[1] for a, b in zip(spans, spans[1:])]
    assert all(g >= 0.045 for g in gaps), gaps


@pytest.mark.asyncio
async def test_arxiv_429_starts_a_process_wide_cooldown(monkeypatch):
    arxiv_calls = []

    async def handler(request):
        if request.url.host == "export.arxiv.org":
            arxiv_calls.append(request.url.params["id_list"])
            return httpx.Response(429, headers={"Retry-After": "120"}, text="Rate exceeded.")
        return httpx.Response(404, json={"errors": []})  # DataCite has no record either

    mock_verify_client(monkeypatch, handler)
    first = await verify.verify_answer("see arXiv:2211.17192", [], check_urls=False, timeout_s=5)
    started = time.monotonic()
    second = await verify.verify_answer("see arXiv:2302.01318", [], check_urls=False, timeout_s=5)
    assert time.monotonic() - started < 1 and arxiv_calls == ["2211.17192"]
    assert reasons(first, "lookup_failed") == {"2211.17192": "arXiv HTTP 429 (rate-limited); cooling down for 120 s"}
    assert "cooling down after HTTP 429" in reasons(second, "lookup_failed")["2302.01318"]
    assert first["unresolved"] == [] and second["unresolved"] == []


@pytest.mark.asyncio
async def test_cooldown_error_keeps_the_status_for_web_fetch(monkeypatch):
    async def handler(request):
        return httpx.Response(503, text="down")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError) as first:
            await verify.arxiv_get(client, {"id_list": "2211.17192"})
        with pytest.raises(verify.RegistryCooldown) as second:
            await verify.arxiv_get(client, {"id_list": "2211.17192"})
    assert first.value.response.status_code == 503 and second.value.response.status_code == 503
    assert classify_exception("arxiv-api", second.value).status == 503


@pytest.mark.asyncio
async def test_arxiv_failure_falls_back_to_datacite(monkeypatch):
    async def handler(request):
        if request.url.host == "export.arxiv.org":
            return httpx.Response(429, text="Rate exceeded.")
        assert request.url.host == "api.datacite.org" and request.url.path == "/dois/10.48550/arXiv.2211.17192"
        return httpx.Response(200, json={"data": {"attributes": {"titles": [{"title": SPEC_DEC}], "publicationYear": 2022}}})

    mock_verify_client(monkeypatch, handler)
    result = await verify.verify_answer(f'"{SPEC_DEC}" (arXiv:2211.17192)', [], check_urls=False, timeout_s=5)
    assert result["arxiv"] == [{"id": "2211.17192", "title": SPEC_DEC, "published": "2022", "authors": [], "registry": "datacite"}]
    assert result["lookup_failed"] == [] and result["title_checks"][0]["status"] == "match"


@pytest.mark.asyncio
async def test_slow_arxiv_leaves_time_for_the_datacite_fallback(monkeypatch):
    async def handler(request):
        if request.url.host == "export.arxiv.org":
            await asyncio.sleep(30)  # arXiv slow, or its one connection busy with other searches
        return httpx.Response(200, json={"data": {"attributes": {"titles": [{"title": SPEC_DEC}], "publicationYear": 2022}}})

    mock_verify_client(monkeypatch, handler)
    started = time.monotonic()
    result = await verify.verify_answer("arXiv:2211.17192", [], check_urls=False, timeout_s=2.0)
    assert time.monotonic() - started < 2.0 and "timed_out" not in result
    assert [(x["id"], x["registry"]) for x in result["arxiv"]] == [("2211.17192", "datacite")]
    assert result["lookup_failed"] == [] and result["unresolved"] == []


@pytest.mark.asyncio
async def test_concurrent_lookups_of_one_id_share_one_request(monkeypatch):
    calls = []

    async def handler(request):
        calls.append(request.url.params["id_list"])
        await asyncio.sleep(0.05)
        return httpx.Response(200, text=atom(("2211.17192", SPEC_DEC)))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await asyncio.gather(*(verify._resolve_arxiv(["2211.17192"], client) for _ in range(3)))
    assert calls == ["2211.17192"]
    assert all(found["2211.17192"]["title"] == SPEC_DEC and not failed for found, _, failed in results)


@pytest.mark.asyncio
async def test_arxiv_failure_reason_names_the_status_and_retries_once(monkeypatch):
    calls = []

    async def handler(request):
        calls.append(request.url.host)
        if request.url.host == "export.arxiv.org":
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(404)

    mock_verify_client(monkeypatch, handler)
    result = await verify.verify_answer("arXiv:2211.17192", [], check_urls=False, timeout_s=10)
    assert reasons(result, "lookup_failed") == {"2211.17192": "arXiv HTTP 502"}
    assert calls.count("export.arxiv.org") == 2 and result["unresolved"] == []


@pytest.mark.asyncio
async def test_arxiv_read_timeout_is_short_and_bounded_by_the_deadline(monkeypatch):
    seen = []

    async def handler(request):
        seen.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, text=atom())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=60) as client:
        await verify.arxiv_get(client, {"id_list": "2211.17192"})
        await verify.arxiv_get(client, {"id_list": "2211.17192"}, deadline=time.monotonic() + 4)
    assert seen[0] == verify.ARXIV_READ_TIMEOUT_S and seen[1] <= 4


def test_lookup_arxiv_raises_when_arxiv_did_not_answer():
    async def run():
        async def handler(request):
            return httpx.Response(429)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await verify.lookup_arxiv(["2211.17192"], client)

    with pytest.raises(verify.RegistryCooldown):
        asyncio.run(run())


# ------------------------------------------------------------ outcome classes
@pytest.mark.asyncio
async def test_url_outcomes_split_unresolved_and_lookup_failed(monkeypatch, dns):
    dns["gone.example.test"] = socket.gaierror(socket.EAI_NONAME, "Name or service not known")
    dns["flaky-dns.example.test"] = socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")

    async def handler(request):
        path = request.url.path
        if path == "/timeout":
            raise httpx.ReadTimeout("slow")
        if path == "/challenge":
            return httpx.Response(403, headers={"cf-mitigated": "challenge"}, text="Just a moment...")
        return httpx.Response(int(path.strip("/")), text="x")

    mock_verify_client(monkeypatch, handler)
    base = "https://site.example.test"
    urls = [f"{base}/{code}" for code in (200, 404, 410, 403, 429, 500, 503)] + [
        f"{base}/challenge", f"{base}/timeout", "https://gone.example.test/", "https://flaky-dns.example.test/"]
    result = await verify.verify_answer("x", [{"url": u} for u in urls], check_urls=True, timeout_s=5)
    got = outcomes(result)
    assert got[("url", f"{base}/200")] == "found"
    for code in (404, 410):
        assert got[("url", f"{base}/{code}")] == "unresolved"
    # A 403 used to be unresolved; refusing a script says nothing about whether the page exists.
    for code in (403, 429, 500, 503):
        assert got[("url", f"{base}/{code}")] == "lookup_failed"
    assert got[("url", f"{base}/challenge")] == "lookup_failed" and got[("url", f"{base}/timeout")] == "lookup_failed"
    assert got[("url", "https://gone.example.test/")] == "unresolved"
    assert got[("url", "https://flaky-dns.example.test/")] == "lookup_failed"
    assert reasons(result, "unresolved")[f"{base}/404"] == "status 404"
    assert reasons(result, "lookup_failed")[f"{base}/403"].startswith("status 403 (access denied to automated requests")
    assert reasons(result, "lookup_failed")[f"{base}/challenge"] == "status 403 (bot challenge)"


def test_proxy_errors_are_not_blamed_on_the_site(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("NO_PROXY", "localhost")
    outcome, reason = verify._url_outcome({"url": "https://[2606:4700:4700::1111]/", "status": 502, "title": ""})
    assert outcome == "lookup_failed" and reason == "status 502 (from the proxy or the site)"


@pytest.mark.asyncio
async def test_mailto_goes_to_registries_only(monkeypatch, dns):
    dns["clash.example.test"] = ["198.18.0.7"]
    agents = {}

    async def handler(request):
        agents[request.url.host] = request.headers["user-agent"]
        if request.url.host == "api.crossref.org":
            return crossref_record("T")
        if request.url.host == "cloudflare-dns.com":
            return httpx.Response(200, json={"Status": 0, "Answer": [{"type": 1, "TTL": 60, "data": PUBLIC_IP}]})
        return httpx.Response(200, text="<title>page</title>")

    mock_verify_client(monkeypatch, handler)
    await verify.verify_answer("10.1234/abc", [{"url": "https://site.example.test/"}, {"url": "https://clash.example.test/"}],
                               check_urls=True, timeout_s=5, mailto="me@example.test", fake_ip="doh")
    assert agents["api.crossref.org"].endswith("(mailto:me@example.test)")
    assert {host for host, agent in agents.items() if "me@example.test" in agent} == {"api.crossref.org"}
    assert {"site.example.test", "clash.example.test", "cloudflare-dns.com"} <= set(agents)


@pytest.mark.asyncio
async def test_identifier_checks_can_be_disabled_alone(monkeypatch):
    hosts = []

    async def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(200, text="<title>page</title>")

    mock_verify_client(monkeypatch, handler)
    result = await verify.verify_answer("arXiv:2211.17192 and 10.1038/nature14539", [{"url": "https://example.test/p"}],
                                        check_ids=False, check_urls=True, timeout_s=5)
    assert hosts == ["example.test"] and [x["url"] for x in result["urls"]] == ["https://example.test/p"]
    assert {(x["kind"], x["reason"]) for x in result["skipped"]} == {
        ("arxiv", "identifier checks disabled (GROK_VERIFY_IDS=false)"), ("doi", "identifier checks disabled (GROK_VERIFY_IDS=false)")}
    assert result["checked"] == {"arxiv": 0, "doi": 0, "urls": 1}


@pytest.mark.parametrize("status", [401, 403, 451])
def test_access_denied_is_unknown_not_suspect(status):
    outcome, reason = verify._url_outcome({"url": "https://www.reuters.com/world/x", "status": status, "title": ""})
    assert outcome == "lookup_failed"
    assert reason == f"status {status} (access denied to automated requests; the page may still exist)"


@pytest.mark.parametrize("status", [400, 404, 405, 410])
def test_other_client_errors_stay_unresolved(status):
    assert verify._url_outcome({"url": "https://example.test/x", "status": status, "title": ""})[0] == "unresolved"


# ------------------------------------------------------- registry requests stay on the registry
@pytest.mark.asyncio
async def test_dot_segment_doi_never_reaches_a_registry(monkeypatch):
    calls = []

    async def handler(request):
        calls.append(str(request.url))
        return httpx.Response(404)

    real = httpx.AsyncClient
    mock_verify_client(monkeypatch, handler)
    answer = "See 10.1234/../../../10.5555/12345678 and 10.1234/%2e%2e/%2E%2E/10.5555/1."
    result = await verify.verify_answer(answer, [{"url": "https://doi.org/10.1234/../../10.5555/x"}], check_urls=False, timeout_s=5)
    assert calls == []
    assert result["doi"] == [] and result["unresolved"] == [] and result["lookup_failed"] == []
    with pytest.raises(verify.LookupFailed):
        async with real(transport=httpx.MockTransport(handler)) as client:
            await verify.lookup_doi("10.1234/../../../10.5555/12345678", client)
    assert calls == []


@pytest.mark.asyncio
async def test_registry_redirects_are_not_followed(monkeypatch):
    calls = []

    async def handler(request):
        calls.append(f"{request.url.host}{request.url.path}")
        if request.url.host == "api.crossref.org":
            return httpx.Response(404, text="Resource not found.")
        if request.url.host == "doi.org":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1:9/internal/admin?action=reboot"})
        return httpx.Response(200, text="should not be fetched")

    mock_verify_client(monkeypatch, handler)
    result = await verify.verify_answer("10.5555/12345678", [], check_urls=False, timeout_s=5, mailto="me@example.test")
    assert calls == ["api.crossref.org/works/10.5555/12345678", "doi.org/api/handles/10.5555/12345678"]
    assert reasons(result, "lookup_failed") == {"10.5555/12345678": "not in Crossref; doi.org handle lookup HTTP 302"}


@pytest.mark.asyncio
async def test_registry_body_is_capped(monkeypatch):
    monkeypatch.setattr(verify, "REGISTRY_MAX_BYTES", 1024)

    async def handler(request):
        return httpx.Response(200, json={"message": {"title": ["T" * 5000]}})

    mock_verify_client(monkeypatch, handler)
    result = await verify.verify_answer("10.5555/12345678", [], check_urls=False, timeout_s=5)
    assert result["doi"] == []
    assert "more than" in reasons(result, "lookup_failed")["10.5555/12345678"]


@pytest.mark.asyncio
async def test_registry_gzip_body_is_inflated_with_a_limit(monkeypatch):
    import gzip
    import json as _json

    monkeypatch.setattr(verify, "REGISTRY_MAX_BYTES", 64 * 1024)
    small = gzip.compress(_json.dumps({"message": {"title": ["Small"], "type": "journal-article"}}).encode())
    bomb = gzip.compress(b"{" + b" " * (8 * 1024 * 1024) + b"}")
    seen_encodings = []

    class Raw(httpx.AsyncByteStream):
        def __init__(self, data):
            self.data = data

        async def __aiter__(self):
            yield self.data

    async def handler(request):
        seen_encodings.append(request.headers.get("accept-encoding"))
        data = bomb if "bomb" in request.url.path else small
        return httpx.Response(200, headers={"Content-Encoding": "gzip", "Content-Type": "application/json"}, stream=Raw(data))

    mock_verify_client(monkeypatch, handler)
    result = await verify.verify_answer("10.5555/ok and 10.5555/bomb", [], check_urls=False, timeout_s=5)
    assert [x["title"] for x in result["doi"]] == ["Small"]
    assert "more than" in reasons(result, "lookup_failed")["10.5555/bomb"]
    assert set(seen_encodings) == {"gzip, deflate"}
