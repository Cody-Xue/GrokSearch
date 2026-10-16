import asyncio
import time

import httpx
import pytest

from grok_search import verify


@pytest.fixture(autouse=True)
def _fresh_arxiv_state(monkeypatch):
    monkeypatch.setattr(verify, "ARXIV_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(verify, "ARXIV_RETRY_DELAY_S", 0.0)
    verify._ARXIV_CACHE.clear()
    verify._arxiv_gate.update(loop=None, lock=None, last_start=0.0)

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><id>http://arxiv.org/abs/2607.06065v1</id><title>SWE-Review: Closing the
  Loop</title><published>2026-07-07T00:00:00Z</published><author><name>Ruoyu Wang</name></author></entry>
  <entry><id>http://arxiv.org/api/errors#incorrect_id</id><title>Error</title></entry>
</feed>"""


def test_extract_identifiers():
    text = ("See arXiv:2607.06065 and 2503.15223v2, DOI 10.1038/s41586-026-10549-w. "
            "Not 0.28.1, not 12345.6789, not 2613.12345 (month 13), and 10.1145/3702972.")
    assert verify.extract_arxiv_ids(text) == ["2607.06065", "2503.15223"]
    assert verify.extract_dois(text) == ["10.1038/s41586-026-10549-w", "10.1145/3702972"]


@pytest.mark.asyncio
async def test_verify_answer_resolves_and_flags(monkeypatch):
    async def handler(request):
        host, path = request.url.host, request.url.path
        if host == "export.arxiv.org":
            ids = request.url.params["id_list"]
            assert "2607.06065" in ids and "2612.99999" in ids
            return httpx.Response(200, text=ATOM)
        if host == "api.crossref.org":
            if path == "/works/10.1038/s41586-026-10549-w":
                return httpx.Response(200, json={"message": {
                    "title": ["Evaluating large language models for accuracy incentivizes hallucinations"],
                    "container-title": ["Nature"], "type": "journal-article",
                    "issued": {"date-parts": [[2026, 4, 22]]}}})
            return httpx.Response(404, json={"status": "error"})
        if host == "example.test":
            if path == "/ok":
                return httpx.Response(200, text="<html><head><title>OK\n  page</title></head></html>")
            return httpx.Response(404, text="nope")
        return httpx.Response(500)

    real = httpx.AsyncClient
    monkeypatch.setattr(verify.httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))

    answer = "SWE-Review (arXiv:2607.06065) and ~2612.99999; Kalai et al. doi:10.1038/s41586-026-10549-w and 10.9999/nope."
    sources = [
        {"url": "https://arxiv.org/abs/2607.06065"},
        {"url": "https://doi.org/10.1038/s41586-026-10549-w"},
        {"url": "https://example.test/ok"},
        {"url": "https://example.test/dead"},
    ]
    result = await verify.verify_answer(answer, sources, check_urls=True, timeout_s=5)

    assert result["arxiv"] == [{"id": "2607.06065", "title": "SWE-Review: Closing the Loop", "published": "2026-07-07", "authors": ["Ruoyu Wang"]}]
    assert result["doi"] == [{"doi": "10.1038/s41586-026-10549-w", "title": "Evaluating large language models for accuracy incentivizes hallucinations", "container": "Nature", "type": "journal-article", "issued": "2026-4-22"}]
    assert result["urls"] == [{"url": "https://example.test/ok", "status": 200, "title": "OK page"}]
    unresolved = {(u["kind"], u["value"]): u["reason"] for u in result["unresolved"]}
    assert unresolved == {
        ("arxiv", "2612.99999"): "not found",
        ("doi", "10.9999/nope"): "not found",
        ("url", "https://example.test/dead"): "status 404",
    }
    assert result["checked"] == {"arxiv": 2, "doi": 2, "urls": 2}


@pytest.mark.asyncio
async def test_verify_answer_without_url_checks(monkeypatch):
    async def handler(request):
        assert request.url.host == "export.arxiv.org"
        return httpx.Response(200, text=ATOM)

    real = httpx.AsyncClient
    monkeypatch.setattr(verify.httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))
    result = await verify.verify_answer("see 2607.06065", [{"url": "https://example.test/x"}], check_urls=False, timeout_s=5)
    assert result["checked"] == {"arxiv": 1, "doi": 0, "urls": 0}
    assert result["arxiv"][0]["title"] == "SWE-Review: Closing the Loop"
    assert result["urls"] == [] and result["unresolved"] == []


@pytest.mark.asyncio
async def test_arxiv_get_retries_once_on_406_and_timeout(monkeypatch):
    monkeypatch.setattr(verify, "ARXIV_RETRY_DELAY_S", 0)
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(406, text="Not Acceptable")
        if calls["n"] == 2:
            return httpx.Response(200, text=ATOM)
        if calls["n"] == 3:
            raise httpx.ConnectTimeout("slow")
        return httpx.Response(200, text=ATOM)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        found, missing = await verify.lookup_arxiv(["2607.06065"], client)
        assert list(found) == ["2607.06065"] and calls["n"] == 2
        verify._ARXIV_CACHE.clear()  # force a second network round: timeout then success
        found, missing = await verify.lookup_arxiv(["2607.06065"], client)
        assert list(found) == ["2607.06065"] and calls["n"] == 4


@pytest.mark.asyncio
async def test_arxiv_get_gives_up_after_second_failure(monkeypatch):
    monkeypatch.setattr(verify, "ARXIV_RETRY_DELAY_S", 0)

    async def handler(request):
        return httpx.Response(503, text="down")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await verify.arxiv_get(client, {"id_list": "2607.06065"})


@pytest.mark.asyncio
async def test_arxiv_calls_are_spaced_process_wide(monkeypatch):
    monkeypatch.setattr(verify, "ARXIV_MIN_INTERVAL_S", 0.15)
    starts = []

    async def handler(request):
        starts.append(time.monotonic())
        return httpx.Response(200, text=ATOM)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await asyncio.gather(*(verify.arxiv_get(client, {"id_list": f"26{i:02d}.00001"}) for i in range(3)))
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert len(starts) == 3 and all(g >= 0.14 for g in gaps), gaps


@pytest.mark.asyncio
async def test_arxiv_lookup_is_cached(monkeypatch):
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        return httpx.Response(200, text=ATOM)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first, _ = await verify.lookup_arxiv(["2607.06065"], client)
        second, missing = await verify.lookup_arxiv(["2607.06065"], client)
    assert first == second and missing == [] and calls["n"] == 1


@pytest.mark.asyncio
async def test_verify_timeout_marks_pending_lookups(monkeypatch):
    async def handler(request):
        await asyncio.sleep(5)
        return httpx.Response(200, text=ATOM)

    real = httpx.AsyncClient
    monkeypatch.setattr(verify.httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))
    result = await verify.verify_answer("see 2607.06065 and 10.1038/x", [{"url": "https://example.test/slow"}], check_urls=True, timeout_s=0.2)
    assert result["timed_out"] is True
    assert {(u["kind"], u["value"], u["reason"]) for u in result["unresolved"]} == {
        ("arxiv", "2607.06065", "timed out"), ("doi", "10.1038/x", "timed out"), ("url", "https://example.test/slow", "timed out")}


def mock_verify_client(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(verify.httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))


async def atom_handler(request):
    return httpx.Response(200, text=ATOM)


@pytest.mark.asyncio
@pytest.mark.parametrize("answer, status", [
    ("See [SWE-Review: Closing the Loop](https://arxiv.org/abs/2607.06065).", "match"),
    ("SWE-Review (arXiv:2607.06065) closes the loop.", "match"),
    ("The paper \u201cScaling Laws for Neural Language Models\u201d (arXiv:2607.06065) is relevant.", "possible_mismatch"),
    ("**The paper arXiv 2607.06065 is about code review.**", "not_stated"),
])
async def test_title_checks(monkeypatch, answer, status):
    mock_verify_client(monkeypatch, atom_handler)
    result = await verify.verify_answer(answer, [], check_urls=False, timeout_s=5)
    assert [(t["id"], t["status"]) for t in result["title_checks"]] == [("2607.06065", status)]
    assert result["title_checks"][0]["registry_title"] == "SWE-Review: Closing the Loop"
    assert result["summary"]["title_" + status] == 1


@pytest.mark.asyncio
async def test_scope_and_skipped_entries(monkeypatch):
    async def handler(request):
        return httpx.Response(200, text="<title>ok</title>")

    mock_verify_client(monkeypatch, handler)
    sources = [{"url": f"https://example.test/{i}"} for i in range(3)] + [
        {"url": "http://127.0.0.1:8080/admin"}, {"url": "http://localhost/x"}, {"url": "ftp://example.test/file"}]
    result = await verify.verify_answer("no identifiers here", sources, check_urls=True, timeout_s=5, max_urls=2)
    assert result["claim_support"] == "not_checked" and "NOT checked" in result["scope"]
    reasons = {item["value"]: item["reason"] for item in result["skipped"]}
    assert reasons["https://example.test/2"] == "over the per-answer limit of 2 URL checks"
    assert "private" in reasons["http://127.0.0.1:8080/admin"] and "private" in reasons["http://localhost/x"]
    assert "scheme" in reasons["ftp://example.test/file"]
    assert result["checked"]["urls"] == 2 and len(result["urls"]) == 2 and result["summary"]["skipped"] == 4


@pytest.mark.asyncio
async def test_disabled_url_checks_are_listed_as_skipped(monkeypatch):
    mock_verify_client(monkeypatch, atom_handler)
    result = await verify.verify_answer("see 2607.06065", [{"url": "https://example.test/x"}], check_urls=False, timeout_s=5)
    assert result["skipped"] == [{"kind": "url", "value": "https://example.test/x", "reason": "URL checks disabled (GROK_VERIFY_URLS=false)"}]


@pytest.mark.asyncio
async def test_redirect_to_a_private_address_is_not_followed(monkeypatch):
    hits = []

    async def handler(request):
        hits.append(request.url.host)
        if request.url.host == "example.test":
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})
        return httpx.Response(200, text="<title>metadata</title>")

    mock_verify_client(monkeypatch, handler)
    result = await verify.verify_answer("x", [{"url": "https://example.test/r"}], check_urls=True, timeout_s=5)
    assert hits == ["example.test"] and result["urls"] == []
    assert result["unresolved"] == [{"kind": "url", "value": "https://example.test/r", "reason": "blocked: redirect to a private or local address"}]
