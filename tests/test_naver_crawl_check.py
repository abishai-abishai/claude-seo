"""Naver crawl check as Yeti (docs/NAVER.md section 7). No network: the HTTP layer is mocked."""

from __future__ import annotations

import gzip
import json
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

requests = pytest.importorskip("requests")
import naver_crawl_check as ncc  # noqa: E402

SITE = "https://www.example.com/"

HOME = """<!doctype html><html><head>
<title>Example Shop</title>
<link rel="canonical" href="https://www.example.com/">
<link rel="alternate" type="application/rss+xml" href="https://www.example.com/rss.xml">
<script>function goCart() { location.href = '/order/cart.php'; }</script>
</head><body>
<a href="/goods/goods_view.php?goodsNo=1">상품</a>
<a href="javascript:void(0)">닫기</a>
<a href="#" onclick="location.href='/goods/goods_list.php?cateCd=001'">분류</a>
<div class="banner" onclick="location.href='/event'">행사</div>
</body></html>"""

BARE_HOME = HOME.replace("Example Shop", "Example Shop (bare)")

STUB = """<!doctype html>
<html lang="ko">
<head>
    <!--<title>결과</title>-->
    <meta http-equiv="Content-Type" content="text/html; charset=utf-8" />
    <script type="text/javascript">
    alert('해당 상품은 현재 구매가 불가한 상품입니다.');
    location.replace("https://www.example.com:443");
    </script>
</head>
<body>
</body>
</html>"""

PRODUCT = """<html><head><title>Product 1</title></head><body>
<script>
$(function() {
    $('.btn_move_cart').click(function() { location.href = '../order/cart.php'; });
});
function gd_goods_order(modeStr) {
    if (modeStr == 'w') { document.location.href = "../member/login.php"; }
}
</script><a href="/goods/goods_view.php?goodsNo=2">다른 상품</a></body></html>"""

ROBOTS = """User-agent: *
Allow: /

User-agent: Googlebot
User-agent: yeti
Disallow: /admin/

User-agent: BadBot
Disallow: /

# second group that names Yeti again
User-agent: Other
User-agent: YETI
Disallow: /goods/goods_list.php
Crawl-delay: 10
"""

INDEX = """<?xml version="1.0"?><sitemapindex>
<sitemap><loc>https://www.example.com/sitemap-goods.xml</loc></sitemap>
<sitemap><loc>https://www.example.com/sitemap-etc.xml</loc></sitemap>
</sitemapindex>"""

GOODS_SITEMAP = """<urlset>
<url><loc>https://www.example.com/goods/goods_view.php?goodsNo=1</loc></url>
<url><loc>https://example.com/goods/goods_view.php?goodsNo=2</loc></url>
<url><loc>https://www.example.com/goods/goods_list.php?cateCd=001&amp;sort=new</loc></url>
</urlset>"""

ETC_SITEMAP = """<urlset>
<url><loc>https://www.example.com/goods/event_sale.php?sno=1</loc></url>
<url><loc>https://www.example.com/</loc></url>
<url><loc>https://www.example.com/about</loc></url>
</urlset>"""


class FakeResponse:
    def __init__(self, body="", status: int = 200, headers: dict | None = None):
        self.status_code = status
        self.content = body if isinstance(body, bytes) else body.encode("utf-8")
        self.headers = {"Content-Type": "text/html; charset=utf-8", **(headers or {})}


def redirect(location: str, status: int = 301) -> FakeResponse:
    return FakeResponse("", status=status, headers={"Location": location})


def site_routes() -> dict:
    return {
        "https://www.example.com/": FakeResponse(HOME),
        "https://example.com/": FakeResponse(BARE_HOME),
        "https://m.example.com/": redirect("https://www.example.com/"),
        "https://www.example.com/robots.txt": FakeResponse(ROBOTS, headers={"Content-Type": "text/plain"}),
        "https://www.example.com/sitemap.xml": FakeResponse(INDEX),
        "https://www.example.com/sitemap-goods.xml": FakeResponse(GOODS_SITEMAP),
        "https://www.example.com/sitemap-etc.xml": FakeResponse(ETC_SITEMAP),
        "https://www.example.com/goods/goods_view.php?goodsNo=1": FakeResponse(PRODUCT),
        "https://example.com/goods/goods_view.php?goodsNo=2": FakeResponse(STUB),
        "https://www.example.com/goods/goods_list.php?cateCd=001&sort=new":
            FakeResponse("<title>Category 001</title>"),
        "https://www.example.com/goods/event_sale.php?sno=1": FakeResponse("<title>Example Shop</title>"),
        "https://www.example.com/about": FakeResponse("not found", status=404),
    }


