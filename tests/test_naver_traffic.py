"""GA4 Naver search inflow with ad clicks removed (docs/NAVER.md section 4).

No network and no credentials: a fake GA4 client applies the request's real
FilterExpression to synthetic session groups, so a wrong filter gives wrong
numbers here.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

pytest.importorskip("google.analytics.data_v1beta")
from google.analytics.data_v1beta.types import (  # noqa: E402
    DimensionValue,
    Filter,
    FilterExpression,
    MetricValue,
    ResponseMetaData,
    Row,
    RunReportResponse,
)

import naver_traffic as nt  # noqa: E402

NOW = datetime(2026, 1, 29, 10, 0)
ORGANIC = "Organic Search"


def rec(source, medium, landing_qs, sessions, transactions=0, channel=ORGANIC, date="20260105"):
    landing = landing_qs.split("?")[0]
    return {"sessionSource": source, "sessionMedium": medium, "sessionDefaultChannelGroup": channel,
            "landingPagePlusQueryString": landing_qs, "landingPage": landing, "date": date,
            "sessions": sessions, "transactions": transactions}


# Synthetic session groups. Naver search ad 80 / natural 22 / total 102 sessions.
RECORDS = [
    rec("m.search.naver.com", "referral", "/goods/view?no=1&NaPm=ct%3Dabc", 50, 2, date="20260105"),
    rec("m.search.naver.com", "referral", "/goods/view?no=2", 10, 1, date="20260106"),
    rec("naver", "organic", "/?NaPm=ct%3Dxyz", 30, 0, date="20260106"),
    rec("naver", "organic", "/", 8, 0, date="20260107"),
    rec("search.naver.com", "referral", "(not set)", 4, 0, date="20260107"),
    rec("naver", "cpc", "/?NaPm=ct%3Dpaid", 100, 5, channel="Paid Search"),
    rec("naver", "blog", "/post", 7, 0, channel="Referral"),
    rec("google", "organic", "/about", 40, 3, date="20260108"),
]


def matches(expr, record) -> bool:
    kind = FilterExpression.pb(expr).WhichOneof("expr")
    if kind == "and_group":
        return all(matches(e, record) for e in expr.and_group.expressions)
    if kind == "or_group":
        return any(matches(e, record) for e in expr.or_group.expressions)
    if kind == "not_expression":
        return not matches(expr.not_expression, record)
    flt = expr.filter
    value = record[flt.field_name]
    which = Filter.pb(flt).WhichOneof("one_filter")
    if which == "in_list_filter":
        values = list(flt.in_list_filter.values)
        if flt.in_list_filter.case_sensitive:
            return value in values
        return value.lower() in [v.lower() for v in values]
    if which == "string_filter":
        sf = flt.string_filter
        left, right = (value, sf.value) if sf.case_sensitive else (value.lower(), sf.value.lower())
        if sf.match_type == Filter.StringFilter.MatchType.EXACT:
            return left == right
        if sf.match_type == Filter.StringFilter.MatchType.CONTAINS:
            return right in left
    raise AssertionError(f"filter not handled by fake: {kind} {which}")


def has_not(expr) -> bool:
    kind = FilterExpression.pb(expr).WhichOneof("expr")
    if kind == "not_expression":
        return True
    if kind in ("and_group", "or_group"):
        return any(has_not(e) for e in getattr(expr, kind).expressions)
    return False


class FakeGA4:
    """Evaluates dimension filters over RECORDS, sums metrics per dimension tuple, honours order/limit/offset."""

    def __init__(self, records=RECORDS, fail_on=None, natural_drift=0):
        self.records = records
        self.fail_on = fail_on or (lambda request: False)
        self.natural_drift = natural_drift
        self.requests = []

    def run_report(self, request):
        # limit/offset are copied now: run_all_rows reuses one request object across pages.
        self.requests.append((request, request.limit, request.offset))
        if self.fail_on(request):
            raise RuntimeError("403 PERMISSION_DENIED: no access")
        dims = [d.name for d in request.dimensions]
        metrics = [m.name for m in request.metrics]
        groups = {}
        for record in self.records:
            if request.dimension_filter and not matches(request.dimension_filter, record):
                continue
            key = tuple(record[d] for d in dims)
            sums = groups.setdefault(key, {m: 0 for m in metrics})
            for m in metrics:
                sums[m] += record[m]
        if self.natural_drift and dims == ["sessionSource", "sessionMedium"] and has_not(request.dimension_filter):
            first = sorted(groups)[0]
            groups[first]["sessions"] -= self.natural_drift
        items = list(groups.items())
        for order in request.order_bys:
            if order.metric.metric_name:
                items.sort(key=lambda kv: kv[1][order.metric.metric_name], reverse=order.desc)
            elif order.dimension.dimension_name:
                idx = dims.index(order.dimension.dimension_name)
                items.sort(key=lambda kv: kv[0][idx], reverse=order.desc)
        limit = request.limit or 10000
        page = items[request.offset:request.offset + limit]
        rows = [Row(dimension_values=[DimensionValue(value=v) for v in key],
                    metric_values=[MetricValue(value=str(sums[m])) for m in metrics]) for key, sums in page]
        return RunReportResponse(rows=rows, row_count=len(items), metadata=ResponseMetaData(time_zone="UTC"))


def run(client) -> dict:
    return nt.run_report("123", days=28, client=client, now=NOW)


# --- window and filters ----------------------------------------------------

def test_window_matches_ga4_report_default():
    assert nt.date_window(28, now=NOW) == {"start": "2026-01-01", "end": "2026-01-28", "days": 28}


def test_property_is_resolved_like_ga4_report():
    result = run(FakeGA4())
    assert result["data"]["property"] == "properties/123"


def test_ad_click_filter_is_contains_napm_on_landing_with_query():
    flt = nt._ad_click().filter
    assert flt.field_name == "landingPagePlusQueryString"
    assert flt.string_filter.match_type == Filter.StringFilter.MatchType.CONTAINS
    assert flt.string_filter.value == "NaPm="


# --- counts ----------------------------------------------------------------

def test_natural_ad_and_total_counts():
    result = run(FakeGA4())
    data = result["data"]
    assert result["status"] == "success" and result["error"] is None
    assert data["naver_search"] == {"sessions": 102, "transactions": 3}
    assert data["ad"] == {"sessions": 80, "transactions": 2}
    assert data["natural"] == {"sessions": 22, "transactions": 1}
    assert data["warnings"] == []
    assert data["time_zone"] == "UTC"


def test_cpc_blog_and_other_sources_are_not_naver_search():
    rows = run(FakeGA4())["data"]["by_source_medium"]
    pairs = {(r["source"], r["medium"]) for r in rows}
    assert pairs == {("m.search.naver.com", "referral"), ("naver", "organic"), ("search.naver.com", "referral")}


def test_breakdown_by_source_medium_has_all_three_groups():
    rows = {(r["source"], r["medium"]): r for r in run(FakeGA4())["data"]["by_source_medium"]}
    mobile = rows[("m.search.naver.com", "referral")]
    assert mobile["naver_search"]["sessions"] == 60
    assert mobile["ad"]["sessions"] == 50
    assert mobile["natural"] == {"sessions": 10, "transactions": 1}
    # A source/medium with no ad click shows 0 ad sessions, not a missing value.
    assert rows[("search.naver.com", "referral")]["ad"] == {"sessions": 0, "transactions": 0}


def test_natural_landing_pages_drop_query_keep_not_set_and_exclude_ads():
    data = run(FakeGA4())["data"]
    pages = data["top_landing_pages"]
    assert [p["landing_page"] for p in pages] == ["/goods/view", "/", "(not set)"]
    assert pages[0] == {"landing_page": "/goods/view", "sessions": 10, "transactions": 1}
    assert data["top_landing_pages_total_rows"] == 3


def test_landing_requests_ask_for_top_20_by_sessions():
    requests = nt.build_requests("properties/1", nt.date_window(28, NOW))
    for name, dimension in (("landing_pages", "landingPage"), ("product_urls", "landingPagePlusQueryString")):
        request = requests[name]
        assert request.limit == 20
        assert [d.name for d in request.dimensions] == [dimension]
        assert request.order_bys[0].metric.metric_name == "sessions" and request.order_bys[0].desc
    assert requests["landing_pages"].dimension_filter == requests["natural"].dimension_filter


def test_product_filter_is_natural_and_any_pattern_contains_case_sensitive():
    requests = nt.build_requests("properties/1", nt.date_window(28, NOW), ["goods_view", "/item/"])
    parts = list(requests["product_urls"].dimension_filter.and_group.expressions)
    assert parts[:2] == list(requests["natural"].dimension_filter.and_group.expressions)
    ors = [e.filter for e in parts[2].or_group.expressions]
    assert [f.string_filter.value for f in ors] == ["goods_view", "/item/"]
    assert all(f.field_name == "landingPagePlusQueryString" for f in ors)
    assert all(f.string_filter.match_type == Filter.StringFilter.MatchType.CONTAINS for f in ors)
    assert all(f.string_filter.case_sensitive for f in ors)


PRODUCT_RECORDS = [
    rec("naver", "organic", "/goods/goods_view.php?goodsNo=1", 6, 1),
    rec("m.search.naver.com", "referral", "/goods/goods_view.php?goodsNo=2", 4, 0),
    rec("naver", "organic", "/goods/goods_view.php?goodsNo=3&NaPm=ct%3Dad", 50, 2),
    rec("m.search.naver.com", "referral", "/goods/goods_list.php?cateCd=001", 30, 1),
    rec("naver", "organic", "/", 20, 0),
    rec("search.naver.com", "referral", "(not set)", 2, 0),
]


def test_product_urls_keep_goods_apart_and_drop_categories_ads_and_not_set():
    data = nt.run_report("123", days=28, client=FakeGA4(records=PRODUCT_RECORDS), now=NOW)["data"]
    assert data["top_product_urls"] == [
        {"url": "/goods/goods_view.php?goodsNo=1", "sessions": 6, "transactions": 1},
        {"url": "/goods/goods_view.php?goodsNo=2", "sessions": 4, "transactions": 0},
    ]
    assert data["top_product_urls_total_rows"] == 2
    assert data["product_patterns"] == ["goods_view", "goodsNo=", "/product"]
    assert data["product_patterns_from"] == "default"
    # The path table still collapses the two products and keeps categories and (not set).
    pages = {p["landing_page"]: p["sessions"] for p in data["top_landing_pages"]}
    assert pages == {"/goods/goods_list.php": 30, "/": 20, "/goods/goods_view.php": 10, "(not set)": 2}
    report = nt.render_markdown({"status": "success", "error": None, "data": data})
    assert "| 2 | `/goods/goods_view.php?goodsNo=2` | 4 | 0 |" in report
    assert "`goods_view` · `goodsNo=` · `/product`" in report


def test_product_patterns_given_to_run_report_are_used_and_recorded():
    records = PRODUCT_RECORDS + [rec("naver", "organic", "/item/77", 9, 0)]
    data = nt.run_report("123", days=28, client=FakeGA4(records=records), now=NOW,
                         product_patterns=["/item/"], patterns_from="cli")["data"]
    assert [u["url"] for u in data["top_product_urls"]] == ["/item/77"]
    assert (data["product_patterns"], data["product_patterns_from"]) == (["/item/"], "cli")


# --- product patterns: command line, naver.json, default --------------------

def test_patterns_from_cli_win_over_config(tmp_path):
    config = tmp_path / "naver.json"
    config.write_text('{"keywords": {"product_patterns": ["/shop/"]}}', encoding="utf-8")
    assert nt.resolve_product_patterns(["/item/", "sku="], str(config)) == (["/item/", "sku="], "cli")


def test_patterns_from_config(tmp_path):
    config = tmp_path / "naver.json"
    config.write_text('{"keywords": {"list": [], "product_patterns": ["/shop/"]}}', encoding="utf-8")
    assert nt.resolve_product_patterns(None, str(config)) == (["/shop/"], "config")


@pytest.mark.parametrize("content", [None, "{}", '{"keywords": {"list": []}}', '{"keywords": {"product_patterns": null}}'])
def test_missing_file_or_key_means_default(tmp_path, content):
    config = tmp_path / "naver.json"
    if content is not None:
        config.write_text(content, encoding="utf-8")
    assert nt.resolve_product_patterns(None, str(config)) == (["goods_view", "goodsNo=", "/product"], "default")


@pytest.mark.parametrize("content", ["{broken", '{"keywords": []}', '{"keywords": {"product_patterns": "goods_view"}}',
                                     '{"keywords": {"product_patterns": []}}', '{"keywords": {"product_patterns": [""]}}'])
def test_unusable_config_is_a_settings_error(tmp_path, content):
    config = tmp_path / "naver.json"
    config.write_text(content, encoding="utf-8")
    with pytest.raises(nt.SettingsError):
        nt.resolve_product_patterns(None, str(config))


def test_organic_search_numbers_and_line():
    data = run(FakeGA4())["data"]
    assert data["organic_search_total"] == 142  # all Organic Search, Naver and Google
    assert data["organic_search_napm"] == 80
    assert data["organic_search_napm_pct"] == 56.3
    assert data["organic_search_line"] == (
        "구글 Organic Search 142 중 네이버 광고 클릭 80 (56.3%), 도착 주소에 NaPm 이 있는 세션")


def test_organic_line_formats_thousands_and_zero_total():
    assert nt.organic_line(1500, 1234) == (
        82.3, "구글 Organic Search 1,500 중 네이버 광고 클릭 1,234 (82.3%), 도착 주소에 NaPm 이 있는 세션")
    pct, line = nt.organic_line(0, 0)
    assert pct is None and "비율 없음" in line
    assert nt.organic_line(None, 5) == (None, None)


def test_direct_and_subtraction_mismatch_is_a_warning_and_direct_wins():
    data = run(FakeGA4(natural_drift=3))["data"]
    assert data["natural"]["sessions"] == 19
    assert len(data["warnings"]) == 1
    assert "direct filtered query 19" in data["warnings"][0] and "minus ad 22" in data["warnings"][0]


# --- paging ----------------------------------------------------------------

def test_multi_row_queries_are_paged_until_row_count(monkeypatch):
    many = [rec("naver", "organic", f"/p{i}", 1, date=f"202601{i + 1:02d}") for i in range(5)]
    monkeypatch.setattr(nt, "PAGE_SIZE", 2)
    client = FakeGA4(records=many)
    data = nt.run_report("123", days=28, client=client, now=NOW)["data"]
    assert data["organic_search_total"] == 5
    date_calls = [(limit, offset) for req, limit, offset in client.requests
                  if [d.name for d in req.dimensions] == ["date"]]
    assert date_calls[:3] == [(2, 0), (2, 2), (2, 4)]


def test_run_all_rows_stops_on_empty_page():
    class Empty:
        def run_report(self, request):
            return RunReportResponse(rows=[], row_count=10)

    rows, _ = nt.run_all_rows(Empty(), nt.build_requests("properties/1", nt.date_window(28, NOW))["natural"])
    assert rows == []


# --- failures --------------------------------------------------------------

def test_failed_query_is_not_written_as_zero_and_status_is_partial():
    client = FakeGA4(fail_on=lambda r: r.dimensions[0].name == "landingPage")
    result = run(client)
    assert result["status"] == "partial"
    assert result["data"]["top_landing_pages"] is None
    assert result["data"]["top_product_urls"] is not None
    assert "landing_pages: Permission denied" in result["error"]
    assert "못 잼" in nt.render_markdown(result)


def test_failed_natural_query_leaves_natural_none():
    client = FakeGA4(fail_on=lambda r: r.dimensions[0].name == "sessionSource" and has_not(r.dimension_filter))
    data = run(client)["data"]
    assert data["natural"] is None and data["ad"] == {"sessions": 80, "transactions": 2}
    assert all(row["natural"] is None for row in data["by_source_medium"])


def test_every_query_failing_is_error():
    result = run(FakeGA4(fail_on=lambda r: True))
    assert result["status"] == "error"


def test_no_client_is_error():
    with patch.object(nt, "_build_ga4_client", return_value=None):
        result = nt.run_report("123")
    assert result["status"] == "error" and result["data"] is None


def test_thresholding_metadata_becomes_a_warning():
    class Thresholded(FakeGA4):
        def run_report(self, request):
            response = super().run_report(request)
            response.metadata.subject_to_thresholding = True
            return response

    warnings = run(Thresholded())["data"]["warnings"]
    assert any("thresholding" in w for w in warnings)


# --- report and CLI ----------------------------------------------------------

def test_markdown_report_and_outputs(tmp_path):
    result = run(FakeGA4())
    report = nt.render_markdown(result)
    assert "| 네이버 자연 검색 | 22 | 1 |" in report
    assert "구글 Organic Search 142 중 네이버 광고 클릭 80 (56.3%)" in report
    assert "`(not set)`" in report
    assert "## 자연 검색의 처음 본 상품 화면 상위 20 (상품 화면만, 쿼리 포함)" in report
    assert "서로 다른 상품 화면 주소 0개" in report  # /goods/view?no= matches none of the default patterns
    assert chr(0x2014) not in report and chr(0x2013) not in report
    paths = nt.write_outputs(result, str(tmp_path))
    assert [Path(p).name for p in paths] == ["naver-traffic.json", "NAVER-TRAFFIC.md"]


FAKE_RESULT = {"status": "success", "data": {}, "error": None, "metadata": {}}


def test_cli_without_property_fails_without_calling_ga4(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sys, "argv", ["naver_traffic.py", "--json", "--config", str(tmp_path / "none.json")])
    with patch.object(nt, "load_config", return_value={}), patch.object(nt, "run_report") as runner:
        assert nt.main() == 1
    runner.assert_not_called()
    assert '"status": "error"' in capsys.readouterr().out


def test_cli_strips_properties_prefix_and_uses_default_patterns(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["naver_traffic.py", "--json", "--config", str(tmp_path / "none.json")])
    with patch.object(nt, "load_config", return_value={"ga4_property_id": "properties/42"}), \
            patch.object(nt, "run_report", return_value=FAKE_RESULT) as runner:
        assert nt.main() == 0
    runner.assert_called_once_with("42", 28, product_patterns=["goods_view", "goodsNo=", "/product"],
                                   patterns_from="default")


def test_cli_product_pattern_is_repeatable_and_overrides_config(monkeypatch, tmp_path):
    config = tmp_path / "naver.json"
    config.write_text('{"keywords": {"product_patterns": ["/shop/"]}}', encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["naver_traffic.py", "-p", "7", "--json", "--config", str(config),
                                      "--product-pattern", "/item/", "--product-pattern", "sku="])
    with patch.object(nt, "run_report", return_value=FAKE_RESULT) as runner:
        assert nt.main() == 0
    runner.assert_called_once_with("7", 28, product_patterns=["/item/", "sku="], patterns_from="cli")


def test_cli_unusable_config_fails_without_calling_ga4(monkeypatch, capsys, tmp_path):
    config = tmp_path / "naver.json"
    config.write_text('{"keywords": {"product_patterns": "goods_view"}}', encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["naver_traffic.py", "-p", "7", "--json", "--config", str(config)])
    with patch.object(nt, "run_report") as runner:
        assert nt.main() == 1
    runner.assert_not_called()
    assert "keywords.product_patterns" in capsys.readouterr().out
