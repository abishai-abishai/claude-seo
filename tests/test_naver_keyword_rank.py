"""Naver keyword candidates and rank check (docs/NAVER.md section 6)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

pytest.importorskip("requests")
import naver_keyword_rank as nkr  # noqa: E402

DOMAIN = "example.com"
DASHES = (chr(0x2014), chr(0x2013))
ORIGINAL_DEFAULT_CONFIG = nkr.DEFAULT_CONFIG


@pytest.fixture(autouse=True)
def no_real_config(tmp_path, monkeypatch):
    """Every test reads a config path under tmp_path, never ~/.config/claude-seo/naver.json."""
    monkeypatch.setattr(nkr, "DEFAULT_CONFIG", str(tmp_path / "no-such-dir" / "naver.json"))


class FakeResponse:
    def __init__(self, text: str, status: int = 200, headers: dict | None = None):
        self.status_code = status
        self.content = text.encode("utf-8")
        self.headers = {"Content-Type": "text/html; charset=utf-8", **(headers or {})}


def block(extra: str, items: list | None = None, body: dict | None = None, fid: str = "0") -> str:
    payload: dict = {"meta": {"ssuidExtra": extra}}
    if items is not None:
        payload["data"] = {"state": {"items": items}}
    if body is not None:
        payload["body"] = body
    return (f'<script>entry.bootstrap(document.getElementById("fdr-{fid}"), '
            + json.dumps(payload, ensure_ascii=False) + ");</script>")


def web_item(url: str, title: str = "제목", pay: bool = False) -> dict:
    item = {
        "title": f"<mark>{title}</mark>",
        "href": url,
        "profile": {"keep": {"keepTriggerUrl": url},
                    "favicon": "https://search.pstatic.net/sunny?src=" + quote(url, safe="")},
    }
    if pay:
        item["href"] = "https://inflow.pay.naver.com/rd?no=1&retUrl=" + quote(url, safe="")
        del item["profile"]["keep"]
    return item


def review_item(url: str) -> dict:
    return {"title": "글", "titleHref": url, "contentHref": url, "keep": {"keepTriggerUrl": url}}


def mobile_page(blocks: list) -> str:
    return "<html>" + "".join(block(extra, items, body, fid=str(i))
                               for i, (extra, items, body) in enumerate(blocks)) + "</html>"


NEXT_P2 = "?nso=&amp;page=2&amp;query=keyword&#43;one&amp;ssc=tab.ur.all&amp;start=1"
NEXT_P3 = "?nso=&amp;page=3&amp;query=keyword&#43;one&amp;ssc=tab.ur.all&amp;start=21"


def pc_page(urls: list, next_href: str | None = None) -> str:
    blocks = [block("fender_renderer-web", [web_item(u)], fid=f"w{i}") for i, u in enumerate(urls)]
    blocks.append(block("fender_renderer-review", [review_item("https://www.example.com/not-web")], fid="r"))
    if next_href:
        nav = f'<a\n\t\t\thref="{next_href}"\n\t\t\tclass="btn_next" aria-disabled="false">다음</a>'
    else:
        nav = '<a class="btn_next" aria-disabled="true">다음</a>'
    return "<html>" + "".join(blocks) + nav + "</html>"


def routed(routes: dict, calls: list):
    def fake_get(url, headers):
        calls.append((url, headers))
        value = routes[url]
        return value.pop(0) if isinstance(value, list) else value
    return fake_get


# --- mobile integrated search ---------------------------------------------

def test_mobile_reports_area_and_order_across_blocks_of_the_same_area():
    html = mobile_page([
        ("fender_renderer-video", None, {"props": {"children": [{"href": "https://video.test/1"}]}}),
        ("fender_renderer-web", [web_item("https://other.test/")], None),
        ("fender_renderer-review", [review_item("https://blog.naver.com/someone/1")], None),
        ("fender_renderer-web", [web_item("https://notexample.com/")], None),
        ("fender_renderer-web", [web_item("https://www.example.com/", "우리", pay=True)], None),
        ("fender_renderer-web", [web_item("https://m.example.com/p?id=1")], None),
        ("fender_renderer-image", None, {"props": {"link": "https://www.example.com/img-page"}}),
    ])
    read = nkr.read_mobile_blocks(html, DOMAIN)
    hits = read["hits"]
    assert [(h["area"], h["position"], h["block_index"], h["position_in_block"]) for h in hits] == [
        ("fender_renderer-web", 3, 5, 1),
        ("fender_renderer-web", 4, 6, 1),
        ("fender_renderer-image", None, 7, None),
    ]
    assert hits[0]["url"] == "https://www.example.com/" and hits[0]["title"] == "우리"
    assert read["area_items"]["fender_renderer-web"] == 4 and read["area_items"]["fender_renderer-video"] == 0


def test_favicon_proxy_and_lookalike_domains_are_not_ours():
    item = web_item("https://notexample.com/")
    assert nkr.own_url_in(item, DOMAIN) is None
    assert nkr.own_url_in({"x": ["https://example.com.evil.test/"]}, DOMAIN) is None
    assert nkr.own_url_in({"x": "https://shop.example.com/a"}, DOMAIN) == "https://shop.example.com/a"


def run_mobile(response: FakeResponse, raw_dir=None) -> dict:
    with patch.object(nkr, "_get", side_effect=lambda url, headers: response):
        return nkr.check_mobile(1, "keyword one", DOMAIN, raw_dir)


def test_mobile_found_and_none():
    found = run_mobile(FakeResponse(mobile_page([("fender_renderer-web", [web_item("https://example.com/")], None)])))
    assert found["status"] == "found" and found["first"]["position"] == 1
    none = run_mobile(FakeResponse(mobile_page([("fender_renderer-web", [web_item("https://other.test/")], None)])))
    assert none["status"] == "none" and none["first"] is None and none["error"] is None


def test_mobile_read_failures_are_errors_not_none(tmp_path):
    broken = ('<script>entry.bootstrap(document.getElementById("fdr-1"), {broken);</script>'
              + mobile_page([("fender_renderer-web", [web_item("https://other.test/")], None)]))
    out = run_mobile(FakeResponse(broken), raw_dir=str(tmp_path / "raw"))
    assert out["status"] == "error" and "cannot be confirmed" in out["error"]
    assert out["raw_files"] == [str(Path("raw") / "01-mo.html")] and (tmp_path / "raw" / "01-mo.html").exists()
    assert run_mobile(FakeResponse("<html>no blocks</html>"))["status"] == "error"
    blocked = run_mobile(FakeResponse("", status=403))
    assert blocked["status"] == "error" and "403" in blocked["error"]


def test_mobile_unreadable_block_does_not_hide_a_found_url():
    html = ('<script>entry.bootstrap(document.getElementById("fdr-1"), {broken);</script>'
            + mobile_page([("fender_renderer-web", [web_item("https://example.com/")], None)]))
    out = run_mobile(FakeResponse(html))
    assert out["status"] == "found" and out["unreadable_blocks"] == 1 and out["hits"][0]["block_index"] == 2


# --- PC web documents -------------------------------------------------------

def test_next_link_is_followed_as_written():
    url = nkr.next_page_url(pc_page(["https://other.test/"], NEXT_P2), "keyword one")
    assert url == ("https://search.naver.com/search.naver"
                   "?nso=&page=2&query=keyword+one&ssc=tab.ur.all&start=1")
    assert nkr.next_page_url(pc_page(["https://other.test/"]), "keyword one") is None


def test_next_link_to_another_keyword_or_host_is_an_error():
    with pytest.raises(nkr.NaverFetchError):
        nkr.next_page_url(pc_page(["https://other.test/"], "?page=2&amp;query=other&amp;start=1"), "keyword one")
    with pytest.raises(nkr.NaverFetchError):
        nkr.next_page_url(pc_page(["https://other.test/"], "https://evil.test/?query=keyword+one"), "keyword one")


def run_pc(pages: list) -> tuple[dict, list]:
    calls = []

    def fake_get(url, headers):
        calls.append(url)
        return pages.pop(0)

    with patch.object(nkr, "_get", side_effect=fake_get):
        out = nkr.check_pc(1, "keyword one", DOMAIN)
    return out, calls


def test_pc_rank_counts_distinct_urls_in_order_across_pages():
    out, calls = run_pc([
        FakeResponse(pc_page(["https://a.test/", "https://b.test/"], NEXT_P2)),
        FakeResponse(pc_page(["https://a.test/", "https://b.test/", "https://c.test/", "https://m.example.com/x"],
                             NEXT_P3)),
    ])
    assert calls[0] == "https://search.naver.com/search.naver?where=web&query=keyword%20one"
    assert "page=2" in calls[1] and "start=1" in calls[1] and len(calls) == 2
    assert out["status"] == "found" and out["rank"] == 4 and out["page"] == 2
    assert out["url"] == "https://m.example.com/x" and out["slot_on_page"] == 4


def test_pc_slot_on_page_counts_blog_cards_but_rank_does_not():
    html = ("<html>" + block("fender_renderer-review", [review_item("https://blog.naver.com/a/1")], fid="r")
            + block("fender_renderer-web", [web_item("https://a.test/")], fid="w1")
            + block("fender_renderer-kin", [{"questionHref": "https://kin.naver.com/q/1"}], fid="k")
            + block("fender_renderer-web", [web_item("https://www.example.com/")], fid="w2") + "</html>")
    out, _ = run_pc([FakeResponse(html)])
    assert out["rank"] == 2 and out["slot_on_page"] == 4


def test_pc_review_blocks_are_not_web_results():
    out, _ = run_pc([FakeResponse(pc_page(["https://a.test/"]))])
    assert out["status"] == "outside_read_pages" == nkr.PC_NOT_FOUND and out["stop_reason"] == "last_page"
    assert out["web_docs_read"] == 1 and out["slots_read"] == 1


def test_pc_outside_read_pages_after_three_pages():
    out, calls = run_pc([
        FakeResponse(pc_page([f"https://p1-{i}.test/" for i in range(20)], NEXT_P2)),
        FakeResponse(pc_page([f"https://p2-{i}.test/" for i in range(20)], NEXT_P3)),
        FakeResponse(pc_page([f"https://p3-{i}.test/" for i in range(20)], NEXT_P3)),
    ])
    assert len(calls) == 3
    assert out["status"] == "outside_read_pages" and out["rank"] is None
    assert out["web_docs_read"] == 60 and out["slots_read"] == 60 and out["pages_fetched"] == 3
    assert out["stop_reason"] == "max_pages"


def test_pc_page_two_repeating_page_one_counts_each_web_document_once():
    page1 = [f"https://p1-{i}.test/" for i in range(9)]
    out, _ = run_pc([
        FakeResponse(pc_page(page1, NEXT_P2)),
        FakeResponse(pc_page(page1 + [f"https://p2-{i}.test/" for i in range(4)], NEXT_P3)),
        FakeResponse(pc_page([f"https://p3-{i}.test/" for i in range(4)], NEXT_P3)),
    ])
    assert out["status"] == "outside_read_pages" and out["web_docs_read"] == 17 and out["pages_fetched"] == 3
    assert nkr._pc_cell(out) == "3쪽 밖 (3쪽에서 서로 다른 웹문서 17건을 읽음)"


def test_pc_failures_are_errors_never_rank_zero():
    out, _ = run_pc([FakeResponse("<html>nothing</html>")])
    assert out["status"] == "error" and out["rank"] is None
    out, _ = run_pc([FakeResponse(pc_page(["https://a.test/"], NEXT_P2)), FakeResponse("", status=500)])
    assert out["status"] == "error" and "500" in out["error"] and out["web_docs_read"] == 1
    out, _ = run_pc([FakeResponse(pc_page(["https://a.test/"], "?page=2&amp;query=other&amp;start=1"))])
    assert out["status"] == "error" and out["rank"] is None


# --- rank run ---------------------------------------------------------------

def test_run_rank_reads_both_screens_without_cookies_and_saves_raw(tmp_path):
    calls = []
    routes = {
        nkr.mobile_url("keyword one"): FakeResponse(mobile_page([
            ("fender_renderer-web", [web_item("https://other.test/")], None),
            ("fender_renderer-web", [web_item("https://www.example.com/")], None)])),
        nkr.pc_url("keyword one"): FakeResponse(pc_page(["https://other.test/", "https://www.example.com/"])),
    }
    with patch.object(nkr, "_get", side_effect=routed(routes, calls)):
        result = nkr.run_rank(["keyword one"], "https://www.example.com/", DOMAIN, unconfirmed=True,
                              output_dir=str(tmp_path))
    assert calls[0][0] == "https://m.search.naver.com/search.naver?where=m&query=keyword%20one"
    assert all("Cookie" not in headers for _, headers in calls)
    assert "iPhone" in calls[0][1]["User-Agent"] and "Windows" in calls[1][1]["User-Agent"]
    row = result["data"]["results"][0]
    assert result["status"] == "success" and row["status"] == "ok"
    assert row["mobile"]["first"]["position"] == 2 and row["pc"]["rank"] == 2
    assert (tmp_path / "raw" / "01-mo.html").exists() and (tmp_path / "raw" / "01-pc-1.html").exists()
    assert result["data"]["unconfirmed_list"] is True
    nkr.write_outputs(result, str(tmp_path), "rank")
    report = (tmp_path / "NAVER-KEYWORD-RANK.md").read_text(encoding="utf-8")
    assert "사람이 확인하지 않은 목록" in report and "`fender_renderer-web` 2번째" in report and "2위 (1쪽, 그 쪽 화면 2번째 칸)" in report
    assert not any(d in report for d in DASHES)
    saved = json.loads((tmp_path / "naver-keyword-rank.json").read_text(encoding="utf-8"))
    assert saved["data"]["summary"]["pc_found"] == 1


def test_run_rank_one_failing_keyword_is_partial_and_keeps_others():
    routes = {
        nkr.mobile_url("keyword one"): FakeResponse("", status=403),
        nkr.pc_url("keyword one"): FakeResponse("<html></html>"),
        nkr.mobile_url("keyword two"): FakeResponse(mobile_page([("fender_renderer-web",
                                                                  [web_item("https://other.test/")], None)])),
        nkr.pc_url("keyword two"): FakeResponse(pc_page(["https://other.test/"])),
    }
    with patch.object(nkr, "_get", side_effect=routed(routes, [])):
        result = nkr.run_rank(["keyword one", "keyword two"], "https://www.example.com/", DOMAIN)
    first, second = result["data"]["results"]
    assert result["status"] == "partial"
    assert first["status"] == "error" and first["pc"]["rank"] is None and "403" in first["error"]
    assert second["mobile"]["status"] == "none" and second["pc"]["status"] == "outside_read_pages"
    assert result["data"]["summary"]["errors"] == 1 and result["data"]["summary"]["pc_outside_read_pages"] == 1
    report = nkr.render_rank_markdown(result)
    assert "오류" in report and "3쪽 밖 (1쪽에서 서로 다른 웹문서 1건을 읽음)" in report
    assert "60위" not in report and "사람이 확인하지 않은" not in report


def test_run_rank_all_failing_is_error_and_cap_is_applied():
    with patch.object(nkr, "_get", side_effect=lambda url, headers: FakeResponse("", status=500)):
        result = nkr.run_rank(["a", "b", "c"], "https://www.example.com/", DOMAIN, max_keywords=2)
    assert result["status"] == "error"
    assert result["data"]["keywords"] == ["a", "b"] and result["data"]["dropped_keywords"] == ["c"]
    assert result["data"]["warnings"]


def test_load_keywords_file_shapes(tmp_path):
    def write(value) -> str:
        path = tmp_path / "k.json"
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return str(path)

    assert nkr.load_keywords_file(write(["a", "b"])) == (["a", "b"], False)
    assert nkr.load_keywords_file(write({"keywords": {"list": ["a"], "candidates": ["b"]}})) == (["a"], False)
    assert nkr.load_keywords_file(write({"keywords": {"list": [], "candidates": ["b"]}})) == (["b"], True)
    assert nkr.load_keywords_file(write({"status": "success", "data": {"candidates": ["c"]}})) == (["c"], True)
    with pytest.raises(ValueError):
        nkr.load_keywords_file(write({"keywords": {"list": [], "candidates": []}}))
    assert nkr.clean_keywords(["  a  b ", "a b", "", None, "c"]) == ["a b", "c"]


def test_cli_rank_marks_unconfirmed_and_rejects_short_delay(capsys):
    routes = {
        nkr.mobile_url("keyword one"): FakeResponse(mobile_page([("fender_renderer-web",
                                                                  [web_item("https://other.test/")], None)])),
        nkr.pc_url("keyword one"): FakeResponse(pc_page(["https://other.test/"])),
    }
    with patch.object(nkr, "_get", side_effect=routed(routes, [])):
        code = nkr.main(["rank", "--site", "https://www.example.com", "--keyword", "keyword one",
                         "--unconfirmed-list", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["data"]["unconfirmed_list"] is True and out["data"]["domain"] == DOMAIN
    assert nkr.main(["rank", "--site", "https://www.example.com", "--keyword", "a", "--delay", "1", "--json"]) == 1
    capsys.readouterr()


# --- candidates -------------------------------------------------------------

HOME = """<html><head><title>예시상점:::좋은 물건 가게</title></head><body>
<nav>
  <a href="../goods/goods_list.php?cateCd=001">첫째 분류 &#9654;</a>
  <a href="/goods/goods_list.php?cateCd=002"><img src="x.png" alt="둘째 분류"></a>
  <a href="https://example.com/goods/goods_list.php?cateCd=001">전체보기</a>
  <a href="/collections/summer">여름 모음</a>
  <a href="/goods/goods_list.php?cateCd=003"></a>
  <a href="https://other.test/goods/goods_list.php?cateCd=009">남의 분류</a>
  <a href="/board/list.php?bdId=notice">공지</a>
  <a href="javascript:void(0)">메뉴</a>
