#!/usr/bin/env python3
"""
Naver search inflow from GA4, with Naver ad clicks taken out.

Counts GA4 sessions that came from Naver search (sessionSource in
m.search.naver.com / search.naver.com / naver, sessionMedium in referral /
organic) and splits them by whether the landing URL carries Naver's ad click
parameter (landingPagePlusQueryString contains "NaPm="). Sessions without it
are Naver natural search. Also counts how many of the original GA4 report's
"Organic Search" sessions carry NaPm, for the one line the Naver block adds
next to the Google numbers (the original number itself is not changed).

All splitting is done with GA4 dimension filters, so only aggregated rows come
back. Full-list queries are paged with limit/offset until row_count; the two
top 20 lists (landing path without query, and product page landing URLs with
query, product pages picked first by URL patterns) ask for 20 rows on purpose
and record row_count next to them.

Spec (source of truth for this script): docs/NAVER.md section 4

Usage:
    python naver_traffic.py --json
    python naver_traffic.py --property 123456789 --days 90 --json
    python naver_traffic.py --product-pattern goods_view --product-pattern /item/ --json
    python naver_traffic.py --output-dir example.com-audit/naver-traffic
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from typing import Optional

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPTS_DIR)

try:
    from google.analytics.data_v1beta.types import (
        DateRange,
        Dimension,
        Filter,
        FilterExpression,
        FilterExpressionList,
        Metric,
        OrderBy,
        RunReportRequest,
    )
except ImportError:
    print(
        "Error: google-analytics-data required. Install with: pip install google-analytics-data",
        file=sys.stderr,
    )
    sys.exit(1)

# Credentials, client and property format come from the original GA4 report.
from ga4_report import _build_ga4_client, _resolve_property  # noqa: E402
from google_auth import load_config  # noqa: E402

SOURCE = "naver_traffic"
SPEC = "docs/NAVER.md#4"
DEFAULT_DAYS = 28
NAVER_SOURCES = ["m.search.naver.com", "search.naver.com", "naver"]
NAVER_MEDIUMS = ["referral", "organic"]
AD_FIELD = "landingPagePlusQueryString"
AD_MARKER = "NaPm="
ORGANIC_CHANNEL = "Organic Search"
TOP_LANDING_PAGES = 20
PAGE_SIZE = 10000
METRICS = ["sessions", "transactions"]
ORGANIC_LINE = "구글 Organic Search {total} 중 네이버 광고 클릭 {napm} ({pct}), 도착 주소에 NaPm 이 있는 세션"
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".config", "claude-seo", "naver.json")
DEFAULT_PRODUCT_PATTERNS = ["goods_view", "goodsNo=", "/product"]


class SettingsError(Exception):
    """naver.json or a command line value cannot be used."""


def _metadata() -> dict:
    return {
        "source": SOURCE,
        "spec": SPEC,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _check_patterns(patterns, where: str) -> list:
    if not isinstance(patterns, list) or not patterns or \
            not all(isinstance(p, str) and p.strip() for p in patterns):
        raise SettingsError(f"{where} must be a non-empty list of non-empty texts")
    return patterns


def resolve_product_patterns(cli_patterns: Optional[list] = None, config_path: str = CONFIG_PATH) -> tuple:
    """(patterns, origin): command line, else keywords.product_patterns in naver.json, else the spec default.

    A missing file, keywords section or product_patterns key means the default (spec 4-2, 10).
    """
    if cli_patterns:
        return _check_patterns(list(cli_patterns), "--product-pattern"), "cli"
    if not os.path.isfile(config_path):
        return list(DEFAULT_PRODUCT_PATTERNS), "default"
    try:
        with open(config_path, encoding="utf-8") as handle:
            config = json.load(handle)
    except (OSError, ValueError) as exc:
        raise SettingsError(f"{config_path} could not be read: {exc}") from exc
    keywords = config.get("keywords") if isinstance(config, dict) else None
    if keywords is None:
        return list(DEFAULT_PRODUCT_PATTERNS), "default"
    if not isinstance(keywords, dict):
        raise SettingsError(f"keywords in {config_path} must be an object")
    if keywords.get("product_patterns") is None:
        return list(DEFAULT_PRODUCT_PATTERNS), "default"
    return _check_patterns(keywords["product_patterns"], "keywords.product_patterns"), "config"


def date_window(days: int, now: Optional[datetime] = None) -> dict:
    """Same window as ga4_report.py: start = today minus days, end = yesterday."""
    now = now or datetime.now()
    return {
        "start": (now - timedelta(days=days)).strftime("%Y-%m-%d"),
        "end": (now - timedelta(days=1)).strftime("%Y-%m-%d"),
        "days": days,
    }


# ---------------------------------------------------------------------------
# Filters and requests
# ---------------------------------------------------------------------------

def _in_list(field: str, values: list) -> FilterExpression:
    return FilterExpression(filter=Filter(
        field_name=field,
        in_list_filter=Filter.InListFilter(values=values, case_sensitive=True),
    ))


def _ad_click() -> FilterExpression:
    """Landing URL with query string contains NaPm= (spec 4-1)."""
    return FilterExpression(filter=Filter(
        field_name=AD_FIELD,
        string_filter=Filter.StringFilter(
            match_type=Filter.StringFilter.MatchType.CONTAINS, value=AD_MARKER, case_sensitive=True),
    ))


def _organic_channel() -> FilterExpression:
    """The original report's Organic Search filter, written the same way as ga4_report.py."""
    return FilterExpression(filter=Filter(
        field_name="sessionDefaultChannelGroup",
        string_filter=Filter.StringFilter(
            match_type=Filter.StringFilter.MatchType.EXACT, value=ORGANIC_CHANNEL),
    ))