def run(routes: dict, **kwargs) -> tuple[dict, list]:
    calls = []

    def fake_get(url, headers):
        calls.append((url, headers["User-Agent"]))
        response = routes.get(url)
        if response is None:
            raise requests.exceptions.ConnectionError(f"no route to {url}")
        return response(headers["User-Agent"]) if callable(response) else response

    with patch.object(ncc, "_get", side_effect=fake_get):
        result = ncc.run_check(SITE, **kwargs)
    return result, calls


def checks(result: dict) -> dict:
    return {c["id"]: c for c in result["data"]["checks"]}


def issue_ids(result: dict) -> dict:
    return {i["id"]: i for i in result["data"]["issues"]}


# --- user agents and pacing ------------------------------------------------

def test_user_agents_are_the_spec_names():
    assert ncc.YETI_UAS["yeti"] == "Mozilla/5.0 (compatible; Yeti/1.1; +https://naver.me/spd)"
    template = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko; compatible; "
                "Yeti/1.1; +https://naver.me/spd) Chrome/W.X.Y.Z Safari/537.36")
    pattern = re.escape(template).replace(re.escape("W.X.Y.Z"), r"\d+\.\d+\.\d+\.\d+")
    assert re.fullmatch(pattern, ncc.YETI_UAS["yeti_chrome"])
    assert "Yeti" not in ncc.BROWSER_UA and ncc.CHROME_VERSION in ncc.BROWSER_UA


def test_requests_are_one_second_apart():
    assert ncc.REQUEST_DELAY == 1.0
    ncc._last_request_time = 100.0
    with patch.object(ncc.time, "time", side_effect=[100.25, 101.0]), patch.object(ncc.time, "sleep") as sleep:
        ncc._rate_limit()
    assert sleep.call_args[0][0] == pytest.approx(0.75)
    ncc._last_request_time = 0.0


def test_fetch_follows_redirects_hop_by_hop_only_when_asked():
    routes = {"https://www.example.com/a": redirect("/b"), "https://www.example.com/b": FakeResponse("ok")}
    with patch.object(ncc, "_get", side_effect=lambda url, headers: routes[url]):
        followed = ncc.fetch("https://www.example.com/a", "yeti", follow=True)
        single = ncc.fetch("https://www.example.com/a", "yeti")
    assert followed["status"] == 200 and followed["final_url"] == "https://www.example.com/b"
    assert [hop["status"] for hop in followed["chain"]] == [301, 200]
    assert single["status"] == 301 and single["location"] == "/b"


def test_fetch_stops_on_redirect_loop_and_records_errors():
    with patch.object(ncc, "_get", side_effect=lambda url, headers: redirect(url)):
        loop = ncc.fetch("https://www.example.com/", "yeti", follow=True)
    assert "redirects" in loop["error"] and len(loop["chain"]) == ncc.MAX_REDIRECTS + 1
    with patch.object(ncc, "_get", side_effect=requests.exceptions.ConnectionError("down")):
        down = ncc.fetch("https://www.example.com/", "yeti")
    assert down["status"] is None and "ConnectionError" in down["error"]


# --- robots.txt --------------------------------------------------------------

def test_robots_merges_every_group_that_names_yeti():
    groups, sitemaps = ncc.parse_robots(ROBOTS + "Sitemap: https://www.example.com/s.xml\n")
    used, chosen = ncc.select_groups(groups)
    assert used == "yeti" and len(chosen) == 2
    assert [g["agents"] for g in chosen] == [["Googlebot", "yeti"], ["Other", "YETI"]]
    assert [r["path"] for g in chosen for r in g["rules"]] == ["/admin/", "/goods/goods_list.php"]
    assert chosen[1]["crawl_delay"] == ["10"]
    assert sitemaps == ["https://www.example.com/s.xml"]


