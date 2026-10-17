"""GROK_VERIFY_DETAIL: compact (default) lists only what needs attention plus every count; full is v1.12.0's output."""
import copy
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastmcp import Client

from grok_search import config as config_module
from grok_search import server, verify
from grok_search.config import Config, config
from grok_search.providers.grok import GrokResponse, GrokSearchProvider
from grok_search.sources import SourcesCache

PUBLIC_IP = "93.184.216.34"
CITED = "https://cited.example.test/paper"

FULL = {
    "scope": verify.SCOPE,
    "claim_support": "not_checked",
    "arxiv": [{"id": "1706.03762", "title": "Attention Is All You Need", "published": "2017-06-12", "authors": ["A. Vaswani"]}],
    "doi": [{"doi": "10.1038/nature14539", "title": "Deep learning", "container": "Nature", "type": "journal-article", "issued": "2015-5-27"}],
    "urls": [
        {"url": "https://ok.example.test/a", "status": 200, "title": "A"},
        {"url": "https://waf.example.test/b", "status": 202, "title": ""},
        {"url": "https://pubmed.example.test/c", "status": 203, "title": "pubmed.example.test"},
    ],
    "unresolved": [{"kind": "arxiv", "value": "2612.99999", "reason": "not found"}],
    "lookup_failed": [{"kind": "url", "value": "https://slow.example.test/", "reason": "status 503"}],
    "title_checks": [
        {"kind": "arxiv", "id": "1706.03762", "registry_title": "Attention Is All You Need", "status": "match"},
        {"kind": "doi", "id": "10.1/p", "registry_title": "P", "status": "partial", "answer_title": "Q", "similarity": 0.7},
        {"kind": "doi", "id": "10.1/m", "registry_title": "M", "status": "possible_mismatch", "answer_title": "X", "similarity": 0.2},
        {"kind": "doi", "id": "10.1/n", "registry_title": "N", "status": "not_stated"},
    ],
    "skipped": [{"kind": "url", "value": "ftp://example.test/f", "reason": "unsupported scheme (only http/https are checked)"}],
    "checked": {"arxiv": 2, "doi": 4, "urls": 4},
    "summary": {"identifiers_found": 2, "urls_reachable": 3, "unresolved": 1, "lookup_failed": 1, "skipped": 1,
                "title_match": 1, "title_partial": 1, "title_possible_mismatch": 1, "title_not_stated": 1},
}


def test_compact_scope_is_short_and_keeps_the_not_checked_caveat():
    assert len(verify.SCOPE_COMPACT) <= 120 and "NOT" in verify.SCOPE_COMPACT
    assert len(verify.SCOPE_COMPACT) < len(verify.SCOPE)


def test_compact_keeps_problems_and_counts_and_drops_what_is_fine():
    full = copy.deepcopy(FULL)
    out = verify.compact_verification(full)
    assert full == FULL  # the full result is not modified
    assert list(out) == ["scope", "claim_support", "detail", "unresolved", "lookup_failed", "title_checks",
                         "urls_unusual", "skipped", "checked", "summary"]
    assert (out["scope"], out["claim_support"], out["detail"]) == (verify.SCOPE_COMPACT, "not_checked", "compact")
    # Resolved records, reachable 200 URLs and matching titles are left out entirely, not sent as empty lists.
    assert not {"arxiv", "doi", "urls"} & set(out)
    assert out["unresolved"] == FULL["unresolved"] and out["lookup_failed"] == FULL["lookup_failed"]
    assert [t["status"] for t in out["title_checks"]] == ["partial", "possible_mismatch", "not_stated"]
    assert out["title_checks"] == FULL["title_checks"][1:]  # entry shape unchanged
    # A reachable URL whose final status is not 200 (202 WAF challenge, 203 proof-of-work page) stays visible.
    assert [u["status"] for u in out["urls_unusual"]] == [202, 203]
    assert out["skipped"] == FULL["skipped"] and "skipped_omitted" not in out
    assert out["checked"] == FULL["checked"] and out["summary"] == FULL["summary"]
    assert "timed_out" not in out