def _and(*expressions: FilterExpression) -> FilterExpression:
    return FilterExpression(and_group=FilterExpressionList(expressions=list(expressions)))


def _product_page(patterns: list) -> FilterExpression:
    """Landing URL with query contains any product pattern (spec 4-2), matched as written."""
    return FilterExpression(or_group=FilterExpressionList(expressions=[
        FilterExpression(filter=Filter(
            field_name=AD_FIELD,
            string_filter=Filter.StringFilter(
                match_type=Filter.StringFilter.MatchType.CONTAINS, value=pattern, case_sensitive=True),
        ))
        for pattern in patterns
    ]))


def _not(expression: FilterExpression) -> FilterExpression:
    return FilterExpression(not_expression=expression)


def naver_search_filter() -> FilterExpression:
    return _and(_in_list("sessionSource", NAVER_SOURCES), _in_list("sessionMedium", NAVER_MEDIUMS))


def build_requests(prop: str, window: dict, product_patterns: Optional[list] = None) -> dict:
    """Every GA4 query this script sends, by name."""
    product_patterns = product_patterns or DEFAULT_PRODUCT_PATTERNS
    date_ranges = [DateRange(start_date=window["start"], end_date=window["end"])]
    metrics = [Metric(name=m) for m in METRICS]
    by_source = [Dimension(name="sessionSource"), Dimension(name="sessionMedium")]
    naver = naver_search_filter()

    def breakdown(dimension_filter: FilterExpression) -> RunReportRequest:
        return RunReportRequest(property=prop, dimensions=by_source, metrics=metrics,
                                date_ranges=date_ranges, dimension_filter=dimension_filter)

    def top_natural(dimension: str, *extra: FilterExpression) -> RunReportRequest:
        # Natural Naver search only, so no NaPm ad value can appear in these rows.
        return RunReportRequest(
            property=prop, dimensions=[Dimension(name=dimension)], metrics=metrics,
            date_ranges=date_ranges, dimension_filter=_and(naver, _not(_ad_click()), *extra),
            order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="sessions"), desc=True)],
            limit=TOP_LANDING_PAGES)

    def organic_by_date(dimension_filter: FilterExpression) -> RunReportRequest:
        # Date rows summed, like ga4_report.py totals.sessions, so {total} matches that number.
        return RunReportRequest(property=prop, dimensions=[Dimension(name="date")],
                                metrics=[Metric(name="sessions")], date_ranges=date_ranges,
                                dimension_filter=dimension_filter,
                                order_bys=[OrderBy(dimension=OrderBy.DimensionOrderBy(dimension_name="date"))])

    return {
        "naver_search": breakdown(naver),
        "ad": breakdown(_and(naver, _ad_click())),
        "natural": breakdown(_and(naver, _not(_ad_click()))),
        # Path without query (spec 4-2), and product pages only, full URL with query, for spec 6-1.
        "landing_pages": top_natural("landingPage"),
        "product_urls": top_natural(AD_FIELD, _product_page(product_patterns)),
        "organic_total": organic_by_date(_organic_channel()),
        "organic_napm": organic_by_date(_and(_organic_channel(), _ad_click())),
    }


