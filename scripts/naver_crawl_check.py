#!/usr/bin/env python3
"""
Naver crawl check (Yeti) for Claude SEO.

Requests a site the way Naver's crawler Yeti does and checks the items of the
Naver Search Advisor webmaster guide: Yeti blocking, robots.txt, duplicate
hosts, sitemap hosts, pages that send the visitor elsewhere with JavaScript,
pages that reuse the homepage title, and the homepage itself.
Read-only GET requests, one second apart, no redirects followed blindly
(each hop is validated by url_safety).

Spec (source of truth for this script): docs/NAVER.md section 7

Usage:
    python naver_crawl_check.py --site https://www.example.com --json
    python naver_crawl_check.py --site https://www.example.com --sample-size 50 \
        --output-dir example.com-audit/naver-crawl
"""

import argparse
import gzip
import html as html_lib
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urljoin, urlsplit

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

SOURCE = "naver_crawl_check"
SPEC = "docs/NAVER.md section 7"
KST = timezone(timedelta(hours=9))

# Yeti names exactly as Naver publishes them (spec 7); W.X.Y.Z filled with one concrete Chrome version.
CHROME_VERSION = "150.0.7871.114"
YETI_UAS = {
    "yeti_chrome": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko; "
                    f"compatible; Yeti/1.1; +https://naver.me/spd) Chrome/{CHROME_VERSION} Safari/537.36"),
    "yeti": "Mozilla/5.0 (compatible; Yeti/1.1; +https://naver.me/spd)",
}
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              f"(KHTML, like Gecko) Chrome/{CHROME_VERSION} Safari/537.36")
USER_AGENTS = {**YETI_UAS, "browser": BROWSER_UA}
PAGE_UA = "yeti_chrome"   # HTML pages
FILE_UA = "yeti"          # robots.txt and sitemaps

CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".config", "claude-seo", "naver.json")
DEFAULT_SAMPLE_SIZE = 50
PRODUCT_PERCENT = 80      # spec 7: products take at most 80% of the sample (rounded down)
MAX_REDIRECTS = 5
MAX_CHILD_SITEMAPS = 20
MAX_LISTED_URLS = 100
REQUEST_DELAY = 1.0       # spec 7: one second between requests
REQUEST_TIMEOUT = 30
_last_request_time = 0.0

CHECK_NAMES = {
    "yeti_blocked": "Yeti 차단",
    "robots_txt": "robots.txt",
    "hosts": "호스트",
    "sitemap_host": "사이트맵 호스트",
    "js_redirect": "자바스크립트로 보내는 화면",
    "same_title": "같은 제목",
    "homepage": "첫 화면",
}
SEVERITY_ORDER = ["Critical", "High", "Medium", "Low", "Info"]

_LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.S | re.I)
_COMMENT_OR_SCRIPT_RE = re.compile(r"<!--.*?(?:-->|$)|<script\b([^>]*)>(.*?)(?:</script\s*>|$)", re.I | re.S)
_TITLE_RE = re.compile(r"<title\b[^>]*>(.*?)</title\s*>", re.I | re.S)
_TAG_RE = re.compile(r"<(a|link|meta|div|span|li|button|td|tr|img|p|dd|dt|section|article)\b([^>]*)>", re.I)
_ATTR_RE = re.compile(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""")
_SCRIPT_TYPE_RE = re.compile(r"""\btype\s*=\s*["']?([^"'\s>]+)""", re.I)
_JS_TYPES = {"", "text/javascript", "application/javascript", "application/x-javascript",
             "text/ecmascript", "application/ecmascript", "module"}
_NAV_RE = re.compile(r"\blocation\s*\.\s*(replace\s*\(|href\s*=(?!=))")
_NAV_TARGET_RE = re.compile(r"""\s*(["'`])(.*?)\1""", re.S)
_OBJ_PREFIX_RE = re.compile(r"(?:[\w$]+\s*\.\s*)+$")
_NAV_HINT_RE = re.compile(r"""location|window\.open|\.php\b|\.html?\b|https?://|['"]\.{0,2}/""", re.I)
_FUNC_HEAD_RES = (
    re.compile(r"=>\s*$"),
    re.compile(r"\bfunction\b[\s\w$*]*\([^()]*\)\s*$"),
    re.compile(r"(?<![\w$.])(?!(?:if|for|while|switch|catch|with)\b)[\w$]+\s*\([^()]*\)\s*$"),
)
_REGEX_AFTER = set("(,=:[!&|?{};+-*%<>~^")
_REGEX_KEYWORD_RE = re.compile(r"(?:^|[^\w$])(?:return|typeof|case|void|delete|throw|in|of|else|do)\s*$")
_SECOND_LEVEL = {"co", "or", "ne", "go", "ac", "re", "pe", "com", "net", "org", "gov", "edu"}


class SettingsError(Exception):
    """naver.json or a command line value cannot be used."""


def _config_section(config_path: str) -> dict:
    """crawl_check section of naver.json; {} when the file or the section is absent."""
    if not os.path.isfile(config_path):
        return {}
    try:
        with open(config_path, encoding="utf-8") as handle:
            config = json.load(handle)
    except (OSError, ValueError) as exc:
        raise SettingsError(f"{config_path} could not be read: {exc}") from exc
    section = config.get("crawl_check") if isinstance(config, dict) else None
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise SettingsError(f"crawl_check in {config_path} must be an object")
    return section


def resolve_sample_size(config_path: str = CONFIG_PATH, sample_size: Optional[int] = None) -> tuple[int, str]:
    """(sample size, where it came from): command line, else naver.json crawl_check.sample_size, else 50."""
    origin = "cli"
    if sample_size is None:
        value = _config_section(config_path).get("sample_size")
        if value is None:
            sample_size, origin = DEFAULT_SAMPLE_SIZE, "default"
        elif isinstance(value, int) and not isinstance(value, bool):
            sample_size, origin = value, "config"
        else:
            raise SettingsError("crawl_check.sample_size must be a whole number")
    if sample_size < 1:
        raise SettingsError("sample size must be at least 1")
    return sample_size, origin


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
    return {"source": SOURCE, "spec": SPEC, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def _get(url: str, headers: dict) -> requests.Response:
    """One cookie-less GET without following redirects."""
    _rate_limit()
    return safe_requests_get(url, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=False)


def _header(headers, name: str) -> Optional[str]:
    for key, value in (headers or {}).items():
        if key.lower() == name.lower():
            return value
    return None


def fetch(url: str, ua: str, follow: bool = False) -> dict:
    """GET url with one of USER_AGENTS. With follow, redirects are followed hop by hop (each hop validated)."""
    headers = {"User-Agent": USER_AGENTS[ua]}
    chain, current = [], url
    out = {"url": url, "final_url": url, "status": None, "location": None, "chain": chain, "bytes": None,
           "headers": {}, "content": b"", "text": "", "error": None}
    for _ in range(MAX_REDIRECTS + 1):
        try:
            response = _get(current, headers)
        except (URLSafetyError, requests.exceptions.RequestException) as exc:
            out.update(final_url=current, error=f"{type(exc).__name__}: {exc}")
            return out
        location = _header(response.headers, "Location")
        chain.append({"url": current, "status": response.status_code, "location": location})
        if follow and 300 <= response.status_code < 400 and location:
            current = urljoin(current, location)
            continue
        content = response.content or b""
        out.update(final_url=current, status=response.status_code, location=location, bytes=len(content),
                   headers=response.headers or {}, content=content, text=decode_response_text(response))
        return out
    out.update(final_url=current, status=chain[-1]["status"], error=f"more than {MAX_REDIRECTS} redirects")
    return out


def _brief_fetch(f: dict) -> dict:
    return {"url": f["url"], "status": f["status"], "final_url": f["final_url"], "location": f["location"],
            "chain": f["chain"], "bytes": f["bytes"], "error": f["error"]}


# ---------------------------------------------------------------------------
# Reading HTML
# ---------------------------------------------------------------------------

def split_html(page: str) -> tuple[str, list]:
    """Return (markup without HTML comments and scripts, [(script attrs, script code)]).

    Comments and scripts are scanned left to right so a commented-out script is
    dropped and a script that contains "<!--" keeps its code.
    """
    scripts, parts, pos = [], [], 0
    for match in _COMMENT_OR_SCRIPT_RE.finditer(page):
        parts += [page[pos:match.start()], " "]
        if match.group(1) is not None:
            scripts.append((match.group(1), match.group(2) or ""))
        pos = match.end()
    parts.append(page[pos:])
    return "".join(parts), scripts


def _attrs(tag_body: str) -> dict:
    out = {}
    for name, dq, sq, bare in _ATTR_RE.findall(tag_body):
        out.setdefault(name.lower(), html_lib.unescape(dq or sq or bare))
    return out


def _skip_quoted(code: str, i: int) -> int:
    quote, j, n = code[i], i + 1, len(code)
    while j < n:
        if code[j] == "\\":
            j += 2
            continue
        if code[j] == quote:
            return j + 1
        if code[j] == "\n" and quote != "`":
            return j
        j += 1
    return n


def _skip_regex(code: str, i: int) -> Optional[int]:
    """End of a regex literal starting at i, or None if this "/" is not one."""
    j, n, in_class = i + 1, len(code), False
    while j < n:
        ch = code[j]
        if ch == "\\":
            j += 2
            continue
        if ch == "\n":
            return None
        if ch == "[":
            in_class = True
        elif ch == "]":
            in_class = False
        elif ch == "/" and not in_class:
            j += 1
            while j < n and (code[j].isalpha()):
                j += 1
            return j
        j += 1
    return None


def scan_js(code: str) -> tuple[list, list]:
    """Return (function body spans, string/comment/regex spans) of a script."""
    fn_spans, skips, stack, buf = [], [], [], []
    i, n, last = 0, len(code), ""
    while i < n:
        ch, nxt = code[i], code[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            end = code.find("\n", i)
            end = n if end < 0 else end
            skips.append((i, end))
            i = end
            continue
        if ch == "/" and nxt == "*":
            end = code.find("*/", i + 2)
            end = n if end < 0 else end + 2
            skips.append((i, end))
            i = end
            continue
        end = None
        if ch in "'\"`":
            end = _skip_quoted(code, i)
        elif ch == "/" and (last == "" or last in _REGEX_AFTER or _REGEX_KEYWORD_RE.search("".join(buf[-12:]))):
            end = _skip_regex(code, i)
        if end is not None:
            skips.append((i, end))
            buf.append('""')
            last = '"'
            i = end
            continue
        if ch == "{":
            tail = "".join(buf[-300:])
            stack.append((i, any(r.search(tail) for r in _FUNC_HEAD_RES)))
        elif ch == "}" and stack:
            start, is_fn = stack.pop()
            if is_fn:
                fn_spans.append((start, i + 1))
        buf.append(ch)
        if not ch.isspace():
            last = ch
        i += 1
    fn_spans += [(start, n) for start, is_fn in stack if is_fn]
    return fn_spans, skips


def js_redirects(scripts: list) -> list:
    """Navigation statements (location.replace / location.href=) that run when the page loads.

    Statements inside a function body (event handlers, callbacks) or an arrow
    function expression are not counted: they run only when called.
    """
    found = []
    for attrs, code in scripts:
        if re.search(r"\bsrc\s*=", attrs, re.I):
            continue
        type_match = _SCRIPT_TYPE_RE.search(attrs)
        if (type_match.group(1).lower() if type_match else "") not in _JS_TYPES:
            continue
        fn_spans, skips = scan_js(code)
        for match in _NAV_RE.finditer(code):
            pos = match.start()
            if any(a <= pos < b for a, b in skips) or any(a < pos < b for a, b in fn_spans):
                continue
            if _OBJ_PREFIX_RE.sub("", code[max(0, pos - 200):pos]).rstrip().endswith("=>"):
                continue
            target = _NAV_TARGET_RE.match(code, match.end())
            found.append({"target": target.group(2) if target else None,
                          "snippet": re.sub(r"\s+", " ", code[pos:pos + 120]).strip()})
    return found


def _robots_tokens(value: Optional[str]) -> set:
    return {t for t in re.split(r"[\s,]+", (value or "").lower()) if t}


def _x_robots_for_yeti(value: Optional[str]) -> set:
    """Directives of an X-Robots-Tag header that apply to Yeti (no bot prefix, or a yeti: prefix)."""
    out, bot = set(), None
    for token in re.split(r",", (value or "").lower()):
        token = token.strip()
        prefix = re.match(r"([a-z][\w-]*)\s*:\s*(.*)$", token)
        if prefix and prefix.group(1) not in ("unavailable_after", "max-snippet", "max-image-preview",
                                              "max-video-preview"):
            bot, token = prefix.group(1), prefix.group(2).strip()
        if bot in (None, "yeti") and token:
            out.add(token)
    return out


def page_facts(page: str, headers=None) -> dict:
    markup, scripts = split_html(page)
    title = _TITLE_RE.search(markup)
    facts = {
        "title": re.sub(r"\s+", " ", html_lib.unescape(title.group(1))).strip() if title else None,
        "canonical": None, "meta_robots": None, "meta_yeti": None,
        "x_robots_tag": _header(headers, "X-Robots-Tag"),
        "frameset": bool(re.search(r"<frameset\b", markup, re.I)),
        "links": 0, "javascript_hrefs": 0, "js_only_links": 0, "js_only_link_examples": [], "rss": [],
        "js_redirects": js_redirects(scripts),
    }
    for match in _TAG_RE.finditer(markup):
        tag, attrs = match.group(1).lower(), _attrs(match.group(2))
        if tag == "link":
            rel = _robots_tokens(attrs.get("rel"))
            if "canonical" in rel and facts["canonical"] is None:
                facts["canonical"] = attrs.get("href")
            if "alternate" in rel and attrs.get("type", "").lower() in ("application/rss+xml", "application/atom+xml"):
                facts["rss"].append(attrs.get("href"))
            continue
        if tag == "meta":
            name = attrs.get("name", "").lower()
            if name == "robots" and facts["meta_robots"] is None:
                facts["meta_robots"] = attrs.get("content", "")
            elif name == "yeti" and facts["meta_yeti"] is None:
                facts["meta_yeti"] = attrs.get("content", "")
            continue
        onclick = attrs.get("onclick", "")
        if tag == "a":
            href = attrs.get("href")
            href_s = (href or "").strip()
            is_js = href_s.lower().startswith("javascript:")
            facts["javascript_hrefs"] += is_js
            if href_s and not is_js and not href_s.startswith("#"):
                facts["links"] += 1
                continue
            nav = _NAV_HINT_RE.search((href_s[11:] if is_js else "") + " " + onclick)
        else:
            nav = onclick and _NAV_HINT_RE.search(onclick)
        if nav:
            facts["js_only_links"] += 1
            if len(facts["js_only_link_examples"]) < 10:
                facts["js_only_link_examples"].append(match.group(0)[:160])
    return facts


# ---------------------------------------------------------------------------
# Site, hosts, robots.txt, sitemap
# ---------------------------------------------------------------------------

def normalize_site(value: str) -> Optional[str]:
    """https://host/ for a site URL or host, or None if it does not look like one."""
    value = value.strip()
    if "://" not in value:
        value = "https://" + value
    parts = urlsplit(value)
    host = (parts.hostname or "").lower().strip(".")
    if parts.scheme not in ("http", "https") or "." not in host:
        return None
    return f"{parts.scheme}://{host}" + (f":{parts.port}" if parts.port else "") + "/"


def registrable_domain(host: str) -> str:
    """example.com for www.example.com; example.co.kr for shop.example.co.kr (two-letter ccTLD rule)."""
    labels = host.lower().strip(".").split(".")
    keep = 3 if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL else 2
    return ".".join(labels[-keep:])


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def parse_robots(text: str) -> tuple[list, list]:
    """Return ([{agents, rules, crawl_delay}], sitemap URLs). Groups follow RFC 9309."""
    groups, sitemaps, current, last_was_agent = [], [], None, False
    for raw in text.lstrip("\ufeff").splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()
        if key == "sitemap":
            if value:
                sitemaps.append(value)
        elif key in ("user-agent", "useragent"):
            if current is None or not last_was_agent:
                current = {"agents": [], "rules": [], "crawl_delay": []}
                groups.append(current)
            current["agents"].append(value)
            last_was_agent = True
        elif key in ("allow", "disallow", "crawl-delay"):
            last_was_agent = False
            if current is None:
                continue
            if key == "crawl-delay":
                current["crawl_delay"].append(value)
            else:
                current["rules"].append({"type": key, "path": value})
    return groups, sitemaps


def select_groups(groups: list) -> tuple[str, list]:
    """All groups naming Yeti (case-insensitive), else all "*" groups (spec 7)."""
    yeti = [g for g in groups if any(a.split("/")[0].strip().lower() == "yeti" for a in g["agents"])]
    if yeti:
        return "yeti", yeti
    star = [g for g in groups if any(a.strip() == "*" for a in g["agents"])]
    return ("*", star) if star else ("none", [])


def _rule_regex(path: str) -> re.Pattern:
    anchored = path.endswith("$")
    body = path[:-1] if anchored else path
    return re.compile("".join(".*" if ch == "*" else re.escape(ch) for ch in body) + ("$" if anchored else ""))


def is_allowed(rules: list, target: str) -> bool:
    """Longest matching rule wins; Allow wins a tie; no match means allowed (RFC 9309)."""
    best = None
    for rule in rules:
        if rule["path"] and _rule_regex(rule["path"]).match(target):
            key = (len(rule["path"]), rule["type"] == "allow")
            best = key if best is None or key > best else best
    return True if best is None else best[1]


def robots_target(url: str) -> str:
    parts = urlsplit(url)
    return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")


def parse_sitemap(xml_text: str) -> tuple[list, bool]:
    """Return (<loc> values with entities decoded, is_sitemap_index)."""
    return [html_lib.unescape(loc) for loc in _LOC_RE.findall(xml_text)], "<sitemapindex" in xml_text.lower()


def _sitemap_text(f: dict) -> str:
    if f["content"][:2] == b"\x1f\x8b":
        return gzip.decompress(f["content"]).decode("utf-8", errors="replace")
    return f["text"]


def read_sitemaps(sources: list) -> dict:
    """Read every sitemap source; a sitemap index is followed one level (spec 7)."""
    out = {"sources": [], "locs": [], "errors": [], "read": 0}

    def read_one(url: str) -> Optional[tuple[list, bool]]:
        f = fetch(url, FILE_UA, follow=True)
        entry = {"url": url, "status": f["status"], "final_url": f["final_url"], "error": f["error"]}
        out["sources"].append(entry)
        if f["error"] or f["status"] != 200:
            out["errors"].append(f"sitemap {url}: {f['error'] or 'HTTP ' + str(f['status'])}")
            return None
        try:
            locs, is_index = parse_sitemap(_sitemap_text(f))
        except (OSError, EOFError) as exc:
            out["errors"].append(f"sitemap {url}: {type(exc).__name__}: {exc}")
            return None
        entry.update(kind="sitemapindex" if is_index else "urlset", locs=len(locs))
        return locs, is_index

    for source in sources:
        got = read_one(source)
        if got is None:
            continue
        locs, is_index = got
        if not is_index:
            out["locs"] += locs
            out["read"] += 1
            continue
        if len(locs) > MAX_CHILD_SITEMAPS:
            out["errors"].append(f"sitemap index {source} lists {len(locs)} sitemaps; read the first {MAX_CHILD_SITEMAPS}")
        for child in locs[:MAX_CHILD_SITEMAPS]:
            child_got = read_one(child)
            if child_got is None:
                continue
            if child_got[1]:
                out["errors"].append(f"sitemap {child}: nested sitemap index not followed")
                continue
            out["locs"] += child_got[0]
            out["read"] += 1
    return out


def is_product_like(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return "goods_view" in path or "product" in path


def is_category_like(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return "goods_list" in path or "categor" in path


def choose_sample(locs: list, size: int) -> tuple[list, dict]:
    """Sitemap URLs to request as Yeti, without repeats (spec 7), and the sample's make-up.

    Products take at most 80% of the sample (rounded down). The rest goes to
    non-product URLs: pages that are neither product nor category first, then
    categories. When one side has too few URLs, the other side fills the
    remaining slots.
    """
    product, plain, category = [], [], []
    for loc in dict.fromkeys(locs):
        if is_product_like(loc):
            product.append(loc)
        else:
            (category if is_category_like(loc) else plain).append(loc)
    non_product = plain + category
    cap = size * PRODUCT_PERCENT // 100
    n_product = min(len(product), cap)
    n_other = min(len(non_product), size - n_product)
    n_product = min(len(product), size - n_other)
    chosen_other = non_product[:n_other]
    composition = {
        "size": size, "product_cap": cap, "product": n_product, "non_product": n_other,
        "non_product_category": sum(1 for u in chosen_other if is_category_like(u)),
        "available": {"product": len(product), "category": len(category), "other": len(plain)},
    }
    return product[:n_product] + chosen_other, composition


def _is_home(url: str) -> bool:
    parts = urlsplit(url)
    return parts.path in ("", "/") and not parts.query


# ---------------------------------------------------------------------------
# The seven checks (spec 7 table)
# ---------------------------------------------------------------------------

def _check(cid: str, evidence: dict, ok: Optional[bool] = None, reason: Optional[str] = None) -> dict:
    return {"id": cid, "check": CHECK_NAMES[cid], "status": "not_measured" if reason else "measured",
            "ok": None if reason else ok, "reason": reason, "evidence": evidence}


def _issue(issues: list, iid: str, cid: str, severity: str, description: str, urls=()) -> None:
    urls = list(urls)
    issues.append({"id": iid, "check": cid, "severity": severity, "description": description,
                   "urls": urls[:MAX_LISTED_URLS], "url_count": len(urls)})


def check_yeti_blocked(home: dict, issues: list) -> dict:
    evidence = {ua: {"user_agent": USER_AGENTS[ua], **_brief_fetch(f)} for ua, f in home.items()}
    if all(f["error"] and f["status"] is None for f in home.values()):
        return _check("yeti_blocked", evidence, reason="첫 화면 요청이 모든 이름에서 응답 없이 실패")
    browser = home["browser"]
    problems, critical = [], False
    for ua in YETI_UAS:
        f = home[ua]
        if f["status"] != 200:
            problems.append(f"{ua}: {('HTTP ' + str(f['status'])) if f['status'] else f['error']}")
            critical = critical or browser["status"] == 200
        elif browser["status"] is not None and (f["status"], f["final_url"]) != (browser["status"], browser["final_url"]):
            problems.append(f"{ua}: HTTP {f['status']} {f['final_url']} (browser: HTTP {browser['status']} "
                            f"{browser['final_url']})")
    if problems:
        _issue(issues, "yeti_blocked.response", "yeti_blocked", "Critical" if critical else "High",
               "Yeti 이름으로 받은 첫 화면이 200이 아니거나 보통 브라우저와 다릅니다: " + "; ".join(problems),
               [home[PAGE_UA]["url"]])
    return _check("yeti_blocked", evidence, ok=not problems)


def check_robots(robots: dict, locs: Optional[list], sample: list, issues: list) -> dict:
    evidence = {"url": robots["url"], "status": robots["status"], "final_url": robots["final_url"],
                "error": robots["error"]}
    if robots["error"] and robots["status"] is None:
        return _check("robots_txt", evidence, reason=f"robots.txt 요청 실패: {robots['error']}")
    if robots["status"] == 200:
        groups, sitemaps = parse_robots(robots["text"])
    elif robots["status"] is not None and 400 <= robots["status"] < 500:
        groups, sitemaps = [], []
    else:
        return _check("robots_txt", evidence, reason=f"robots.txt 응답 HTTP {robots['status']}")
    used, chosen = select_groups(groups)
    rules = [rule for g in chosen for rule in g["rules"]]
    evidence.update({
        "groups_used": used, "groups": chosen, "rules": rules,
        "crawl_delay": [value for g in chosen for value in g["crawl_delay"]],
        "sitemaps": sitemaps, "home_allowed": is_allowed(rules, "/"),
    })
    if locs is None:
        return _check("robots_txt", evidence, reason="사이트맵을 못 읽어 상품 · 분류 주소를 시험하지 못함")
    tested = [u for u in dict.fromkeys(locs) if is_product_like(u) or is_category_like(u)]
    blocked = [u for u in tested if not is_allowed(rules, robots_target(u))]
    evidence.update({
        "product_category_tested": len(tested), "product_category_blocked": len(blocked),
        "blocked_urls": blocked[:MAX_LISTED_URLS],
        "sample_tested": len(sample),
        "sample_blocked": [u for u in sample if not is_allowed(rules, robots_target(u))][:MAX_LISTED_URLS],
    })
    if blocked:
        _issue(issues, "robots_txt.blocked", "robots_txt", "Critical" if len(blocked) == len(tested) else "High",
               f"robots.txt({used} 묶음)가 사이트맵의 상품 · 분류 주소 {len(tested)}개 중 {len(blocked)}개를 막습니다.",
               blocked)
    return _check("robots_txt", evidence, ok=not blocked)


def check_hosts(fetched: list, site_host: str, issues: list) -> dict:
    rows = []
    for host, f in fetched:
        canonical = page_facts(f["text"], f["headers"])["canonical"] if f["status"] == 200 else None
        rows.append({"host": host, "url": f["url"], "status": f["status"], "location": f["location"],
                     "canonical": canonical, "canonical_host": _host(canonical) if canonical else None,
                     "bytes": f["bytes"], "error": f["error"]})
    evidence = {"hosts": rows, "hosts_200": [r["host"] for r in rows if r["status"] == 200]}
    if all(r["status"] is None for r in rows):
        return _check("hosts", evidence, reason="세 호스트 모두 응답 없이 실패")
    ok_rows = [r for r in rows if r["status"] == 200]
    if len(ok_rows) >= 2:
        one_target = {r["canonical_host"] for r in ok_rows} == {site_host}
        detail = ", ".join(f"{r['host']} canonical={r['canonical'] or '없음'}" for r in ok_rows)
        _issue(issues, "hosts.multiple_200", "hosts", "Medium" if one_target else "High",
               f"호스트 {len(ok_rows)}개가 리다이렉트 없이 각자 200으로 응답합니다 ({detail}).",
               [r["url"] for r in ok_rows])
    return _check("hosts", evidence, ok=len(ok_rows) <= 1)


def check_sitemap_host(sitemap: dict, site_host: str, issues: list) -> dict:
    evidence = {"sources": sitemap["sources"], "errors": sitemap["errors"]}
    if not sitemap["read"]:
        return _check("sitemap_host", evidence, reason="읽은 사이트맵이 없음: " + "; ".join(sitemap["errors"]))
    locs = sitemap["locs"]
    off = [u for u in locs if _host(u) != site_host]
    evidence.update({
        "total_locs": len(locs), "site_host": site_host,
        "by_host": dict(Counter(_host(u) or "(호스트 없음)" for u in locs).most_common()),
        "by_scheme": dict(Counter(urlsplit(u).scheme or "(없음)" for u in locs).most_common()),
        "off_host": len(off), "off_host_urls": off[:MAX_LISTED_URLS],
    })
    if off:
        _issue(issues, "sitemap_host.off_host", "sitemap_host", "High",
               f"사이트맵 주소 {len(locs)}개 중 {len(off)}개가 등록 사이트 호스트({site_host})와 다른 호스트에 있습니다 "
               "(가이드: 등록 사이트와 호스트가 다르면 수집되지 않는다).", off)
    return _check("sitemap_host", evidence, ok=not off)


def check_js_redirect(sample_rows: Optional[list], composition: Optional[dict], issues: list) -> dict:
    if sample_rows is None:
        return _check("js_redirect", {}, reason="사이트맵을 못 읽어 표본이 없음")
    pages = [r for r in sample_rows if r["status"] == 200]
    evidence = {"sampled": len(sample_rows), "sample_product": composition["product"],
                "sample_non_product": composition["non_product"], "fetched_200": len(pages),
                "non_200": [{"url": r["url"], "status": r["status"], "location": r["location"], "error": r["error"]}
                            for r in sample_rows if r["status"] != 200]}
    if not pages:
        return _check("js_redirect", evidence, reason="표본 중 200으로 받은 화면이 없음")
    hits = [r for r in pages if r["js_redirects"]]
    evidence.update({"js_redirect": len(hits),
                     "pages": [{"url": r["url"], "bytes": r["bytes"], "title": r["title"],
                                "targets": [j["target"] for j in r["js_redirects"]]} for r in hits]})
    if hits:
        _issue(issues, "js_redirect.pages", "js_redirect", "Medium",
               f"표본 {len(pages)}개 중 {len(hits)}개가 200으로 응답하면서 자바스크립트(location.replace · "
               "location.href)로 다른 화면으로 보냅니다.", [r["url"] for r in hits])
    return _check("js_redirect", evidence, ok=not hits)


def check_same_title(home_facts: Optional[dict], sample_rows: Optional[list], issues: list) -> dict:
    if home_facts is None:
        return _check("same_title", {}, reason="Yeti 이름으로 첫 화면을 200으로 받지 못함")
    home_title = home_facts["title"]
    if not home_title:
        return _check("same_title", {"home_title": home_title}, reason="첫 화면에 <title>이 없음")
    if sample_rows is None:
        return _check("same_title", {"home_title": home_title}, reason="사이트맵을 못 읽어 표본이 없음")
    pages = [r for r in sample_rows if r["status"] == 200 and not _is_home(r["url"])]
    titled = [r for r in pages if r["title"]]
    same = [r["url"] for r in titled if r["title"] == home_title]
    evidence = {"home_title": home_title, "compared": len(titled),
                "untitled": [r["url"] for r in pages if not r["title"]], "same_title": len(same), "urls": same}
    if not titled:
        return _check("same_title", evidence, reason="표본 중 제목이 있는 200 화면이 없음")
    if same:
        _issue(issues, "same_title.home", "same_title", "Medium",
               f"표본 {len(titled)}개 중 {len(same)}개 화면이 첫 화면과 같은 제목을 씁니다: \"{home_title}\"", same)
    return _check("same_title", evidence, ok=not same)


def check_homepage(home: dict, home_facts: Optional[dict], issues: list) -> dict:
    f = home[PAGE_UA]
    if home_facts is None:
        reason = f"Yeti 이름으로 받은 첫 화면이 200이 아님 ({('HTTP ' + str(f['status'])) if f['status'] else f['error']})"
        return _check("homepage", {"url": f["url"], "final_url": f["final_url"]}, reason=reason)
    robots = _robots_tokens(home_facts["meta_robots"])
    yeti = _robots_tokens(home_facts["meta_yeti"])
    header = _x_robots_for_yeti(home_facts["x_robots_tag"])
    noindex = bool({"noindex", "none"} & (robots | header))
    yeti_block = {"noindex", "nofollow", "none"} & yeti
    evidence = {"url": f["url"], "final_url": f["final_url"],
                **{k: home_facts[k] for k in ("title", "canonical", "meta_robots", "meta_yeti", "x_robots_tag",
                                               "frameset", "links", "javascript_hrefs", "js_only_links",
                                               "js_only_link_examples")},
                "noindex": noindex, "rss": home_facts["rss"], "rss_present": bool(home_facts["rss"])}
    url = [f["final_url"]]
    if noindex:
        _issue(issues, "homepage.noindex", "homepage", "Critical",
               f"첫 화면에 noindex가 있습니다 (meta robots: {home_facts['meta_robots']}, "
               f"X-Robots-Tag: {home_facts['x_robots_tag']}).", url)
    if yeti_block:
        _issue(issues, "homepage.yeti_meta", "homepage", "Critical" if yeti_block - {"nofollow"} else "High",
               f"첫 화면에 Yeti 전용 meta가 수집을 막습니다: <meta name=\"Yeti\" content=\"{home_facts['meta_yeti']}\">", url)
    if home_facts["frameset"]:
        _issue(issues, "homepage.frameset", "homepage", "High", "첫 화면이 <frameset>으로 되어 있습니다.", url)
    if home_facts["js_only_links"]:
        _issue(issues, "homepage.js_only_links", "homepage", "Medium",
               f"첫 화면에 자바스크립트로만 된 링크가 {home_facts['js_only_links']}개 있습니다 "
               f"(보통 <a href> 링크 {home_facts['links']}개).", url)
    ok = not (noindex or yeti_block or home_facts["frameset"] or home_facts["js_only_links"])
    return _check("homepage", evidence, ok=ok)


# ---------------------------------------------------------------------------
# Run, summarize, report
# ---------------------------------------------------------------------------

def run_check(site: str, sitemap: Optional[str] = None, sample_size: int = DEFAULT_SAMPLE_SIZE,
              sample_size_from: str = "cli") -> dict:
    site_host = _host(site)
    domain = registrable_domain(site_host)
    scheme = urlsplit(site).scheme
    checked_at = _now_kst()
    issues, errors = [], []

    home = {ua: fetch(site, ua, follow=True) for ua in USER_AGENTS}
    home_page = home[PAGE_UA]
    home_facts = page_facts(home_page["text"], home_page["headers"]) if home_page["status"] == 200 else None

    robots = fetch(urljoin(site, "/robots.txt"), FILE_UA, follow=True)
    if robots["error"]:
        errors.append(f"robots.txt: {robots['error']}")

    host_names = [f"www.{domain}", domain, f"m.{domain}"]
    hosts = [(h, fetch(f"{scheme}://{h}/", PAGE_UA, follow=False)) for h in host_names]

    robots_sitemaps = parse_robots(robots["text"])[1] if robots["status"] == 200 else []
    sources = [sitemap] if sitemap else (list(dict.fromkeys(robots_sitemaps)) or [urljoin(site, "/sitemap.xml")])
    sitemap_data = read_sitemaps(sources)
    errors += sitemap_data["errors"]
    locs = sitemap_data["locs"] if sitemap_data["read"] else None

    sample, composition = choose_sample(locs, sample_size) if locs is not None else ([], None)
    sample_rows = None
    if locs is not None:
        sample_rows = []
        for url in sample:
            f = fetch(url, PAGE_UA, follow=False)
            facts = page_facts(f["text"], f["headers"]) if f["status"] == 200 else None
            sample_rows.append({"url": url, "status": f["status"], "location": f["location"], "bytes": f["bytes"],
                                "error": f["error"], "title": facts["title"] if facts else None,
                                "js_redirects": facts["js_redirects"] if facts else []})

    checks = [
        check_yeti_blocked(home, issues),
        check_robots(robots, locs, sample, issues),
        check_hosts(hosts, site_host, issues),
        check_sitemap_host(sitemap_data, site_host, issues),
        check_js_redirect(sample_rows, composition, issues),
        check_same_title(home_facts, sample_rows, issues),
        check_homepage(home, home_facts, issues),
    ]
    issues.sort(key=lambda i: SEVERITY_ORDER.index(i["severity"]))
    not_measured = [c["id"] for c in checks if c["status"] == "not_measured"]
    data = {
        "site": site, "host": site_host, "registrable_domain": domain, "checked_at": checked_at,
        "user_agents": USER_AGENTS, "request_delay_seconds": REQUEST_DELAY,
        "sitemap": {"sources": sitemap_data["sources"],
                    "total_locs": len(locs) if locs is not None else None},
        "sample": {"requested_size": sample_size, "size_from": sample_size_from, "composition": composition,
                   "urls": sample_rows},
        "checks": checks,
        "issues": issues,
        "summary": {
            "measured": len(checks) - len(not_measured),
            "not_measured": not_measured,
            "problems": [c["id"] for c in checks if c["ok"] is False],
            "issues_by_severity": dict(Counter(i["severity"] for i in issues)),
        },
    }
    if len(not_measured) == len(checks):
        status = "error"
    elif not_measured or errors:
        status = "partial"
    else:
        status = "success"
    return {"status": status, "data": data, "error": "; ".join(errors) or None, "metadata": _metadata()}


def _brief(check: dict) -> str:
    e = check["evidence"]
    cid = check["id"]
    if check["status"] == "not_measured":
        return check["reason"]
    if cid == "yeti_blocked":
        return " · ".join(f"{ua} HTTP {v['status']}" for ua, v in e.items())
    if cid == "robots_txt":
        delay = ", ".join(e["crawl_delay"]) or "없음"
        return (f"HTTP {e['status']} · 쓴 묶음 `{e['groups_used']}` {len(e['groups'])}개 · 상품 · 분류 막힘 "
                f"{e['product_category_blocked']}/{e['product_category_tested']} · Crawl-delay {delay}")
    if cid == "hosts":
        return " · ".join(f"{r['host']} {r['status'] or '응답 없음'}"
                          + (f" → {r['location']}" if r["location"] else "")
                          + (f" (canonical {r['canonical']})" if r["canonical"] else "") for r in e["hosts"])
    if cid == "sitemap_host":
        return f"loc {e['total_locs']}개 · 다른 호스트 {e['off_host']}개 · " + ", ".join(
            f"{h} {n}" for h, n in e["by_host"].items())
    if cid == "js_redirect":
        return (f"표본 {e['sampled']} (상품 {e['sample_product']} · 상품 아님 {e['sample_non_product']}) · "
                f"200 {e['fetched_200']} · 자바스크립트로 보냄 {e['js_redirect']}")
    if cid == "same_title":
        return f"비교 {e['compared']} · 같은 제목 {e['same_title']} · 제목 없음 {len(e['untitled'])}"
    return (f"noindex {'있음' if e['noindex'] else '없음'} · Yeti meta {e['meta_yeti'] or '없음'} · "
            f"frameset {'있음' if e['frameset'] else '없음'} · 자바스크립트로만 된 링크 {e['js_only_links']} · "
            f"RSS {'있음' if e['rss_present'] else '없음'}")


def _cell(value) -> str:
    return str(value).replace("|", "/").replace("\n", " ")


def render_markdown(result: dict) -> str:
    data = result["data"] or {}
    lines = [
        f"# 네이버 수집 점검 (Yeti): {data.get('host', '')}",
        "",
        f"- 사이트: {data.get('site', '')}",
        f"- 시각: {data.get('checked_at', '')}",
        f"- 상태: `{result['status']}`" + (f" · {result['error']}" if result.get("error") else ""),
        "",
        "## 점검",
        "",
        "| 점검 | 결과 | 근거 |",
        "|---|---|---|",
    ]
    for check in data.get("checks") or []:
        label = "못 잼" if check["status"] == "not_measured" else ("문제 없음" if check["ok"] else "문제")
        lines.append(f"| {check['check']} | {label} | {_cell(_brief(check))} |")
    lines += ["", "## 문제", "", "| 심각도 | 점검 | 내용 | 주소 수 |", "|---|---|---|---|"]
    for issue in data.get("issues") or []:
        lines.append(f"| {issue['severity']} | {CHECK_NAMES[issue['check']]} | {_cell(issue['description'])} | "
                     f"{issue['url_count']} |")
    for issue in data.get("issues") or []:
        if issue["url_count"] > 1:
            lines += ["", f"### {CHECK_NAMES[issue['check']]}: 주소 ({issue['url_count']}개, 앞 20개)", ""]
            lines += [f"- {u}" for u in issue["urls"][:20]]
    lines += [
        "",
        "## 읽는 법 · 한계",
        "",
        "- 사이트에 Yeti 이름으로 직접 요청한 결과입니다. 네이버가 실제로 수집한 기록이 아닙니다.",
        "- 자바스크립트로 보내는 화면은 화면을 읽자마자 도는 `location.replace` · `location.href=`만 셉니다. "
        "함수 안(버튼 처리 등)에 있는 것은 세지 않습니다.",
        "- 표본은 사이트맵에서 고릅니다. 상품 화면이 80%까지, 나머지는 상품 아닌 화면(분류 아닌 것 먼저)입니다.",
        "- `Crawl-delay`는 값만 적습니다. 네이버가 따르는지 공식 문서가 없습니다.",
        "- 못 잰 항목은 0이 아니라 「못 잼」으로 적습니다.",
        "",
    ]
    return "\n".join(lines)


def write_outputs(result: dict, output_dir: str) -> list:
    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, "naver-crawl-check.json")
    md_path = os.path.join(output_dir, "NAVER-CRAWL-CHECK.md")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(render_markdown(result))
    return [json_path, md_path]


def main() -> int:
    parser = argparse.ArgumentParser(description="Check a site the way Naver's crawler Yeti sees it (read-only GET)")
    parser.add_argument("--site", required=True, help="Registered site URL, e.g. https://www.example.com")
    parser.add_argument("--sitemap", help="Sitemap URL (default: Sitemap: lines in robots.txt, else <site>/sitemap.xml)")
    parser.add_argument("--sample-size", type=int,
                        help="Sitemap URLs to request as Yeti "
                             f"(default: crawl_check.sample_size in naver.json, else {DEFAULT_SAMPLE_SIZE})")
    parser.add_argument("--config", default=CONFIG_PATH, help="naver.json path (default: %(default)s)")
    parser.add_argument("--output-dir", help="Write naver-crawl-check.json and NAVER-CRAWL-CHECK.md here")
    parser.add_argument("--json", action="store_true", help="Print the result as JSON")
    args = parser.parse_args()

    def fail(message: str) -> int:
        result = {"status": "error", "data": None, "error": message, "metadata": _metadata()}
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"Error: {message}", file=sys.stderr)
        return 1

    site = normalize_site(args.site)
    if not site:
        return fail("--site must look like https://www.example.com")
    try:
        sample_size, size_from = resolve_sample_size(args.config, args.sample_size)
    except SettingsError as exc:
        return fail(str(exc))

    result = run_check(site, sitemap=args.sitemap, sample_size=sample_size, sample_size_from=size_from)
    written = write_outputs(result, args.output_dir) if args.output_dir else []

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        data = result["data"]
        print(f"Naver crawl check {data['host']}: {data['summary']['measured']}/7 checks measured, "
              f"{len(data['issues'])} issues (status {result['status']})")
        for issue in data["issues"]:
            print(f"  [{issue['severity']}] {issue['id']}: {issue['url_count']} URL(s)")
        for cid in data["summary"]["not_measured"]:
            print(f"  not measured: {cid}")
        if result["error"]:
            print(f"  error: {result['error']}")
        for path in written:
            print(f"  wrote {path}")
    return 1 if result["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