def test_robots_falls_back_to_star_groups_then_none():
    used, chosen = ncc.select_groups(ncc.parse_robots("User-agent: *\nDisallow: /tmp/\nUser-agent: Googlebot\n"
                                                      "Disallow: /\n")[0])
    assert used == "*" and chosen[0]["rules"] == [{"type": "disallow", "path": "/tmp/"}]
    assert ncc.select_groups(ncc.parse_robots("User-agent: Googlebot\nDisallow: /\n")[0]) == ("none", [])
    with_bom = ncc.parse_robots(chr(0xFEFF) + "User-agent: Yeti\nDisallow: /x\n")[0]
    assert ncc.select_groups(with_bom)[0] == "yeti"


def test_robots_rule_matching_longest_match_and_allow_tie():
    rules = [{"type": "disallow", "path": "/goods/"}, {"type": "allow", "path": "/goods/goods_view.php"},
             {"type": "disallow", "path": "/*.pdf$"}, {"type": "disallow", "path": ""},
             {"type": "disallow", "path": "/same"}, {"type": "allow", "path": "/same"}]
    assert ncc.is_allowed(rules, "/goods/goods_view.php?goodsNo=1")
    assert not ncc.is_allowed(rules, "/goods/goods_list.php")
    assert not ncc.is_allowed(rules, "/files/a.pdf") and ncc.is_allowed(rules, "/files/a.pdf?x=1")
    assert ncc.is_allowed(rules, "/same/x") and ncc.is_allowed(rules, "/other")
    assert ncc.robots_target("https://www.example.com/a.php?b=1") == "/a.php?b=1"


# --- reading pages -----------------------------------------------------------

def test_stub_page_is_a_js_redirect_with_no_title():
    facts = ncc.page_facts(STUB)
    assert facts["title"] is None
    assert [j["target"] for j in facts["js_redirects"]] == ["https://www.example.com:443"]


def test_navigation_inside_functions_strings_and_comments_is_not_a_redirect():
    assert ncc.page_facts(PRODUCT)["js_redirects"] == []
    assert ncc.page_facts(HOME)["js_redirects"] == []
    page = """<script>
    var tpl = "location.replace('/x')"; // location.href = '/y';
    /* location.href = '/z'; */
    var re = /[{]/; btn.onclick = () => location.href = '/cart';
    var obj = { go() { location.href = '/m'; } };
    </script>
    <script type="application/ld+json">{"a": "location.href = '/j'"}</script>
    <script src="/app.js">location.replace('/s')</script>
    <!-- <script>location.replace('/commented')</script> -->"""
    assert ncc.page_facts(page)["js_redirects"] == []


def test_top_level_navigation_in_blocks_and_legacy_comment_scripts_counts():
    page = """<script><!--
    if (!window.ok) { top.location.href = '/login.php'; }
    //--></script><script>var re = /a{2}/; window.location.replace('/next');</script>"""
    targets = [j["target"] for j in ncc.page_facts(page)["js_redirects"]]
    assert targets == ["/login.php", "/next"]


def test_homepage_facts():
    facts = ncc.page_facts(HOME, {"x-robots-tag": "googlebot: noindex"})
    assert facts["title"] == "Example Shop" and facts["canonical"] == "https://www.example.com/"
    assert facts["links"] == 1 and facts["javascript_hrefs"] == 1
    assert facts["js_only_links"] == 2 and len(facts["js_only_link_examples"]) == 2
    assert facts["rss"] == ["https://www.example.com/rss.xml"] and facts["frameset"] is False
    assert ncc._x_robots_for_yeti(facts["x_robots_tag"]) == set()
    assert ncc._x_robots_for_yeti("yeti: noindex, nofollow") == {"noindex", "nofollow"}
    assert ncc._x_robots_for_yeti("noarchive") == {"noarchive"}


# --- site, hosts, sitemap, sample ----------------------------------------------