# ---------------------------------------------------------------------------
# Running queries
# ---------------------------------------------------------------------------

def _number(value: str) -> int:
    return int(float(value or 0))


def _response_warnings(name: str, response) -> list:
    meta = getattr(response, "metadata", None)
    if not meta:
        return []
    out = []
    if meta.subject_to_thresholding:
        out.append(f"{name}: GA4 applied thresholding, some rows may be withheld")
    if meta.data_loss_from_other_row:
        out.append(f"{name}: GA4 grouped some rows into (other)")
    if len(meta.sampling_metadatas):
        out.append(f"{name}: GA4 returned sampled data")
    if len(meta.data_truncation_reasons):
        out.append(f"{name}: GA4 reported data truncation")
    return out


def run_all_rows(client, request: RunReportRequest, page_size: Optional[int] = None) -> tuple:
    """All rows of a report, paged with limit/offset until row_count. Returns (rows, last response)."""
    page_size = page_size or PAGE_SIZE
    rows, offset = [], 0
    while True:
        request.limit = page_size
        request.offset = offset
        response = client.run_report(request)
        rows.extend(response.rows)
        offset += len(response.rows)
        if not response.rows or offset >= response.row_count:
            return rows, response


def _error_text(exc: Exception) -> str:
    text = str(exc)
    if "403" in text or "PERMISSION_DENIED" in text:
        return ("Permission denied for the GA4 property. Add the service account email as Viewer in "
                "GA4 Admin > Property Access Management.")
    if "404" in text or "NOT_FOUND" in text:
        return "GA4 property not found. Verify the numeric property ID in GA4 Admin > Property Details."
    return f"GA4 API error: {type(exc).__name__}: {exc}"


def _counts(row) -> dict:
    return {"sessions": _number(row.metric_values[0].value),
            "transactions": _number(row.metric_values[1].value)}


def _sum(groups: list) -> dict:
    return {"sessions": sum(g["sessions"] for g in groups),
            "transactions": sum(g["transactions"] for g in groups)}


def merge_breakdown(groups: dict) -> list:
    """One row per source/medium with naver_search, ad and natural counts (None when that query failed)."""
    keys = []
    for name in ("naver_search", "ad", "natural"):
        for key in (groups.get(name) or {}):
            if key not in keys:
                keys.append(key)
    out = []
    for source, medium in keys:
        row = {"source": source, "medium": medium}
        for name in ("naver_search", "ad", "natural"):
            got = groups.get(name)
            row[name] = None if got is None else got.get((source, medium), {"sessions": 0, "transactions": 0})
        out.append(row)
    out.sort(key=lambda r: -max((r[n] or {}).get("sessions", 0) for n in ("naver_search", "ad", "natural")))
    return out


def organic_line(total: Optional[int], napm: Optional[int]) -> tuple:
    """(percent or None, the spec 4-3 sentence or None)."""
    if total is None or napm is None:
        return None, None
    if total == 0:
        return None, ORGANIC_LINE.format(total=0, napm=f"{napm:,}", pct="비율 없음")
    pct = round(napm / total * 100, 1)
    return pct, ORGANIC_LINE.format(total=f"{total:,}", napm=f"{napm:,}", pct=f"{pct:.1f}%")


