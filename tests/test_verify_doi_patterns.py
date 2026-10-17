"""DOI mentions are a DOI, a template (pattern) or skipped with a reason; nothing DOI-shaped is dropped silently.

A fabricated DOI written in any ordinary prose, markdown, HTML, table or Chinese form must be looked up and reach
unresolved. A mention is a pattern only when the template is explicit; when in doubt it is a DOI.
"""
import time

import httpx
import pytest

from grok_search import verify

F = "10.1109/FAKE.2099.12345"
SHORT_ID = lambda value: value[:48]  # noqa: E731 - test IDs for long inputs
REF = ("Zhang, W., Li, X., Wang, Y., Chen, H., Liu, Q., & Zhao, M. (2023). A comprehensive survey of retrieval-augmented "
       "generation for large language models in knowledge-intensive tasks. IEEE Transactions on Pattern Analysis and "
       "Machine Intelligence, 45(7), 1234-1250. https://doi.org/")
SICI_HASH = "10.1002/(SICI)1099-1263(199705)7:2<143::AID-ASL56>3.0.CO;2-#"
# SICI DOIs with a single ":" in <...> (AMS) or in [...] (ESA, BioOne).
AMS = "10.1175/1520-0477(1996)077<0437:TNYRP>2.0.CO;2"
BIOONE = "10.1641/0006-3568(2001)051[0933:TEOTWA]2.0.CO;2"
ESA = "10.1890/0012-9658(2002)083[1421:SDEOLC]2.0.CO;2"
ALPHAFOLD = "10.1038/s41586-021-03819-2"
BIORXIV = "10.1101/2020.03.22.002386"