def test_site_and_registrable_domain():
    assert ncc.normalize_site("www.Example.com/shop") == "https://www.example.com/"
    assert ncc.normalize_site("http://example.com:8080/") == "http://example.com:8080/"
    assert ncc.normalize_site("ftp://example.com") is None and ncc.normalize_site("localhost") is None
    assert ncc.registrable_domain("www.example.com") == "example.com"
    assert ncc.registrable_domain("m.shop.example.co.kr") == "example.co.kr"
    assert ncc.registrable_domain("example.kr") == "example.kr"


def _locs(products: int, plain: int, categories: int) -> list:
    return ([f"https://www.example.com/goods/goods_view.php?goodsNo={i}" for i in range(products)]
            + [f"https://www.example.com/goods/goods_list.php?cateCd={i}" for i in range(categories)]
            + [f"https://www.example.com/goods/event_sale.php?sno={i}" for i in range(plain)])


def test_sample_keeps_at_most_80_percent_products_and_prefers_plain_pages():
    sample, comp = ncc.choose_sample(_locs(20, 5, 5), 10)
    assert comp["product_cap"] == 8 and (comp["product"], comp["non_product"]) == (8, 2)
    assert comp["non_product_category"] == 0
    assert sample[8:] == ["https://www.example.com/goods/event_sale.php?sno=0",
                          "https://www.example.com/goods/event_sale.php?sno=1"]
    assert comp["available"] == {"product": 20, "category": 5, "other": 5}
    assert ncc.choose_sample(_locs(20, 5, 5), 7)[1]["product"] == 5      # 80% of 7 = 5.6, rounded down
    assert ncc.choose_sample(_locs(60, 20, 0), 50)[1]["product"] == 40


def test_sample_categories_come_after_plain_pages_and_sides_fill_each_other():
    sample, comp = ncc.choose_sample(_locs(2, 5, 15), 10)
    assert (comp["product"], comp["non_product"], comp["non_product_category"]) == (2, 8, 3)
    assert [u for u in sample if "goods_list" in u] == [f"https://www.example.com/goods/goods_list.php?cateCd={i}"
                                                        for i in range(3)]
    sample, comp = ncc.choose_sample(_locs(20, 1, 0), 10)
    assert (comp["product"], comp["non_product"]) == (9, 1) and len(sample) == 10
    sample, comp = ncc.choose_sample(_locs(0, 3, 30), 10)
    assert (comp["product"], comp["non_product"]) == (0, 10)
    assert ncc.choose_sample(_locs(5, 0, 0), 1)[1]["product"] == 1
    assert ncc.choose_sample(_locs(5, 2, 0), 1)[1]["non_product"] == 1


def test_sample_drops_repeats_and_stays_under_the_size():
    locs = ["https://www.example.com/a", "https://www.example.com/goods/goods_view.php?goodsNo=1",
            "https://www.example.com/a", "https://www.example.com/product/9"]
    sample, comp = ncc.choose_sample(locs, 50)
    assert sample == ["https://www.example.com/goods/goods_view.php?goodsNo=1",
                      "https://www.example.com/product/9", "https://www.example.com/a"]
    assert (comp["product"], comp["non_product"]) == (2, 1)


def test_sample_size_from_cli_then_config_then_default(tmp_path):
    config = tmp_path / "naver.json"
    missing = str(tmp_path / "missing.json")
    assert ncc.resolve_sample_size(missing) == (50, "default")
    config.write_text(json.dumps({"site": "https://www.example.com"}), encoding="utf-8")
    assert ncc.resolve_sample_size(str(config)) == (50, "default")
    config.write_text(json.dumps({"crawl_check": {}}), encoding="utf-8")
    assert ncc.resolve_sample_size(str(config)) == (50, "default")
    config.write_text(json.dumps({"crawl_check": {"sample_size": 7}}), encoding="utf-8")
    assert ncc.resolve_sample_size(str(config)) == (7, "config")
    assert ncc.resolve_sample_size(str(config), 3) == (3, "cli")
    for bad in ({"crawl_check": {"sample_size": "50"}}, {"crawl_check": {"sample_size": True}},
                {"crawl_check": {"sample_size": 0}}, {"crawl_check": 50}):
        config.write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(ncc.SettingsError):
            ncc.resolve_sample_size(str(config))
    config.write_text("{not json", encoding="utf-8")
    with pytest.raises(ncc.SettingsError):
        ncc.resolve_sample_size(str(config))
    with pytest.raises(ncc.SettingsError):
        ncc.resolve_sample_size(missing, 0)


