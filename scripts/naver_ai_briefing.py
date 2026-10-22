#!/usr/bin/env python3
"""
Naver AI Briefing citation checker for Claude SEO.

Reads Naver search result pages directly (no paid SERP API, no browser, no
cookies), finds the AI Briefing block, and records for each query whether the
briefing appeared, the answer text, and the cited sources in on-screen order.
Brand mentions are judged by string matching and citations by domain
matching, then rolled up into shown rate, exposure score, citation share,
mention share and attribution rate.

Spec (source of truth for this script): docs/NAVER-AI-BRIEFING.md

Config: ~/.config/claude-seo/naver.json, top-level "site" plus an "ai_briefing"
object with "devices", "queries" and "brands" (docs/NAVER.md section 10).
During the move, when naver.json is missing or has no "ai_briefing", the old
flat ~/.config/claude-seo/naver-ai-briefing.json is read instead and
metadata.warnings says so.

Usage:
    python naver_ai_briefing.py --json
    python naver_ai_briefing.py --query "query text" --brand "Brand=example.com" --device mo --json
    python naver_ai_briefing.py --config cfg.json --output-dir example.com-audit/naver
"""

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote, urlparse

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

SOURCE = "naver_ai_briefing"
KST = timezone(timedelta(hours=9))
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "claude-seo")
DEFAULT_CONFIG = os.path.join(CONFIG_DIR, "naver.json")
# Old flat file, read only while naver.json has no "ai_briefing" object.
LEGACY_CONFIG = os.path.join(CONFIG_DIR, "naver-ai-briefing.json")
CONFIG_SECTION = "ai_briefing"

SEARCH_URLS = {
    "mo": "https://m.search.naver.com/search.naver?where=m&query={q}",
    "pc": "https://search.naver.com/search.naver?where=nexearch&query={q}",
}
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
DEVICE_LABELS = {"mo": "모바일", "pc": "PC"}

AIB_MARKER = "fender_renderer-ai_briefing"
AIB_BLOCK_PREFIX = "ai-briefing/"
AIB_API_HOST = "aib-api.naver.com"
FOOTNOTE_HEADER = "정보출처"

NAVER_UGC_HOSTS = ("blog.naver.com", "cafe.naver.com", "in.naver.com", "kin.naver.com", "post.naver.com")
NAVER_SHOPPING_HOSTS = ("smartstore.naver.com", "brand.naver.com", "shopping.naver.com")
COMMUNITY_HOSTS = (
    "namu.wiki", "tistory.com", "ruliweb.com", "quasarzone.com", "clien.net", "dcinside.com",
    "ppomppu.co.kr", "theqoo.net", "fmkorea.com", "82cook.com", "bobaedream.co.kr",
    "instiz.net", "reddit.com", "youtube.com", "youtu.be", "instagram.com", "tiktok.com",
)
SOURCE_TYPE_LABELS = {
    "own": "자사",
    "competitor": "경쟁사",
    "naver_ugc": "네이버 블로그·카페",
    "naver_shopping": "네이버 쇼핑",
    "community": "커뮤니티",
    "third_party": "제3자",
}

# Polite delay between requests. The spec fixes 3 seconds; the CLI only allows longer.
MIN_REQUEST_DELAY = 3.0
REQUEST_DELAY = MIN_REQUEST_DELAY
REQUEST_TIMEOUT = 30
_last_request_time = 0.0

_BOOTSTRAP_RE = re.compile(r'\.bootstrap\(\s*document\.getElementById\(\s*"(fdr-[\w-]+)"\s*\)\s*,\s*')
_FOOTNOTE_LINE_RE = re.compile(r"^\[(\d+)\]\s*(.*?),\s*(https?://\S+)\s*$")
_DECODER = json.JSONDecoder()


class NaverFetchError(Exception):
    """A Naver page or stream could not be fetched or followed."""


def _rate_limit() -> None:
    """Wait so that consecutive Naver requests are at least REQUEST_DELAY apart."""
    global _last_request_time
    now = time.time()
    elapsed = now - _last_request_time
    if _last_request_time > 0 and elapsed < REQUEST_DELAY:
        time.sleep(REQUEST_DELAY - elapsed)
    _last_request_time = time.time()