@pytest.fixture(autouse=True)
def _fresh_registry_state(monkeypatch):
    monkeypatch.setattr(verify, "ARXIV_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(verify, "ARXIV_RETRY_DELAY_S", 0.0)
    monkeypatch.setattr(verify, "CROSSREF_MIN_INTERVAL_S", 0.0)
    verify._ARXIV_CACHE.clear()
    verify._arxiv_gate.update(loop=None, lock=None, last_start=0.0)
    verify._crossref_gate.update(loop=None)


def mock_verify_client(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(verify.httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))


def registries_know_nothing(calls):
    async def handler(request):
        calls.append(request.url)
        if request.url.host == "doi.org":
            return httpx.Response(404, json={"responseCode": 100})
        if request.url.host == "export.arxiv.org":
            return httpx.Response(200, text='<feed xmlns="http://www.w3.org/2005/Atom"></feed>')
        return httpx.Response(404, text="Resource not found.")
    return handler


# ------------------------------------------------------------------ concrete DOIs
@pytest.mark.parametrize("text, doi", [
    # A long tracking query or text fragment is cut off before the length cap applies.
    (f"https://doi.org/{F}?utm_source=chatgpt.com&casa_token=" + "Ab3_dE-f" * 50, F),
    (f"Paper: https://doi.org/{F}?utm_source=chatgpt.com&utm_medium=referral&utm_campaign=" + "x" * 380, F),
    (f"https://doi.org/{F}#:~:text=" + "%20".join(["the", "quick", "brown", "fox"] * 25), F),
    (f"https://onlinelibrary.wiley.com/doi/full/{F}?casa_token=" + "Ab3_dE-f" * 50, F),
    (f"https://doi.org/{F}?q=" + "x" * 20000, F),
    # Prose ellipses of any length after a sentence-final period.
    (f"See DOI {F}....", F),
    (f"相关工作见 {F}......", F),
    (f"See DOI {F}.…", F),
    (f"参考 {F}……", F),
    (f"DOI {F}…", F),
    (f"DOI {F}...", F),
    # Italic emphasis opened far back or on an earlier line, and footnote stars.
    (f"*{REF}{F}.*", F),
    (f"*Zhang et al. (2023). A survey.\nIEEE TPAMI. https://doi.org/{F}.*", F),
    (f"- *{REF}{F}.*", F),
    (f"**{REF}{F}.**", F),
    (f"Results: 5*3 = 15 groups; see {F}.*", F),
    (f"{F}.*", F),
    # HTML after a sentence-final period, and word-break hints inside the DOI.
    (f"{F}.<h3>Next</h3>", F),
    (f"{F}.<!-- note -->", F),
    (f"{F}.<ref>", F),
    (f"{F}.<Ref name=x>", F),
    ("10.1109/FAKE.<wbr>2099.12345", F),
    ("10.1109/FAKE.<wbr/>2099.12345", F),
    ("10.1109/FAKE.&shy;2099.12345", F),
    ("10.1109/FAKE.­2099.12345", F),
    # Italic underscores, markdown-escaped underscores, HTML entities.
    (f"_{F}_", F),
    (f"__{F}__", F),
    (f"\\_{F}\\_", F),
    (f"_DOI {F}_…", F),
    ("10.1007/978-3-319-24277-4\\_9", "10.1007/978-3-319-24277-4_9"),
    (f"{F}&nbsp;next", F),
    (f"{F}&#160;next", F),
    (f"{F}&lt;br&gt;", F),
    (f"{F}.{{", F),
    # SICI DOIs whose check character is "#".
    (SICI_HASH, SICI_HASH),
    (SICI_HASH + ".", SICI_HASH),
    ("https://onlinelibrary.wiley.com/doi/10.1002/(SICI)1099-1263(199705)7:2%3C143::AID-ASL56%3E3.0.CO;2-%23", SICI_HASH),
    # Formatting that already worked and must keep working.
    (f"| {F}<br> |", F),
    (f"<td>{F}</td>", F),
    (f"**DOI: {F}**", F),
    (f"**DOI: {F}.**", F),
    (f"*DOI: {F}*", F),
    (f"*DOI: {F}.*", F),
    (f"_DOI: {F}_", F),
    (f"(see {F})", F),
    (f"（见 {F}）", F),
    (f"DOI为{F}。", F),
    (f"DOI为{F}，以及", F),
    (f"[{F}]", F),
    (f"{F}[1]", F),
    (f"|{F}|", F),
    (f"[paper](https://doi.org/{F})", F),
    (f"[{F}](https://doi.org/{F})", F),
    (f"`{F}`", F),
    ("doi:10.1016/S0140-6736(20)30183-5.", "10.1016/S0140-6736(20)30183-5"),
    ("doi:10.1234/abc/.", "10.1234/abc"),
    # Letters that are not a template: single letters ("nmd") or only two ("XX").
    ("10.1016/j.nmd.2020.01.001", "10.1016/j.nmd.2020.01.001"),
    ("10.1016/j.ydbio.2020.01.001", "10.1016/j.ydbio.2020.01.001"),
    ("10.1145/3XX.12", "10.1145/3XX.12"),
    # A single "?" ends a question, it is not a placeholder.
    (f"Was it {F}?", F),
    (f"Was it {F}??", F),
    # Three equal letters other than X are a journal code, not a placeholder (IEEE Microwave Magazine).
    ("Smith (2023), IEEE Microwave Magazine, doi: 10.1109/MMM.2023.1234567.", "10.1109/MMM.2023.1234567"),
    ("10.1109/MMM.2014.2356092", "10.1109/MMM.2014.2356092"),
    ("10.5555/nnn.123", "10.5555/nnn.123"),
    # "/*" after a suffix with a digit closes emphasis (opened on an earlier line or far back) or marks a footnote.
    (f"*Zhang et al. (2023). A survey.\nIEEE TPAMI. https://doi.org/{F}/*", F),
    (f"*{REF}{F}/*", F),
    (f"https://doi.org/{F}/* (preprint)", F),
    # Zero-width spaces, word joiners and BOMs inside a DOI, as characters or HTML entities.
    ("10.1038/s41586-\u200b099-12345-6", "10.1038/s41586-099-12345-6"),
    ("10.1038/s41586-&#8203;099-12345-6", "10.1038/s41586-099-12345-6"),
    ("10.1038/s41586-\u2060099-12345-6", "10.1038/s41586-099-12345-6"),
    ("10.1038/s41586-\ufeff099-12345-6", "10.1038/s41586-099-12345-6"),
    ("10.1109/FAKE.\u200b2099.12345", F),
    ("10.1109/FAKE.&#x200b;2099.12345", F),
    ("10.1109/FAKE.&ZeroWidthSpace;2099.12345", F),
    (f"doi\u200b{F}", F),  # one before a DOI still separates it from the word
    # A "-" next to the DOI: HTML comments, dashes, "DOI-", arrows.
    (f"<!--{F}-->", F),
    (f"see--{F}", F),
    (f"DOI-{F}", F),
    (f"{F}->Crossref", F),
    (f"DOI: {F}-->", F),
    # A percent-encoded "/" after the prefix.
    ("https://doi.org/10.1109%2FFAKE.2099.12345", F),
    ("https://doi.org/10.1109%2fFAKE.2099.12345", F),
    ("https://doi.org/10.1109%252FFAKE.2099.12345", F),
    ("https://www.google.com/url?sa=t&url=https%3A%2F%2Fdoi.org%2F10.1109%252FFAKE.2099.12345&usg=x", F),
    # SICI DOIs with a single ":" in <...> or [...]; citation markers and closing tags stay outside.
    (f"Kalnay et al. (1996), doi:{AMS}", AMS),
    (f"https://doi.org/{BIOONE}", BIOONE),
    (ESA, ESA),
    (f"{ESA}.", ESA),
    (f"[{ESA}](https://doi.org/{ESA})", ESA),
    (f"{F}[^1]", F),
    (f"<dc:identifier>{F}</dc:identifier>", F),
    (f"{F}<https://doi.org/{F}>", F),
    # A DOI inside a percent-encoded query value: a redirect, search or proxy link. A literal "&" starts the next
    # parameter, a "+" is a space, and an encoded "?" or space ends the DOI as in plain text.
    ("https://www.google.com/url?sa=t&url=https%3A%2F%2Fdoi.org%2F10.1109%2FFAKE.2099.12345", F),
    ("https://scholar.google.com/scholar?q=doi%3A10.1109%2FFAKE.2099.12345", F),
    ("https://login.ezproxy.lib.example.edu/login?url=https%3A%2F%2Fdoi.org%2F10.1109/FAKE.2099.12345", F),
    ("https://www.google.com/url?sa=t&url=https%3A%2F%2Fdoi.org%2F10.1109%2FFAKE.2099.12345&ved=2ahUKEwi&usg=AOvVaw3x", F),
    ("https://scholar.google.com/scholar?q=doi%3A10.1109%2FFAKE.2099.12345+survey&hl=en", F),
    ("https://www.google.com/url?url=https%3A%2F%2Fdoi.org%2F10.1109%2FFAKE.2099.12345%3Futm_source%3Dchatgpt.com&usg=x", F),
    ("https://scholar.google.com/scholar?q=doi%3A10.1109%2FFAKE.2099.12345%20survey", F),
    ("https://example.com/page#:~:text=DOI%2010.1109%2FFAKE.2099.12345", F),
    ("https://www.google.com/url?url=https%3A%2F%2Fdoi.org%2F10.1016%2FS0140-6736(20)30183-5&usg=x",
     "10.1016/S0140-6736(20)30183-5"),
    ("https://www.google.com/url?url=https%3A%2F%2Fdoi.org%2F10.1002%2F(SICI)1099-1263(199705)7%3A2%3C143%3A%3AAID-ASL56"
     "%3E3.0.CO%3B2-%2523&usg=x", SICI_HASH),
    ("https://www.google.com/url?url=https%3A%2F%2Fdoi.org%2F10.1038%2Fs41586%E2%80%91021%E2%80%91038%E2%80%8B19-2&usg=x",
     ALPHAFOLD),
    # A "DOI" label without a separator, and a "." directly before the prefix (a sentence end, an ellipsis).
    ("Zhang W. RAG survey. IEEE TPAMI, 2023, 45(7): 1234-1250. DOI10.1109/FAKE.2099.12345", F),
    ("doi10.1109/FAKE.2099.12345", F),
    ("IEEE TPAMI, 45(7), 1234-1250.10.1109/FAKE.2099.12345", F),
    ("See also...10.1109/FAKE.2099.12345", F),
    ("1.10.1109/FAKE.2099.12345", F),
    # U+2010 hyphens and U+2011 non-breaking hyphens inside a DOI are "-"; one before the prefix separates it.
    ("Nature 论文（DOI: 10.1038/s41586\u2011099\u201112345\u20116）", "10.1038/s41586-099-12345-6"),
    ("DOI: 10.1038/s41586\u2010021\u201003819\u20102", ALPHAFOLD),
    (f"see\u2010{F}", F),
    # A run of two or more trailing "-" is an ASCII dash, not a cut at a separator.
    ("The key paper (10.1109/FAKE.2099.12345-- see also) shows", F),
    (f"{F}--- see also", F),
    (f"{F}--)", F),
    # A single stray hyphen after a DOI with several segments is punctuation, ASCII or Unicode.
    (f"DOI: {F}- (IEEE)", F),
    (f"DOI: {F}‐ (IEEE)", F),
    (f"DOI: {F}‑", F),
    ("参见 10.1038/s41586‑099‑12345‑6‑ 一文", "10.1038/s41586-099-12345-6"),
    # The bioRxiv/medRxiv ".full.pdf+html" view.
    (f"Preprint: https://www.biorxiv.org/content/{BIORXIV}v1.full.pdf+html", BIORXIV),
    (f"https://www.medrxiv.org/content/{BIORXIV}v2.full.pdf+html?versioned=true", BIORXIV),
], ids=SHORT_ID)
def test_concrete_dois_in_any_written_form_are_identifiers(text, doi):
    assert verify.scan_doi_mentions(text) == ([doi], [], [])


@pytest.mark.parametrize("text", [
    "https://example.com/export?a=%2F10.1234%2Fa1&b=%2F10.5678%2Fb2+c&d=doi%3A10.9999%2Fc3&e=1",
    "https://scholar.google.com/scholar?q=doi%3A10.1234%2Fa1%20doi%3A10.5678%2Fb2%20doi%3A10.9999%2Fc3",
    "https://example.com/export?dois=%2010.1234%2Fa1%2C10.5678%2Fb2%3B10.9999%2Fc3",
])
def test_every_doi_in_a_query_string_is_found(text):
    assert verify.scan_doi_mentions(text) == (["10.1234/a1", "10.5678/b2", "10.9999/c3"], [], [])


def test_a_query_value_doi_whose_decoded_text_is_no_doi_is_kept_as_written():
    assert verify.scan_doi_mentions("q=%2010.1234%2F%20x") == (["10.1234/%20x"], [], [])


def test_dot_segments_in_a_query_value_doi_are_skipped():
    text = "https://www.google.com/url?url=https%3A%2F%2Fdoi.org%2F10.1234%2F..%2F..%2F10.5555%2Fx&usg=x"
    assert verify.scan_doi_mentions(text) == ([], [], [("10.1234/../../10.5555/x", verify.DOI_DOT_SEGMENT_REASON)])


@pytest.mark.parametrize("text, pattern", [
    ("10.1038/s41586-", "10.1038/s41586-"),
    ("10.1038/s41586\u2010", "10.1038/s41586-"),
    ("10.1038/s41586\u2011 is the prefix", "10.1038/s41586-"),
    ("10.1038/s41586-\u2026", "10.1038/s41586-"),
])
def test_a_single_trailing_hyphen_stays_a_template(text, pattern):
    assert verify.scan_doi_mentions(text) == ([], [pattern], [])


@pytest.mark.parametrize("text", [f"file_{F}", f"name-{F}", f"2{F}", f"x{F}"])
def test_name_glued_to_a_doi_with_an_underscore_or_a_hyphen_is_not_a_doi(text):
    assert verify.scan_doi_mentions(text) == ([], [], [])


def test_doi_org_url_with_a_journal_code_of_three_equal_letters_is_a_doi():
    assert verify._doi_from_url("https://doi.org/10.1109/MMM.2023.1234567") == "10.1109/MMM.2023.1234567"


@pytest.mark.parametrize("text, dois", [
    ("10.1234/abc1,10.1234/abc2", ["10.1234/abc1", "10.1234/abc2"]),
    ("10.1234/abc1;10.5678/def2", ["10.1234/abc1", "10.5678/def2"]),
    ("doi:10.1234/abc1;doi:10.5678/def2", ["10.1234/abc1", "10.5678/def2"]),
    ("10.1234/abc1,https://doi.org/10.5678/def2", ["10.1234/abc1", "10.5678/def2"]),
    ("10.1002/(SICI)1097-4636(199706)35:4<456::AID-JBM6>3.0.CO;2-L,10.1234/abc1",
     ["10.1002/(SICI)1097-4636(199706)35:4<456::AID-JBM6>3.0.CO;2-L", "10.1234/abc1"]),
])
def test_dois_joined_by_a_comma_or_semicolon_are_separate(text, dois):
    assert verify.scan_doi_mentions(text) == (dois, [], [])


# ------------------------------------------------------------------ templates
@pytest.mark.parametrize("text, pattern", [
    ("ACM DOIs look like 10.1145/3XXXXXX.3XXXXXX", "10.1145/3XXXXXX.3XXXXXX"),
    ("Nature DOIs look like 10.1038/s41586-yyy-nnnnn-n", "10.1038/s41586-yyy-nnnnn-n"),
    ("Cell DOIs: 10.1016/j.cell.2020.XX.XXX", "10.1016/j.cell.2020.XX.XXX"),
    ("Lancet DOIs: 10.1016/S0140-6736(XX)XXXXX-X", "10.1016/S0140-6736(XX)XXXXX-X"),
    ("dated: 10.5555/report.YYYYMMDD", "10.5555/report.YYYYMMDD"),
    ("monthly: 10.5555/report.YYMM", "10.5555/report.YYMM"),
    ("IEEE DOIs look like 10.1109/TPAMI.YYYY.NNNNNNN", "10.1109/TPAMI.YYYY.NNNNNNN"),
    ("Cell DOIs: 10.1016/j.cell.2020.01.XXX", "10.1016/j.cell.2020.01.XXX"),
    ("arXiv DOIs: 10.48550/arXiv.2301.?????", "10.48550/arXiv.2301"),
    ("arXiv DOIs: 10.48550/arXiv.2301.#####", "10.48550/arXiv.2301"),
    ("Nature DOIs: 10.1038/s41586-0??-?????-?", "10.1038/s41586-0"),
    ("PLOS ONE DOIs look like 10.1371/journal.pone.*", "10.1371/journal.pone"),
    ("older Nature DOIs: 10.1038/nature/*", "10.1038/nature"),
    ("Zenodo uses 10.5281/zenodo.{id}", "10.5281/zenodo"),
    (f"the form {F}.<id>", F),
    ("prefix only: 10.1038/*", "10.1038/"),
])
def test_explicit_templates_are_patterns(text, pattern):
    assert verify.scan_doi_mentions(text) == ([], [pattern], [])
    assert verify.extract_dois(text) == []


@pytest.mark.parametrize("text, doi", [
    (f"Supplements: {F}.* ; the article: {F}.", F),
    ("Zenodo uses 10.5281/zenodo…; this record is 10.5281/zenodo.", "10.5281/zenodo"),
    ("This record is 10.5281/zenodo; Zenodo uses 10.5281/zenodo…", "10.5281/zenodo"),
])
def test_a_doi_also_written_as_a_pattern_is_a_doi(text, doi):
    assert verify.scan_doi_mentions(text) == ([doi], [], [])


# ------------------------------------------------------------------ skipped with a reason
@pytest.mark.parametrize("text, value, reason", [
    ("10.1234/" + "a" * 500, "10.1234/" + "a" * 72 + "…", verify.DOI_TOO_LONG_REASON),
    (f"https://doi.org/{F}" + "x" * 400 + "?utm_source=chatgpt.com", (F + "x" * 400)[:80] + "…", verify.DOI_TOO_LONG_REASON),
    ("10.1234/../../../10.5555/12345678", "10.1234/../../../10.5555/12345678", verify.DOI_DOT_SEGMENT_REASON),
    ("10.1234/%2e%2e/%2E%2E/10.5555/1", "10.1234/../../10.5555/1", verify.DOI_DOT_SEGMENT_REASON),
    ("10.1234/./x", "10.1234/./x", verify.DOI_DOT_SEGMENT_REASON),
    ("https://doi.org/10.1234%2F..%2F..%2F10.5555%2Fx", "10.1234/../../10.5555/x", verify.DOI_DOT_SEGMENT_REASON),
], ids=SHORT_ID)
def test_doi_shaped_text_that_is_not_looked_up_is_listed_with_a_reason(text, value, reason):
    assert verify.scan_doi_mentions(text) == ([], [], [(value, reason)])
    assert verify.extract_doi_mentions(text) == ([], [])


# ------------------------------------------------------------------ end to end
@pytest.mark.asyncio
async def test_fabricated_dois_in_every_written_form_reach_unresolved(monkeypatch):
    fakes = [f"10.1109/FAKE.2099.{10000 + i}" for i in range(14)]
    answer = "\n".join([
        f"1. https://doi.org/{fakes[0]}?utm_source=chatgpt.com&casa_token=" + "Ab3_dE-f" * 50,
        f"2. https://doi.org/{fakes[1]}#:~:text=" + "%20".join(["the", "quick", "brown", "fox"] * 25),
        f"3. See DOI {fakes[2]}....",
        f"4. 相关工作见 {fakes[3]}......",
        f"5. See DOI {fakes[4]}.…",
        f"6. *{REF}{fakes[5]}.*",
        f"7. *Zhang et al. (2023). A survey.\nIEEE TPAMI. https://doi.org/{fakes[6]}.*",
        f"8. Supplements: {fakes[7]}.* ; the article: {fakes[7]}.",
        f"9. {fakes[8]}.<h3>Next</h3>",
        f"10. {fakes[9]}.<!-- note -->",
        f"11. _{fakes[10]}_",
        f"12. {fakes[11]}&nbsp;next",
        f"13. {fakes[12]},{fakes[13]}",
        f"14. 10.1109/FAKE.<wbr>2099.99999 and {SICI_HASH}",
    ])
    calls = []
    mock_verify_client(monkeypatch, registries_know_nothing(calls))
    result = await verify.verify_answer(answer, [], check_urls=False, timeout_s=10)
    expected = sorted(fakes + ["10.1109/FAKE.2099.99999", SICI_HASH])
    assert sorted(x["value"] for x in result["unresolved"]) == expected
    assert {x["reason"] for x in result["unresolved"]} == {"not found"}
    assert result["skipped"] == [] and result["lookup_failed"] == []
    assert result["checked"]["doi"] == len(expected)
    assert {url.host for url in calls} <= {"api.crossref.org", "doi.org", "api.datacite.org"}
    assert any(url.raw_path.endswith(b"CO;2-%23") for url in calls)


@pytest.mark.asyncio
async def test_fabricated_dois_with_dashes_word_breaks_encoded_slashes_or_trailing_slash_stars_reach_unresolved(monkeypatch):
    fakes = [f"10.1109/FAKE.2099.{20000 + i}" for i in range(12)]
    answer = "\n".join([
        "1. Smith (2023), IEEE Microwave Magazine, doi: 10.1109/MMM.2099.1234567.",
        f"2. *Zhang et al. (2023). A survey.\nIEEE TPAMI. https://doi.org/{fakes[0]}/*",
        f"3. *{REF}{fakes[1]}/*",
        f"4. https://doi.org/{fakes[2]}/* (preprint)",
        "5. 10.1038/s41586-\u200b099-12345-6",
        "6. 10.1038/s41586-&#8203;099-12345-7 and 10.1038/s41586-\u2060099-12345-8, 10.1038/s41586-\ufeff099-12345-9",
        "7. 10.1109/FAKE.\u200b2099.99999",
        f"8. <!--{fakes[3]}--> see--{fakes[4]} DOI-{fakes[5]}",
        f"9. {fakes[6]}->Crossref; DOI: {fakes[7]}-->",
        f"10. https://doi.org/{fakes[8].replace('/', '%2F', 1)} and https://doi.org/{fakes[9].replace('/', '%2f', 1)}",
        f"11. {fakes[10]}[^1] <dc:identifier>{fakes[11]}</dc:identifier>",
    ])
    sources = [{"url": "https://doi.org/10.1109/MMM.2099.7654321"}]
    calls = []
    mock_verify_client(monkeypatch, registries_know_nothing(calls))
    result = await verify.verify_answer(answer, sources, check_urls=False, timeout_s=10)
    expected = sorted(fakes + ["10.1109/MMM.2099.1234567", "10.1109/MMM.2099.7654321", "10.1109/FAKE.2099.99999"]
                      + [f"10.1038/s41586-099-12345-{n}" for n in (6, 7, 8, 9)])
    assert sorted(x["value"] for x in result["unresolved"]) == expected
    assert {x["reason"] for x in result["unresolved"]} == {"not found"}
    assert result["skipped"] == [] and result["lookup_failed"] == []
    assert result["checked"]["doi"] == len(expected)
    assert {url.host for url in calls} <= {"api.crossref.org", "doi.org", "api.datacite.org"}


@pytest.mark.asyncio
async def test_fabricated_dois_in_encoded_links_glued_or_with_unicode_hyphens_or_dashes_reach_unresolved(monkeypatch):
    fakes = [f"10.1109/FAKE.2099.{30000 + i}" for i in range(10)]
    enc = [fake.replace("/", "%2F") for fake in fakes]
    answer = "\n".join([
        f"1. Source: https://www.google.com/url?sa=t&url=https%3A%2F%2Fdoi.org%2F{enc[0]}",
        f"2. https://scholar.google.com/scholar?q=doi%3A{enc[1]}",
        f"3. https://login.ezproxy.lib.example.edu/login?url=https%3A%2F%2Fdoi.org%2F{fakes[2]}",
        f"4. Zhang W. RAG survey. IEEE TPAMI, 2023, 45(7): 1234-1250. DOI{fakes[3]}",
        f"5. IEEE TPAMI, 45(7), 1234-1250.{fakes[4]}",
        f"6. See also...{fakes[5]}",
        f"7. doi{fakes[6]}",
        "8. Nature 论文（DOI: 10.1038/s41586\u2011099\u201112345\u20116）",
        f"9. The key paper ({fakes[7]}-- see also) shows",
        "10. Preprint: https://www.biorxiv.org/content/10.1101/2099.03.22.002386v1.full.pdf+html",
        f"11. https://www.google.com/url?url=https%3A%2F%2Fdoi.org%2F{enc[8]}&ved=2ahUKEwi&usg=AOvVaw3x"
        f" https://scholar.google.com/scholar?q=doi%3A{enc[9]}+survey&hl=en",
    ])
    calls = []
    mock_verify_client(monkeypatch, registries_know_nothing(calls))
    result = await verify.verify_answer(answer, [], check_urls=False, timeout_s=10)
    expected = sorted(fakes + ["10.1038/s41586-099-12345-6", "10.1101/2099.03.22.002386"])
    assert sorted(x["value"] for x in result["unresolved"]) == expected
    assert {x["reason"] for x in result["unresolved"]} == {"not found"}
    assert result["skipped"] == [] and result["lookup_failed"] == []
    assert result["checked"]["doi"] == len(expected)
    assert {url.host for url in calls} <= {"api.crossref.org", "doi.org", "api.datacite.org"}


@pytest.mark.asyncio
async def test_real_dois_with_unicode_hyphens_or_a_pdf_html_view_are_looked_up_whole(monkeypatch):
    calls = []

    async def handler(request):
        calls.append(request.url)
        if request.url.host == "api.crossref.org" and request.url.path in (f"/works/{ALPHAFOLD}", f"/works/{BIORXIV}"):
            return httpx.Response(200, json={"message": {"title": ["A record"], "type": "journal-article"}})
        return httpx.Response(404, text="Resource not found.")

    mock_verify_client(monkeypatch, handler)
    answer = ("AlphaFold (DOI: 10.1038/s41586\u2010021\u201003819\u20102) and the preprint "
              f"https://www.biorxiv.org/content/{BIORXIV}v1.full.pdf+html")
    result = await verify.verify_answer(answer, [], check_urls=False, timeout_s=5)
    assert sorted(x["doi"] for x in result["doi"]) == sorted([ALPHAFOLD, BIORXIV])
    assert result["unresolved"] == [] and result["skipped"] == [] and result["lookup_failed"] == []
    assert sorted(url.path for url in calls) == sorted([f"/works/{ALPHAFOLD}", f"/works/{BIORXIV}"])


@pytest.mark.asyncio
async def test_sici_dois_with_a_single_colon_are_looked_up_whole(monkeypatch):
    calls = []

    async def handler(request):
        calls.append(request.url)
        if request.url.host == "api.crossref.org":
            return httpx.Response(200, json={"message": {"title": ["A record"], "type": "journal-article"}})
        return httpx.Response(404)

    mock_verify_client(monkeypatch, handler)
    answer = f"Kalnay et al. (1996), doi:{AMS}\n- {ESA}.\n- [BioScience](https://doi.org/{BIOONE})"
    result = await verify.verify_answer(answer, [], check_urls=False, timeout_s=5)
    assert sorted(x["doi"] for x in result["doi"]) == sorted([AMS, ESA, BIOONE])
    assert result["unresolved"] == [] and result["skipped"] == [] and result["lookup_failed"] == []
    assert sorted(url.raw_path for url in calls) == sorted([
        b"/works/10.1175/1520-0477(1996)077%3C0437:TNYRP%3E2.0.CO;2",
        b"/works/10.1890/0012-9658(2002)083%5B1421:SDEOLC%5D2.0.CO;2",
        b"/works/10.1641/0006-3568(2001)051%5B0933:TEOTWA%5D2.0.CO;2",
    ])


@pytest.mark.asyncio
async def test_templates_are_skipped_without_a_lookup(monkeypatch):
    calls = []
    mock_verify_client(monkeypatch, registries_know_nothing(calls))
    answer = ("ACM DOIs look like 10.1145/3XXXXXX.3XXXXXX, Nature DOIs look like 10.1038/s41586-yyy-nnnnn-n, "
              "Cell DOIs: 10.1016/j.cell.2020.XX.XXX, arXiv DOIs: 10.48550/arXiv.2301.?????")
    result = await verify.verify_answer(answer, [], check_urls=False, timeout_s=5)
    assert calls == [] and result["unresolved"] == [] and result["lookup_failed"] == []
    templates = ["10.1145/3XXXXXX.3XXXXXX", "10.1038/s41586-yyy-nnnnn-n", "10.1016/j.cell.2020.XX.XXX", "10.48550/arXiv.2301"]
    assert [(x["value"], x["reason"]) for x in result["skipped"]] == [(t, verify.DOI_PATTERN_REASON) for t in templates]


@pytest.mark.asyncio
async def test_doi_shaped_text_not_looked_up_is_in_skipped(monkeypatch):
    calls = []
    mock_verify_client(monkeypatch, registries_know_nothing(calls))
    answer = "10.1234/" + "a" * 500 + " and 10.1234/../../../10.5555/12345678"
    result = await verify.verify_answer(answer, [], check_urls=False, timeout_s=5)
    assert calls == [] and result["checked"]["doi"] == 0
    assert [x["reason"] for x in result["skipped"]] == [verify.DOI_TOO_LONG_REASON, verify.DOI_DOT_SEGMENT_REASON]
    assert result["summary"]["skipped"] == 2


@pytest.mark.asyncio
async def test_patterns_never_hide_other_skipped_entries(monkeypatch):
    calls = []
    mock_verify_client(monkeypatch, registries_know_nothing(calls))
    patterns = " ".join(f"10.{1000 + i}/x.<id>" for i in range(50))
    fakes = " ".join(f"10.1109/FAKE.2099.{10000 + i}" for i in range(35))
    result = await verify.verify_answer(patterns + " " + fakes, [{"url": "http://127.0.0.1/admin"}],
                                        check_urls=True, timeout_s=10, max_dois=30)
    reasons = [x["reason"] for x in result["skipped"]]
    others = [r for r in reasons if r != verify.DOI_PATTERN_REASON]
    assert len(others) == 6 and reasons[:6] == others  # 5 over the DOI limit, 1 blocked URL, listed first
    assert sum(r.startswith("over the per-answer limit") for r in others) == 5
    assert sum(r.startswith("blocked:") for r in others) == 1
    assert reasons.count(verify.DOI_PATTERN_REASON) == verify.MAX_PATTERNS_LISTED
    assert result["summary"]["skipped"] == 56
    assert len(result["unresolved"]) == 30


# ------------------------------------------------------------------ hostile input stays linear
N = 200_000


HOSTILE = {
    "long token": "10.1234/" + "a" * N,
    "v1 tails": ("10.1101/" + "v1" * 195 + "x ") * (N // 400),
    "full tails": ("10.1101/x" + ".full" * 78 + " ") * (N // 400),
    "one full tail run": "10.1101/x" + ".full" * (N // 5),
    "full tail run with query": "https://doi.org/10.1101/x" + ".full" * (N // 5) + "?q=1",
    "stars before": ("*" * 190 + " 10.1234/a.* ") * (N // 205),
    "star runs": ("* ** *** **** " * 14 + "10.1234/ab1.*\n") * (N // 160),
    "lt after": ("10.1234/a.<" + "x" * 5 + " ") * (N // 18),
    "lt run": "10.1234/a" + "<" * N,
    "sici unclosed": "10.1234/a" + ("<" + "a" * 40 + "::" + "b" * 39) * (N // 82),
    "paren unclosed": "10.1234/a" + ("(" + "a" * 59) * (N // 60),
    "percent dots": "10.1234/" + "%2e" * (N // 3),
    "many dois": " ".join(f"10.1234/a{i}" for i in range(N // 12)),
    "many patterns": " ".join(f"10.1234/a{i}.<id>" for i in range(N // 17)),
    "many duplicates": " ".join(["10.1234/abc"] * (N // 12)),
    "dot run": "10.1234/a" + "." * N,
    "unicode ellipses": "10.1234/a…" * (N // 10),
    "underscores": "_" + "10.1234/a_" * (N // 10),
    "slash runs": ("10.1234/" + "/" * 390 + " ") * (N // 400),
    "path tails": ("10.1234/x" + "/full" * 78 + " ") * (N // 400),
    "question marks": "10.1234/a" + "?" * N,
    "hashes": "10.1234/a" + "#" * N,
    "ampersands": "10.1234/a" + "&amp" * (N // 4),
    "sici checks": "10.1234/a" + ";2-#" * (N // 4),
    "sici checks closed": "10.1234/a>" + ";2-#" * (N // 4),
    "comma prefixes": "10.1234/a" + ",10.1" * (N // 5),
    "escaped underscores": "10.1234/" + "\\_" * (N // 2),
    "template letters": "10.1234/" + "XxYyMmDdNn" * (N // 10),
    "template letters unfinished": "10.1234/" + "xx" * (N // 2) + "z",
    "template letter tokens": ("10.1234/" + "xx" * 190 + "z ") * (N // 390),
    "question mark tokens": ("10.1234/a." + "?" * 380 + "x ") * (N // 392),
    "entity tokens": ("10.1234/a" + "&nbsp" * 75 + "; ") * (N // 387),
    "sici location unclosed": "10.1234/a" + ("<" + "1" * 40 + ":" + "b" * 39) * (N // 81),
    "sici bracket unclosed": "10.1234/a" + ("[" + "1" * 40 + ":" + "b" * 39) * (N // 81),
    "sici colon tokens": ("10.1234/a<" + ":" * 80 + " ") * (N // 92),
    "sici bracket groups": "10.1234/a" + ("[" + "1" * 40 + ":" + "b" * 40 + "]") * (N // 83),
    "encoded slash tokens": "10.1234%2Fa " * (N // 12),
    "encoded slash run": "10.1234%2F" + "%2F" * (N // 3),
    "zero-width spaces inside": "10.1234/a" + "\u200b1" * (N // 2),
    "zero-width spaces before dois": "\u200b10.1234/a" * (N // 10),
    "zero-width entities": "10.1234/a" + "&#8203;" * (N // 7),
    "dash runs before": ("-" * 50 + "10.1234/a ") * (N // 60),
    "doi dash prefixes": "DOI-10.1234/a " * (N // 14),
    "arrow tails": ("10.1234/a" + "-" * 380 + "> ") * (N // 392),
    "slash star tokens": "10.1234/a1/* " * (N // 13),
    "query value tokens": "%2F10.1234/a&" * (N // 13),
    "double-encoded slash tokens": "10.1234%252Fa " * (N // 14),
    "double-encoded slash run": "10.1234%252F" + "%252F" * (N // 5),
    "query value plus tokens": "%3A10.1234%2Fa+" * (N // 15),
    "query value encoded run": "%2F10.1234%2F" + "%41" * (N // 3),
    "query value double encoded": "%2F10.1234%2F" + "%2525" * (N // 5),
    "query value encoded queries": ("%2F10.1234%2Fa%3F" + "x" * 60 + "&") * (N // 78),
    "query value paren unclosed": "%2F10.1234/a" + ("(" + "a" * 58 + "&") * (N // 60),
    "percent-encoded runs before": "%20" * (N // 3) + "10.1234/a",
    "glued doi tokens": "doi10.1234/a " * (N // 13),
    "glued doi runs": "DOI" * (N // 3) + "10.1234/a",
    "dot prefixes": ".10.1234/a" * (N // 10),
    "unicode hyphens": "\u2011" * N,
    "unicode hyphen prefixes": "\u201110.1234/" * (N // 9),
    "unicode hyphens inside": "10.1234/a" + "\u2010b" * (N // 2),
    "dash tails": ("10.1234/a" + "-" * 390 + " ") * (N // 400),
    "pdf+html tails": ("10.1101/x" + ".pdf+html" * 43 + " ") * (N // 400),
}


@pytest.mark.parametrize("name", list(HOSTILE))
def test_hostile_doi_text_is_linear(name):
    text = HOSTILE[name]
    started = time.perf_counter()
    verify.scan_doi_mentions(text)
    assert time.perf_counter() - started < 0.5
