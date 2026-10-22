#!/usr/bin/env python3
"""
Naver keyword rank checker for Claude SEO.

Two subcommands:

  candidates  Build a keyword candidate list for a site: category names taken from
              the category links on the site's homepage, plus product names from
              the top GA4 Naver natural search landing pages (naver_traffic.py
              output). Product names are the product page <title> with the site
              name removed. With --save-to-config the names are written into
              keywords.candidates of the config file, only when that key is
              missing or empty; no other key is changed. A person then edits
              them into keywords.list.

  rank        For each keyword, read Naver search pages directly (no search API,
              no browser, no cookies, requests 3 seconds apart) and record
              (a) mobile integrated search: the result areas (meta.ssuidExtra)
                  that show a URL of the site's domain, and the order inside
                  that area, else "none";
              (b) PC web documents: the rank of the first URL of the domain
                  within three result pages, else "outside_read_pages" with
                  the number of distinct web documents read.
              A page that cannot be read leaves that keyword as an error, never 0.

Spec (source of truth for this script): docs/NAVER.md section 6
Paging, result fields and domain rule: docs/NAVER-SITE-INDEX.md 3-1, 3-2, 3-4

Usage:
    python naver_keyword_rank.py candidates --site https://www.example.com --landing-json naver-traffic.json --json
    python naver_keyword_rank.py candidates --site https://www.example.com --landing-json naver-traffic.json \
        --save-to-config --config ~/.config/claude-seo/naver.json
    python naver_keyword_rank.py rank --site https://www.example.com --keyword "keyword one" --keyword "keyword two"
    python naver_keyword_rank.py rank --site https://www.example.com --keywords-file keywords.json \
        --unconfirmed-list --output-dir example.com-audit/naver-rank
"""

import argparse
import html as html_lib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional
from urllib.parse import parse_qs, quote, urljoin, urlsplit

try:
    import requests
except ImportError:
    print("Error: requests library required. Install with: pip install requests", file=sys.stderr)
    sys.exit(1)

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPTS_DIR)
try:
    from url_safety import URLSafetyError, decode_response_text, safe_requests_get
except ImportError:
    print("Error: url_safety.py required in scripts/", file=sys.stderr)
    sys.exit(1)
try:
    # Shared Naver result-page helpers: bootstrap block reader, web result reader,
    # document URL rule and domain rule (docs/NAVER-SITE-INDEX.md 3-2, 3-4).
    import naver_site_index as nsi
except ImportError:
    print("Error: naver_site_index.py required in scripts/", file=sys.stderr)
    sys.exit(1)

SOURCE = "naver_keyword_rank"
SPEC = "docs/NAVER.md#6"
KST = timezone(timedelta(hours=9))

MOBILE_URL = "https://m.search.naver.com/search.naver?where=m&query={q}"
PC_URL = "https://search.naver.com/search.naver?where=web&query={q}"
PC_HOST = "search.naver.com"
USER_AGENTS = {
    "mo": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "pc": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
}
PC_PAGES = 3
PC_NOT_FOUND = "outside_read_pages"
DEFAULT_MAX = 20
DEFAULT_CONFIG = os.path.join(os.path.expanduser("~"), ".config", "claude-seo", "naver.json")

# Polite delays. Naver pages: the spec fixes 3 seconds; the CLI only allows longer.
# The site's own pages (candidates): 1 second, as for the crawl check in docs/NAVER.md 7.
MIN_REQUEST_DELAY = 3.0
REQUEST_DELAY = MIN_REQUEST_DELAY
SITE_REQUEST_DELAY = 1.0
REQUEST_TIMEOUT = 30
MAX_REDIRECTS = 3
_last_request_time = 0.0

NaverFetchError = nsi.NaverFetchError

# Category list links on a homepage: a query parameter named like a category id,
# or a category-like path segment. Matched case-insensitively.
CATEGORY_QUERY_KEYS = frozenset({
    "catecd", "cate_no", "cateno", "xcode", "category", "category_id", "categoryid", "cat_id",
})
CATEGORY_PATH_RE = re.compile(r"/(?:category|categories|collections|product-category)/[^/?#]+", re.I)

# Product pages among landing pages, matched against "path?query".
DEFAULT_PRODUCT_PATTERNS = (
    r"[?&]goodsNo=\d+",
    r"[?&]product_no=\d+",
    r"[?&]branduid=\d+",
    r"/product/[^/?#]+/\d+/?(?:[?#]|$)",
    r"/products?/[^/?#.]+/?(?:[?#]|$)",
)
# Query parameters that name one product; other parameters (tracking and so on) are ignored
# when two landing URLs are compared as the same product.
PRODUCT_ID_KEYS = ("goodsno", "product_no", "branduid")
# A product page file name that needs an id query to mean one product.
PRODUCT_PAGE_WITHOUT_ID_RE = re.compile(r"(?:goods_view\.php|/product/detail\.html|shopdetail\.html)$", re.I)
LANDING_PATH_KEYS = ("path", "landing_page", "landingPage", "landing_page_plus_query_string",
                     "landingPagePlusQueryString", "page", "url")