def _now_kst() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def _metadata(warnings: Optional[list] = None) -> dict:
    return {
        "source": SOURCE,
        "spec": "docs/NAVER-AI-BRIEFING.md",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "warnings": list(warnings or []),
    }


def _get(url: str, headers: dict) -> requests.Response:
    """One cookie-less GET. requests.get without a Session keeps no cookie jar between calls."""
    _rate_limit()
    return safe_requests_get(url, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=False)


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def search_url(query: str, device: str) -> str:
    return SEARCH_URLS[device].format(q=quote(query, safe=""))


def fetch_search_page(query: str, device: str) -> tuple[str, str]:
    """Fetch the Naver search page for one query. Returns (url, html)."""
    url = search_url(query, device)
    headers = {"User-Agent": USER_AGENTS[device], "Accept-Language": "ko-KR,ko;q=0.9"}
    response = _get(url, headers)
    if response.status_code != 200:
        raise NaverFetchError(f"search page returned HTTP {response.status_code}")
    return url, decode_response_text(response)


def runtime_api_url(props: dict) -> str:
    """Return the aibAnswerRuntime apiURL only when it is https on the Naver AI briefing host."""
    api_url = props.get("apiURL") if isinstance(props, dict) else None
    if not isinstance(api_url, str) or not api_url:
        raise NaverFetchError("aibAnswerRuntime block has no apiURL")
    parsed = urlparse(api_url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != AIB_API_HOST:
        raise NaverFetchError(f"apiURL host is not {AIB_API_HOST}; not followed")
    return api_url


def fetch_runtime_stream(props: dict, referer: str, device: str) -> str:
    """Second request for aibAnswerRuntime blocks. Returns the raw event-stream text."""
    api_url = runtime_api_url(props)
    headers = {
        "User-Agent": USER_AGENTS[device],
        "Accept": "text/event-stream",
        "Accept-Language": "ko-KR,ko;q=0.9",
        "Referer": referer,
    }
    custom = props.get("customHeaders")
    if isinstance(custom, dict) and isinstance(custom.get("X-NX-Query-Info"), str):
        headers["X-NX-Query-Info"] = custom["X-NX-Query-Info"]
    response = _get(api_url, headers)
    if response.status_code != 200:
        raise NaverFetchError(f"AI briefing stream returned HTTP {response.status_code}")
    return decode_response_text(response)


# ---------------------------------------------------------------------------
# Parsing (pure functions, covered by tests/test_naver_ai_briefing.py)
# ---------------------------------------------------------------------------

def extract_briefing_payload(html: str) -> Optional[dict]:
    """Return the AI Briefing bootstrap payload from a Naver search page, or None."""
    for match in _BOOTSTRAP_RE.finditer(html or ""):
        try:
            payload, _ = _DECODER.raw_decode(html, match.end())
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        refs = payload.get("refs") if isinstance(payload.get("refs"), dict) else {}
        if AIB_MARKER in str(meta.get("ssuidExtra", "")) or str(refs.get("blockId", "")).startswith(
            AIB_BLOCK_PREFIX
        ):
            return payload
    return None


def parse_event_stream(text: str) -> dict:
    """Parse the aib-api text/event-stream into the final copy, sources and verdict."""
    result = {"copy": None, "sources": [], "footnote_sources": [], "verdict": None, "done": False}
    counts = Counter()
    for block in re.split(r"\r?\n\r?\n", text or ""):
        name = None
        data_lines = []
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                value = line[5:]
                data_lines.append(value[1:] if value.startswith(" ") else value)
        if name is None and not data_lines:
            continue
        counts[name or "message"] += 1
        data = "\n".join(data_lines)
        try:
            value = json.loads(data) if data else None
        except json.JSONDecodeError:
            value = data
        if name == "summary" and isinstance(value, dict) and isinstance(value.get("copy"), str):
            result["copy"] = value["copy"]
        elif name == "sources" and isinstance(value, list):
            result["sources"] = value
        elif name == "footnote_sources" and isinstance(value, list):
            result["footnote_sources"] = value
        elif name == "rv" and isinstance(value, dict):
            result["verdict"] = value.get("verdict")
        elif name == "done":
            result["done"] = True
    result["event_counts"] = dict(counts)
    return result


def split_copy(copy: str) -> tuple[str, list]:
    """Split the briefing copy into body text and the on-screen numbered source list."""
    lines = (copy or "").splitlines()
    header = next((i for i, line in enumerate(lines) if line.strip() == FOOTNOTE_HEADER), None)
    if header is None:
        return (copy or "").strip(), []
    body = "\n".join(lines[:header]).strip()
    notes = []
    for line in lines[header + 1:]:
        match = _FOOTNOTE_LINE_RE.match(line.strip())
        if match:
            notes.append({"rank": int(match.group(1)), "name": match.group(2).strip(), "url": match.group(3)})
    return body, notes


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().rstrip(".")


def _host_matches(host: str, base: str) -> bool:
    base = base.lower().strip().rstrip(".")
    if base.startswith("www."):
        base = base[4:]
    return bool(base) and (host == base or host.endswith("." + base))


def _domain_matches(url: str, entry: str) -> bool:
    """Match a URL against a brand domain entry ("example.com" or "blog.naver.com/id")."""
    entry = re.sub(r"^https?://", "", (entry or "").strip(), flags=re.IGNORECASE)
    if not entry:
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if "/" not in entry.strip("/"):
        return _host_matches(host, entry.strip("/"))
    entry_host, entry_path = entry.split("/", 1)
    entry_path = entry_path.strip("/").lower()
    path = parsed.path.strip("/").lower()
    return _host_matches(host, entry_host) and (path == entry_path or path.startswith(entry_path + "/"))


def classify_source(url: str, brands: list) -> tuple[str, Optional[str]]:
    """Return (source_type, brand_name) for a cited URL. First match wins, ours first."""
    for brand in sorted(brands, key=lambda b: not b["is_ours"]):
        if any(_domain_matches(url, entry) for entry in brand["domains"]):
            return ("own" if brand["is_ours"] else "competitor"), brand["name"]
    host = _host(url)
    if any(_host_matches(host, base) for base in NAVER_UGC_HOSTS):
        return "naver_ugc", None
    if any(_host_matches(host, base) for base in NAVER_SHOPPING_HOSTS):
        return "naver_shopping", None
    if any(_host_matches(host, base) for base in COMMUNITY_HOSTS):
        return "community", None
    return "third_party", None


def _term_pattern(term: str) -> re.Pattern:
    escaped = re.escape(term)
    if re.search(r"[A-Za-z0-9]", term):
        escaped = rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])"
    return re.compile(escaped, re.IGNORECASE)


