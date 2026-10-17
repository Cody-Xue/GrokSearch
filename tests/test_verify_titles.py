"""title_checks: a title is only compared when the answer states it for this identifier."""
import time

import httpx
import pytest

from grok_search import verify

SPEC_DEC = "Fast Inference from Transformers via Speculative Decoding"
SPEC_SAMPLING = "Accelerating Large Language Model Decoding with Speculative Sampling"
ALPHAFOLD = "Highly accurate protein structure prediction with AlphaFold"
AFDB_2022 = "AlphaFold Protein Structure Database: massively expanding the structural coverage of protein-sequence space with high-accuracy models"
AFDB_2024 = "AlphaFold Protein Structure Database in 2024: providing structure coverage for over 214 million protein sequences"
SURVEY = "Unlocking Efficiency in Large Language Model Inference: A Comprehensive Survey of Speculative Decoding"
SPECDEC_2023 = "Speculative Decoding: Exploiting Speculative Execution for Accelerating Seq2seq Generation"


@pytest.mark.parametrize("answer, identifier, registry, others, container", [
    # author-year link text
    ("Speculative decoding was introduced by [Leviathan et al., 2023](https://arxiv.org/abs/2211.17192).",
     "2211.17192", SPEC_DEC, [], ""),
    # a neighbour's quoted title on the same line
    (f'"{SPEC_SAMPLING}" (arXiv:2302.01318) appeared at the same time as arXiv:2211.17192 by Leviathan et al.',
     "2211.17192", SPEC_DEC, [("2302.01318", SPEC_SAMPLING)], ""),
    # italic journal name
    ("Jumper et al., *Nature* 596, 583–589 (2021), DOI: 10.1038/s41586-021-03819-2",
     "10.1038/s41586-021-03819-2", ALPHAFOLD, [], "Nature"),
    # bold technique name, English and Chinese
    ("**Speculative decoding** (arXiv:2211.17192) uses a small draft model.", "2211.17192", SPEC_DEC, [], ""),
    ("**推测解码**（arXiv:2211.17192）由 Google 提出。", "2211.17192", SPEC_DEC, [], ""),
    # bold label ending in ':'
    ("**bioRxiv preprint:** https://www.biorxiv.org/content/10.1101/2020.03.22.002386v1  \n**Preprint DOI:** x",
     "10.1101/2020.03.22.002386", "A SARS-CoV-2-Human Protein-Protein Interaction Map Reveals Drug Targets", [], "bioRxiv"),
    # venue with a year
    ("**ICML 2023 Oral** arXiv:2211.17192", "2211.17192", SPEC_DEC, [], ""),
])
def test_text_that_is_not_a_title_claim_is_not_flagged(answer, identifier, registry, others, container):
    check = verify.compare_title(answer, identifier, registry, others=others, container=container)
    assert check["status"] == "not_stated", check


def test_neighbours_title_is_never_reported_as_partial():
    answer = (f'The Varadi et al. paper ("{AFDB_2022}"). Its DOI is **10.1093/nar/gkab1061**. '
              "A 2024 update paper exists (DOI 10.1093/nar/gkad1011), but the question matches the original.")
    check = verify.compare_title(answer, "10.1093/nar/gkad1011", AFDB_2024, others=[("10.1093/nar/gkab1061", AFDB_2022)])
    assert check["status"] == "not_stated", check


def test_title_of_another_identifier_later_in_the_paragraph_is_ignored():
    answer = ("Later methods (e.g., EAGLE arXiv:2401.15077, SpecInfer arXiv:2305.09781) build directly on them. "
              f'The primary comprehensive survey is "{SURVEY}" (DOI 10.18653/v1/2024.findings-acl.456).')
    others = [("2401.15077", "EAGLE: Speculative Sampling Requires Rethinking Feature Uncertainty"),
              ("10.18653/v1/2024.findings-acl.456", SURVEY)]
    check = verify.compare_title(answer, "2305.09781", "SpecInfer: Accelerating Large Language Model Serving with Tree-based Speculative Inference and Verification", others=others)
    assert check["status"] == "match" and check["answer_title"] == "SpecInfer"
    check = verify.compare_title(answer, "2401.15077", others[0][1], others=[("2305.09781", "SpecInfer: x y z"), others[1]])
    assert check["status"] == "not_stated"


