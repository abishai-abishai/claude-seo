#!/usr/bin/env python3
"""
Naver site: page counter for Claude SEO.

Reads Naver web search result pages for "site:<domain>" directly (no search
API, no browser, no cookies), pages through them by following each page's
"next" link, and collects the domain's URLs in result order. URLs are counted by host and path,
and optionally compared with the site's sitemap.xml.

This is not the Search Advisor indexed-page count. It is the list of pages
Naver shows in its web results, useful before Search Advisor has data.

Spec (source of truth for this script): docs/NAVER-SITE-INDEX.md

Usage:
    python naver_site_index.py --domain example.com --json
    python naver_site_index.py --domain example.com --sitemap https://www.example.com/sitemap.xml \
        --output-dir example.com-audit/naver-site
"""

import argparse
import html as html_lib
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urljoin, urlsplit

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

SOURCE = "naver_site_index"
KST = timezone(timedelta(hours=9))
SEARCH_URL = "https://search.naver.com/search.naver?where=web&query={q}"
SEARCH_HOST = "search.naver.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
WEB_MARKER = "fender_renderer-web"
PAY_INFLOW_HOST = "inflow.pay.naver.com"
PAGE_SIZE = 20
DEFAULT_MAX_PAGES = 50
MAX_CHILD_SITEMAPS = 20

# Polite delay between requests. The spec fixes 3 seconds; the CLI only allows longer.
MIN_REQUEST_DELAY = 3.0
REQUEST_DELAY = MIN_REQUEST_DELAY
REQUEST_TIMEOUT = 30
_last_request_time = 0.0

_BOOTSTRAP_RE = re.compile(r'\.bootstrap\(\s*document\.getElementById\(\s*"(fdr-[\w-]+)"\s*\)\s*,\s*')
_NEXT_RE = re.compile(r'<[^>]*class="btn_next"[^>]*>')
_TAG_RE = re.compile(r"<[^>]+>")
_LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.S)
_DECODER = json.JSONDecoder()


class NaverFetchError(Exception):
    """A Naver page or the sitemap could not be fetched or read."""


def _rate_limit() -> None:
    """Wait so that consecutive requests are at least REQUEST_DELAY apart."""
    global _last_request_time
    now = time.time()
    elapsed = now - _last_request_time
    if _last_request_time > 0 and elapsed < REQUEST_DELAY:
        time.sleep(REQUEST_DELAY - elapsed)
    _last_request_time = time.time()


