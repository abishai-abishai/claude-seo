"""Naver AI Briefing direct-read parser and metrics (docs/NAVER-AI-BRIEFING.md)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

pytest.importorskip("requests")
import naver_ai_briefing as nab  # noqa: E402

BRANDS = nab.normalize_brands([
    {"name": "OurBrand", "aliases": ["우리브랜드"], "domains": ["example.com", "blog.naver.com/ourbrand"], "is_ours": True},
    {"name": "ABC", "domains": ["shop.competitor.test"]},
    {"name": "경쟁사B", "domains": []},
])

COPY = (
    "우리브랜드는 설명이 자세하고 ABC도 있습니다. XABC는 다른 이름입니다. [1] [2]\n\n"
    "### 구매처\n* 경쟁사B 추천 [3]\n\n"
    "정보출처\n"
    "[1] 우리 블로그, https://m.blog.naver.com/ourbrand/2240001\n"
    "[2] shop.competitor.test, https://shop.competitor.test/guide/faq\n"
    "[3] 리뷰어, https://blog.naver.com/someone/1\n\n"
    "AI브리핑에서 생성된 결과입니다. \n2026.10.21\n"
)


def page(payload: dict, fid: str = "fdr-0001") -> str:
    other = {"meta": {"ssuidExtra": "fender_renderer-web"}, "body": {"templateId": "web"}}
    return (
        "<html><script>entry.bootstrap(document.getElementById(\"fdr-9999\"), "
        + json.dumps(other)
        + ");</script><script>entry.bootstrap(document.getElementById(\""
        + fid
        + "\"), "
        + json.dumps(payload, ensure_ascii=False)
        + ");</script></html>"
    )


def shopping_payload(copy: str = COPY) -> dict:
    return {
        "refs": {"blockId": "ai-briefing/prs_template_aib_shopping_mo.ts"},
        "meta": {"ssuidExtra": "fender_renderer-ai_briefing"},
        "body": {
            "templateId": "aibShopping",
            "props": {
                "summary": {"copy": copy},
                "feedback": {"aibType": "answertype.shopping"},
                "sources": [
                    {"url": "https://m.blog.naver.com/ourbrand/2240001", "title": "우리 글", "platform": "우리 블로그",
                     "footnoteSourceIndex": 0},
                ],
            },
        },
    }


def runtime_payload(api_url: str = "https://aib-api.naver.com/aibrender.naver?where=m&query=q") -> dict:
    return {
        "meta": {"ssuidExtra": "fender_renderer-ai_briefing"},
        "body": {
            "templateId": "aibAnswerRuntime",
            "props": {"apiURL": api_url, "customHeaders": {"X-NX-Query-Info": "token"},
                      "feedback": {"aibType": "answertype"}},
        },
    }


def stream(copy: str = COPY, verdict: str = "show", done: bool = True) -> str:
    parts = [
        'event:summary\ndata:{"markdown": "우리"}',
        "event:summary\ndata:" + json.dumps({"copy": copy}, ensure_ascii=False),
        "event:sources\ndata:" + json.dumps([{"url": "https://shop.competitor.test/guide/faq", "title": "FAQ",
                                              "platform": "ABC", "footnoteSourceIndex": 1}]),
        "event:rv\ndata:" + json.dumps({"verdict": verdict}),
    ]
    if done:
        parts.append("event:done\ndata:{}")
    return "\r\n\r\n".join(parts) + "\r\n\r\n"


# --- parsing --------------------------------------------------------------

def test_extract_picks_ai_briefing_among_other_blocks():
    payload = nab.extract_briefing_payload(page(shopping_payload()))
    assert payload["body"]["templateId"] == "aibShopping"


def test_extract_returns_none_without_briefing():
    html = "<script>entry.bootstrap(document.getElementById(\"fdr-1\"), {\"meta\": {\"ssuidExtra\": \"fender_renderer-web\"}});</script>"
    assert nab.extract_briefing_payload(html) is None


def test_split_copy_uses_on_screen_numbers_and_drops_disclaimer():
    body, notes = nab.split_copy(COPY)
    assert "정보출처" not in body and "AI브리핑에서" not in body
    assert [n["rank"] for n in notes] == [1, 2, 3]
    assert notes[1] == {"rank": 2, "name": "shop.competitor.test", "url": "https://shop.competitor.test/guide/faq"}


def test_parse_event_stream_keeps_final_copy_sources_and_verdict():
    parsed = nab.parse_event_stream(stream())
    assert parsed["copy"] == COPY
    assert parsed["sources"][0]["title"] == "FAQ"
    assert parsed["verdict"] == "show"
    assert parsed["done"] is True
    assert parsed["event_counts"]["summary"] == 2


# --- judging --------------------------------------------------------------

def test_classify_source_types():
    assert nab.classify_source("https://www.example.com/a", BRANDS) == ("own", "OurBrand")
    assert nab.classify_source("https://m.blog.naver.com/ourbrand/1", BRANDS) == ("own", "OurBrand")
    assert nab.classify_source("https://blog.naver.com/ourbrand2/1", BRANDS) == ("naver_ugc", None)
    assert nab.classify_source("https://shop.competitor.test/x", BRANDS) == ("competitor", "ABC")
    assert nab.classify_source("https://smartstore.naver.com/x", BRANDS) == ("naver_shopping", None)
    assert nab.classify_source("https://namu.wiki/w/1", BRANDS) == ("community", None)
    assert nab.classify_source("https://notexample.com/", BRANDS) == ("third_party", None)


def test_mentions_respect_ascii_word_boundary_and_aliases():
    counts = nab.count_mentions("우리브랜드 ourbrand ABC XABC ABC2 (ABC)", BRANDS)
    assert counts == {"OurBrand": 2, "ABC": 2, "경쟁사B": 0}


def test_judge_answer_marks_own_citation_and_mention():
    judged = nab.judge_answer(COPY, shopping_payload()["body"]["props"]["sources"], [], BRANDS)
    assert judged["own_cited"] is True
    assert judged["own_cited_ranks"] == [1]
    assert judged["own_mentioned"] is True
    assert judged["sources"][0]["title"] == "우리 글"
    assert [s["source_type"] for s in judged["sources"]] == ["own", "competitor", "naver_ugc"]
    assert judged["warnings"] == []


def test_judge_answer_without_copy_falls_back_to_sources_with_warning():
    judged = nab.judge_answer(None, [{"url": "https://www.example.com/", "footnoteSourceIndex": 0}], [], BRANDS)
    assert judged["warnings"] == ["copy_missing"]
    assert judged["own_cited_ranks"] == [1]
    assert judged["own_mentioned"] is False


# --- one query (network patched) -------------------------------------------

class FakeResponse:
    def __init__(self, text: str, status: int = 200):
        self.status_code = status
        self.content = text.encode("utf-8")
        self.headers = {"Content-Type": "text/html; charset=utf-8"}


def run_one(responses: list) -> tuple[dict, list]:
    calls = []

    def fake_get(url, headers):
        calls.append((url, headers))
        return responses.pop(0)

    with patch.object(nab, "_get", side_effect=fake_get):
        answer = nab.check_query(1, "질문", "mo", BRANDS)
    return answer, calls


def test_check_query_inline_shopping_needs_one_request():
    answer, calls = run_one([FakeResponse(page(shopping_payload()))])
    assert answer["status"] == "ok" and answer["shown"] is True
    assert len(calls) == 1
    assert "Cookie" not in calls[0][1]


def test_check_query_runtime_follows_api_with_token_and_referer():
    answer, calls = run_one([FakeResponse(page(runtime_payload())), FakeResponse(stream())])
    assert answer["status"] == "ok" and answer["template"] == "aibAnswerRuntime"
    assert calls[1][0].startswith("https://aib-api.naver.com/")
    assert calls[1][1]["X-NX-Query-Info"] == "token"
    assert calls[1][1]["Referer"] == calls[0][0]
    assert answer["sources"][1]["title"] == "FAQ"


def test_check_query_refuses_foreign_api_host():
    answer, calls = run_one([FakeResponse(page(runtime_payload("https://evil.test/aibrender")))])
    assert answer["status"] == "error"
    assert "not followed" in answer["error"]
    assert len(calls) == 1


def test_check_query_hidden_verdict_is_not_shown():
    answer, _ = run_one([FakeResponse(page(runtime_payload())), FakeResponse(stream(verdict="hide", done=False))])
    assert answer["status"] == "hidden" and answer["shown"] is False
    assert "stream_incomplete" in answer["warnings"]


def test_check_query_marker_without_payload_is_an_error_not_no_briefing():
    html = '<div data-meta-ssuid-extra="fender_renderer-ai_briefing"></div>'
    answer, _ = run_one([FakeResponse(html)])
    assert answer["status"] == "error"


def test_check_query_no_briefing():
    answer, _ = run_one([FakeResponse("<html></html>")])
    assert answer["status"] == "no_briefing" and answer["shown"] is False


# --- metrics --------------------------------------------------------------

def test_summarize_excludes_errors_and_uses_null_for_empty_denominators():
    ok, _ = run_one([FakeResponse(page(shopping_payload()))])
    none, _ = run_one([FakeResponse("<html></html>")])
    err = nab._empty_answer("질문", "mo")
    summary = nab.summarize([ok, none, err], BRANDS)
    assert summary["attempts"] == 2 and summary["errors"] == 1
    assert summary["shown_rate"] == 0.5
    assert summary["exposure_score"] == 1.0
    assert summary["citation_share"] == 1.0
    assert summary["own_mentions"] == 1 and summary["competitor_mentions"] == 2
    assert summary["mention_share"] == round(1 / 3, 4)
    assert summary["attribution_rate"] == 1.0

    empty = nab.summarize([none], BRANDS)
    assert empty["exposure_score"] is None
    assert empty["citation_share"] is None
    assert empty["mention_share"] is None


def test_report_renders_korean_tables():
    responses = [FakeResponse(page(shopping_payload())), FakeResponse(page(shopping_payload()))]
    with patch.object(nab, "_get", side_effect=lambda url, headers: responses.pop(0)):
        result = nab.run_check(["질문"], ["mo", "pc"], BRANDS, site="https://www.example.com")
    assert result["status"] == "success"
    report = nab.render_markdown(result)
    assert "## 요약" in report and "| 노출률 |" in report
    assert "100.0% (2/2)" in report
    assert "—" not in report and "–" not in report


# --- config: naver.json "ai_briefing", old flat file as fallback ------------

NEW_CONFIG = {
    "site": "https://www.example.com",
    "ai_briefing": {
        "devices": ["pc"],
        "queries": ["질문 1", "질문 2"],
        "brands": [{"name": "우리브랜드", "aliases": [], "domains": ["example.com"], "is_ours": True}],
    },
    "keywords": {"list": [], "candidates": [], "max": 20},
}
OLD_CONFIG = {
    "site": "https://old.example.com",
    "devices": ["mo"],
    "queries": ["질문 3"],
    "brands": [{"name": "우리브랜드", "aliases": [], "domains": ["example.com"], "is_ours": True}],
}


def test_default_config_is_naver_json_with_old_file_as_fallback():
    assert Path(nab.DEFAULT_CONFIG).parts[-3:] == (".config", "claude-seo", "naver.json")
    assert Path(nab.LEGACY_CONFIG).parts[-3:] == (".config", "claude-seo", "naver-ai-briefing.json")


def write_json(path: Path, value: dict) -> str:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return str(path)


def test_resolve_config_reads_ai_briefing_of_naver_json(tmp_path):
    new = write_json(tmp_path / "naver.json", NEW_CONFIG)
    old = write_json(tmp_path / "naver-ai-briefing.json", OLD_CONFIG)
    settings, warnings = nab.resolve_config(default_path=new, legacy_path=old)
    assert warnings == []
    assert settings["site"] == "https://www.example.com"
    assert settings["queries"] == ["질문 1", "질문 2"] and settings["devices"] == ["pc"]
    assert nab.normalize_brands(settings["brands"])[0]["is_ours"] is True


def test_resolve_config_falls_back_to_old_file_when_naver_json_is_missing(tmp_path):
    old = write_json(tmp_path / "naver-ai-briefing.json", OLD_CONFIG)
    settings, warnings = nab.resolve_config(default_path=str(tmp_path / "naver.json"), legacy_path=old)
    assert settings["queries"] == ["질문 3"] and settings["site"] == "https://old.example.com"
    assert len(warnings) == 1
    assert "naver-ai-briefing.json" in warnings[0] and "does not exist" in warnings[0]


def test_resolve_config_falls_back_when_naver_json_has_no_ai_briefing(tmp_path):
    new = write_json(tmp_path / "naver.json", {"site": "https://www.example.com", "keywords": {"list": []}})
    old = write_json(tmp_path / "naver-ai-briefing.json", OLD_CONFIG)
    settings, warnings = nab.resolve_config(default_path=new, legacy_path=old)
    assert settings["queries"] == ["질문 3"] and settings["devices"] == ["mo"]
    assert settings["site"] == "https://www.example.com"
    assert len(warnings) == 1 and 'no "ai_briefing"' in warnings[0]


def test_resolve_config_without_any_file_is_empty(tmp_path):
    missing = (str(tmp_path / "naver.json"), str(tmp_path / "naver-ai-briefing.json"))
    assert nab.resolve_config(default_path=missing[0], legacy_path=missing[1]) == ({}, [])


def test_resolve_config_explicit_path_accepts_both_shapes(tmp_path):
    settings, warnings = nab.resolve_config(write_json(tmp_path / "cfg.json", NEW_CONFIG))
    assert settings["queries"] == ["질문 1", "질문 2"] and warnings == []
    settings, warnings = nab.resolve_config(write_json(tmp_path / "flat.json", OLD_CONFIG))
    assert settings["queries"] == ["질문 3"]
    assert len(warnings) == 1 and "flat.json" in warnings[0] and "old flat shape" in warnings[0]


def test_unreadable_naver_json_is_an_error_not_a_silent_fallback(tmp_path):
    bad = tmp_path / "naver.json"
    bad.write_text("{broken", encoding="utf-8")
    old = write_json(tmp_path / "naver-ai-briefing.json", OLD_CONFIG)
    with pytest.raises(nab.ConfigError, match="naver.json"):
        nab.resolve_config(default_path=str(bad), legacy_path=old)


def run_main(monkeypatch, capsys, tmp_path, argv: list, new: dict | None = None,
             old: dict | None = None) -> tuple[int, dict, list]:
    """Run main() --json against config files in tmp_path, with the network patched out."""
    default, legacy = tmp_path / "naver.json", tmp_path / "naver-ai-briefing.json"
    for path, value in ((default, new), (legacy, old)):
        if value is None:
            path.unlink(missing_ok=True)
        else:
            write_json(path, value)
    monkeypatch.setattr(nab, "DEFAULT_CONFIG", str(default))
    monkeypatch.setattr(nab, "LEGACY_CONFIG", str(legacy))
    monkeypatch.setattr(sys, "argv", ["naver_ai_briefing.py", "--json", *argv])
    calls = []

    def fake_get(url, headers):
        calls.append(url)
        return FakeResponse("<html></html>")

    monkeypatch.setattr(nab, "_get", fake_get)
    code = nab.main()
    return code, json.loads(capsys.readouterr().out), calls


def test_main_reads_naver_json_and_cli_query_and_device_take_precedence(monkeypatch, capsys, tmp_path):
    code, result, calls = run_main(monkeypatch, capsys, tmp_path, [], new=NEW_CONFIG, old=OLD_CONFIG)
    assert code == 0 and result["metadata"]["warnings"] == []
    assert result["data"]["queries"] == ["질문 1", "질문 2"] and result["data"]["devices"] == ["pc"]
    assert result["data"]["site"] == "https://www.example.com"
    assert len(calls) == 2

    code, result, calls = run_main(monkeypatch, capsys, tmp_path, ["--query", "질문 9", "--device", "mo"],
                                   new=NEW_CONFIG)
    assert code == 0
    assert result["data"]["queries"] == ["질문 9"] and result["data"]["devices"] == ["mo"]
    assert calls == [nab.search_url("질문 9", "mo")]


def test_main_old_file_fallback_is_flagged_in_metadata(monkeypatch, capsys, tmp_path):
    code, result, _ = run_main(monkeypatch, capsys, tmp_path, [], old=OLD_CONFIG)
    assert code == 0
    assert result["data"]["queries"] == ["질문 3"] and result["data"]["devices"] == ["mo"]
    warnings = result["metadata"]["warnings"]
    assert len(warnings) == 1 and "naver-ai-briefing.json" in warnings[0]


def test_main_explicit_config_overrides_default_files(monkeypatch, capsys, tmp_path):
    cfg = write_json(tmp_path / "cfg.json", NEW_CONFIG)
    code, result, _ = run_main(monkeypatch, capsys, tmp_path, ["--config", cfg], old=OLD_CONFIG)
    assert code == 0 and result["metadata"]["warnings"] == []
    assert result["data"]["queries"] == ["질문 1", "질문 2"]


def test_main_failure_keeps_the_fallback_warning(monkeypatch, capsys, tmp_path):
    code, result, calls = run_main(monkeypatch, capsys, tmp_path, [], old=dict(OLD_CONFIG, brands=[]))
    assert code == 1 and result["status"] == "error" and "no own brand" in result["error"]
    assert len(result["metadata"]["warnings"]) == 1
    assert calls == []