def count_mentions(body: str, brands: list) -> dict:
    """Count brand-name and alias occurrences in the answer body, for every brand."""
    counts = {}
    for brand in brands:
        terms = [t for t in [brand["name"], *brand["aliases"]] if t and t.strip()]
        counts[brand["name"]] = sum(len(_term_pattern(t.strip()).findall(body or "")) for t in terms)
    return counts


def _source_index(*source_lists) -> dict:
    """Map every URL variant of a Naver source entry to its title and platform."""
    index = {}
    for items in source_lists:
        for item in items or []:
            if not isinstance(item, dict):
                continue
            info = {"title": item.get("title") or "", "platform": item.get("platform") or ""}
            for key in ("url", "pcUrl", "mobileUrl"):
                url = item.get(key)
                if isinstance(url, str) and url and url not in index:
                    index[url] = info
    return index


def _fallback_notes(sources: list) -> list:
    """Numbered list from the sources array when the copy has no source list."""
    usable = [s for s in sources or [] if isinstance(s, dict) and isinstance(s.get("url"), str)]
    usable.sort(key=lambda s: (s.get("footnoteSourceIndex") is None, s.get("footnoteSourceIndex") or 0))
    return [
        {"rank": i + 1, "name": s.get("platform") or s.get("sourceName") or "", "url": s["url"]}
        for i, s in enumerate(usable)
    ]