</nav></body></html>"""


def test_extract_categories_keeps_page_order_and_first_name_per_url():
    cats = nkr.extract_categories(HOME, "https://www.example.com/main/index.php", DOMAIN)
    assert [c["name"] for c in cats] == ["첫째 분류", "둘째 분류", "여름 모음"]
    assert cats[0]["url"] == "https://www.example.com/goods/goods_list.php?cateCd=001"


def test_site_name_and_title_stripping():
    assert nkr.site_name_from(HOME) == "예시상점"
    assert nkr.site_name_from('<meta content="상점이름" property="og:site_name"><title>a | b</title>') == "상점이름"
    assert nkr.strip_site_name("예시상점:::상품 A 256GB", "예시상점") == "상품 A 256GB"
    assert nkr.strip_site_name("[예시상점] 상품 B", "예시상점") == "상품 B"
    assert nkr.strip_site_name("상품 C | 예시상점", "예시상점") == "상품 C"
    assert nkr.strip_site_name("예시상점", "예시상점") == ""
    assert nkr.strip_site_name("상품 D", "") == "상품 D"


def test_load_landing_pages_accepts_either_key_and_sorts(tmp_path):
    path = tmp_path / "traffic.json"
    path.write_text(json.dumps({"status": "success", "data": {"top_landing_pages": [
        {"path": "/a", "sessions": 3},
        {"landing_page": "/b", "sessions": "12"},
        {"sessions": 99},
        "/c",
    ]}}), encoding="utf-8")
    pages, used = nkr.load_landing_pages(str(path))
    assert [p["path"] for p in pages] == ["/b", "/a", "/c"] and used == "top_landing_pages"
    path.write_text(json.dumps({"data": {
        "top_landing_pages": [{"landing_page": "/goods/goods_view.php", "sessions": 9}],
        "top_product_urls": [{"url": "/goods/goods_view.php?goodsNo=1&inflow=naver", "sessions": 4}],
    }}), encoding="utf-8")
    pages, used = nkr.load_landing_pages(str(path))
    assert used == "top_product_urls" and pages[0]["path"] == "/goods/goods_view.php?goodsNo=1&inflow=naver"
    path.write_text(json.dumps({"data": {"top_landing_pages": [{"path": "/x"}], "top_product_urls": []}}),
                    encoding="utf-8")
    assert nkr.load_landing_pages(str(path))[1] == "top_landing_pages"
    path.write_text(json.dumps({"data": {}}), encoding="utf-8")
    with pytest.raises(ValueError):
        nkr.load_landing_pages(str(path))


def test_merge_candidates_dedupes_and_shares_the_cap():
    cats = [{"name": f"분류{i}", "url": f"https://example.com/c{i}"} for i in range(10)]
    prods = [{"name": f"상품{i}", "url": f"https://example.com/p{i}", "sessions": 10 - i} for i in range(10)]
    merged = nkr.merge_candidates(cats, prods + [{"name": "분류1", "url": "x", "sessions": 0}], 6)
    assert [m["keyword"] for m in merged] == ["분류0", "분류1", "분류2", "상품0", "상품1", "상품2"]
    assert [m["keyword"] for m in nkr.merge_candidates(cats, prods[:1], 4)] == ["분류0", "분류1", "분류2", "상품0"]
    assert len(nkr.merge_candidates(cats[:1], prods, 4)) == 4


def test_run_candidates_with_landing_pages(tmp_path):
    landing = tmp_path / "traffic.json"
    landing.write_text(json.dumps({"data": {"top_landing_pages": [
        {"path": "/goods/goods_view.php?goodsNo=11", "sessions": 50},
        {"path": "/goods/goods_view.php?goodsNo=11&inflow=naver", "sessions": 45},
        {"path": "/goods/goods_view.php?goodsNo=14", "sessions": 42},
        {"path": "/goods/goods_view.php", "sessions": 40},
        {"path": "(not set)", "sessions": 30},
        {"landing_page": "/", "sessions": 20},
        {"path": "/goods/goods_view.php?goodsNo=12", "sessions": 10},
        {"path": "/goods/goods_view.php?goodsNo=13", "sessions": 5},
    ]}}), encoding="utf-8")
    calls = []
    # No route for goodsNo=11&inflow=naver: fetching it would raise KeyError and fail the test.
    routes = {
        "https://www.example.com/": FakeResponse("", status=301, headers={"Location": "/main/index.php"}),
        "https://www.example.com/main/index.php": FakeResponse(HOME),
        "https://www.example.com/goods/goods_view.php?goodsNo=11": FakeResponse("<title>예시상점:::상품 하나</title>"),
        "https://www.example.com/goods/goods_view.php?goodsNo=14": FakeResponse("<title>예시상점 - 상품 하나</title>"),
        "https://www.example.com/goods/goods_view.php?goodsNo=12": FakeResponse("", status=404),
        "https://www.example.com/goods/goods_view.php?goodsNo=13": FakeResponse("<title>상품 셋 | 예시상점</title>"),
    }
    with patch.object(nkr, "_get", side_effect=routed(routes, calls)):
        result = nkr.run_candidates("https://www.example.com/", DOMAIN, landing_json=str(landing), max_n=20)
    data = result["data"]
    assert data["candidates"] == ["첫째 분류", "둘째 분류", "여름 모음", "상품 하나", "상품 셋"]
    assert [d["source"] for d in data["candidate_details"]][-2:] == ["product", "product"]
    reasons = {s["path"]: s["reason"] for s in data["skipped_landing_pages"]}
    assert reasons == {"/goods/goods_view.php?goodsNo=11&inflow=naver": "duplicate_product",
                       "/goods/goods_view.php?goodsNo=14": "duplicate_name",
                       "/goods/goods_view.php": "product_page_without_id", "(not set)": "not_set",
                       "/": "not_product", "/goods/goods_view.php?goodsNo=12": "fetch_failed"}
    assert data["landing_list"] == "top_landing_pages"
    # A product page that cannot be read (deleted or sold out) is a warning, not a failed run.
    assert result["status"] == "success" and result["error"] is None
    assert any("goodsNo=12" in w and "404" in w for w in data["warnings"])
    assert data["unconfirmed_list"] is True and all("Cookie" not in h for _, h in calls)
    report = nkr.render_candidates_markdown(result)
    assert "사람이 확인하지 않은 후보" in report and not any(d in report for d in DASHES)


def test_product_key_uses_the_product_id_parameter():
    assert nkr.product_key("https://www.example.com/goods/goods_view.php?goodsNo=21&mtn=1") == "goodsno=21"
    assert nkr.product_key("https://www.example.com/goods/goods_view.php?mtn=5&goodsNo=21") == "goodsno=21"
    assert nkr.product_key("https://www.example.com/product/detail.html?product_no=7&cate_no=3") == "product_no=7"
    assert nkr.product_key("https://www.example.com/shop/item.html?branduid=9") == "branduid=9"
    no_id = nkr.product_key("https://www.example.com/product/name-of-item/")
    assert no_id == nkr.product_key("https://www.example.com/product/name-of-item/") and "=" not in no_id


def test_top_product_urls_keep_one_row_per_product_id(tmp_path):
    landing = tmp_path / "traffic.json"
    landing.write_text(json.dumps({"data": {
        "top_landing_pages": [{"landing_page": "/goods/goods_view.php", "sessions": 99}],
        "top_product_urls": [
            {"url": "/goods/goods_view.php?goodsNo=21", "sessions": 30},
            {"url": "/goods/goods_view.php?goodsNo=21&mtn=1", "sessions": 20},
            {"url": "/goods/goods_view.php?mtn=2&goodsNo=21", "sessions": 15},
            {"url": "/goods/goods_view.php?goodsNo=22&mtn=1", "sessions": 10},
        ],
    }}, ensure_ascii=False), encoding="utf-8")
    calls = []
    routes = {
        "https://www.example.com/": FakeResponse(HOME),
        "https://www.example.com/goods/goods_view.php?goodsNo=21": FakeResponse("<title>예시상점:::상품 가</title>"),
        "https://www.example.com/goods/goods_view.php?goodsNo=22&mtn=1": FakeResponse("<title>예시상점:::상품 나</title>"),
    }
    with patch.object(nkr, "_get", side_effect=routed(routes, calls)):
        result = nkr.run_candidates("https://www.example.com/", DOMAIN, landing_json=str(landing), max_n=20)
    data = result["data"]
    assert data["landing_list"] == "top_product_urls" and result["status"] == "success"
    assert [p["name"] for p in data["products"]] == ["상품 가", "상품 나"]
    assert [s["reason"] for s in data["skipped_landing_pages"]] == ["duplicate_product", "duplicate_product"]
    assert len(calls) == 3


def test_config_product_patterns_are_plain_text_like_naver_traffic():
    patterns, warning = nkr.config_product_patterns({"keywords": {"product_patterns": ["item.php?id=", "/goods/"]}})
    assert warning is None
    assert patterns[0].search("/shop/item.php?id=5") and not patterns[0].search("/shop/itemXphp?id=5")
    assert not patterns[1].search("/GOODS/a")
    assert nkr.config_product_patterns({"keywords": {"max": 5}}) == (None, None)
    assert nkr.config_product_patterns(None) == (None, None)
    for bad in ([], [""], "goods_view", [1]):
        got, warning = nkr.config_product_patterns({"keywords": {"product_patterns": bad}})
        assert got is None and "product_patterns" in warning


def test_candidates_use_config_product_patterns(tmp_path, capsys):
    landing = tmp_path / "traffic.json"
    landing.write_text(json.dumps({"data": {"top_landing_pages": [
        {"path": "/item/blue-cup", "sessions": 30},
        {"path": "/goods/goods_view.php", "sessions": 20},
        {"path": "/goods/goods_view.php?goodsNo=3", "sessions": 10},
    ]}}), encoding="utf-8")
    config = tmp_path / "naver.json"
    config.write_text(json.dumps({"site": "https://www.example.com",
                                  "keywords": {"product_patterns": ["/item/", "goods_view"]}}), encoding="utf-8")
    routes = {
        "https://www.example.com/": FakeResponse(HOME),
        "https://www.example.com/item/blue-cup": FakeResponse("<title>예시상점:::파란 컵</title>"),
        "https://www.example.com/goods/goods_view.php?goodsNo=3": FakeResponse("<title>예시상점:::상품 다</title>"),
    }
    out = tmp_path / "out"
    with patch.object(nkr, "_get", side_effect=routed(routes, [])):
        code = nkr.main(["candidates", "--site", "https://www.example.com", "--landing-json", str(landing),
                         "--config", str(config), "--output-dir", str(out), "--json"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["product_patterns_from"] == "config keywords.product_patterns"
    assert [p["name"] for p in data["products"]] == ["파란 컵", "상품 다"]
    reasons = {s["path"]: s["reason"] for s in data["skipped_landing_pages"]}
    assert reasons == {"/goods/goods_view.php": "product_page_without_id"}


def test_run_candidates_without_landing_or_categories():
    with patch.object(nkr, "_get", side_effect=lambda url, headers: FakeResponse("<title>x</title>")):
        result = nkr.run_candidates("https://www.example.com/", DOMAIN)
    assert result["status"] == "error" and "no category links" in result["error"]
    assert result["data"]["candidates"] == [] and result["data"]["warnings"]
    with patch.object(nkr, "_get", side_effect=lambda url, headers: FakeResponse(HOME)):
        result = nkr.run_candidates("https://www.example.com/", DOMAIN, max_n=2)
    assert result["status"] == "success" and result["data"]["candidates"] == ["첫째 분류", "둘째 분류"]


def test_redirect_off_the_site_is_not_followed():
    with patch.object(nkr, "_get", side_effect=lambda url, headers: FakeResponse(
            "", status=302, headers={"Location": "https://other.test/"})):
        result = nkr.run_candidates("https://www.example.com/", DOMAIN)
    assert result["status"] == "error" and "leaves the site" in result["error"]


# --- config file: keywords.candidates and keywords.max ----------------------

def write_config(path: Path, value) -> bytes:
    raw = json.dumps(value, ensure_ascii=False, indent=4).encode("utf-8")
    path.write_bytes(raw)
    return raw


FULL_CONFIG = {
    "site": "https://www.example.com",
    "ai_briefing": {"devices": ["mo"], "queries": ["질문 하나"], "brands": [{"name": "예시", "domains": ["example.com"]}]},
    "keywords": {"list": ["손으로 고친 것"], "candidates": [], "max": 20, "product_patterns": ["goodsNo="]},
    "crawl_check": {"sample_size": 50},
}


def test_default_config_path():
    assert Path(ORIGINAL_DEFAULT_CONFIG).parts[-3:] == (".config", "claude-seo", "naver.json")


def test_save_candidates_writes_when_empty_and_keeps_every_other_key(tmp_path):
    path = tmp_path / "naver.json"
    write_config(path, FULL_CONFIG)
    out = nkr.save_candidates_to_config(str(path), ["첫째 분류", "상품 하나"])
    assert out["written"] is True and out["count"] == 2 and out["reason"] is None
    text = path.read_text(encoding="utf-8")
    saved = json.loads(text)
    expected = json.loads(json.dumps(FULL_CONFIG))
    expected["keywords"]["candidates"] = ["첫째 분류", "상품 하나"]
    assert saved == expected and list(saved) == list(FULL_CONFIG) and list(saved["keywords"]) == list(FULL_CONFIG["keywords"])
    assert "첫째 분류" in text and "\\u" not in text and text.endswith("}\n")
    assert '\n  "site"' in text  # indent 2
    assert sorted(p.name for p in tmp_path.iterdir()) == ["naver.json"]  # no temp file left


def test_save_candidates_adds_the_key_when_it_is_missing(tmp_path):
    path = tmp_path / "naver.json"
    write_config(path, {"site": "https://www.example.com"})
    assert nkr.save_candidates_to_config(str(path), ["가"])["written"] is True
    assert json.loads(path.read_text(encoding="utf-8")) == {"site": "https://www.example.com",
                                                            "keywords": {"candidates": ["가"]}}
    write_config(path, {"keywords": {"list": [], "max": 5}})
    assert nkr.save_candidates_to_config(str(path), ["나"])["written"] is True
    assert json.loads(path.read_text(encoding="utf-8")) == {"keywords": {"list": [], "max": 5, "candidates": ["나"]}}


def test_save_candidates_does_not_overwrite_a_filled_list(tmp_path):
    path = tmp_path / "naver.json"
    before = write_config(path, {"keywords": {"candidates": ["이미 있음", "둘"], "list": []}})
    out = nkr.save_candidates_to_config(str(path), ["새 후보"])
    assert out["written"] is False and "already has 2 entries" in out["reason"]
    assert path.read_bytes() == before


@pytest.mark.parametrize("raw", [
    b'{"keywords": ["not", "an", "object"]}',
    b'{"keywords": {"candidates": "a string"}}',
    b'{"keywords": {broken',
    b'["a list"]',
])
def test_save_candidates_leaves_unusable_configs_alone(tmp_path, raw):
    path = tmp_path / "naver.json"
    path.write_bytes(raw)
    out = nkr.save_candidates_to_config(str(path), ["가"])
    assert out["written"] is False and out["reason"] and path.read_bytes() == raw


def test_save_candidates_never_creates_the_config_file(tmp_path):
    path = tmp_path / "naver.json"
    out = nkr.save_candidates_to_config(str(path), ["가"])
    assert out["written"] is False and "does not exist" in out["reason"] and not path.exists()
    write_config(path, {"keywords": {"candidates": []}})
    assert nkr.save_candidates_to_config(str(path), [])["written"] is False


def test_failed_replace_keeps_the_original_and_removes_the_temp_file(tmp_path):
    path = tmp_path / "naver.json"
    before = write_config(path, {"keywords": {"candidates": []}})
    with patch.object(nkr.os, "replace", side_effect=OSError("disk full")):
        out = nkr.save_candidates_to_config(str(path), ["가"])
    assert out["written"] is False and "disk full" in out["reason"]
    assert path.read_bytes() == before and sorted(p.name for p in tmp_path.iterdir()) == ["naver.json"]


def test_config_max_reads_only_whole_numbers():
    assert nkr.config_max({"keywords": {"max": 7}}) == (7, None)
    assert nkr.config_max({"keywords": {}}) == (None, None) and nkr.config_max(None) == (None, None)
    for bad in (0, True, "5", 2.5):
        value, warning = nkr.config_max({"keywords": {"max": bad}})
        assert value is None and "keywords.max" in warning


def run_cli(argv: list, capsys, page: str = HOME) -> tuple[int, dict]:
    with patch.object(nkr, "_get", side_effect=lambda url, headers: FakeResponse(page)):
        code = nkr.main(argv + ["--json"])
    return code, json.loads(capsys.readouterr().out)


def test_cli_candidates_save_to_config_then_refuse_the_second_time(tmp_path, capsys):
    path = tmp_path / "naver.json"
    write_config(path, {"keywords": {"list": [], "candidates": [], "max": 2}, "site": "https://www.example.com"})
    argv = ["candidates", "--site", "https://www.example.com", "--config", str(path), "--save-to-config"]
    code, out = run_cli(argv, capsys)
    data = out["data"]
    assert code == 0 and data["max"] == 2 and data["max_source"] == "config keywords.max"
    assert data["candidates"] == ["첫째 분류", "둘째 분류"]
    assert data["config_write"]["written"] is True and data["config_write"]["count"] == 2
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved == {"keywords": {"list": [], "candidates": ["첫째 분류", "둘째 분류"], "max": 2},
                     "site": "https://www.example.com"}
    after_first = path.read_bytes()

    code, out = run_cli(argv + ["--max", "3"], capsys)
    data = out["data"]
    assert code == 0 and data["max"] == 3 and data["max_source"] == "--max" and len(data["candidates"]) == 3
    assert data["config_write"]["written"] is False and "already has 2 entries" in data["config_write"]["reason"]
    assert path.read_bytes() == after_first

    with patch.object(nkr, "_get", side_effect=lambda url, headers: FakeResponse(HOME)):
        assert nkr.main(argv + ["--output-dir", str(tmp_path / "out")]) == 0
    printed = capsys.readouterr().out
    assert "config: not written (keywords.candidates already has 2 entries; not changed)" in printed
    report = (tmp_path / "out" / "NAVER-KEYWORD-CANDIDATES.md").read_text(encoding="utf-8")
    assert "설정 파일에 적지 않았습니다" in report and "상한 2개(설정 keywords.max)" in report
    assert not any(d in report for d in DASHES)


def test_cli_candidates_without_the_flag_does_not_write(tmp_path, capsys):
    path = tmp_path / "naver.json"
    before = write_config(path, {"keywords": {"candidates": []}})
    code, out = run_cli(["candidates", "--site", "https://www.example.com", "--config", str(path)], capsys)
    assert code == 0 and "config_write" not in out["data"] and path.read_bytes() == before
    assert out["data"]["max"] == nkr.DEFAULT_MAX and out["data"]["max_source"] == "default"


def test_cli_candidates_does_not_write_a_failed_result(tmp_path, capsys):
    path = tmp_path / "naver.json"
    before = write_config(path, {"keywords": {"candidates": []}})
    code, out = run_cli(["candidates", "--site", "https://www.example.com", "--config", str(path),
                         "--save-to-config"], capsys, page="<title>x</title>")
    assert code == 1 and out["data"]["config_write"]["written"] is False
    assert "status is error" in out["data"]["config_write"]["reason"] and path.read_bytes() == before


def test_cli_rank_takes_the_cap_from_the_config(tmp_path, capsys):
    path = tmp_path / "naver.json"
    write_config(path, {"keywords": {"max": 1}})
    page = pc_page(["https://other.test/"])
    with patch.object(nkr, "_get", side_effect=lambda url, headers: FakeResponse(page)):
        code = nkr.main(["rank", "--site", "https://www.example.com", "--keyword", "a", "--keyword", "b",
                         "--config", str(path), "--json"])
    data = json.loads(capsys.readouterr().out)["data"]
    assert code == 0 and data["keywords"] == ["a"] and data["dropped_keywords"] == ["b"]
    assert data["max_source"] == "config keywords.max"


def test_cli_unreadable_config_falls_back_to_the_default_cap_with_a_warning(tmp_path, capsys):
    path = tmp_path / "naver.json"
    path.write_bytes(b"{broken")
    code, out = run_cli(["candidates", "--site", "https://www.example.com", "--config", str(path)], capsys)
    assert code == 0 and out["data"]["max"] == nkr.DEFAULT_MAX and out["data"]["max_source"] == "default"
    assert any("could not read the config" in w for w in out["data"]["warnings"])
    assert path.read_bytes() == b"{broken"


def test_script_has_no_dash_characters():
    source = (ROOT / "scripts" / "naver_keyword_rank.py").read_text(encoding="utf-8")
    assert not any(d in source for d in DASHES)