def test_compact_of_an_all_fine_result_keeps_stable_keys():
    full = copy.deepcopy(FULL)
    full.update(urls=full["urls"][:1], unresolved=[], lookup_failed=[], title_checks=full["title_checks"][:1], skipped=[],
                summary=dict(FULL["summary"], unresolved=0, lookup_failed=0, skipped=0, title_partial=0,
                             title_possible_mismatch=0, title_not_stated=0))
    out = verify.compact_verification(full)
    assert out["unresolved"] == out["lookup_failed"] == out["title_checks"] == out["skipped"] == []
    assert "urls_unusual" not in out and "skipped_omitted" not in out
    assert out["summary"]["urls_reachable"] == 3 and out["summary"]["title_match"] == 1
    assert len(json.dumps(out, ensure_ascii=False, separators=(",", ":"))) < 600


def test_compact_caps_skipped_specific_reasons_first_patterns_last():
    pattern = {"kind": "doi", "value": "10.48550/arXiv.*", "reason": verify.DOI_PATTERN_REASON}
    over = [{"kind": "url", "value": f"https://example.test/{i}", "reason": "over the per-answer limit of 30 URL checks"}
            for i in range(12)]
    blocked = [{"kind": "url", "value": f"http://10.0.0.{i}/", "reason": f"blocked: 10.0.0.{i} is not a public address (not fetched)"}
               for i in range(12)]
    full = dict(FULL, skipped=over + blocked + [pattern], summary=dict(FULL["summary"], skipped=25))
    out = verify.compact_verification(full)
    # Bulk skips are only counted; specific ones are listed up to the cap, DOI patterns last.
    assert out["skipped_by_reason"] == {"over the per-answer limit of 30 URL checks": 12}
    assert len(out["skipped"]) == verify.COMPACT_MAX_SKIPPED == 10 and out["skipped"] == blocked[:10]
    assert pattern not in out["skipped"] and out["skipped_omitted"] == 3
    # The count of omitted entries includes those the full listing had already capped (summary.skipped).
    out = verify.compact_verification(dict(full, summary=dict(FULL["summary"], skipped=80)))
    assert out["skipped_omitted"] == 58
    assert verify.compact_verification(full, max_skipped=20)["skipped"][-1] == pattern
    out = verify.compact_verification(dict(full, skipped=over, summary=dict(FULL["summary"], skipped=12)))
    assert out["skipped"] == [] and "skipped_omitted" not in out


def test_compact_groups_lookup_failures_of_one_host_and_reason():
    """platform="Stack Overflow": 14 cited questions, all "status 403 (bot challenge)" (2,877 characters)."""
    so = [{"kind": "url", "value": f"https://stackoverflow.com/questions/{i}/q", "reason": "status 403 (bot challenge)"} for i in range(14)]
    zhihu = {"kind": "url", "value": "https://www.zhihu.com/question/1", "reason": "status 403 (access denied)"}
    slow = {"kind": "url", "value": "https://stackoverflow.com/questions/99/q", "reason": "status 503"}
    arxiv = {"kind": "arxiv", "value": "2401.00001", "reason": "timed out"}
    full = dict(FULL, lookup_failed=[so[0], zhihu, arxiv] + so[1:] + [slow], summary=dict(FULL["summary"], lookup_failed=17))
    out = verify.compact_verification(full)
    assert out["lookup_failed"] == [
        {"kind": "url", "host": "stackoverflow.com", "reason": "status 403 (bot challenge)", "count": 14,
         "sample": ["https://stackoverflow.com/questions/0/q", "https://stackoverflow.com/questions/1/q"]},
        zhihu, arxiv, slow,
    ]
    assert out["summary"]["lookup_failed"] == 17
    assert len(json.dumps(out["lookup_failed"], ensure_ascii=False)) < 600
    # full keeps every entry
    assert full["lookup_failed"][0] == so[0] and len(full["lookup_failed"]) == 17


