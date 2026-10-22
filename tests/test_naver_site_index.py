"""Naver site: page counter (docs/NAVER-SITE-INDEX.md)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

pytest.importorskip("requests")
import naver_site_index as nsi  # noqa: E402


def item(url: str, title: str = "제목", pay: bool = False, keep: bool = True) -> dict:
    out = {"title": f"<mark>{title}</mark> &amp; 더", "href": url}
    if pay:
        out["href"] = "https://inflow.pay.naver.com/rd?no=1&retUrl=" + url.replace(":", "%3A").replace("/", "%2F")
    if keep:
        out["profile"] = {"keep": {"keepTriggerUrl": url}}
    return out


NEXT_HREF = "?nso=&amp;page=3&amp;query=site%3Aexample.com&amp;ssc=tab.ur.all&amp;start=21"


def page(items: list, next_enabled: bool = True, next_href: str = NEXT_HREF) -> str:
    blocks = []
    for i, it in enumerate(items):
        payload = {"meta": {"ssuidExtra": "fender_renderer-web"}, "data": {"state": {"items": [it]}}}
        blocks.append(f'<script>entry.bootstrap(document.getElementById("fdr-{i}"), '
                      + json.dumps(payload, ensure_ascii=False) + ");</script>")
    other = {"meta": {"ssuidExtra": "fender_renderer-qra"}, "data": {"state": {"items": [item("https://x.test/")]}}}
    blocks.append('<script>entry.bootstrap(document.getElementById("fdr-q"), ' + json.dumps(other) + ");</script>")
    disabled = "false" if next_enabled else "true"
    href = f'href="{next_href}"\n\t\t\t' if next_enabled else ""
    return "<html>" + "".join(blocks) + f'<a {href}class="btn_next" aria-disabled="{disabled}">다음</a></html>'


class FakeResponse:
    def __init__(self, text: str, status: int = 200):
        self.status_code = status
        self.content = text.encode("utf-8")
        self.headers = {"Content-Type": "text/html; charset=utf-8"}


def run_collect(responses: list, max_pages: int = 50) -> tuple[dict, list]:
    calls = []

    def fake_get(url, headers):
        calls.append((url, headers))
        return responses.pop(0)

    with patch.object(nsi, "_get", side_effect=fake_get):
        out = nsi.collect("example.com", max_pages=max_pages)
    return out, calls


# --- reading one page -----------------------------------------------------

def test_extract_uses_keep_url_then_pay_ret_url_and_strips_title_tags():
    html = page([
        item("https://www.example.com/a"),
        item("https://www.example.com/", pay=True, keep=False),
        item("https://m.example.com/b?x=1", keep=False),
    ])
    items = nsi.extract_web_items(html)
    assert [i["url"] for i in items] == ["https://www.example.com/a", "https://www.example.com/",
                                          "https://m.example.com/b?x=1"]
    assert items[0]["title"] == "제목 & 더"


def test_extract_ignores_non_web_blocks():
    assert nsi.extract_web_items(page([])) == []


def test_web_marker_with_unreadable_json_is_an_error():
    html = '<div>fender_renderer-web</div><script>entry.bootstrap(document.getElementById("fdr-1"), {broken);</script>'
    with pytest.raises(nsi.NaverFetchError):
        nsi.extract_web_items(html)


def test_next_link_is_followed_as_written():
    url = nsi.next_page_url(page([item("https://example.com/")]), "example.com")
    assert url == ("https://search.naver.com/search.naver"
                   "?nso=&page=3&query=site%3Aexample.com&ssc=tab.ur.all&start=21")


def test_no_next_link_on_last_page():
    one = [item("https://example.com/")]
    assert nsi.next_page_url(page(one, next_enabled=False), "example.com") is None
    assert nsi.next_page_url('<a class="btn_next" aria-disabled="false">다음</a>', "example.com") is None
    assert nsi.next_page_url("<html></html>", "example.com") is None


def test_next_link_to_another_query_or_host_is_an_error():
    one = [item("https://example.com/")]
    with pytest.raises(nsi.NaverFetchError):
        nsi.next_page_url(page(one, next_href="?page=2&amp;query=site%3Aother.com&amp;start=1"), "example.com")
    with pytest.raises(nsi.NaverFetchError):
        nsi.next_page_url(page(one, next_href="https://evil.test/?query=site%3Aexample.com"), "example.com")


def test_own_domain_includes_subdomains_only():
    assert nsi.is_own("https://m.example.com/x", "example.com")
    assert nsi.is_own("https://example.com/x", "example.com")
    assert not nsi.is_own("https://notexample.com/x", "example.com")


# --- paging ---------------------------------------------------------------

def test_collect_follows_next_links_until_last_page():
    out, calls = run_collect([
        FakeResponse(page([item("https://www.example.com/1"), item("https://other.test/z")])),
        FakeResponse(page([item("https://m.example.com/2")], next_enabled=False)),
    ])
    assert calls[0][0] == "https://search.naver.com/search.naver?where=web&query=site%3Aexample.com"
    assert "page=3" in calls[1][0] and "start=21" in calls[1][0]
    assert "Cookie" not in calls[0][1]
    assert out["stop_reason"] == "last_page" and out["pages_fetched"] == 2
    assert [u["url"] for u in out["urls"]] == ["https://www.example.com/1", "https://m.example.com/2"]
    assert out["urls"][1]["page"] == 2 and out["urls"][1]["rank"] == 2
    assert out["other_domain"] == 1


def test_one_repeated_page_does_not_stop_paging():
    # Naver's page 2 (start=1) repeats most of page 1; paging must go on to page 3.
    out, calls = run_collect([
        FakeResponse(page([item("https://www.example.com/1")])),
        FakeResponse(page([item("https://www.example.com/1")])),
        FakeResponse(page([item("https://www.example.com/3")], next_enabled=False)),
    ])
    assert len(calls) == 3 and out["stop_reason"] == "last_page"
    assert [u["url"] for u in out["urls"]] == ["https://www.example.com/1", "https://www.example.com/3"]


def test_collect_stops_after_two_pages_without_new_urls():
    same = page([item("https://www.example.com/1")])
    out, calls = run_collect([FakeResponse(same), FakeResponse(same), FakeResponse(same)])
    assert out["stop_reason"] == "no_new_urls" and len(calls) == 3


def test_bad_next_link_keeps_the_urls_of_that_page():
    out, _ = run_collect([FakeResponse(page([item("https://www.example.com/1")],
                                            next_href="https://evil.test/?query=site%3Aexample.com"))])
    assert out["stop_reason"] == "error" and len(out["urls"]) == 1


def test_collect_stops_on_empty_page_and_max_pages():
    out, _ = run_collect([FakeResponse(page([]))])
    assert out["stop_reason"] == "empty_page"
    out, calls = run_collect([FakeResponse(page([item("https://www.example.com/1")]))], max_pages=1)
    assert out["stop_reason"] == "max_pages" and len(calls) == 1


def test_collect_http_error_stops_with_error():
    out, _ = run_collect([FakeResponse(page([item("https://www.example.com/1")])), FakeResponse("", status=403)])
    assert out["stop_reason"] == "error" and "403" in out["error"]
    assert len(out["urls"]) == 1


# --- sitemap --------------------------------------------------------------

def test_parse_sitemap_decodes_amp_and_detects_index():
    locs, is_index = nsi.parse_sitemap(
        "<urlset><url><loc>https://www.example.com/v.php?a=1&amp;b=2</loc></url></urlset>")
    assert locs == ["https://www.example.com/v.php?a=1&b=2"] and is_index is False
    assert nsi.parse_sitemap("<sitemapindex><sitemap><loc>https://www.example.com/s1.xml</loc></sitemap></sitemapindex>")[1]


def test_compare_ignores_host_and_query_order():
    found = ["https://m.example.com/v.php?b=2&a=1", "https://example.com/only-search"]
    sitemap = ["https://www.example.com/v.php?a=1&b=2", "https://www.example.com/only-sitemap"]
    out = nsi.compare_with_sitemap(found, sitemap)
    assert (out["in_both"], out["search_only"], out["sitemap_only"]) == (1, 1, 1)
    assert out["search_only_urls"] == ["https://example.com/only-search"]
    assert out["sitemap_only_urls"] == ["https://www.example.com/only-sitemap"]


# --- run and report -------------------------------------------------------

def test_run_check_summary_status_and_report_without_dashes():
    responses = [
        FakeResponse(page([item("https://www.example.com/a.php?x=1"), item("https://m.example.com/a.php?x=2")],
                          next_enabled=False)),
        FakeResponse("<urlset><url><loc>https://www.example.com/a.php?x=1</loc></url></urlset>"),
    ]
    with patch.object(nsi, "_get", side_effect=lambda url, headers: responses.pop(0)):
        result = nsi.run_check("example.com", sitemap="https://www.example.com/sitemap.xml")
    assert result["status"] == "success"
    summary = result["data"]["summary"]
    assert summary["total_urls"] == 2
    assert summary["by_host"] == {"www.example.com": 1, "m.example.com": 1}
    assert summary["by_path"] == {"/a.php": 2}
    assert result["data"]["sitemap"]["in_both"] == 1
    report = nsi.render_markdown(result)
    assert "## 사이트맵 대조" in report and "| 잡힌 주소 | 2 |" in report
    assert "—" not in report and "–" not in report


def test_full_last_page_is_reported_as_a_possible_limit():
    full = [item(f"https://www.example.com/{i}") for i in range(20)]
    with patch.object(nsi, "_get", side_effect=lambda url, headers: FakeResponse(page(full, next_enabled=False))):
        result = nsi.run_check("example.com")
    assert result["data"]["last_page_items"] == 20
    assert "상한" in nsi.render_markdown(result)
    with patch.object(nsi, "_get", side_effect=lambda url, headers: FakeResponse(page(full[:5], next_enabled=False))):
        result = nsi.run_check("example.com")
    assert "상한" not in nsi.render_markdown(result).split("## 요약")[0]


def test_run_check_first_page_failure_is_error():
    with patch.object(nsi, "_get", side_effect=lambda url, headers: FakeResponse("", status=500)):
        result = nsi.run_check("example.com")
    assert result["status"] == "error"


def test_normalize_domain():
    assert nsi.normalize_domain("https://www.Example.com/") == "example.com"
    assert nsi.normalize_domain("example.com") == "example.com"