def _now_kst() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def _metadata() -> dict:
    return {
        "source": SOURCE,
        "spec": "docs/NAVER-SITE-INDEX.md",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _get(url: str, headers: dict) -> requests.Response:
    """One cookie-less GET. requests.get without a Session keeps no cookie jar between calls."""
    _rate_limit()
    return safe_requests_get(url, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=False)


# ---------------------------------------------------------------------------
# Reading one result page
# ---------------------------------------------------------------------------

def search_url(domain: str) -> str:
    """First result page. Later pages come from the page's own next link (spec 3-1)."""
    return SEARCH_URL.format(q=quote(f"site:{domain}", safe=""))


def _doc_url(item: dict) -> str:
    """Document URL of one result item (spec 3-2)."""
    keep = ((item.get("profile") or {}).get("keep") or {}).get("keepTriggerUrl")
    if keep:
        return keep
    href = item.get("href") or ""
    if urlsplit(href).netloc == PAY_INFLOW_HOST:
        return (parse_qs(urlsplit(href).query).get("retUrl") or [href])[0]
    return href


def extract_web_items(page_html: str) -> list:
    """Result items in page order as {url, title}. Raises NaverFetchError if a web block cannot be read."""
    items = []
    unreadable = 0
    for match in _BOOTSTRAP_RE.finditer(page_html):
        try:
            payload, _ = _DECODER.raw_decode(page_html, match.end())
        except ValueError:
            unreadable += 1
            continue
        if not isinstance(payload, dict) or (payload.get("meta") or {}).get("ssuidExtra") != WEB_MARKER:
            continue
        state = (payload.get("data") or {}).get("state") or {}
        for item in state.get("items") or []:
            url = _doc_url(item)
            if url:
                title = html_lib.unescape(_TAG_RE.sub("", str(item.get("title") or ""))).strip()
                items.append({"url": url, "title": title})
    if not items and WEB_MARKER in page_html and unreadable:
        raise NaverFetchError("web result block present but its JSON could not be read")
    return items


def next_page_url(page_html: str, domain: str) -> Optional[str]:
    """URL in the page's next button, or None on the last page (spec 3-1, 3-3).

    Raises NaverFetchError if the link leaves search.naver.com or changes the query.
    """
    match = _NEXT_RE.search(page_html)
    if not match or 'aria-disabled="true"' in match.group(0):
        return None
    href = re.search(r'href="([^"]*)"', match.group(0))
    if not href or not href.group(1):
        return None
    url = urljoin(SEARCH_URL.split("?")[0], html_lib.unescape(href.group(1)))
    parts = urlsplit(url)
    if parts.hostname != SEARCH_HOST or parse_qs(parts.query).get("query") != [f"site:{domain}"]:
        raise NaverFetchError("next link does not point to the same site: search")
    return url


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def is_own(url: str, domain: str) -> bool:
    host = _host(url)
    return host == domain or host.endswith("." + domain)


# ---------------------------------------------------------------------------
# Paging
# ---------------------------------------------------------------------------

def _save_raw(raw_dir: Optional[str], name: str, text: str, raw_files: list) -> None:
    if not raw_dir:
        return
    os.makedirs(raw_dir, exist_ok=True)
    path = os.path.join(raw_dir, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    raw_files.append(path)


def collect(domain: str, max_pages: int = DEFAULT_MAX_PAGES, raw_dir: Optional[str] = None) -> dict:
    """Page through site:<domain> results until a stop condition from spec 3-3."""
    urls, seen, raw_files = [], set(), []
    other_domain = 0
    pages_fetched = 0
    stop_reason = "max_pages"
    error = None
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9"}
    url = search_url(domain)
    pages_without_new = 0
    last_page_items = 0

    for page in range(1, max_pages + 1):
        try:
            response = _get(url, headers)
            if response.status_code != 200:
                raise NaverFetchError(f"page {page} returned HTTP {response.status_code}")
            page_html = decode_response_text(response)
            _save_raw(raw_dir, f"page-{page:03d}.html", page_html, raw_files)
            items = extract_web_items(page_html)
        except (NaverFetchError, URLSafetyError, requests.exceptions.RequestException) as exc:
            stop_reason, error = "error", f"{type(exc).__name__}: {exc}"
            break
        pages_fetched = page
        last_page_items = len(items)
        if not items:
            stop_reason = "empty_page"
            break
        new = 0
        for item in items:
            if not is_own(item["url"], domain):
                other_domain += 1
                continue
            if item["url"] in seen:
                continue
            seen.add(item["url"])
            new += 1
            parts = urlsplit(item["url"])
            urls.append({"rank": len(urls) + 1, "page": page, "url": item["url"], "host": _host(item["url"]),
                         "path": parts.path or "/", "title": item["title"]})
        pages_without_new = pages_without_new + 1 if new == 0 else 0
        if pages_without_new >= 2:
            stop_reason = "no_new_urls"
            break
        try:
            next_url = next_page_url(page_html, domain)
        except NaverFetchError as exc:
            stop_reason, error = "error", f"{type(exc).__name__}: {exc}"
            break
        if next_url is None:
            stop_reason = "last_page"
            break
        url = next_url

    return {"urls": urls, "other_domain": other_domain, "pages_fetched": pages_fetched,
            "stop_reason": stop_reason, "last_page_items": last_page_items, "error": error,
            "raw_files": raw_files}


# ---------------------------------------------------------------------------
# Sitemap comparison
# ---------------------------------------------------------------------------

def parse_sitemap(xml_text: str) -> tuple[list, bool]:
    """Return (<loc> values with &amp; decoded, is_sitemap_index)."""
    locs = [html_lib.unescape(loc) for loc in _LOC_RE.findall(xml_text)]
    return locs, "<sitemapindex" in xml_text


def fetch_sitemap(url: str) -> list:
    """Fetch a sitemap; follow a sitemap index one level (spec 4)."""
    headers = {"User-Agent": USER_AGENT}

    def read(target: str) -> str:
        response = _get(target, headers)
        if response.status_code != 200:
            raise NaverFetchError(f"sitemap returned HTTP {response.status_code}")
        return decode_response_text(response)

    locs, is_index = parse_sitemap(read(url))
    if not is_index:
        return locs
    pages = []
    for child in locs[:MAX_CHILD_SITEMAPS]:
        pages += parse_sitemap(read(child))[0]
    return pages


def compare_key(url: str) -> str:
    """Host-free key: path plus query sorted by name (spec 4)."""
    parts = urlsplit(html_lib.unescape(url))
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return (parts.path or "/") + ("?" + query if query else "")


def compare_with_sitemap(found_urls: list, sitemap_urls: list) -> dict:
    found = {}
    for url in found_urls:
        found.setdefault(compare_key(url), url)
    mapped = {}
    for url in sitemap_urls:
        mapped.setdefault(compare_key(url), url)
    both = found.keys() & mapped.keys()
    return {
        "total": len(mapped),
        "in_both": len(both),
        "search_only": len(found.keys() - mapped.keys()),
        "sitemap_only": len(mapped.keys() - found.keys()),
        "search_only_urls": [found[k] for k in found if k not in mapped],
        "sitemap_only_urls": [mapped[k] for k in mapped if k not in found],
    }


# ---------------------------------------------------------------------------
# Run, summarize, report
# ---------------------------------------------------------------------------

def summarize(collected: dict) -> dict:
    urls = collected["urls"]
    return {
        "total_urls": len(urls),
        "other_domain": collected["other_domain"],
        "by_host": dict(Counter(u["host"] for u in urls).most_common()),
        "by_path": dict(Counter(u["path"] for u in urls).most_common()),
    }


def run_check(domain: str, max_pages: int = DEFAULT_MAX_PAGES, sitemap: Optional[str] = None,
              output_dir: Optional[str] = None) -> dict:
    raw_dir = os.path.join(output_dir, "raw") if output_dir else None
    searched_at = _now_kst()
    collected = collect(domain, max_pages=max_pages, raw_dir=raw_dir)
    data = {
        "domain": domain,
        "searched_at": searched_at,
        "pages_fetched": collected["pages_fetched"],
        "stop_reason": collected["stop_reason"],
        "max_pages": max_pages,
        "last_page_items": collected["last_page_items"],
        "urls": collected["urls"],
        "summary": summarize(collected),
        "raw_files": collected["raw_files"],
    }
    errors = [collected["error"]] if collected["error"] else []
    if sitemap:
        try:
            data["sitemap"] = {"url": sitemap, **compare_with_sitemap([u["url"] for u in collected["urls"]],
                                                                       fetch_sitemap(sitemap))}
        except (NaverFetchError, URLSafetyError, requests.exceptions.RequestException) as exc:
            errors.append(f"sitemap: {type(exc).__name__}: {exc}")

    if collected["pages_fetched"] == 0:
        status = "error"
    elif errors or collected["stop_reason"] == "max_pages":
        status = "partial"
    else:
        status = "success"
    return {"status": status, "data": data, "error": "; ".join(errors) or None, "metadata": _metadata()}


STOP_LABELS = {
    "last_page": "마지막 쪽까지 읽음",
    "empty_page": "결과가 없는 쪽에서 멈춤",
    "no_new_urls": "두 쪽 연달아 새 주소가 없어 멈춤",
    "max_pages": "쪽 상한에 닿아 멈춤 (뒤에 더 있을 수 있음)",
    "error": "요청 실패로 멈춤",
}


def render_markdown(result: dict) -> str:
    data = result["data"] or {}
    summary = data.get("summary") or {}
    lines = [
        f"# 네이버 검색에 잡힌 페이지: {data.get('domain', '')}",
        "",
        f"- 검색: `site:{data.get('domain', '')}` (네이버 웹문서, PC)",
        f"- 시각: {data.get('searched_at', '')}",
        f"- 읽은 쪽: {data.get('pages_fetched', 0)}쪽 · {STOP_LABELS.get(data.get('stop_reason'), '')}",
        f"- 상태: `{result['status']}`" + (f" · {result['error']}" if result.get("error") else ""),
    ]
    if data.get("stop_reason") == "last_page" and data.get("last_page_items", 0) >= PAGE_SIZE:
        lines.append(f"- 마지막 쪽도 {PAGE_SIZE}건이 꽉 차 있었습니다. 네이버가 보여 주는 쪽 수 상한(10쪽으로 보임)에 "
                     "닿은 것이라, 실제로 잡힌 페이지는 이보다 많을 수 있습니다.")
    lines += [
        "",
        "## 요약",
        "",
        "| 항목 | 값 |",
        "|---|---|",
        f"| 잡힌 주소 | {summary.get('total_urls', 0)} |",
        f"| 다른 도메인 주소 | {summary.get('other_domain', 0)} |",
        "",
        "## 호스트별",
        "",
        "| 호스트 | 주소 수 |",
        "|---|---|",
    ]
    lines += [f"| `{host}` | {count} |" for host, count in (summary.get("by_host") or {}).items()]
    lines += ["", "## 화면 종류(경로)별", "", "| 경로 | 주소 수 |", "|---|---|"]
    lines += [f"| `{path}` | {count} |" for path, count in (summary.get("by_path") or {}).items()]

    sitemap = data.get("sitemap")
    if sitemap:
        lines += [
            "",
            "## 사이트맵 대조",
            "",
            f"사이트맵: `{sitemap['url']}` (서로 다른 화면 {sitemap['total']}개, 호스트를 빼고 대조)",
            "",
            "| 묶음 | 수 |",
            "|---|---|",
            f"| 검색에 잡혔고 사이트맵에도 있음 | {sitemap['in_both']} |",
            f"| 검색에 잡혔는데 사이트맵에 없음 | {sitemap['search_only']} |",
            f"| 사이트맵에 있는데 이번 검색 결과에 없음 | {sitemap['sitemap_only']} |",
        ]

    lines += ["", "## 잡힌 주소", "", "| 순서 | 쪽 | 주소 | 제목 |", "|---|---|---|---|"]
    for u in data.get("urls") or []:
        title = u["title"].replace("|", "/")
        lines.append(f"| {u['rank']} | {u['page']} | {u['url']} | {title} |")
    lines += [
        "",
        "## 읽는 법 · 한계",
        "",
        "- 서치어드바이저의 색인 페이지 수와 같은 값이 아닙니다. 네이버가 검색 결과 목록으로 보여 준 주소만 셉니다.",
        "- 네이버가 `site:` 결과를 전부 보여 준다는 보장이 없어서, \"적어도 이만큼은 잡혀 있다\"로 읽습니다.",
        "- 네이버 검색 화면을 직접 읽습니다(공식 API 아님). 화면 구조가 바뀌면 오류로 남고 원문은 `raw/`에 저장됩니다.",
        "",
    ]
    return "\n".join(lines)


def write_outputs(result: dict, output_dir: str) -> list:
    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, "naver-site-index.json")
    md_path = os.path.join(output_dir, "NAVER-SITE-INDEX.md")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(render_markdown(result))
    return [json_path, md_path]


def normalize_domain(value: str) -> str:
    value = value.strip().lower()
    if "://" in value:
        value = urlsplit(value).hostname or ""
    return value.removeprefix("www.").strip(".")


def main() -> int:
    global REQUEST_DELAY
    parser = argparse.ArgumentParser(description="Count pages Naver web search shows for site:<domain> (direct read)")
    parser.add_argument("--domain", required=True, help="Domain or site URL, e.g. example.com")
    parser.add_argument("--sitemap", help="Sitemap URL to compare with")
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES,
                        help=f"Result pages to read at most ({PAGE_SIZE} results each, default {DEFAULT_MAX_PAGES})")
    parser.add_argument("--output-dir", help="Write naver-site-index.json, NAVER-SITE-INDEX.md and raw/ here")
    parser.add_argument("--delay", type=float, default=MIN_REQUEST_DELAY,
                        help=f"Seconds between requests (minimum {MIN_REQUEST_DELAY:g})")
    parser.add_argument("--json", action="store_true", help="Print the result as JSON")
    args = parser.parse_args()

    def fail(message: str) -> int:
        result = {"status": "error", "data": None, "error": message, "metadata": _metadata()}
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"Error: {message}", file=sys.stderr)
        return 1

    if args.delay < MIN_REQUEST_DELAY:
        return fail(f"--delay must be at least {MIN_REQUEST_DELAY:g} seconds")
    if args.max_pages < 1:
        return fail("--max-pages must be at least 1")
    domain = normalize_domain(args.domain)
    if not domain or "." not in domain:
        return fail("--domain must look like example.com")
    REQUEST_DELAY = args.delay

    result = run_check(domain, max_pages=args.max_pages, sitemap=args.sitemap, output_dir=args.output_dir)
    written = write_outputs(result, args.output_dir) if args.output_dir else []

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        data = result["data"]
        print(f"Naver site:{domain}: {data['summary']['total_urls']} URLs in {data['pages_fetched']} pages "
              f"({data['stop_reason']}, status {result['status']})")
        if data.get("sitemap"):
            s = data["sitemap"]
            print(f"  sitemap: in both {s['in_both']}, search only {s['search_only']}, sitemap only {s['sitemap_only']}")
        if result["error"]:
            print(f"  error: {result['error']}")
        for path in written:
            print(f"  wrote {path}")
    return 1 if result["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