def collect(client, prop: str, window: dict, product_patterns: Optional[list] = None) -> dict:
    """Run every query. A failed query leaves its value None and adds to errors (never 0)."""
    requests = build_requests(prop, window, product_patterns)
    errors, warnings = [], []
    groups, time_zone = {}, None

    for name in ("naver_search", "ad", "natural"):
        try:
            rows, response = run_all_rows(client, requests[name])
        except Exception as exc:  # noqa: BLE001 - every GA4 failure is reported, not raised
            errors.append(f"{name}: {_error_text(exc)}")
            groups[name] = None
            continue
        time_zone = time_zone or (response.metadata.time_zone or None)
        warnings += _response_warnings(name, response)
        found = {}
        for row in rows:
            key = (row.dimension_values[0].value, row.dimension_values[1].value)
            found[key] = _counts(row)
        groups[name] = found

    totals = {name: (None if groups[name] is None else _sum(list(groups[name].values())))
              for name in ("naver_search", "ad", "natural")}
    if all(totals[n] is not None for n in totals):
        by_subtraction = {m: totals["naver_search"][m] - totals["ad"][m] for m in METRICS}
        for metric in METRICS:
            direct = totals["natural"][metric]
            if direct != by_subtraction[metric]:
                warnings.append(
                    f"natural {metric}: direct filtered query {direct:,}, naver search minus ad "
                    f"{by_subtraction[metric]:,} (difference {direct - by_subtraction[metric]:+,}). "
                    "Reported value is the direct query.")

    tops = {}
    for name, item_key in (("landing_pages", "landing_page"), ("product_urls", "url")):
        try:
            response = client.run_report(requests[name])
            warnings += _response_warnings(name, response)
            items = [{item_key: row.dimension_values[0].value, **_counts(row)} for row in response.rows]
            tops[name] = (items, response.row_count)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {_error_text(exc)}")
            tops[name] = (None, None)

    organic = {}
    for name in ("organic_total", "organic_napm"):
        try:
            rows, response = run_all_rows(client, requests[name])
            warnings += _response_warnings(name, response)
            organic[name] = sum(_number(row.metric_values[0].value) for row in rows)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {_error_text(exc)}")
            organic[name] = None
    pct, line = organic_line(organic["organic_total"], organic["organic_napm"])

    data = {
        "property": prop,
        "date_range": window,
        "time_zone": time_zone,
        "definition": {
            "session_sources": NAVER_SOURCES,
            "session_mediums": NAVER_MEDIUMS,
            "ad_click": f"{AD_FIELD} contains {AD_MARKER}",
            "natural": "naver search sessions without the ad click marker",
        },
        "natural": totals["natural"],
        "ad": totals["ad"],
        "naver_search": totals["naver_search"],
        "by_source_medium": merge_breakdown(groups),
        "top_landing_pages": tops["landing_pages"][0],
        "top_landing_pages_total_rows": tops["landing_pages"][1],
        "product_patterns": list(product_patterns or DEFAULT_PRODUCT_PATTERNS),
        "top_product_urls": tops["product_urls"][0],
        "top_product_urls_total_rows": tops["product_urls"][1],
        "organic_search_total": organic["organic_total"],
        "organic_search_napm": organic["organic_napm"],
        "organic_search_napm_pct": pct,
        "organic_search_line": line,
        "warnings": warnings,
    }
    queries = len(requests)
    return {"data": data, "errors": errors, "failed": len(errors), "queries": queries}


def run_report(property_id: str, days: int = DEFAULT_DAYS, client=None, now: Optional[datetime] = None,
               product_patterns: Optional[list] = None, patterns_from: Optional[str] = None) -> dict:
    """product_patterns None means the spec default; patterns_from says where they came from (cli/config/default)."""
    if product_patterns is None:
        product_patterns, patterns_from = list(DEFAULT_PRODUCT_PATTERNS), "default"
    prop = _resolve_property(property_id)
    window = date_window(days, now)
    if client is None:
        client = _build_ga4_client()
    if client is None:
        return {"status": "error", "data": None,
                "error": ("Could not build GA4 client. Ensure the service account has Viewer access "
                          "in GA4 Admin > Property Access Management."),
                "metadata": _metadata()}
    collected = collect(client, prop, window, product_patterns)
    collected["data"]["product_patterns_from"] = patterns_from
    if collected["failed"] == collected["queries"]:
        status = "error"
    elif collected["failed"]:
        status = "partial"
    else:
        status = "success"
    return {"status": status, "data": collected["data"],
            "error": "; ".join(collected["errors"]) or None, "metadata": _metadata()}


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