def judge_answer(copy: Optional[str], sources: list, extra_sources: list, brands: list) -> dict:
    """Turn the raw briefing (copy + source arrays) into the judged answer fields."""
    warnings = []
    if isinstance(copy, str) and copy.strip():
        body, notes = split_copy(copy)
    else:
        body, notes = "", []
        warnings.append("copy_missing")
    if not notes and sources:
        notes = _fallback_notes(sources)
        if "copy_missing" not in warnings:
            warnings.append("footnotes_missing_used_sources")

    meta = _source_index(sources, extra_sources)
    judged_sources = []
    for note in notes:
        info = meta.get(note["url"], {})
        source_type, brand = classify_source(note["url"], brands)
        judged_sources.append({
            "rank": note["rank"],
            "name": note["name"],
            "title": info.get("title", ""),
            "url": note["url"],
            "domain": _host(note["url"]),
            "platform": info.get("platform", ""),
            "source_type": source_type,
            "brand": brand,
        })

    mentions = count_mentions(body, brands)
    own_names = [b["name"] for b in brands if b["is_ours"]]
    own_cited_ranks = [s["rank"] for s in judged_sources if s["source_type"] == "own"]
    return {
        "answer_text": body,
        "sources": judged_sources,
        "own_cited": bool(own_cited_ranks),
        "own_cited_ranks": own_cited_ranks,
        "mentions": mentions,
        "own_mentioned": any(mentions.get(name, 0) > 0 for name in own_names),
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# One query, one device
# ---------------------------------------------------------------------------

def _empty_answer(query: str, device: str) -> dict:
    return {
        "query": query,
        "device": device,
        "search_url": search_url(query, device),
        "searched_at": None,
        "status": "error",
        "shown": False,
        "template": None,
        "aib_type": None,
        "verdict": None,
        "answer_text": "",
        "sources": [],
        "own_cited": False,
        "own_cited_ranks": [],
        "mentions": {},
        "own_mentioned": False,
        "warnings": [],
        "error": None,
        "raw_files": [],
    }


def _save_raw(raw_dir: Optional[str], name: str, text: str, answer: dict) -> None:
    if not raw_dir:
        return
    os.makedirs(raw_dir, exist_ok=True)
    path = os.path.join(raw_dir, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    answer["raw_files"].append(os.path.join("raw", name))


def check_query(index: int, query: str, device: str, brands: list, raw_dir: Optional[str] = None) -> dict:
    """Search one query on one device and judge its AI Briefing."""
    answer = _empty_answer(query, device)
    answer["mentions"] = {b["name"]: 0 for b in brands}
    prefix = f"{index:02d}-{device}"
    try:
        url, html = fetch_search_page(query, device)
        answer["searched_at"] = _now_kst()
        _save_raw(raw_dir, f"{prefix}-search.html", html, answer)

        payload = extract_briefing_payload(html)
        if payload is None:
            if AIB_MARKER in html:
                answer["error"] = "AI briefing marker found but its data could not be read (page structure may have changed)"
                return answer
            answer["status"] = "no_briefing"
            return answer

        body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
        props = body.get("props") if isinstance(body.get("props"), dict) else {}
        template = body.get("templateId")
        feedback = props.get("feedback") if isinstance(props.get("feedback"), dict) else {}
        answer["template"] = template
        answer["aib_type"] = feedback.get("aibType")

        if template == "aibShopping":
            summary = props.get("summary") if isinstance(props.get("summary"), dict) else {}
            ai_tab = props.get("aiTabInput") if isinstance(props.get("aiTabInput"), dict) else {}
            copy = summary.get("copy")
            sources = props.get("sources") if isinstance(props.get("sources"), list) else []
            extra = ai_tab.get("footnoteSources") if isinstance(ai_tab.get("footnoteSources"), list) else []
        elif template == "aibAnswerRuntime":
            stream_text = fetch_runtime_stream(props, url, device)
            _save_raw(raw_dir, f"{prefix}-aib-stream.txt", stream_text, answer)
            stream = parse_event_stream(stream_text)
            copy, sources, extra = stream["copy"], stream["sources"], stream["footnote_sources"]
            answer["verdict"] = stream["verdict"]
            if not stream["done"]:
                answer["warnings"].append("stream_incomplete")
        else:
            answer["status"] = "unknown_template"
            answer["warnings"].append(f"unknown templateId: {template}")
            return answer

        judged = judge_answer(copy, sources, extra, brands)
        answer["warnings"].extend(judged.pop("warnings"))
        answer.update(judged)
        if answer["verdict"] not in (None, "show"):
            answer["status"] = "hidden"
            return answer
        answer["shown"] = True
        answer["status"] = "ok"
        return answer
    except (NaverFetchError, URLSafetyError) as exc:
        answer["error"] = str(exc)
        return answer
    except requests.exceptions.Timeout:
        answer["error"] = f"request timed out after {REQUEST_TIMEOUT} seconds"
        return answer
    except requests.exceptions.RequestException as exc:
        answer["error"] = f"request failed ({type(exc).__name__})"
        return answer


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _ratio(numerator: int, denominator: int) -> Optional[float]:
    return round(numerator / denominator, 4) if denominator else None


def summarize(answers: list, brands: list) -> dict:
    """Roll judged answers into the five metrics of the spec (section 5)."""
    attempts = [a for a in answers if a["status"] != "error"]
    shown = [a for a in attempts if a["shown"]]
    with_sources = [a for a in shown if a["sources"]]
    own_mentioned = [a for a in shown if a["own_mentioned"]]
    own_cited = [a for a in shown if a["own_cited"]]
    cited_and_mentioned = [a for a in own_cited if a["own_mentioned"]]

    mention_totals = Counter({b["name"]: 0 for b in brands})
    domains = Counter()
    source_types = Counter()
    for answer in shown:
        mention_totals.update(answer["mentions"])
        for source in answer["sources"]:
            domains[source["domain"]] += 1
            source_types[source["source_type"]] += 1

    own = sum(mention_totals[b["name"]] for b in brands if b["is_ours"])
    competitors = sum(mention_totals[b["name"]] for b in brands if not b["is_ours"])
    return {
        "attempts": len(attempts),
        "errors": len(answers) - len(attempts),
        "shown": len(shown),
        "shown_rate": _ratio(len(shown), len(attempts)),
        "answers_with_sources": len(with_sources),
        "own_mentioned_answers": len(own_mentioned),
        "own_cited_answers": len(own_cited),
        "own_cited_and_mentioned": len(cited_and_mentioned),
        "exposure_score": _ratio(len(own_mentioned), len(shown)),
        "citation_share": _ratio(len(own_cited), len(with_sources)),
        "own_mentions": own,
        "competitor_mentions": competitors,
        "mention_share": _ratio(own, own + competitors),
        "attribution_rate": _ratio(len(cited_and_mentioned), len(own_cited)),
        "mention_totals": dict(mention_totals),
        "top_domains": [[d, n] for d, n in domains.most_common(10)],
        "source_type_counts": dict(source_types),
    }


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def normalize_brands(raw: list) -> list:
    brands = []
    for item in raw or []:
        if not isinstance(item, dict) or not str(item.get("name", "")).strip():
            continue
        brands.append({
            "name": str(item["name"]).strip(),
            "aliases": [str(a).strip() for a in item.get("aliases") or [] if str(a).strip()],
            "domains": [str(d).strip() for d in item.get("domains") or [] if str(d).strip()],
            "is_ours": bool(item.get("is_ours")),
        })
    return brands


def parse_brand_arg(value: str, is_ours: bool) -> dict:
    """Parse "Name=domain1,domain2" (domains optional)."""
    name, _, domains = value.partition("=")
    return {
        "name": name.strip(),
        "aliases": [],
        "domains": [d.strip() for d in domains.split(",") if d.strip()],
        "is_ours": is_ours,
    }


class ConfigError(Exception):
    """A config file that has to be read is unreadable or not a JSON object."""


def load_config(path: Optional[str]) -> dict:
    if not path:
        return {}
    name = os.path.basename(path)
    try:
        with open(path, encoding="utf-8") as handle:
            config = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"could not read config {name} ({type(exc).__name__})") from exc
    if not isinstance(config, dict):
        raise ConfigError(f"config {name} must be a JSON object")
    return config


def briefing_settings(config: dict) -> Optional[dict]:
    """Settings from a naver.json-shaped config, or None when it has no "ai_briefing" object.

    naver.json keeps "site" at the top level and "devices", "queries" and
    "brands" under "ai_briefing" (docs/NAVER.md section 10).
    """
    section = config.get(CONFIG_SECTION)
    if not isinstance(section, dict):
        return None
    settings = {key: section.get(key) for key in ("devices", "queries", "brands")}
    settings["site"] = config.get("site") or ""
    return settings


def resolve_config(path: Optional[str] = None, default_path: Optional[str] = None,
                   legacy_path: Optional[str] = None) -> tuple[dict, list]:
    """Return (settings, warnings). settings may hold site, devices, queries and brands.

    path (--config): that file, in the naver.json shape or the old flat shape.
    No path: the "ai_briefing" object of naver.json. While naver.json is
    missing or has no "ai_briefing", the old flat naver-ai-briefing.json is
    read instead and a warning says so. A file that exists but cannot be read
    raises ConfigError; it is never skipped silently.
    """
    if path:
        config = load_config(path)
        settings = briefing_settings(config)
        if settings is not None:
            return settings, []
        return config, [
            f"legacy_config: {os.path.basename(path)} has no \"{CONFIG_SECTION}\" object, "
            "so it was read in the old flat shape"
        ]

    default_path = default_path or DEFAULT_CONFIG
    legacy_path = legacy_path or LEGACY_CONFIG
    config = load_config(default_path) if os.path.exists(default_path) else None
    settings = briefing_settings(config) if config is not None else None
    if settings is not None:
        return settings, []
    if os.path.exists(legacy_path):
        legacy = dict(load_config(legacy_path))
        default_name = os.path.basename(default_path)
        if config is None:
            reason = f"{default_name} does not exist"
        else:
            reason = f"{default_name} has no \"{CONFIG_SECTION}\" object"
            if config.get("site"):
                legacy["site"] = config["site"]
        return legacy, [
            f"legacy_config: read the old {os.path.basename(legacy_path)} because {reason}; "
            f"move its devices, queries and brands into {default_name} under \"{CONFIG_SECTION}\""
        ]
    return ({"site": config.get("site") or ""} if config else {}), []


# ---------------------------------------------------------------------------
# Run + report
# ---------------------------------------------------------------------------

def run_check(queries: list, devices: list, brands: list, site: str = "", output_dir: Optional[str] = None,
              warnings: Optional[list] = None) -> dict:
    """Check every query on every device and return the standard response envelope.

    warnings (for example the legacy config notice) go into metadata.warnings.
    """
    raw_dir = os.path.join(output_dir, "raw") if output_dir else None
    answers = []
    for i, query in enumerate(queries, start=1):
        for device in devices:
            answers.append(check_query(i, query, device, brands, raw_dir=raw_dir))

    data = {
        "site": site,
        "run_at": _now_kst(),
        "queries": queries,
        "devices": devices,
        "brands": brands,
        "answers": answers,
        "summary": summarize(answers, brands),
        "summary_by_device": {d: summarize([a for a in answers if a["device"] == d], brands) for d in devices},
    }
    errors = sum(1 for a in answers if a["status"] == "error")
    if answers and errors == len(answers):
        status, error = "error", "every query failed; see data.answers[].error"
    else:
        status, error = ("partial" if errors else "success"), None
    return {"status": status, "data": data, "error": error, "metadata": _metadata(warnings)}


def _pct(numerator: int, denominator: int) -> str:
    if not denominator:
        return "해당 없음 (0/0)"
    return f"{numerator / denominator * 100:.1f}% ({numerator}/{denominator})"


def _metric_rows(summary: dict) -> list:
    return [
        _pct(summary["shown"], summary["attempts"]),
        _pct(summary["own_mentioned_answers"], summary["shown"]),
        _pct(summary["own_cited_answers"], summary["answers_with_sources"]),
        _pct(summary["own_mentions"], summary["own_mentions"] + summary["competitor_mentions"]),
        _pct(summary["own_cited_and_mentioned"], summary["own_cited_answers"]),
        str(summary["errors"]),
    ]


def _shown_label(answer: dict) -> str:
    if answer["status"] == "ok":
        return f"뜸 ({answer['template']})"
    return {
        "no_briefing": "안 뜸",
        "hidden": f"숨김 ({answer['verdict']})",
        "unknown_template": f"모르는 틀 ({answer['template']})",
    }.get(answer["status"], "오류")


def render_markdown(result: dict) -> str:
    """Korean Markdown report for marketers."""
    data = result["data"]
    brands = data["brands"]
    own = [b["name"] for b in brands if b["is_ours"]]
    competitors = [b["name"] for b in brands if not b["is_ours"]]
    devices = data["devices"]
    lines = [
        f"# 네이버 AI 브리핑 점검 · {data['site'] or '(사이트 미지정)'}",
        "",
        f"- 실행 시각: {data['run_at']}",
        f"- 질문 {len(data['queries'])}개 × 기기 {len(devices)}종 ({', '.join(DEVICE_LABELS[d] for d in devices)})",
        f"- 자사: {', '.join(own) or '(없음)'} · 경쟁사: {', '.join(competitors) or '(없음)'}",
        f"- 결과 상태: `{result['status']}`",
        "",
        "## 요약",
        "",
        "| 지표 | 계산식 | " + " | ".join(DEVICE_LABELS[d] for d in devices) + " | 합계 |",
        "|---|---|" + "---|" * len(devices) + "---|",
    ]
    names = [
        ("노출률", "AI 브리핑이 뜬 수 ÷ 검색한 수"),
        ("노출도 점수", "자사가 본문에 언급된 답변 ÷ 답변"),
        ("인용 점유율", "자사 링크가 출처에 있는 답변 ÷ 출처 있는 답변"),
        ("언급 점유율", "자사 언급 ÷ (자사 + 경쟁사 언급)"),
        ("인용 → 브랜드 귀속률", "자사 인용 답변 중 본문에도 자사가 있는 비율"),
        ("오류", "읽지 못한 건수 (분모에서 뺌)"),
    ]
    per_device = [_metric_rows(data["summary_by_device"][d]) for d in devices]
    total = _metric_rows(data["summary"])
    for row, (name, formula) in enumerate(names):
        cells = [values[row] for values in per_device] + [total[row]]
        lines.append(f"| {name} | {formula} | " + " | ".join(cells) + " |")

    lines += [
        "",
        "## 질문별",
        "",
        "| # | 질문 | 기기 | AI 브리핑 | 자사 인용 | 자사 언급 | 경쟁사 언급 | 출처 수 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    order = {q: i for i, q in enumerate(data["queries"], start=1)}
    for answer in data["answers"]:
        own_cited = ", ".join(f"[{r}]" for r in answer["own_cited_ranks"]) or "없음"
        own_mentions = sum(answer["mentions"].get(n, 0) for n in own)
        comp = ", ".join(f"{n} {answer['mentions'][n]}" for n in competitors if answer["mentions"].get(n)) or "없음"
        lines.append(
            f"| {order[answer['query']]} | {answer['query']} | {DEVICE_LABELS[answer['device']]} | "
            f"{_shown_label(answer)} | {own_cited} | {own_mentions or '없음'} | {comp} | {len(answer['sources'])} |"
        )

    summary = data["summary"]
    lines += ["", "## 많이 인용된 도메인 (뜬 답변 전체)", "", "| 도메인 | 종류 | 횟수 |", "|---|---|---|"]
    type_by_domain = {}
    for answer in data["answers"]:
        for source in answer["sources"]:
            type_by_domain.setdefault(source["domain"], source["source_type"])
    for domain, count in summary["top_domains"]:
        label = SOURCE_TYPE_LABELS.get(type_by_domain.get(domain, ""), "")
        lines.append(f"| {domain} | {label} | {count} |")
    if not summary["top_domains"]:
        lines.append("| (없음) | | |")

    lines += ["", "## 출처 종류", "", "| 종류 | 횟수 |", "|---|---|"]
    for key, label in SOURCE_TYPE_LABELS.items():
        lines.append(f"| {label} | {summary['source_type_counts'].get(key, 0)} |")

    lines += ["", "## 답변과 출처 (질문별)", ""]
    for answer in data["answers"]:
        lines.append(f"### {order[answer['query']]}. {answer['query']} · {DEVICE_LABELS[answer['device']]}")
        lines.append("")
        lines.append(f"- 상태: {_shown_label(answer)}")
        if answer["error"]:
            lines.append(f"- 오류: {answer['error']}")
        if answer["warnings"]:
            lines.append(f"- 경고: {', '.join(answer['warnings'])}")
        for source in answer["sources"]:
            label = SOURCE_TYPE_LABELS.get(source["source_type"], source["source_type"])
            brand = f" · {source['brand']}" if source["brand"] else ""
            title = f" · {source['title']}" if source["title"] else ""
            lines.append(f"- [{source['rank']}] {source['domain']} · {label}{brand}{title}")
        if answer["answer_text"]:
            lines += ["", "<details><summary>답변 본문</summary>", "", answer["answer_text"], "", "</details>"]
        lines.append("")

    lines += [
        "## 읽는 법 · 한계",
        "",
        "- 한 번 잰 값입니다. 같은 질문에도 AI 브리핑은 떴다 안 떴다 하고, 시각 · IP에 따라 답이 달라집니다. 하루 한 번씩 쌓아 추세로 읽습니다.",
        "- 네이버 검색 화면을 직접 읽습니다(공식 API 아님). 화면 구조가 바뀌면 오류로 남고 원문은 `raw/`에 저장됩니다.",
        "- 언급은 브랜드 이름을 글자로 찾은 것이고, 인용은 출처 링크의 도메인으로 판정한 것입니다.",
        "",
    ]
    return "\n".join(lines)


def write_outputs(result: dict, output_dir: str) -> list:
    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, "naver-ai-briefing.json")
    md_path = os.path.join(output_dir, "NAVER-AI-BRIEFING.md")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(render_markdown(result))
    return [json_path, md_path]


def main() -> int:
    global REQUEST_DELAY
    parser = argparse.ArgumentParser(description="Naver AI Briefing citation checker (direct read, no cookies)")
    parser.add_argument("--config", help=(
        f"JSON config in the naver.json shape or the old flat shape (default: the \"{CONFIG_SECTION}\" "
        f"object of {DEFAULT_CONFIG}, falling back to {LEGACY_CONFIG})"
    ))
    parser.add_argument("--query", action="append", help="Query to check (repeatable, overrides config)")
    parser.add_argument("--device", choices=["mo", "pc", "both"], help="Device (overrides config, default both)")
    parser.add_argument("--brand", action="append", help='Own brand as "Name=domain1,domain2" (repeatable)')
    parser.add_argument("--competitor", action="append", help='Competitor as "Name=domain1,domain2" (repeatable)')
    parser.add_argument("--site", help="Site URL shown in the report")
    parser.add_argument("--output-dir", help="Write naver-ai-briefing.json, NAVER-AI-BRIEFING.md and raw/ here")
    parser.add_argument("--delay", type=float, default=MIN_REQUEST_DELAY,
                        help=f"Seconds between requests (minimum {MIN_REQUEST_DELAY:g})")
    parser.add_argument("--json", action="store_true", help="Print the result as JSON")
    args = parser.parse_args()
    config_warnings = []

    def fail(message: str) -> int:
        result = {"status": "error", "data": None, "error": message, "metadata": _metadata(config_warnings)}
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"Error: {message}", file=sys.stderr)
            for warning in config_warnings:
                print(f"Warning: {warning}", file=sys.stderr)
        return 1

    if args.delay < MIN_REQUEST_DELAY:
        return fail(f"--delay must be at least {MIN_REQUEST_DELAY:g} seconds")
    REQUEST_DELAY = args.delay

    try:
        config, warnings = resolve_config(args.config)
    except ConfigError as exc:
        return fail(str(exc))
    config_warnings.extend(warnings)

    queries = [q.strip() for q in (args.query or config.get("queries") or []) if str(q).strip()]
    if args.device:
        devices = ["mo", "pc"] if args.device == "both" else [args.device]
    else:
        devices = [d for d in config.get("devices") or ["mo", "pc"] if d in SEARCH_URLS]
    brands = normalize_brands(config.get("brands"))
    brands += [parse_brand_arg(v, True) for v in args.brand or []]
    brands += [parse_brand_arg(v, False) for v in args.competitor or []]
    brands = [b for b in brands if b["name"]]

    if not queries:
        return fail(f"no queries: pass --query or put \"queries\" under \"{CONFIG_SECTION}\" in naver.json")
    if not devices:
        return fail("no valid devices (use mo and/or pc)")
    if not any(b["is_ours"] for b in brands):
        return fail(f"no own brand: pass --brand or mark one brand under \"{CONFIG_SECTION}\" "
                    "with \"is_ours\": true")

    result = run_check(queries, devices, brands, site=args.site or config.get("site") or "",
                       output_dir=args.output_dir, warnings=config_warnings)
    written = write_outputs(result, args.output_dir) if args.output_dir else []

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        summary = result["data"]["summary"]
        print(f"Naver AI Briefing: {summary['shown']}/{summary['attempts']} shown, {summary['errors']} errors")
        print(f"  own cited answers:    {summary['own_cited_answers']}/{summary['answers_with_sources']}")
        print(f"  own mentioned answers: {summary['own_mentioned_answers']}/{summary['shown']}")
        for warning in config_warnings:
            print(f"  warning: {warning}")
        for path in written:
            print(f"  wrote {path}")
    return 1 if result["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