def test_compact_passes_errors_through_and_tolerates_partial_results():
    error = {"error": "RuntimeError: boom", "claim_support": "not_checked"}
    assert verify.compact_verification(error) is error
    assert verify.compact_verification(None) is None
    out = verify.compact_verification({"summary": {"lookup_failed": 1}, "lookup_failed": [{"url": CITED, "reason": "HTTP 503"}],
                                       "timed_out": True})
    assert out["lookup_failed"] == [{"url": CITED, "reason": "HTTP 503"}] and out["timed_out"] is True
    assert out["unresolved"] == out["title_checks"] == out["skipped"] == [] and "checked" not in out
    assert out["summary"] == {"lookup_failed": 1} and out["claim_support"] == "not_checked"


# ------------------------------------------------------------------ on a real verify_answer result
ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><id>http://arxiv.org/abs/2607.06065v1</id><title>SWE-Review: Closing the Loop</title>
  <published>2026-07-07T00:00:00Z</published><author><name>Ruoyu Wang</name></author></entry>
  <entry><id>http://arxiv.org/api/errors#incorrect_id</id><title>Error</title></entry>
</feed>"""


@pytest.fixture
def mocked_network(monkeypatch):
    monkeypatch.setattr(verify, "ARXIV_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(verify, "ARXIV_RETRY_DELAY_S", 0.0)
    verify._ARXIV_CACHE.clear()
    verify._arxiv_gate.update(loop=None, lock=None, last_start=0.0)
    verify._crossref_gate.update(loop=None)

    async def resolve(host, port):
        return [PUBLIC_IP]

    async def handler(request):
        host, path = request.url.host, request.url.path
        if host == "export.arxiv.org":
            return httpx.Response(200, text=ATOM)
        if host == "site.example.test":
            if path == "/ok":
                return httpx.Response(200, text="<title>Fine page</title>")
            if path == "/waf":
                return httpx.Response(202, headers={"x-amzn-waf-action": "challenge"})
            if path == "/busy":
                return httpx.Response(503)
            return httpx.Response(404)
        return httpx.Response(500)

    real = httpx.AsyncClient
    monkeypatch.setattr(verify, "resolve_host", resolve)
    monkeypatch.setattr(verify.httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))


@pytest.mark.asyncio
async def test_compact_of_a_real_verification(mocked_network):
    answer = "SWE-Review: Closing the Loop (arXiv:2607.06065); also arXiv:2612.99999."
    sources = [{"url": f"https://site.example.test/{p}"} for p in ("ok", "waf", "busy", "dead")] + [{"url": "ftp://x.test/f"}]
    full = await verify.verify_answer(answer, sources, check_urls=True, timeout_s=5)
    assert "detail" not in full and full["scope"] == verify.SCOPE  # full mode stays as in v1.12.0
    assert [t["status"] for t in full["title_checks"]] == ["match"]
    out = verify.compact_verification(full)
    assert {(u["kind"], u["value"]) for u in out["unresolved"]} == {("arxiv", "2612.99999"), ("url", "https://site.example.test/dead")}
    assert [u["value"] for u in out["lookup_failed"]] == ["https://site.example.test/busy"]
    assert out["title_checks"] == []
    assert out["urls_unusual"] == [{"url": "https://site.example.test/waf", "status": 202, "title": ""}]
    assert [s["value"] for s in out["skipped"]] == ["ftp://x.test/f"]
    assert out["summary"] == full["summary"] and out["summary"]["urls_reachable"] == 2
    assert out["summary"]["identifiers_found"] == 1 and out["summary"]["title_match"] == 1
    compact_len = len(json.dumps(out, separators=(",", ":")))
    assert compact_len < len(json.dumps(full, separators=(",", ":"))) - 400


# ------------------------------------------------------------------ setting and call site
def test_verify_detail_setting(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Config, "_instance", None)
    monkeypatch.setattr(config_module, "_ENV_WARNINGS", {})
    monkeypatch.delenv("GROK_VERIFY_DETAIL", raising=False)
    cfg = Config()
    assert cfg.verify_detail == "compact"
    monkeypatch.setenv("GROK_VERIFY_DETAIL", " FULL ")
    assert cfg.verify_detail == "full"
    monkeypatch.setenv("GROK_VERIFY_DETAIL", "verbose")
    info = cfg.get_config_info()
    assert info["GROK_VERIFY_DETAIL"] == "compact"  # unknown value: default, with a warning
    assert any(w.startswith("GROK_VERIFY_DETAIL='verbose'") for w in info["config_warnings"])
    monkeypatch.setenv("GROK_VERIFY_DETAIL", "compact")
    assert cfg.verify_detail == "compact"
    assert not any(w.startswith("GROK_VERIFY_DETAIL") for w in cfg.config_warnings())


@pytest.fixture
def search_with_verification(monkeypatch):
    monkeypatch.setenv("GROK_API_URL", "https://api.example.test/v1")
    monkeypatch.setenv("GROK_API_KEY", "test-key")
    monkeypatch.setenv("GROK_MODEL", "test-model")
    monkeypatch.setenv("GROK_VERIFY_IDS", "true")
    monkeypatch.setenv("GROK_VERIFY_URLS", "true")
    for key in ("GUDA_API_KEY", "TAVILY_API_KEY", "FIRECRAWL_API_KEY", "GROK_VERIFY_DETAIL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(config_module, "_ENV_WARNINGS", {})
    monkeypatch.setattr(server.config, "_cached_model", None)
    monkeypatch.setattr(server, "_SOURCES_CACHE", SourcesCache())
    answer = GrokResponse(f"see arXiv:1706.03762 [[1]]({CITED})", [{"type": "url_citation", "url": CITED, "title": "1"}])
    monkeypatch.setattr(GrokSearchProvider, "search_with_sources", AsyncMock(return_value=answer))
    fake = AsyncMock(side_effect=lambda *a, **k: copy.deepcopy(FULL))
    monkeypatch.setattr(server, "verify_answer", fake)
    return fake


async def search_text(arguments=None) -> str:
    async with Client(server.mcp) as client:
        result = await client.call_tool("web_search", {"query": "q", **(arguments or {})})
    assert not result.is_error
    return next(block.text for block in result.content if block.type == "text")


@pytest.mark.asyncio
async def test_web_search_attaches_compact_verification_by_default(search_with_verification):
    verification = json.loads(await search_text())["verification"]
    assert verification == json.loads(json.dumps(verify.compact_verification(copy.deepcopy(FULL))))
    assert verification["detail"] == "compact" and "urls" not in verification


@pytest.mark.asyncio
@pytest.mark.parametrize("value, detail", [("full", "full"), ("compact", "compact"), ("bogus", "compact")])
async def test_web_search_honours_grok_verify_detail(search_with_verification, monkeypatch, value, detail):
    monkeypatch.setenv("GROK_VERIFY_DETAIL", value)
    text = await search_text()
    verification = json.loads(text)["verification"]
    if detail == "full":
        # Byte-equal to verify_answer's result as the client receives it: no detail key, nothing dropped.
        assert verification == FULL
        assert '"verification":' + json.dumps(FULL, ensure_ascii=False, separators=(",", ":")) in text
    else:
        assert verification["detail"] == "compact" and verification["scope"] == verify.SCOPE_COMPACT
    if value == "bogus":
        assert any(w.startswith("GROK_VERIFY_DETAIL='bogus'") for w in config.config_warnings())


@pytest.mark.asyncio
async def test_verification_error_is_reported_in_both_modes(search_with_verification, monkeypatch):
    search_with_verification.side_effect = RuntimeError("boom")
    for value in ("compact", "full"):
        monkeypatch.setenv("GROK_VERIFY_DETAIL", value)
        verification = json.loads(await search_text())["verification"]
        assert verification == {"error": "RuntimeError: boom", "claim_support": "not_checked"}


@pytest.mark.asyncio
async def test_description_documents_compact_and_full():
    async with Client(server.mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    description = " ".join(tools["web_search"].description.split())
    assert "GROK_VERIFY_DETAIL=compact" in description and "GROK_VERIFY_DETAIL=full" in description
    assert "urls_unusual" in description and "skipped_omitted" in description
    assert "GROK_VERIFY_DETAIL=full" in tools["web_search"].input_schema["properties"]["verify"]["description"]