NOT_SET = "(not set)"

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_META_RE = re.compile(r"<meta\b[^>]*>", re.I)
_ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.S | re.I)
_IMG_RE = re.compile(r"<img\b([^>]*)>", re.I)
_ATTR_RE = re.compile(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_TAG_RE = re.compile(r"<[^>]+>")
_SITE_TITLE_SPLIT_RE = re.compile(r"\s*(?:\|+|:{2,}|\s-\s|\s·\s|>)\s*")
_TRAILING_ARROWS = " \t▶▷►›»→>"
_NAME_SEPARATORS = " \t:|-·>_~/"


class SiteFetchError(Exception):
    """A page of the site itself could not be fetched."""


def _rate_limit(delay: float) -> None:
    """Wait so that consecutive requests are at least `delay` seconds apart."""
    global _last_request_time
    now = time.time()
    elapsed = now - _last_request_time
    if _last_request_time > 0 and elapsed < delay:
        time.sleep(delay - elapsed)
    _last_request_time = time.time()


def _is_naver(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == "naver.com" or host.endswith(".naver.com")


def _get(url: str, headers: dict) -> requests.Response:
    """One cookie-less GET. requests.get without a Session keeps no cookie jar between calls."""
    _rate_limit(REQUEST_DELAY if _is_naver(url) else SITE_REQUEST_DELAY)
    return safe_requests_get(url, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=False)


def _headers(device: str) -> dict:
    return {"User-Agent": USER_AGENTS[device], "Accept-Language": "ko-KR,ko;q=0.9"}


def _now_kst() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def _metadata() -> dict:
    return {"source": SOURCE, "spec": SPEC, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def _err(exc: Exception) -> str:
    if isinstance(exc, requests.exceptions.Timeout):
        return f"request timed out after {REQUEST_TIMEOUT} seconds"
    return f"{type(exc).__name__}: {exc}"


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _norm_kw(value) -> str:
    return " ".join(str(value or "").split())


def _text(fragment: str) -> str:
    return " ".join(html_lib.unescape(_TAG_RE.sub(" ", fragment or "")).split())


def _save_raw(raw_dir: Optional[str], name: str, text: str, raw_files: list) -> None:
    if not raw_dir:
        return
    os.makedirs(raw_dir, exist_ok=True)
    with open(os.path.join(raw_dir, name), "w", encoding="utf-8") as handle:
        handle.write(text)
    raw_files.append(os.path.join("raw", name))


# ---------------------------------------------------------------------------
# Mobile integrated search (docs/NAVER.md 6-2, first row)
# ---------------------------------------------------------------------------

def mobile_url(keyword: str) -> str:
    return MOBILE_URL.format(q=quote(keyword, safe=""))


def _strings(value) -> Iterator[str]:
    """Every string inside a JSON value, in document order."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def _unwrap(text: str) -> Optional[str]:
    """A URL string as a document URL: Naver Pay inflow links point at their retUrl (NAVER-SITE-INDEX 3-2)."""
    if not text.startswith(("http://", "https://")):
        return None
    parts = urlsplit(text)
    if parts.hostname == nsi.PAY_INFLOW_HOST:
        return (parse_qs(parts.query).get("retUrl") or [None])[0]
    return text


def own_url_in(value, domain: str) -> Optional[str]:
    """First URL of the domain inside one result (or one block without an item list)."""
    if isinstance(value, dict):
        try:
            doc = nsi._doc_url(value)
        except (AttributeError, TypeError, ValueError):
            doc = ""
        if isinstance(doc, str) and doc and nsi.is_own(doc, domain):
            return doc
    for text in _strings(value):
        url = _unwrap(text)
        if url and nsi.is_own(url, domain):
            return url
    return None


def _item_title(item: dict) -> str:
    title = item.get("title") if isinstance(item, dict) else None
    return _text(title) if isinstance(title, str) else ""


def read_mobile_blocks(page_html: str, domain: str) -> dict:
    """Walk every entry.bootstrap block in page order and find the domain's results.

    position          order inside the area: results of every block with the same
                      meta.ssuidExtra, counted in page order
    block_index       order of the block among all bootstrap blocks on the page
    position_in_block order inside that one block
    Blocks without data.state.items are searched as a whole; their position is None.
    """
    hits = []
    area_items = Counter()
    blocks_read = unreadable = 0
    for block_index, match in enumerate(nsi._BOOTSTRAP_RE.finditer(page_html), start=1):
        try:
            payload, _ = nsi._DECODER.raw_decode(page_html, match.end())
        except ValueError:
            unreadable += 1
            continue
        if not isinstance(payload, dict):
            unreadable += 1
            continue
        blocks_read += 1
        area = str(_dict(payload.get("meta")).get("ssuidExtra") or "(none)")
        state = _dict(_dict(payload.get("data")).get("state"))
        items = state.get("items")
        if isinstance(items, list):
            for index, item in enumerate(items, start=1):
                area_items[area] += 1
                url = own_url_in(item, domain)
                if url:
                    hits.append({"area": area, "position": area_items[area], "block_index": block_index,
                                 "position_in_block": index, "url": url, "title": _item_title(item)})
        else:
            area_items[area] += 0
            url = own_url_in({"body": payload.get("body"), "data": payload.get("data")}, domain)
            if url:
                hits.append({"area": area, "position": None, "block_index": block_index,
                             "position_in_block": None, "url": url, "title": ""})
    return {"hits": hits, "blocks_read": blocks_read, "unreadable_blocks": unreadable, "area_items": dict(area_items)}


def check_mobile(index: int, keyword: str, domain: str, raw_dir: Optional[str] = None) -> dict:
    out = {"search_url": mobile_url(keyword), "searched_at": None, "status": "error", "first": None, "hits": [],
           "blocks_read": 0, "unreadable_blocks": 0, "area_items": {}, "error": None, "raw_files": []}
    try:
        response = _get(out["search_url"], _headers("mo"))
        page_html = decode_response_text(response)
        _save_raw(raw_dir, f"{index:02d}-mo.html", page_html, out["raw_files"])
        if response.status_code != 200:
            raise NaverFetchError(f"mobile search returned HTTP {response.status_code}")
        out["searched_at"] = _now_kst()
        read = read_mobile_blocks(page_html, domain)
    except (NaverFetchError, URLSafetyError, requests.exceptions.RequestException) as exc:
        out["error"] = _err(exc)
        return out
    out.update(read)
    if read["blocks_read"] == 0:
        out["error"] = "no result blocks could be read (page structure may have changed)"
    elif read["unreadable_blocks"] and not read["hits"]:
        out["error"] = f"{read['unreadable_blocks']} result blocks could not be read, so 'none' cannot be confirmed"
    else:
        out["status"] = "found" if read["hits"] else "none"
        out["first"] = read["hits"][0] if read["hits"] else None
    return out


# ---------------------------------------------------------------------------
# PC web documents (docs/NAVER.md 6-2, second row; paging as NAVER-SITE-INDEX 3-1)
# ---------------------------------------------------------------------------

def pc_url(keyword: str) -> str:
    return PC_URL.format(q=quote(keyword, safe=""))


def next_page_url(page_html: str, keyword: str) -> Optional[str]:
    """URL in the page's next button, or None on the last page.

    Raises NaverFetchError if the link leaves search.naver.com or changes the keyword.
    """
    match = nsi._NEXT_RE.search(page_html)
    if not match or 'aria-disabled="true"' in match.group(0):
        return None
    href = re.search(r'href="([^"]*)"', match.group(0))
    if not href or not href.group(1):
        return None
    url = urljoin(PC_URL.split("?")[0], html_lib.unescape(href.group(1)))
    parts = urlsplit(url)
    queries = parse_qs(parts.query).get("query") or []
    if parts.hostname != PC_HOST or len(queries) != 1 or _norm_kw(queries[0]) != _norm_kw(keyword):
        raise NaverFetchError("next link does not point to the same keyword search")
    return url


def page_cards(page_html: str) -> list:
    """Every result item on a page in screen order, of any kind (web, blog and cafe, Q&A)."""
    cards = []
    for match in nsi._BOOTSTRAP_RE.finditer(page_html):
        try:
            payload, _ = nsi._DECODER.raw_decode(page_html, match.end())
        except ValueError:
            continue
        items = _dict(_dict(_dict(payload).get("data")).get("state")).get("items")
        if isinstance(items, list):
            cards.extend(items)
    return cards


def check_pc(index: int, keyword: str, domain: str, raw_dir: Optional[str] = None) -> dict:
    """Rank of the first domain URL among web results, read up to PC_PAGES pages.

    rank          distinct web document URLs (fender_renderer-web) in the order they
                  appear; a URL repeated on a later page is counted once
                  (NAVER-SITE-INDEX 3-1, 3-2)
    slot_on_page  on the page where it was found, the order of the first result item
                  of any kind (web, blog and cafe, Q&A) that holds a domain URL
    web_docs_read distinct web document URLs seen over the pages read
    slots_read    the same number, kept for earlier readers of this output
    Not found within PC_PAGES pages: status "outside_read_pages" (docs/NAVER.md 6-2).
    """
    out = {"search_url": pc_url(keyword), "searched_at": None, "status": "error", "rank": None, "page": None,
           "slot_on_page": None, "url": None, "title": None, "pages_fetched": 0, "web_docs_read": 0,
           "slots_read": 0, "stop_reason": None, "error": None, "raw_files": []}
    seen = set()
    url = out["search_url"]
    out["stop_reason"] = "max_pages"
    for page in range(1, PC_PAGES + 1):
        try:
            response = _get(url, _headers("pc"))
            page_html = decode_response_text(response)
            _save_raw(raw_dir, f"{index:02d}-pc-{page}.html", page_html, out["raw_files"])
            if response.status_code != 200:
                raise NaverFetchError(f"PC web search page {page} returned HTTP {response.status_code}")
            items = nsi.extract_web_items(page_html)
        except (NaverFetchError, URLSafetyError, requests.exceptions.RequestException) as exc:
            out["stop_reason"], out["error"] = "error", _err(exc)
            return out
        if page == 1:
            out["searched_at"] = _now_kst()
        out["pages_fetched"] = page
        if not items:
            if page == 1:
                out["stop_reason"] = "error"
                out["error"] = "no web results could be read on page 1 (page structure may have changed)"
                return out
            out["stop_reason"] = "empty_page"
            break
        for item in items:
            if item["url"] in seen:
                continue
            seen.add(item["url"])
            if nsi.is_own(item["url"], domain):
                slot = next((i for i, card in enumerate(page_cards(page_html), start=1)
                             if own_url_in(card, domain)), None)
                out.update(status="found", rank=len(seen), page=page, slot_on_page=slot, url=item["url"],
                           title=item["title"], web_docs_read=len(seen), slots_read=len(seen),
                           stop_reason="found")
                return out
        out["web_docs_read"] = out["slots_read"] = len(seen)
        if page == PC_PAGES:
            break
        try:
            next_url = next_page_url(page_html, keyword)
        except NaverFetchError as exc:
            out["stop_reason"], out["error"] = "error", _err(exc)
            return out
        if next_url is None:
            out["stop_reason"] = "last_page"
            break
        url = next_url
    out["status"] = PC_NOT_FOUND
    return out


# ---------------------------------------------------------------------------
# rank: run, summarize, report
# ---------------------------------------------------------------------------

def check_keyword(index: int, keyword: str, domain: str, raw_dir: Optional[str] = None) -> dict:
    mobile = check_mobile(index, keyword, domain, raw_dir)
    pc = check_pc(index, keyword, domain, raw_dir)
    errors = [f"{name}: {part['error']}" for name, part in (("mobile", mobile), ("pc", pc))
              if part["status"] == "error"]
    status = "error" if len(errors) == 2 else ("partial" if errors else "ok")
    return {"keyword": keyword, "status": status, "mobile": mobile, "pc": pc, "error": "; ".join(errors) or None}


def summarize_rank(results: list) -> dict:
    mobile = Counter(r["mobile"]["status"] for r in results)
    pc = Counter(r["pc"]["status"] for r in results)
    areas = Counter(hit["area"] for r in results for hit in r["mobile"]["hits"])
    return {
        "keywords": len(results),
        "errors": sum(1 for r in results if r["status"] == "error"),
        "mobile_found": mobile["found"], "mobile_none": mobile["none"], "mobile_errors": mobile["error"],
        "pc_found": pc["found"], "pc_outside_read_pages": pc[PC_NOT_FOUND], "pc_errors": pc["error"],
        "pc_top10": sum(1 for r in results if r["pc"]["status"] == "found" and r["pc"]["rank"] <= 10),
        "mobile_hit_areas": dict(areas.most_common()),
    }


def run_rank(keywords: list, site: str, domain: str, unconfirmed: bool = False, max_keywords: int = DEFAULT_MAX,
             output_dir: Optional[str] = None) -> dict:
    raw_dir = os.path.join(output_dir, "raw") if output_dir else None
    kept, dropped = keywords[:max_keywords], keywords[max_keywords:]
    warnings = [f"keyword list cut to the first {max_keywords} (keywords.max)"] if dropped else []
    run_at = _now_kst()
    results = [check_keyword(i, kw, domain, raw_dir) for i, kw in enumerate(kept, start=1)]
    data = {
        "site": site,
        "domain": domain,
        "run_at": run_at,
        "unconfirmed_list": bool(unconfirmed),
        "keywords": kept,
        "dropped_keywords": dropped,
        "max": max_keywords,
        "pc_pages": PC_PAGES,
        "results": results,
        "summary": summarize_rank(results),
        "warnings": warnings,
    }
    errors = [r for r in results if r["status"] != "ok"]
    if results and all(r["status"] == "error" for r in results):
        status, error = "error", "every keyword failed; see data.results[].error"
    else:
        status, error = ("partial" if errors else "success"), None
    return {"status": status, "data": data, "error": error, "metadata": _metadata()}


def _mobile_cell(mobile: dict) -> str:
    if mobile["status"] == "error":
        return "오류"
    if mobile["status"] == "none":
        return "없음"
    parts = []
    for hit in mobile["hits"]:
        where = f"{hit['position']}번째" if hit["position"] is not None else "순서 모름"
        parts.append(f"`{hit['area']}` {where}")
    return "<br>".join(parts)


def _pc_cell(pc: dict) -> str:
    if pc["status"] == "found":
        slot = f", 그 쪽 화면 {pc['slot_on_page']}번째 칸" if pc.get("slot_on_page") else ""
        return f"{pc['rank']}위 ({pc['page']}쪽{slot})"
    if pc["status"] == PC_NOT_FOUND:
        return f"{PC_PAGES}쪽 밖 ({pc['pages_fetched']}쪽에서 서로 다른 웹문서 {pc['web_docs_read']}건을 읽음)"
    return "오류"


def render_rank_markdown(result: dict) -> str:
    data = result["data"] or {}
    summary = data.get("summary") or {}
    lines = [
        f"# 네이버 검색어 순위: {data.get('domain', '')}",
        "",
        f"- 시각: {data.get('run_at', '')}",
        f"- 검색어: {len(data.get('keywords') or [])}개 · 상태 `{result['status']}`"
        + (f" · {result['error']}" if result.get("error") else ""),
    ]
    if data.get("unconfirmed_list"):
        lines.append("- 목록: **사람이 확인하지 않은 목록입니다.** 스크립트가 만든 후보를 그대로 썼습니다.")
    for warning in data.get("warnings") or []:
        lines.append(f"- 경고: {warning}")
    lines += [
        "",
        "## 요약",
        "",
        "| 항목 | 값 |",
        "|---|---|",
        f"| 모바일 통합검색에 우리 주소가 나온 검색어 | {summary.get('mobile_found', 0)} |",
        f"| 모바일 통합검색에 없음 | {summary.get('mobile_none', 0)} |",
        f"| PC 웹문서에서 순위가 잡힌 검색어 | {summary.get('pc_found', 0)} (10위 안 {summary.get('pc_top10', 0)}) |",
        f"| PC 웹문서 {PC_PAGES}쪽 밖 | {summary.get('pc_outside_read_pages', 0)} |",
        f"| 못 잰 것 (모바일 / PC) | {summary.get('mobile_errors', 0)} / {summary.get('pc_errors', 0)} |",
        "",
        "## 검색어별",
        "",
        "| # | 검색어 | 모바일 통합검색 (영역 · 영역 안 순서) | PC 웹문서 순위 | 오류 |",
        "|---|---|---|---|---|",
    ]
    for i, row in enumerate(data.get("results") or [], start=1):
        error = (row.get("error") or "").replace("|", "/")
        lines.append(f"| {i} | {row['keyword']} | {_mobile_cell(row['mobile'])} | {_pc_cell(row['pc'])} | {error} |")
    lines += ["", "## 우리 주소가 나온 자리", "", "| 검색어 | 화면 | 자리 | 주소 |", "|---|---|---|---|"]
    for row in data.get("results") or []:
        for hit in row["mobile"]["hits"]:
            where = f"`{hit['area']}` {hit['position']}번째" if hit["position"] is not None else f"`{hit['area']}`"
            lines.append(f"| {row['keyword']} | 모바일 | {where} (화면 묶음 {hit['block_index']}번째) | {hit['url']} |")
        if row["pc"]["status"] == "found":
            lines.append(f"| {row['keyword']} | PC 웹문서 | {row['pc']['rank']}위 | {row['pc']['url']} |")
    lines += [
        "",
        "## 읽는 법 · 한계",
        "",
        "- 한 번 잰 값입니다. 시각 · IP에 따라 다릅니다. 감사끼리 추세로 읽습니다.",
        "- 모바일 영역 이름은 네이버 화면 안의 값(`meta.ssuidExtra`)을 그대로 적었습니다. "
        "영역 안 순서는 같은 영역 값을 가진 묶음의 결과를 화면 순서로 이어 센 번호입니다.",
        "- 광고(파워링크 · 쇼핑 광고)는 `entry.bootstrap` 묶음 밖에 있어서 세지 않습니다.",
        f"- PC 웹문서 순위는 {PC_PAGES}쪽까지 읽은 웹문서(`fender_renderer-web`)끼리 센 순서이고, 겹쳐 나온 주소는 "
        "한 번만 셉니다. 한 쪽 20칸에는 블로그 · 카페 · 지식iN도 섞여 있어서 웹문서는 20건보다 적고, "
        "그래서 읽은 웹문서 수와 그 쪽 화면의 칸 번호를 함께 적습니다.",
        "- 네이버 검색 화면을 직접 읽습니다(공식 경로 아님). 화면 구조가 바뀌면 그 검색어는 오류로 남고 원문은 `raw/`에 저장됩니다.",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# candidates (docs/NAVER.md 6-1)
# ---------------------------------------------------------------------------

def _attrs(fragment: str) -> dict:
    out = {}
    for name, double, single in _ATTR_RE.findall(fragment or ""):
        out.setdefault(name.lower(), html_lib.unescape(double if double else single))
    return out


def page_title(page_html: str) -> str:
    match = _TITLE_RE.search(page_html or "")
    return _text(match.group(1)) if match else ""


def site_name_from(page_html: str) -> str:
    """og:site_name, else the first part of the homepage <title>."""
    for tag in _META_RE.findall(page_html or ""):
        attrs = _attrs(tag)
        if (attrs.get("property") or attrs.get("name") or "").lower() == "og:site_name" and attrs.get("content"):
            return _norm_kw(attrs["content"])
    title = page_title(page_html)
    return _SITE_TITLE_SPLIT_RE.split(title)[0].strip() if title else ""


def strip_site_name(title: str, site_name: str) -> str:
    """Remove the site name from the front (and, if present, the end) of a page title."""
    text = _norm_kw(title)
    name = _norm_kw(site_name)
    if not name:
        return text
    lowered, lname = text.lower(), name.lower()
    if lowered.startswith(f"[{lname}]"):
        text = text[len(name) + 2:]
    elif lowered.startswith(lname):
        text = text[len(name):]
    text = text.lstrip(_NAME_SEPARATORS)
    lowered = text.lower()
    if lowered.endswith(f"[{lname}]"):
        text = text[:-(len(name) + 2)]
    elif lowered.endswith(lname) and len(text) > len(name):
        text = text[:-len(name)]
    return text.rstrip(_NAME_SEPARATORS).strip()


def is_category_url(url: str) -> bool:
    parts = urlsplit(url)
    keys = {key.lower() for key in parse_qs(parts.query)}
    return bool(keys & CATEGORY_QUERY_KEYS) or bool(CATEGORY_PATH_RE.search(parts.path))


def clean_category_name(text: str) -> str:
    return _norm_kw(text).rstrip(_TRAILING_ARROWS).strip()


def extract_categories(page_html: str, base_url: str, domain: str) -> list:
    """Category links of the domain in page order, one per URL, named by the first non-empty link text."""
    found, order = {}, []
    for attr_text, inner in _ANCHOR_RE.findall(page_html or ""):
        attrs = _attrs(attr_text)
        href = (attrs.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        url = urljoin(base_url, href)
        if not url.startswith(("http://", "https://")) or not nsi.is_own(url, domain) or not is_category_url(url):
            continue
        name = clean_category_name(_text(inner))
        if not name:
            img = _IMG_RE.search(inner)
            name = clean_category_name((_attrs(img.group(1)).get("alt") if img else "") or attrs.get("title") or "")
        key = nsi.compare_key(url)
        if key not in found:
            found[key] = {"name": "", "url": url}
            order.append(key)
        if name and not found[key]["name"]:
            found[key]["name"] = name
    return [found[key] for key in order if found[key]["name"]]


def fetch_site_page(url: str, domain: str) -> tuple[str, str]:
    """GET a page of the site, following up to MAX_REDIRECTS redirects that stay on the domain."""
    for _ in range(MAX_REDIRECTS + 1):
        response = _get(url, _headers("pc"))
        if response.status_code in (301, 302, 303, 307, 308):
            location = (response.headers or {}).get("Location")
            target = urljoin(url, location) if location else ""
            if not target or not nsi.is_own(target, domain):
                raise SiteFetchError(f"redirect from {url} leaves the site")
            url = target
            continue
        if response.status_code != 200:
            raise SiteFetchError(f"{url} returned HTTP {response.status_code}")
        return url, decode_response_text(response)
    raise SiteFetchError(f"more than {MAX_REDIRECTS} redirects")


def _number(value) -> float:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return value
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0


LANDING_LISTS = ("top_product_urls", "top_landing_pages")


def load_landing_pages(path: str) -> tuple[list, str]:
    """Landing pages from naver_traffic.py output, most sessions first. Returns (pages, list name).

    Uses data.top_product_urls (product pages with their query) when it has rows, else
    data.top_landing_pages. The object may also be the data part itself, or a bare list.
    Each row may name its page as path, landing_page, landingPage, url (and a few variants).
    """
    with open(path, encoding="utf-8-sig") as handle:
        raw = json.load(handle)
    rows, used = raw, "list"
    if isinstance(raw, dict):
        inner = raw.get("data") if isinstance(raw.get("data"), dict) else raw
        used = next((name for name in LANDING_LISTS if isinstance(inner.get(name), list) and inner[name]),
                    next((name for name in LANDING_LISTS if isinstance(inner.get(name), list)), ""))
        rows = inner.get(used) if used else None
    if not isinstance(rows, list):
        raise ValueError("no top_product_urls or top_landing_pages list in the landing JSON")
    pages = []
    for row in rows:
        if isinstance(row, str):
            pages.append({"path": row.strip(), "sessions": 0})
        elif isinstance(row, dict):
            path = next((row[k] for k in LANDING_PATH_KEYS if isinstance(row.get(k), str) and row[k].strip()), None)
            if path:
                pages.append({"path": path.strip(), "sessions": _number(row.get("sessions"))})
    pages.sort(key=lambda r: -r["sessions"])
    return pages, used


def _path_and_query(url: str) -> str:
    parts = urlsplit(url)
    return (parts.path or "/") + ("?" + parts.query if parts.query else "")


def product_key(url: str) -> str:
    """One product, whatever extra query it carries: its id parameter, else path plus sorted query."""
    parts = urlsplit(url)
    for key, values in parse_qs(parts.query).items():
        if key.lower() in PRODUCT_ID_KEYS and values and values[0].strip():
            return f"{key.lower()}={values[0].strip()}"
    return nsi.compare_key(url)


def collect_products(landing: list, site_url: str, domain: str, site_name: str, patterns: list,
                     limit: int) -> tuple[list, list, list]:
    """Fetch product landing pages (most sessions first) and name them by their <title>.

    The same product (same id parameter, or same name) is kept once, at its first row.
    A product page that cannot be fetched is skipped with a warning (it may be sold out).
    Returns (products, skipped rows, warnings).
    """
    products, skipped, warnings = [], [], []
    seen_keys = set()
    for row in landing:
        if len(products) >= limit:
            break
        path = row["path"]
        if path == NOT_SET:
            skipped.append({"path": path, "reason": "not_set"})
            continue
        url = urljoin(site_url, path)
        if not nsi.is_own(url, domain):
            skipped.append({"path": path, "reason": "other_domain"})
            continue
        target = _path_and_query(url)
        without_id = PRODUCT_PAGE_WITHOUT_ID_RE.search(urlsplit(url).path) and "=" not in product_key(url)
        if not any(p.search(target) for p in patterns) or without_id:
            reason = "product_page_without_id" if PRODUCT_PAGE_WITHOUT_ID_RE.search(urlsplit(url).path) else "not_product"
            skipped.append({"path": path, "reason": reason})
            continue
        key = product_key(url)
        if key in seen_keys:
            skipped.append({"path": path, "reason": "duplicate_product"})
            continue
        seen_keys.add(key)
        try:
            _, page_html = fetch_site_page(url, domain)
        except (SiteFetchError, URLSafetyError, requests.exceptions.RequestException) as exc:
            skipped.append({"path": path, "reason": "fetch_failed"})
            warnings.append(f"product {path} skipped: {_err(exc)}")
            continue
        title = page_title(page_html)
        name = strip_site_name(title, site_name)
        if not name:
            skipped.append({"path": path, "reason": "empty_title"})
            continue
        if any(p["name"].lower() == name.lower() for p in products):
            skipped.append({"path": path, "reason": "duplicate_name"})
            continue
        products.append({"name": name, "title": title, "url": url, "path": path, "sessions": row["sessions"]})
    return products, skipped, warnings


def merge_candidates(categories: list, products: list, max_n: int) -> list:
    """Dedupe by name and cap at max_n. Products keep up to half the slots; unused slots go to the other side."""
    seen = set()

    def unique(rows: list, source: str) -> list:
        out = []
        for row in rows:
            keyword = _norm_kw(row["name"])
            if keyword and keyword.lower() not in seen:
                seen.add(keyword.lower())
                detail = {"keyword": keyword, "source": source, "url": row["url"]}
                if "sessions" in row:
                    detail["sessions"] = row["sessions"]
                out.append(detail)
        return out

    cats = unique(categories, "category")
    prods = unique(products, "product")
    n_prod = min(len(prods), max_n // 2)
    n_cat = min(len(cats), max_n - n_prod)
    n_prod = min(len(prods), max_n - n_cat)
    return cats[:n_cat] + prods[:n_prod]


def run_candidates(site: str, domain: str, landing_json: Optional[str] = None, max_n: int = DEFAULT_MAX,
                   site_name: Optional[str] = None, patterns: Optional[list] = None) -> dict:
    patterns = patterns or [re.compile(p, re.I) for p in DEFAULT_PRODUCT_PATTERNS]
    errors, warnings = [], []
    categories, name, home_title = [], site_name or "", ""
    try:
        final_url, home = fetch_site_page(site, domain)
        home_title = page_title(home)
        name = site_name or site_name_from(home)
        categories = extract_categories(home, final_url, domain)
        if not categories:
            errors.append("homepage: no category links found")
    except (SiteFetchError, URLSafetyError, requests.exceptions.RequestException) as exc:
        errors.append(f"homepage: {_err(exc)}")

    products, skipped, landing_list = [], [], None
    if landing_json:
        try:
            landing, landing_list = load_landing_pages(landing_json)
        except (OSError, ValueError) as exc:
            errors.append(f"landing json: {type(exc).__name__}: {exc}")
            landing = []
        products, skipped, product_warnings = collect_products(landing, site, domain, name, patterns, max_n)
        warnings += product_warnings
        if landing and not products:
            warnings.append("no product page found among the landing pages")
    else:
        warnings.append("no --landing-json given: product names were not added")

    details = merge_candidates(categories, products, max_n)
    data = {
        "site": site,
        "domain": domain,
        "generated_at": _now_kst(),
        "site_name": name,
        "homepage_title": home_title,
        "max": max_n,
        "unconfirmed_list": True,
        "candidates": [d["keyword"] for d in details],
        "candidate_details": details,
        "categories": categories,
        "products": products,
        "landing_list": landing_list,
        "skipped_landing_pages": skipped,
        "warnings": warnings,
    }
    if not details:
        status = "error"
    else:
        status = "partial" if errors else "success"
    return {"status": status, "data": data, "error": "; ".join(errors) or None, "metadata": _metadata()}


_MAX_SOURCE_LABELS = {"--max": "--max", "config keywords.max": "설정 keywords.max", "default": "기본값"}


def render_candidates_markdown(result: dict) -> str:
    data = result["data"] or {}
    lines = [
        f"# 네이버 검색어 후보: {data.get('domain', '')}",
        "",
        f"- 시각: {data.get('generated_at', '')}",
        f"- 사이트 이름(제목에서 뗀 앞머리): {data.get('site_name') or '(모름)'}",
        f"- 상태: `{result['status']}`" + (f" · {result['error']}" if result.get("error") else ""),
        "- **사람이 확인하지 않은 후보입니다.** 고친 목록을 설정 파일 `keywords.list`에 적습니다.",
    ]
    write = data.get("config_write")
    if write and write.get("written"):
        lines.append(f"- 설정 파일 `keywords.candidates`에 후보 {write['count']}개를 적었습니다: `{write['path']}`")
    elif write:
        lines.append(f"- 설정 파일에 적지 않았습니다: {write['reason']}")
    for warning in data.get("warnings") or []:
        lines.append(f"- 경고: {warning}")
    lines += ["", "## 후보", "", "| # | 검색어 | 출처 | 주소 |", "|---|---|---|---|"]
    labels = {"category": "사이트 분류", "product": "GA4 네이버 자연 검색 상위 상품"}
    for i, row in enumerate(data.get("candidate_details") or [], start=1):
        lines.append(f"| {i} | {row['keyword']} | {labels.get(row['source'], row['source'])} | {row['url']} |")
    lines += [
        "",
        f"찾은 분류 {len(data.get('categories') or [])}개 · 상품 {len(data.get('products') or [])}개 중 "
        f"상한 {data.get('max')}개({_MAX_SOURCE_LABELS.get(data.get('max_source'), '기본값')})까지 담았습니다.",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Keyword input, output files, CLI
# ---------------------------------------------------------------------------

def load_keywords_file(path: str) -> tuple[list, bool]:
    """Return (keywords, came_from_candidates).

    Accepts a JSON list; {"list": [...], "candidates": [...]}; a config with a "keywords"
    object; or this script's candidates output. An empty list falls back to candidates
    (docs/NAVER.md 6-1), which marks the list as not reviewed by a person.
    """
    with open(path, encoding="utf-8-sig") as handle:
        raw = json.load(handle)
    if isinstance(raw, list):
        return raw, False
    if not isinstance(raw, dict):
        raise ValueError("keywords file must be a JSON list or object")
    if isinstance(raw.get("data"), dict) and isinstance(raw["data"].get("candidates"), list):
        return raw["data"]["candidates"], True
    section = raw.get("keywords") if isinstance(raw.get("keywords"), dict) else raw
    listed = section.get("list") if isinstance(section.get("list"), list) else []
    if listed:
        return listed, False
    candidates = section.get("candidates") if isinstance(section.get("candidates"), list) else []
    if candidates:
        return candidates, True
    raise ValueError("keywords file has no keywords (list or candidates)")


def read_config(path: str) -> Optional[dict]:
    """Return the config object (docs/NAVER.md 10), or None when the file does not exist."""
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8-sig") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise ValueError("config file must be a JSON object")
    return raw


def config_max(config: Optional[dict]) -> tuple[Optional[int], Optional[str]]:
    """Return (keywords.max, warning). The value is None when it is absent or not usable."""
    section = config.get("keywords") if isinstance(config, dict) else None
    if not isinstance(section, dict) or section.get("max") is None:
        return None, None
    value = section["max"]
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value, None
    return None, f"keywords.max in the config is not a whole number of 1 or more ({value!r}); using {DEFAULT_MAX}"


def config_product_patterns(config: Optional[dict]) -> tuple[Optional[list], Optional[str]]:
    """Return (patterns, warning) from keywords.product_patterns (docs/NAVER.md 4-2, 6-1).

    Each entry is plain text matched as written, case-sensitive, the same way
    naver_traffic.py filters GA4. None when the key is absent or not usable.
    """
    section = config.get("keywords") if isinstance(config, dict) else None
    if not isinstance(section, dict) or section.get("product_patterns") is None:
        return None, None
    value = section["product_patterns"]
    if isinstance(value, list) and value and all(isinstance(p, str) and p.strip() for p in value):
        return [re.compile(re.escape(p)) for p in value], None
    return None, ("keywords.product_patterns in the config is not a list of non-empty strings "
                  f"({value!r}); using the script default")


def _atomic_write_json(path: str, payload) -> None:
    """Write JSON to a temp file in the same folder, then os.replace it over the target."""
    target = os.path.realpath(path)
    fd, tmp = tempfile.mkstemp(prefix=".naver-config-", suffix=".tmp", dir=os.path.dirname(target))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if os.path.exists(target):
            shutil.copymode(target, tmp)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def save_candidates_to_config(path: str, candidates: list) -> dict:
    """Write candidates into keywords.candidates only when that key is missing or an empty list.

    No other key is changed. A missing config file is not created, because its presence
    turns on the Naver checks in seo-audit (docs/NAVER.md 1, 10).
    """
    out = {"path": path, "written": False, "reason": None, "count": 0}
    if not candidates:
        out["reason"] = "no candidates to write"
        return out
    try:
        config = read_config(path)
    except (OSError, ValueError) as exc:
        out["reason"] = f"could not read the config ({type(exc).__name__}: {exc}); not changed"
        return out
    if config is None:
        out["reason"] = "config file does not exist; not created"
        return out
    section = config.get("keywords")
    if section is None:
        section = {}
    elif not isinstance(section, dict):
        out["reason"] = "keywords in the config is not an object; not changed"
        return out
    current = section.get("candidates")
    if isinstance(current, list) and current:
        out["reason"] = f"keywords.candidates already has {len(current)} entries; not changed"
        return out
    if current is not None and not isinstance(current, list):
        out["reason"] = "keywords.candidates in the config is not a list; not changed"
        return out
    section["candidates"] = list(candidates)
    config["keywords"] = section
    try:
        _atomic_write_json(path, config)
    except OSError as exc:
        out["reason"] = f"could not write the config ({type(exc).__name__}: {exc}); not changed"
        return out
    out.update(written=True, count=len(candidates))
    return out


def clean_keywords(values: list) -> list:
    out, seen = [], set()
    for value in values or []:
        keyword = _norm_kw(value) if isinstance(value, (str, int, float)) else ""
        if keyword and keyword not in seen:
            seen.add(keyword)
            out.append(keyword)
    return out


def write_outputs(result: dict, output_dir: str, kind: str) -> list:
    os.makedirs(output_dir, exist_ok=True)
    if kind == "rank":
        json_name, md_name, render = "naver-keyword-rank.json", "NAVER-KEYWORD-RANK.md", render_rank_markdown
    else:
        json_name, md_name, render = ("naver-keyword-candidates.json", "NAVER-KEYWORD-CANDIDATES.md",
                                      render_candidates_markdown)
    json_path = os.path.join(output_dir, json_name)
    md_path = os.path.join(output_dir, md_name)
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(render(result))
    return [json_path, md_path]


def site_root(value: str) -> str:
    value = value.strip()
    if "://" not in value:
        value = "https://" + value
    parts = urlsplit(value)
    return f"{parts.scheme}://{parts.netloc}{parts.path or '/'}"


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--site", required=True, help="Site URL, e.g. https://www.example.com")
    common.add_argument("--max", type=int,
                        help=f"Keyword cap (default: keywords.max from --config, else {DEFAULT_MAX})")
    common.add_argument("--config", help="Config file read for keywords.max and written by --save-to-config "
                                         "(default ~/.config/claude-seo/naver.json)")
    common.add_argument("--output-dir", help="Write the JSON and Markdown report (and raw/ for rank) here")
    common.add_argument("--delay", type=float, default=MIN_REQUEST_DELAY,
                        help=f"Seconds between Naver requests (minimum {MIN_REQUEST_DELAY:g})")
    common.add_argument("--json", action="store_true", help="Print the result as JSON")

    parser = argparse.ArgumentParser(description="Naver keyword candidates and rank check (direct read, no cookies)")
    sub = parser.add_subparsers(dest="command", required=True)
    cand = sub.add_parser("candidates", parents=[common], help="Build keyword candidates (categories + top products)")
    cand.add_argument("--landing-json",
                      help="naver_traffic.py output: data.top_product_urls, else data.top_landing_pages")
    cand.add_argument("--site-name", help="Site name to strip from titles (default: og:site_name or homepage title)")
    cand.add_argument("--product-pattern", action="append",
                      help="Regex for product landing pages, matched on path?query (repeatable, replaces defaults)")
    cand.add_argument("--save-to-config", action="store_true",
                      help="Write the candidates into keywords.candidates of --config, only when that key is "
                           "missing or empty; no other key is changed and a missing file is not created")
    rank = sub.add_parser("rank", parents=[common], help="Check mobile integrated search and PC web ranks")
    rank.add_argument("--keyword", action="append", help="Keyword (repeatable)")
    rank.add_argument("--keywords-file", help="JSON list of keywords (or a config with keywords.list / candidates)")
    rank.add_argument("--unconfirmed-list", action="store_true",
                      help="Mark the keyword list as not reviewed by a person (docs/NAVER.md 6-1)")
    return parser


def main(argv: Optional[list] = None) -> int:
    global REQUEST_DELAY
    args = build_parser().parse_args(argv)

    def fail(message: str) -> int:
        result = {"status": "error", "data": None, "error": message, "metadata": _metadata()}
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"Error: {message}", file=sys.stderr)
        return 1

    if args.delay < MIN_REQUEST_DELAY:
        return fail(f"--delay must be at least {MIN_REQUEST_DELAY:g} seconds")
    if args.max is not None and args.max < 1:
        return fail("--max must be at least 1")
    domain = nsi.normalize_domain(args.site)
    if not domain or "." not in domain:
        return fail("--site must look like https://www.example.com")
    REQUEST_DELAY = args.delay
    site = site_root(args.site)

    config_path = os.path.expanduser(args.config or DEFAULT_CONFIG)
    config_warnings = []
    try:
        config = read_config(config_path)
    except (OSError, ValueError) as exc:
        config = None
        config_warnings.append(f"could not read the config ({type(exc).__name__}: {exc})")
    from_config, max_warning = config_max(config)
    if max_warning:
        config_warnings.append(max_warning)
    if args.max is not None:
        max_n, max_source = args.max, "--max"
    elif from_config is not None:
        max_n, max_source = from_config, "config keywords.max"
    else:
        max_n, max_source = DEFAULT_MAX, "default"

    if args.command == "candidates":
        try:
            patterns = [re.compile(p, re.I) for p in args.product_pattern] if args.product_pattern else None
        except re.error as exc:
            return fail(f"--product-pattern is not a valid regex ({exc})")
        patterns_from = "--product-pattern" if patterns else None
        if patterns is None:
            patterns, patterns_warning = config_product_patterns(config)
            if patterns_warning:
                config_warnings.append(patterns_warning)
            patterns_from = "config keywords.product_patterns" if patterns else "default"
        result = run_candidates(site, domain, landing_json=args.landing_json, max_n=max_n,
                                site_name=args.site_name, patterns=patterns)
        data = result["data"]
        data["product_patterns_from"] = patterns_from
        if args.save_to_config:
            if result["status"] == "success":
                write = save_candidates_to_config(config_path, data["candidates"])
            else:
                write = {"path": config_path, "written": False, "count": 0,
                         "reason": f"result status is {result['status']}; not written"}
            data["config_write"] = write
    else:
        keywords = list(args.keyword or [])
        unconfirmed = args.unconfirmed_list
        if args.keywords_file:
            try:
                from_file, from_candidates = load_keywords_file(args.keywords_file)
            except (OSError, ValueError) as exc:
                return fail(f"could not read --keywords-file ({type(exc).__name__}: {exc})")
            keywords += from_file
            unconfirmed = unconfirmed or from_candidates
        keywords = clean_keywords(keywords)
        if not keywords:
            return fail("no keywords: pass --keyword or --keywords-file")
        result = run_rank(keywords, site, domain, unconfirmed=unconfirmed, max_keywords=max_n,
                          output_dir=args.output_dir)
        data = result["data"]

    data["max_source"] = max_source
    data["warnings"] = config_warnings + data["warnings"]
    written = write_outputs(result, args.output_dir, args.command) if args.output_dir else []
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if args.command == "candidates":
            print(f"Naver keyword candidates for {domain}: {len(data['candidates'])} (status {result['status']})")
            for keyword in data["candidates"]:
                print(f"  {keyword}")
            write = data.get("config_write")
            if write and write["written"]:
                print(f"  config: wrote {write['count']} names to keywords.candidates in {write['path']}")
            elif write:
                print(f"  config: not written ({write['reason']})")
        else:
            s = data["summary"]
            print(f"Naver keyword rank for {domain}: {s['keywords']} keywords (status {result['status']})")
            print(f"  mobile found {s['mobile_found']}, none {s['mobile_none']}, errors {s['mobile_errors']}")
            print(f"  pc found {s['pc_found']}, outside {PC_PAGES} pages {s['pc_outside_read_pages']}, "
                  f"errors {s['pc_errors']}")
        if result["error"]:
            print(f"  error: {result['error']}")
        for warning in data["warnings"]:
            print(f"  warning: {warning}")
        for path in written:
            print(f"  wrote {path}")
    return 1 if result["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
