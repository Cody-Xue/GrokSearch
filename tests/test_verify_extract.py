"""Identifier extraction for the verification layer: DOIs and arXiv IDs exactly as written, wherever they sit."""
import httpx
import pytest

from grok_search import verify


@pytest.fixture(autouse=True)
def _fresh_arxiv_state(monkeypatch):
    monkeypatch.setattr(verify, "ARXIV_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(verify, "ARXIV_RETRY_DELAY_S", 0.0)
    verify._ARXIV_CACHE.clear()
    verify._arxiv_gate.update(loop=None, lock=None, last_start=0.0)


def mock_verify_client(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(verify.httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))


def atom(*entries):
    body = "".join(f"<entry><id>http://arxiv.org/abs/{idv}v1</id><title>{title}</title></entry>" for idv, title in entries)
    return f'<feed xmlns="http://www.w3.org/2005/Atom">{body}</feed>'


@pytest.mark.parametrize("text, expected", [
    ("Huang C, et al. Lancet 2020. DOI: 10.1016/S0140-6736(20)30183-5", ["10.1016/S0140-6736(20)30183-5"]),
    ("doi:10.1016/0022-2836(81)90087-5.", ["10.1016/0022-2836(81)90087-5"]),
    ("(see 10.1016/S0140-6736(20)30183-5)", ["10.1016/S0140-6736(20)30183-5"]),
    ("该论文发表于《Nature》，DOI：10.1038/s41586-021-03819-2。", ["10.1038/s41586-021-03819-2"]),
    ("DOI为10.1038/s41586-021-03819-2，发表于Nature", ["10.1038/s41586-021-03819-2"]),
    ("AlphaFold2 论文（DOI: 10.1038/s41586-021-03819-2）", ["10.1038/s41586-021-03819-2"]),
    ("Tunyasuvunakool et al. 10.1038/s41586-021-03828-1，页 590", ["10.1038/s41586-021-03828-1"]),
    ("https://link.springer.com/content/pdf/10.1007/s11263-015-0816-y.pdf", ["10.1007/s11263-015-0816-y"]),
    ("[preprint](https://www.biorxiv.org/content/10.1101/2020.03.22.002386v1.full)", ["10.1101/2020.03.22.002386"]),
    ("https://www.biorxiv.org/content/10.1101/2020.03.22.002386v2.full.pdf", ["10.1101/2020.03.22.002386"]),
    ("Genome Res. doi:10.1101/gr.275643.121", ["10.1101/gr.275643.121"]),
    ("https://onlinelibrary.wiley.com/doi/full/10.1002/anie.201916522#sec1", ["10.1002/anie.201916522"]),
    ("https://onlinelibrary.wiley.com/doi/10.1002/anie.201916522/full", ["10.1002/anie.201916522"]),
    ("https://pubs.acs.org/doi/10.1021/jacs.0c01234?ref=recommended", ["10.1021/jacs.0c01234"]),
    ("**10.1038/nature14539**", ["10.1038/nature14539"]),
    ("doi `10.1038/nature14539`", ["10.1038/nature14539"]),
    ("10.1002/(SICI)1097-4636(199709)36:3<342::AID-JBM10>3.0.CO;2-H", ["10.1002/(SICI)1097-4636(199709)36:3<342::AID-JBM10>3.0.CO;2-H"]),
    ("https://doi.org/10.48550/arXiv.2211.17192", ["10.48550/arXiv.2211.17192"]),
    ("not DOIs: 210.1234/abc, x10.1234/abc, 10.12/short", []),
])
def test_extract_dois_exactly(text, expected):
    assert verify.extract_dois(text) == expected


def test_parenthesised_dois_stay_distinct_and_case_duplicates_collapse():
    text = "10.1016/S0140-6736(20)30183-5 and 10.1016/S0140-6736(20)30211-7; again 10.1016/s0140-6736(20)30183-5."
    assert verify.extract_dois(text) == ["10.1016/S0140-6736(20)30183-5", "10.1016/S0140-6736(20)30211-7"]


@pytest.mark.parametrize("text, expected", [
    ("Speculative decoding was introduced in arXiv:2211.17192.", ["2211.17192"]),
    ("Attention Is All You Need, arXiv:1706.03762v7.", ["1706.03762"]),
    ("arXiv:2211.17192提出了推测解码", ["2211.17192"]),
    ("该方法见论文2211.17192，", ["2211.17192"]),
    ("（arXiv：2211.17192）", ["2211.17192"]),
    ("PDF: https://arxiv.org/pdf/2211.17192v2.pdf", ["2211.17192"]),
    ("DOI: 10.48550/arXiv.2211.17192", ["2211.17192"]),
    ("Maldacena, hep-th/9711200v3.", ["hep-th/9711200"]),
    ("old style with a subject class: math.AG/0601001", ["math/0601001"]),
    ("arXiv:1409.0473 (Bahdanau) and arXiv:2401.10774", ["1409.0473", "2401.10774"]),
    ("not IDs: 12345.6789, 1.2211.17192, 2211.171923, 2613.12345, 0612.12345, hep-th/1301001, foo/9901001", []),
])
def test_extract_arxiv_ids(text, expected):
    assert verify.extract_arxiv_ids(text) == expected


@pytest.mark.parametrize("url, expected", [
    ("https://arxiv.org/abs/2211.17192", "2211.17192"),
    ("https://arxiv.org/pdf/2211.17192.pdf", "2211.17192"),
    ("https://arxiv.org/pdf/2211.17192v2.pdf", "2211.17192"),
    ("https://www.arxiv.org/abs/2401.10774v1", "2401.10774"),
    ("https://arxiv.org/abs/hep-th/9711200", "hep-th/9711200"),
    ("https://arxiv.org/list/cs.CL/recent", None),
])
def test_arxiv_id_from_url(url, expected):
    assert verify._arxiv_id_from_url(url) == expected


def test_arxiv_id_from_datacite_doi():
    assert verify.arxiv_id_from_doi("10.48550/arXiv.2211.17192") == "2211.17192"
    assert verify.arxiv_id_from_doi("10.48550/ARXIV.HEP-TH/9711200") == "hep-th/9711200"
    assert verify.arxiv_id_from_doi("10.1038/nature14539") is None


@pytest.mark.asyncio
async def test_sentence_final_ids_are_checked_and_a_fabricated_one_is_unresolved(monkeypatch):
    async def handler(request):
        assert request.url.host == "export.arxiv.org"
        assert request.url.params["id_list"] == "1706.03762,2512.99999"
        return httpx.Response(200, text=atom(("1706.03762", "Attention Is All You Need")))

    mock_verify_client(monkeypatch, handler)
    answer = "- Attention Is All You Need: arXiv:1706.03762.\n- A fabricated paper: arXiv:2512.99999."
    result = await verify.verify_answer(answer, [], check_urls=False, timeout_s=5)
    assert [x["id"] for x in result["arxiv"]] == ["1706.03762"]
    assert [(u["kind"], u["value"], u["reason"]) for u in result["unresolved"]] == [("arxiv", "2512.99999", "not found")]
    assert result["checked"]["arxiv"] == 2


@pytest.mark.asyncio
async def test_arxiv_datacite_dois_are_checked_as_arxiv_ids_not_through_crossref(monkeypatch):
    hosts = []

    async def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(200, text=atom(("2211.17192", "Fast Inference from Transformers via Speculative Decoding"),
                                             ("2302.01318", "Accelerating Large Language Model Decoding with Speculative Sampling")))

    mock_verify_client(monkeypatch, handler)
    answer = "DOI: 10.48550/arXiv.2211.17192。"
    sources = [{"url": "https://doi.org/10.48550/arXiv.2302.01318"}]
    result = await verify.verify_answer(answer, sources, check_urls=False, timeout_s=5)
    assert set(hosts) == {"export.arxiv.org"}
    assert [x["id"] for x in result["arxiv"]] == ["2211.17192", "2302.01318"]
    assert result["checked"] == {"arxiv": 2, "doi": 0, "urls": 0} and result["unresolved"] == []


@pytest.mark.asyncio
async def test_cited_arxiv_and_doi_urls_without_an_identifier_are_listed_as_skipped(monkeypatch):
    async def handler(request):
        return httpx.Response(200, text=atom(("2211.17192", "Fast Inference from Transformers via Speculative Decoding")))

    mock_verify_client(monkeypatch, handler)
    sources = [
        {"url": "https://arxiv.org/list/cs.CL/recent"},
        {"url": "https://doi.org/"},
        {"url": "https://arxiv.org/pdf/2211.17192v2.pdf"},
    ]
    result = await verify.verify_answer("no identifiers in the text", sources, check_urls=False, timeout_s=5)
    reasons = {item["value"]: item["reason"] for item in result["skipped"]}
    assert "arxiv.org URL without a recognisable paper ID" in reasons["https://arxiv.org/list/cs.CL/recent"]
    assert "doi.org URL without a recognisable DOI" in reasons["https://doi.org/"]
    assert [x["id"] for x in result["arxiv"]] == ["2211.17192"] and result["checked"]["arxiv"] == 1



# --------------------------------------------------------- DOI patterns written in prose
@pytest.mark.parametrize("text, pattern", [
    ("the 10.48550/arXiv.* form is the standardized DOI assigned by arXiv", "10.48550/arXiv"),
    ("written as 10.48550/arXiv.<id>", "10.48550/arXiv"),
    ("prefix 10.48550/arXiv (DataCite)", "10.48550/arXiv"),
    ("格式为 10.48550/arXiv.{编号}", "10.48550/arXiv"),
    ("10.48550/arXiv.YYMM.NNNNN", "10.48550/arXiv.YYMM.NNNNN"),
    ("Nature papers look like 10.1038/s41586-…", "10.1038/s41586-"),
    ("Zenodo uses 10.5281/zenodo.NNNNNNN", "10.5281/zenodo.NNNNNNN"),
    ("**the 10.48550/arXiv.* form**", "10.48550/arXiv"),
    ("- **PLOS ONE**: 10.1371/journal.pone.* covers it", "10.1371/journal.pone"),
    ("* item: 10.1371/journal.pone.* covers it", "10.1371/journal.pone"),
    ("Nature papers: 10.1038/nature/*", "10.1038/nature"),
    ("Nature papers look like 10.1038/s41586-...", "10.1038/s41586-"),
    ("Zenodo uses 10.5281/zenodo....", "10.5281/zenodo"),
    ("older Nature papers use 10.1038/nature…", "10.1038/nature"),
    ("older Nature papers use 10.1038/nature<NNNNN>", "10.1038/nature"),
])
def test_doi_patterns_are_not_identifiers(text, pattern):
    assert verify.extract_doi_mentions(text) == ([], [pattern])
    assert verify.extract_dois(text) == []


@pytest.mark.parametrize("text, doi", [
    ("**DOI:** **10.1038/nature14539**", "10.1038/nature14539"),
    ("DOI 10.1038/nature14539[[1]](https://doi.org/10.1038/nature14539).", "10.1038/nature14539"),
    ("10.48550/arXiv.2613.12345", "10.48550/arXiv.2613.12345"),  # invalid, but a concrete claim: looked up
    # Markdown, HTML and prose ellipses after a concrete DOI are not template markers.
    ("| ResNet | 10.1109/CVPR.2016.90<br>arXiv:1512.03385 |", "10.1109/CVPR.2016.90"),
    ("| BERT | DOI: 10.18653/v1/N19-1423<br/>NAACL 2019 |", "10.18653/v1/N19-1423"),
    ("| ResNet | 10.1109/CVPR.2016.90.<br />2016 |", "10.1109/CVPR.2016.90"),
    ("**He et al. (2016), DOI 10.1109/CVPR.2016.90.**", "10.1109/CVPR.2016.90"),
    ("*He et al. (2016), DOI 10.1109/CVPR.2016.90.*", "10.1109/CVPR.2016.90"),
    ("***DOI 10.1109/CVPR.2016.90.***", "10.1109/CVPR.2016.90"),
    ("<doi>10.1038/nature14539</doi>", "10.1038/nature14539"),
    ("10.1038/nature14539<sup>[1]</sup>", "10.1038/nature14539"),
    ("10.1038/nature14539<span class='ref'>", "10.1038/nature14539"),
    ("See 10.1038/nature14539...", "10.1038/nature14539"),
    ("See 10.1038/nature14539…", "10.1038/nature14539"),
    ("doi:10.1234/abc/.", "10.1234/abc"),
    # ".*" or "/*" after a suffix with a digit is emphasis or a footnote star, not a wildcard: looked up.
    ("- **Nature**: 10.1038/s41586.* covers it", "10.1038/s41586"),
    ("Nature papers: 10.1038/s41586/*", "10.1038/s41586"),
])
def test_concrete_dois_are_still_identifiers(text, doi):
    assert verify.extract_doi_mentions(text) == ([doi], [])


@pytest.mark.asyncio
async def test_fabricated_dois_next_to_markup_are_looked_up(monkeypatch):
    calls = []

    async def handler(request):
        calls.append(request.url.host)
        if request.url.host == "doi.org":
            return httpx.Response(404, json={"responseCode": 100})
        return httpx.Response(404, text="Resource not found.")

    mock_verify_client(monkeypatch, handler)
    answer = ("| Paper | Identifiers |\n|---|---|\n"
              "| Made-up paper | DOI 10.1109/FAKE.2099.12345<br>2099 |\n"
              "| Other | **Ref: 10.5555/fabricated.2099.**|\n")
    result = await verify.verify_answer(answer, [], check_urls=False, timeout_s=5)
    assert result["skipped"] == []
    assert sorted(item["value"] for item in result["unresolved"]) == ["10.1109/FAKE.2099.12345", "10.5555/fabricated.2099"]
    assert {item["value"]: item["reason"] for item in result["unresolved"]} == {
        "10.1109/FAKE.2099.12345": "not found", "10.5555/fabricated.2099": verify.DOI_TEST_PREFIX_REASON}
    assert "api.crossref.org" in calls


@pytest.mark.asyncio
async def test_doi_patterns_are_skipped_not_unresolved(monkeypatch):
    calls = []

    async def handler(request):
        calls.append(request.url.host)
        return httpx.Response(404)

    mock_verify_client(monkeypatch, handler)
    answer = "arXiv papers also have DOIs: the 10.48550/arXiv.* form is the standardized DOI assigned by arXiv."
    result = await verify.verify_answer(answer, [], check_urls=False, timeout_s=5)
    assert calls == [] and result["unresolved"] == [] and result["lookup_failed"] == []
    assert result["skipped"] == [{"kind": "doi", "value": "10.48550/arXiv",
                                  "reason": "DOI pattern/prefix, not an identifier (not looked up)"}]


# --------------------------------------------------------- hostile DOI-shaped tokens
@pytest.mark.parametrize("text", [
    "10.1101/2020.03.22.002386" + ".fullv1" * 7000,
    "https://doi.org/10.1101/x" + "v1.full" * 7000,
    "10.1000/x" + ".pdf" * 25000,
    "10.1000/x" + "/full" * 20000,
])
def test_long_doi_tails_are_linear(text):
    import time

    started = time.perf_counter()
    verify.extract_dois(text)
    verify._doi_from_url(text)
    assert time.perf_counter() - started < 0.5


def test_many_short_doi_tails_are_linear():
    # Tokens just under DOI_MAX_CHARS made of repeated tails used to cost one pass per tail
    # (about 200 passes each; 1 s for 100 KB, 5 s for 500 KB).
    import time

    text = ("10.1101/x" + "v1" * 195 + " ") * 250 + ("10.1101/x" + ".fullv1" * 55 + " ") * 250
    started = time.perf_counter()
    verify.extract_doi_mentions(text)
    assert time.perf_counter() - started < 0.3


def test_dot_segments_are_not_dois():
    assert verify.extract_dois("10.1234/../../../10.5555/12345678") == []
    assert verify.extract_dois("10.1234/%2e%2e/%2E%2E/10.5555/1") == []
    assert verify.extract_dois("10.1234/./x") == []
    assert verify._doi_from_url("https://doi.org/10.1234/../10.5555/x") is None
    assert verify.extract_dois("10.1101/2020.03.22.002386v1.full") == ["10.1101/2020.03.22.002386"]