NOT_MEASURED = "못 잼"


def _n(value) -> str:
    return NOT_MEASURED if value is None else f"{value:,}"


def _pair(counts: Optional[dict]) -> tuple:
    if counts is None:
        return NOT_MEASURED, NOT_MEASURED
    return _n(counts["sessions"]), _n(counts["transactions"])


def render_markdown(result: dict) -> str:
    data = result.get("data") or {}
    window = data.get("date_range") or {}
    lines = [
        "# 네이버 검색 유입 (GA4)",
        "",
        f"- 기간: {window.get('start', '')} ~ {window.get('end', '')} ({window.get('days', '')}일, 어제까지)"
        + (f" · GA4 시간대 {data['time_zone']}" if data.get("time_zone") else ""),
        f"- 상태: `{result['status']}`" + (f" · {result['error']}" if result.get("error") else ""),
        "",
        "## 네이버 검색에서 온 세션",
        "",
        "| 묶음 | 세션 | 구매 |",
        "|---|---|---|",
        "| 네이버 자연 검색 | {} | {} |".format(*_pair(data.get("natural"))),
        "| 광고 클릭 (도착 주소에 `NaPm=`, 참고) | {} | {} |".format(*_pair(data.get("ad"))),
        "| 네이버 검색 전체 | {} | {} |".format(*_pair(data.get("naver_search"))),
        "",
        "## 출처 · 매체별",
        "",
        "| sessionSource | sessionMedium | 전체 세션 | 광고 클릭 세션 | 자연 검색 세션 | 자연 검색 구매 |",
        "|---|---|---|---|---|---|",
    ]
    for row in data.get("by_source_medium") or []:
        total, ad, natural = (row.get(n) or {} for n in ("naver_search", "ad", "natural"))
        lines.append(f"| `{row['source']}` | `{row['medium']}` | {_n(total.get('sessions'))} | "
                     f"{_n(ad.get('sessions'))} | {_n(natural.get('sessions'))} | {_n(natural.get('transactions'))} |")

    tables = (
        ("top_landing_pages", "landing_page", f"자연 검색의 처음 본 화면 상위 {TOP_LANDING_PAGES} (쿼리를 뗀 경로)",
         "화면 (`landingPage`)", "화면"),
        ("top_product_urls", "url", f"자연 검색의 처음 본 상품 화면 상위 {TOP_LANDING_PAGES} (상품 화면만, 쿼리 포함)",
         "주소 (`landingPagePlusQueryString`)", "상품 화면 주소"),
    )
    for key, item_key, title, column, noun in tables:
        lines += ["", f"## {title}", ""]
        if key == "top_product_urls" and data.get("product_patterns"):
            patterns = " · ".join(f"`{p}`" for p in data["product_patterns"])
            lines += [f"상품 화면 = 도착 주소에 {patterns} 중 하나가 들어 있는 것"
                      + (f" (`{data['product_patterns_from']}`)" if data.get("product_patterns_from") else ""), ""]
        items = data.get(key)
        if items is None:
            lines.append(f"{NOT_MEASURED} (오류는 위 상태 줄)")
            continue
        if data.get(f"{key}_total_rows") is not None:
            lines += [f"서로 다른 {noun} {data[f'{key}_total_rows']:,}개 중 세션 상위", ""]
        lines += [f"| 순위 | {column} | 세션 | 구매 |", "|---|---|---|---|"]
        for rank, item in enumerate(items, 1):
            value = item[item_key].replace("|", "\\|")
            lines.append(f"| {rank} | `{value}` | {item['sessions']:,} | {item['transactions']:,} |")

    lines += ["", "## 구글 Organic Search 숫자에 덧붙이는 한 줄", ""]
    if data.get("organic_search_line"):
        lines += ["```", data["organic_search_line"], "```"]
    else:
        lines.append(f"{NOT_MEASURED} (오류는 위 상태 줄)")

    if data.get("warnings"):
        lines += ["", "## 주의", ""] + [f"- {w}" for w in data["warnings"]]

    lines += [
        "",
        "## 읽는 법 · 한계",
        "",
        "- 네이버 검색 = `sessionSource` 가 `m.search.naver.com` · `search.naver.com` · `naver` 중 하나이고 "
        "`sessionMedium` 이 `referral` · `organic` 중 하나인 세션.",
        "- 광고 클릭 = 그중 도착 주소(`landingPagePlusQueryString`)에 `NaPm=` 이 있는 세션. 자연 검색 = 나머지.",
        "- 상품 화면 표는 상품 화면만 먼저 고른 뒤 쿼리까지 포함해 셉니다. 상품 번호가 쿼리에 붙는 화면도 "
        "상품마다 따로 나옵니다. 자연 검색만 세므로 `NaPm` 값은 섞이지 않습니다.",
        "- `naver / cpc`(태그를 단 광고) · `naver / blog`(검색이 아님)는 넣지 않았습니다.",
        "- 구글 쪽 Organic Search 숫자는 고치지 않습니다. 위 한 줄만 그 옆에 덧붙입니다.",
        "- `NaPm` 안의 `tr=` 값으로 광고 종류를 나누지 않습니다. GA4로는 네이버 검색어를 셀 수 없습니다.",
        "",
    ]
    return "\n".join(lines)