def test_sitemap_index_one_level_gzip_and_limits():
    child = "https://www.example.com/s1.xml.gz"
    nested = "https://www.example.com/nested.xml"
    routes = {
        "https://www.example.com/index.xml": FakeResponse(
            "<sitemapindex>" + "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in (child, nested)) + "</sitemapindex>"),
        child: FakeResponse(gzip.compress(b"<urlset><url><loc>https://www.example.com/x</loc></url></urlset>"),
                            headers={"Content-Type": "application/octet-stream"}),
        nested: FakeResponse(INDEX),
    }
    with patch.object(ncc, "_get", side_effect=lambda url, headers: routes[url]):
        out = ncc.read_sitemaps(["https://www.example.com/index.xml"])
    assert out["locs"] == ["https://www.example.com/x"] and out["read"] == 1
    assert any("nested sitemap index not followed" in e for e in out["errors"])

    many = "<sitemapindex>" + "".join(f"<sitemap><loc>https://www.example.com/c{i}.xml</loc></sitemap>"
                                      for i in range(ncc.MAX_CHILD_SITEMAPS + 5)) + "</sitemapindex>"
    calls = []

    def fake_get(url, headers):
        calls.append(url)
        return FakeResponse(many) if url.endswith("index.xml") else FakeResponse("<urlset></urlset>")

    with patch.object(ncc, "_get", side_effect=fake_get):
        out = ncc.read_sitemaps(["https://www.example.com/index.xml"])
    assert len(calls) == 1 + ncc.MAX_CHILD_SITEMAPS
    assert any("read the first" in e for e in out["errors"])


# --- full run ----------------------------------------------------------------

def test_full_run_finds_every_problem_of_the_spec_table():
    result, calls = run(site_routes())
    assert result["status"] == "success", result["error"]
    assert calls[0] == (SITE, ncc.YETI_UAS["yeti_chrome"])
    assert {ua for url, ua in calls[:3]} == set(ncc.USER_AGENTS.values())
    assert ("https://www.example.com/robots.txt", ncc.YETI_UAS["yeti"]) in calls
    c = checks(result)
    assert list(c) == ["yeti_blocked", "robots_txt", "hosts", "sitemap_host", "js_redirect", "same_title", "homepage"]
    assert all(check["status"] == "measured" for check in c.values())

    assert c["yeti_blocked"]["ok"] is True
    robots = c["robots_txt"]["evidence"]
    assert c["robots_txt"]["ok"] is False and robots["groups_used"] == "yeti" and len(robots["groups"]) == 2
    assert robots["crawl_delay"] == ["10"]
    assert (robots["product_category_tested"], robots["product_category_blocked"]) == (3, 1)
    assert robots["sample_blocked"] == ["https://www.example.com/goods/goods_list.php?cateCd=001&sort=new"]

    hosts = {r["host"]: r for r in c["hosts"]["evidence"]["hosts"]}
    assert hosts["www.example.com"]["status"] == 200 and hosts["example.com"]["status"] == 200
    assert hosts["m.example.com"]["status"] == 301 and hosts["m.example.com"]["location"] == "https://www.example.com/"
    assert hosts["example.com"]["canonical"] == "https://www.example.com/"

    sitemap = c["sitemap_host"]["evidence"]
    assert sitemap["total_locs"] == 6 and sitemap["off_host"] == 1
    assert sitemap["by_host"] == {"www.example.com": 5, "example.com": 1}

    sample = [r["url"] for r in result["data"]["sample"]["urls"]]
    assert sample[:2] == ["https://www.example.com/goods/goods_view.php?goodsNo=1",
                          "https://example.com/goods/goods_view.php?goodsNo=2"]
    assert sample[-1] == "https://www.example.com/goods/goods_list.php?cateCd=001&sort=new"
    composition = result["data"]["sample"]["composition"]
    assert (composition["product"], composition["non_product"], composition["non_product_category"]) == (2, 4, 1)
    assert result["data"]["sample"]["size_from"] == "cli"
    js = c["js_redirect"]["evidence"]
    assert (js["sampled"], js["fetched_200"], js["js_redirect"]) == (6, 5, 1)
    assert (js["sample_product"], js["sample_non_product"]) == (2, 4)
    assert js["pages"][0]["targets"] == ["https://www.example.com:443"]
    same = c["same_title"]["evidence"]
    assert same["urls"] == ["https://www.example.com/goods/event_sale.php?sno=1"] and same["compared"] == 3
    assert same["untitled"] == ["https://example.com/goods/goods_view.php?goodsNo=2"]
    home = c["homepage"]["evidence"]
    assert home["js_only_links"] == 2 and home["rss_present"] is True and home["noindex"] is False

    issues = issue_ids(result)
    assert set(issues) == {"robots_txt.blocked", "hosts.multiple_200", "sitemap_host.off_host", "js_redirect.pages",
                           "same_title.home", "homepage.js_only_links"}
    assert issues["robots_txt.blocked"]["severity"] == "High"
    assert issues["hosts.multiple_200"]["severity"] == "Medium"
    assert issues["sitemap_host.off_host"]["severity"] == "High"
    assert issues["sitemap_host.off_host"]["urls"] == ["https://example.com/goods/goods_view.php?goodsNo=2"]
    severities = [i["severity"] for i in result["data"]["issues"]]
    assert severities == sorted(severities, key=ncc.SEVERITY_ORDER.index)
    for issue in result["data"]["issues"]:
        assert set(issue) >= {"id", "check", "severity", "description", "urls"}
    assert result["metadata"]["source"] == "naver_crawl_check" and "section 7" in result["metadata"]["spec"]


def test_sitemap_from_robots_line_and_cli_override():
    routes = site_routes()
    routes["https://www.example.com/robots.txt"] = FakeResponse(
        "User-agent: *\nDisallow:\nSitemap: https://www.example.com/sitemap-goods.xml\n")
    result, calls = run(routes)
    urls = [u for u, _ in calls]
    assert "https://www.example.com/sitemap-goods.xml" in urls and "https://www.example.com/sitemap.xml" not in urls
    assert checks(result)["robots_txt"]["evidence"]["groups_used"] == "*"
    result, calls = run(site_routes(), sitemap="https://www.example.com/sitemap-etc.xml")
    assert checks(result)["sitemap_host"]["evidence"]["total_locs"] == 3


def test_missing_sitemap_is_not_measured_not_zero():
    routes = site_routes()
    routes["https://www.example.com/sitemap.xml"] = FakeResponse("", status=404)
    result, _ = run(routes)
    c = checks(result)
    assert result["status"] == "partial" and "HTTP 404" in result["error"]
    for cid in ("robots_txt", "sitemap_host", "js_redirect", "same_title"):
        assert c[cid]["status"] == "not_measured" and c[cid]["ok"] is None and c[cid]["reason"]
    assert "off_host" not in c["sitemap_host"]["evidence"] and "js_redirect" not in c["js_redirect"]["evidence"]
    assert "product_category_blocked" not in c["robots_txt"]["evidence"]
    assert c["robots_txt"]["evidence"]["crawl_delay"] == ["10"]
    assert result["data"]["sitemap"]["total_locs"] is None
    assert set(result["data"]["summary"]["not_measured"]) == {"robots_txt", "sitemap_host", "js_redirect", "same_title"}
    assert "못 잼" in ncc.render_markdown(result)


def test_yeti_blocked_while_browser_gets_the_page_is_critical():
    routes = site_routes()
    routes[SITE] = lambda ua: FakeResponse(HOME) if ua == ncc.BROWSER_UA else FakeResponse("denied", status=403)
    result, _ = run(routes)
    c = checks(result)
    assert c["yeti_blocked"]["ok"] is False
    assert c["yeti_blocked"]["evidence"]["yeti"]["status"] == 403
    assert issue_ids(result)["yeti_blocked.response"]["severity"] == "Critical"
    assert c["homepage"]["status"] == "not_measured" and c["same_title"]["status"] == "not_measured"


def test_yeti_sent_somewhere_else_than_the_browser_is_high():
    routes = site_routes()
    routes[SITE] = lambda ua: FakeResponse(HOME) if ua == ncc.BROWSER_UA else redirect("/robot-page")
    routes["https://www.example.com/robot-page"] = FakeResponse(HOME)
    result, _ = run(routes)
    assert checks(result)["yeti_blocked"]["ok"] is False
    assert issue_ids(result)["yeti_blocked.response"]["severity"] == "High"


def test_homepage_noindex_yeti_meta_and_frameset():
    page = ('<html><head><title>Example Shop</title><meta name="robots" content="NOINDEX, follow">'
            '<meta name="Yeti" content="nofollow"></head><frameset><frame src="/main"></frameset></html>')
    routes = site_routes()
    routes[SITE] = FakeResponse(page)
    result, _ = run(routes)
    issues = issue_ids(result)
    assert issues["homepage.noindex"]["severity"] == "Critical"
    assert issues["homepage.yeti_meta"]["severity"] == "High"
    assert issues["homepage.frameset"]["severity"] == "High"
    assert "homepage.js_only_links" not in issues
    routes[SITE] = FakeResponse(HOME, headers={"X-Robots-Tag": "yeti: noindex"})
    result, _ = run(routes)
    assert checks(result)["homepage"]["evidence"]["noindex"] is True


def test_one_host_answering_is_fine_and_unreachable_host_is_recorded():
    routes = site_routes()
    routes["https://example.com/"] = redirect("https://www.example.com/")
    del routes["https://m.example.com/"]
    result, _ = run(routes)
    c = checks(result)["hosts"]
    assert c["ok"] is True and c["evidence"]["hosts_200"] == ["www.example.com"]
    m = [r for r in c["evidence"]["hosts"] if r["host"] == "m.example.com"][0]
    assert m["status"] is None and "ConnectionError" in m["error"]


def test_report_and_cli(tmp_path, capsys):
    result, _ = run(site_routes())
    report = ncc.render_markdown(result)
    assert "## 점검" in report and "| 같은 제목 | 문제 |" in report
    assert "\u2014" not in report and "\u2013" not in report

    routes = site_routes()
    config = tmp_path / "naver.json"
    config.write_text(json.dumps({"crawl_check": {"sample_size": 1}}), encoding="utf-8")

    def cli(*extra: str) -> tuple[int, dict]:
        argv = ["naver_crawl_check.py", "--site", "www.example.com", "--json", "--config", str(config), *extra]
        with patch.object(ncc, "_get", side_effect=lambda url, headers: routes[url]), \
                patch.object(sys, "argv", argv):
            code = ncc.main()
        return code, json.loads(capsys.readouterr().out)

    code, printed = cli("--sample-size", "2", "--output-dir", str(tmp_path / "out"))
    assert code == 0 and printed["data"]["sample"]["size_from"] == "cli"
    assert printed["data"]["sample"]["requested_size"] == 2 and len(printed["data"]["sample"]["urls"]) == 2
    assert printed["data"]["sample"]["composition"]["product"] == 1
    assert (tmp_path / "out" / "naver-crawl-check.json").exists()
    assert (tmp_path / "out" / "NAVER-CRAWL-CHECK.md").exists()

    code, printed = cli()
    assert code == 0 and printed["data"]["sample"]["size_from"] == "config"
    assert len(printed["data"]["sample"]["urls"]) == 1

    config.write_text(json.dumps({"crawl_check": {"sample_size": "many"}}), encoding="utf-8")
    code, printed = cli()
    assert code == 1 and printed["status"] == "error" and "sample_size" in printed["error"]

    config.unlink()
    code, printed = cli("--sitemap", "https://www.example.com/sitemap-goods.xml")
    assert code == 0 and printed["data"]["sample"]["size_from"] == "default"

    with patch.object(sys, "argv", ["naver_crawl_check.py", "--site", "not a site", "--json"]):
        assert ncc.main() == 1
    assert json.loads(capsys.readouterr().out)["status"] == "error"