@pytest.mark.parametrize("answer", [
    f'**3. "{SPEC_DEC}"**  \narXiv:2211.17192, DOI: 10.48550/arXiv.2211.17192 (ICML 2023 Oral).',
    f"- **{SPEC_DEC}**\n- arXiv: 2211.17192",
    f"**{SPEC_DEC}**\nhttps://arxiv.org/abs/2211.17192",
])
def test_title_on_the_previous_line_is_used_when_the_identifier_starts_its_line(answer):
    assert verify.compare_title(answer, "2211.17192", SPEC_DEC)["status"] == "match"


def test_doi_after_an_arxiv_id_of_the_same_paper_sees_the_title():
    answer = f'**2. "{SPECDEC_2023}" (SpecDec)**  \narXiv:2203.16487, DOI: 10.18653/v1/2023.findings-emnlp.257 (EMNLP Findings 2023).'
    check = verify.compare_title(answer, "10.18653/v1/2023.findings-emnlp.257", SPECDEC_2023, others=[("2203.16487", SPECDEC_2023)])
    assert check["status"] == "match"


@pytest.mark.parametrize("answer, wrong", [
    ('"Scaling Laws for Neural Language Models" (arXiv:2211.17192) introduced speculative decoding.', "Scaling Laws for Neural Language Models"),
    ("See [Attention Is All You Need](https://arxiv.org/abs/2211.17192).", "Attention Is All You Need"),
    ('**"Attention Is All You Need"**  \narXiv:2211.17192', "Attention Is All You Need"),
    ("论文《Scaling Laws for Neural Language Models》（arXiv:2211.17192）", "Scaling Laws for Neural Language Models"),
])
def test_a_wrong_adjacent_title_is_still_flagged(answer, wrong):
    check = verify.compare_title(answer, "2211.17192", SPEC_DEC)
    assert check["status"] == "possible_mismatch" and check["answer_title"] == wrong


def test_each_identifier_only_gets_its_own_title():
    answer = f'"{SPEC_DEC}" (arXiv:2211.17192) and "Some Unrelated Paper Title Here" (arXiv:2302.01318)'
    others_a = [("2302.01318", SPEC_SAMPLING)]
    others_b = [("2211.17192", SPEC_DEC)]
    assert verify.compare_title(answer, "2211.17192", SPEC_DEC, others=others_a)["status"] == "match"
    check = verify.compare_title(answer, "2302.01318", SPEC_SAMPLING, others=others_b)
    assert check["status"] == "possible_mismatch" and check["answer_title"] == "Some Unrelated Paper Title Here"
    between = f'arXiv:2211.17192 and "{SPEC_SAMPLING}" (arXiv:2302.01318)'
    assert verify.compare_title(between, "2211.17192", SPEC_DEC, others=others_a)["status"] == "not_stated"


@pytest.mark.parametrize("answer", [
    " " * 20000 + "arXiv:2211.17192",
    "(" * 5000 + " arXiv:2211.17192",
    "arxiv " * 3000 + "\nx arXiv " * 2000 + "2211.17192",
    ("**" + "a" * 300) * 200 + " 2211.17192",
])
def test_title_comparison_stays_fast_on_hostile_text(answer):
    started = time.monotonic()
    verify.compare_title(answer, "2211.17192", SPEC_DEC)
    assert time.monotonic() - started < 1.0


@pytest.mark.asyncio
async def test_verify_answer_ties_titles_to_identifiers(monkeypatch):
    feed = ('<feed xmlns="http://www.w3.org/2005/Atom">'
            f'<entry><id>http://arxiv.org/abs/2211.17192v2</id><title>{SPEC_DEC}</title></entry>'
            f'<entry><id>http://arxiv.org/abs/2302.01318v1</id><title>{SPEC_SAMPLING}</title></entry></feed>')

    async def handler(request):
        return httpx.Response(200, text=feed)

    monkeypatch.setattr(verify, "ARXIV_MIN_INTERVAL_S", 0.0)
    verify._ARXIV_CACHE.clear()
    verify._arxiv_gate.update(loop=None)
    real = httpx.AsyncClient
    monkeypatch.setattr(verify.httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))
    answer = f'"{SPEC_SAMPLING}" (arXiv:2302.01318) appeared at the same time as [Leviathan et al., 2023](https://arxiv.org/abs/2211.17192).'
    result = await verify.verify_answer(answer, [], check_urls=False, timeout_s=5)
    assert {(t["id"], t["status"]) for t in result["title_checks"]} == {("2211.17192", "not_stated"), ("2302.01318", "match")}
    assert result["summary"]["title_possible_mismatch"] == 0