def write_outputs(result: dict, output_dir: str) -> list:
    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, "naver-traffic.json")
    md_path = os.path.join(output_dir, "NAVER-TRAFFIC.md")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(render_markdown(result))
    return [json_path, md_path]


def main() -> int:
    parser = argparse.ArgumentParser(description="GA4 Naver search inflow with Naver ad clicks (NaPm) removed")
    parser.add_argument("--property", "-p",
                        help="GA4 property ID (numeric). Uses ga4_property_id from google-api.json if not given.")
    parser.add_argument("--days", "-d", type=int, default=DEFAULT_DAYS,
                        help=f"Number of days, ending yesterday (default: {DEFAULT_DAYS})")
    parser.add_argument("--product-pattern", action="append", dest="product_patterns", metavar="TEXT",
                        help="Text a product page URL contains (repeatable). Overrides keywords.product_patterns "
                             f"in naver.json; default {' '.join(DEFAULT_PRODUCT_PATTERNS)}")
    parser.add_argument("--config", default=CONFIG_PATH, help="naver.json path (default: %(default)s)")
    parser.add_argument("--output-dir", help="Write naver-traffic.json and NAVER-TRAFFIC.md here")
    parser.add_argument("--json", "-j", action="store_true", help="Print the result as JSON")
    args = parser.parse_args()

    def fail(message: str) -> int:
        result = {"status": "error", "data": None, "error": message, "metadata": _metadata()}
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"Error: {message}", file=sys.stderr)
        return 1

    if args.days < 1:
        return fail("--days must be at least 1")
    prop = args.property or load_config().get("ga4_property_id") or ""
    prop = prop.removeprefix("properties/")
    if not prop:
        return fail("No GA4 property specified. Use --property or set ga4_property_id in config.")
    try:
        patterns, patterns_from = resolve_product_patterns(args.product_patterns, args.config)
    except SettingsError as exc:
        return fail(str(exc))

    result = run_report(prop, args.days, product_patterns=patterns, patterns_from=patterns_from)
    written = write_outputs(result, args.output_dir) if args.output_dir else []

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        data = result["data"] or {}
        window = data.get("date_range") or {}
        natural, ad = data.get("natural"), data.get("ad")
        print(f"Naver search (GA4) {window.get('start')}..{window.get('end')}: "
              f"natural {_pair(natural)[0]} sessions / {_pair(natural)[1]} transactions, "
              f"ad clicks {_pair(ad)[0]} sessions (status {result['status']})")
        if data.get("organic_search_line"):
            print(f"  {data['organic_search_line']}")
        for warning in data.get("warnings") or []:
            print(f"  warning: {warning}")
        if result["error"]:
            print(f"  error: {result['error']}")
        for path in written:
            print(f"  wrote {path}")
    return 1 if result["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
