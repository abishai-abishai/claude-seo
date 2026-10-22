#!/usr/bin/env python3
"""
Naver Search Advisor chart export reader for Claude SEO.

Search Advisor has no public API (spec 5-1). A person downloads a chart with
the "xls" item in the menu at the top right of the graph, and this script reads
those files from a folder. The ".xls" file is really an HTML table (sheet name
"Ark1"), so it is read with the standard library HTML parser.

Every export is named export_chart.xls (later ones get " (1)", " (2)"), so the
chart is told apart by its row labels, not by the file name (spec 5-3):
노출/클릭 is the impressions/clicks chart, 수집/오류 is the crawl chart.
Zeros before the first non-zero date are "no data" (the site was not verified
yet) and stay out of totals and averages (spec 5-4).

Spec (source of truth for this script): docs/NAVER.md section 5

Usage:
    python naver_search_advisor.py --json
    python naver_search_advisor.py --export-dir ~/Downloads --max-age-days 7 \
        --output-dir example.com-audit/naver-search-advisor
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Optional

SOURCE = "naver_search_advisor"
SPEC = "docs/NAVER.md#5"
KST = timezone(timedelta(hours=9))
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".config", "claude-seo", "naver.json")
DEFAULT_EXPORT_DIR = "~/Downloads"
DEFAULT_MAX_AGE_DAYS = 7
FILE_PREFIX = "export_chart"
MAX_FILE_BYTES = 5 * 1024 * 1024

# Row labels decide the chart (spec 5-3). Series keys keep the file's row order.
CHARTS = {
    "impressions_clicks": {"label": "콘텐츠 노출/클릭", "series": {"노출": "impressions", "클릭": "clicks"}},
    "crawl": {"label": "수집 현황", "series": {"수집": "crawled", "오류": "errors"}},
}

LIMITATIONS = [
    "서치어드바이저는 등록한 호스트만 셉니다. 사이트가 다른 호스트(m. · www 없는 주소)로도 응답하면 "
    "그 호스트 몫은 이 숫자에 빠져 있습니다 (수집 점검의 호스트 항목과 함께 읽습니다).",
    "처음으로 0이 아닌 날짜 앞의 0은 소유 확인 전 값이라 「값 없음」(null)으로 적고 합계 · 평균에 넣지 않습니다.",
    "「자세히 보기」(기간 선택 · 검색어별 · 페이지별) 내보내기와 csv 내보내기는 아직 읽지 않습니다.",
]

_DATE_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})$")
_NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?$")


class SettingsError(Exception):
    """naver.json or a command line value cannot be used."""


class ExportReadError(Exception):
    """An export file is not the expected HTML table."""


def _now_kst() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def _metadata() -> dict:
    return {
        "source": SOURCE,
        "spec": SPEC,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


# ---------------------------------------------------------------------------
# Settings (spec 5-2, 10)
# ---------------------------------------------------------------------------

def _config_section(config_path: str) -> dict:
    """search_advisor section of naver.json; {} when the file or the section is absent."""
    if not os.path.isfile(config_path):
        return {}
    try:
        with open(config_path, encoding="utf-8") as handle:
            config = json.load(handle)
    except (OSError, ValueError) as exc:
        raise SettingsError(f"{config_path} could not be read: {exc}") from exc
    section = config.get("search_advisor") if isinstance(config, dict) else None
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise SettingsError(f"search_advisor in {config_path} must be an object")
    return section


def resolve_settings(config_path: str = CONFIG_PATH, export_dir: Optional[str] = None,
                     max_age_days: Optional[int] = None) -> dict:
    """Command line value, else naver.json, else the spec default. naver.json is read only when needed."""
    origin = {"export_dir": "cli", "max_age_days": "cli"}
    section = _config_section(config_path) if export_dir is None or max_age_days is None else {}

    if export_dir is None:
        value = section.get("export_dir")
        if value is None:
            export_dir, origin["export_dir"] = DEFAULT_EXPORT_DIR, "default"
        elif isinstance(value, str) and value.strip():
            export_dir, origin["export_dir"] = value, "config"
        else:
            raise SettingsError("search_advisor.export_dir must be a folder path")

    if max_age_days is None:
        value = section.get("max_age_days")
        if value is None:
            max_age_days, origin["max_age_days"] = DEFAULT_MAX_AGE_DAYS, "default"
        elif isinstance(value, int) and not isinstance(value, bool):
            max_age_days, origin["max_age_days"] = value, "config"
        else:
            raise SettingsError("search_advisor.max_age_days must be a whole number of days")
    if max_age_days < 1:
        raise SettingsError("max_age_days must be at least 1")

    return {"export_dir": os.path.abspath(os.path.expanduser(export_dir)), "max_age_days": max_age_days,
            "settings_from": origin}


# ---------------------------------------------------------------------------
# Reading one export file (spec 5-3)
# ---------------------------------------------------------------------------

class _TableParser(HTMLParser):
    """Collects the text of every th/td cell, row by row."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list = []
        self._row: Optional[list] = None
        self._cell: Optional[list] = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._close_row()
            self._row = []
        elif tag in ("td", "th"):
            self._close_cell()
            if self._row is None:
                self._row = []
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            self._close_cell()
        elif tag in ("tr", "table"):
            self._close_row()

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def _close_cell(self) -> None:
        if self._cell is not None and self._row is not None:
            self._row.append("".join(self._cell).strip())
        self._cell = None

    def _close_row(self) -> None:
        self._close_cell()
        if self._row:
            self.rows.append(self._row)
        self._row = None

    def close(self) -> None:
        super().close()
        self._close_row()


def read_rows(path: str) -> list:
    """Table rows as lists of cell text. Raises ExportReadError if the file is not an HTML table."""
    if os.path.getsize(path) > MAX_FILE_BYTES:
        raise ExportReadError(f"file is larger than {MAX_FILE_BYTES} bytes")
    with open(path, "rb") as handle:
        raw = handle.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ExportReadError("not UTF-8 text (expected an HTML table)") from exc
    parser = _TableParser()
    parser.feed(text)
    parser.close()
    if len(parser.rows) < 2:
        raise ExportReadError("no table with a date row and value rows")
    return parser.rows


def identify_chart(rows: list) -> Optional[str]:
    """Chart key from the row labels (first cell of each row after the date row), or None."""
    labels = [row[0] for row in rows[1:]]
    for key, chart in CHARTS.items():
        if len(labels) == len(chart["series"]) and set(labels) == set(chart["series"]):
            return key
    return None


def parse_date(text: str, mtime_date: date) -> date:
    """MM.DD with the file's year; one year earlier when that would be after the file date (spec 5-3)."""
    match = _DATE_RE.match(text)
    if not match:
        raise ExportReadError(f"date cell {text!r} is not MM.DD")
    month, day = int(match.group(1)), int(match.group(2))
    for year in (mtime_date.year, mtime_date.year - 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate <= mtime_date:
            return candidate
    raise ExportReadError(f"date cell {text!r} is not a valid date on or before {mtime_date.isoformat()}")


def parse_value(text: str):
    """Cell number as int (or float). An empty cell is None."""
    cleaned = text.replace(",", "").strip()
    if not cleaned:
        return None
    if not _NUMBER_RE.match(cleaned):
        raise ExportReadError(f"value cell {text!r} is not a number")
    return float(cleaned) if "." in cleaned else int(cleaned)


def _ctr(impressions, clicks) -> Optional[float]:
    if impressions is None or clicks is None or impressions == 0:
        return None
    return round(clicks / impressions, 4)


def _total(values: list):
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def _average(values: list) -> Optional[float]:
    present = [v for v in values if v is not None]
    return round(sum(present) / len(present), 2) if present else None


def build_chart(key: str, rows: list, file_name: str, mtime: float) -> dict:
    """One chart: daily rows, leading zeros as None (spec 5-4), totals, averages, CTR (spec 5-5)."""
    chart = CHARTS[key]
    mtime_dt = datetime.fromtimestamp(mtime, KST)
    dates = [parse_date(cell, mtime_dt.date()) for cell in rows[0][1:]]
    if not dates:
        raise ExportReadError("the date row has no dates")

    values = {}
    for row in rows[1:]:
        cells = row[1:]
        if len(cells) != len(dates):
            raise ExportReadError(f"row {row[0]!r} has {len(cells)} values for {len(dates)} dates")
        values[chart["series"][row[0]]] = [parse_value(cell) for cell in cells]
    names = list(values)

    # A date has data when any series of this chart is non-zero there; everything before the first one is None.
    first = next((i for i in range(len(dates)) if any(values[n][i] not in (None, 0) for n in names)), None)
    for name in names:
        for i in range(len(dates) if first is None else first):
            values[name][i] = None

    days = []
    for i, day in enumerate(dates):
        entry = {"date": day.isoformat()}
        entry.update({name: values[name][i] for name in names})
        if key == "impressions_clicks":
            entry["ctr"] = _ctr(entry["impressions"], entry["clicks"])
        days.append(entry)

    totals = {name: _total(values[name]) for name in names}
    if key == "impressions_clicks":
        paired = [d for d in days if d["impressions"] is not None and d["clicks"] is not None]
        totals["ctr"] = _ctr(sum(d["impressions"] for d in paired), sum(d["clicks"] for d in paired)) \
            if paired else None

    return {
        "label": chart["label"],
        "series": {name: label for label, name in chart["series"].items()},
        "source_file": file_name,
        "source_mtime": mtime_dt.isoformat(timespec="seconds"),
        "first_data_date": dates[first].isoformat() if first is not None else None,
        "days_with_data": sum(1 for d in days if any(d[n] is not None for n in names)),
        "days": days,
        "totals": totals,
        "daily_average": {name: _average(values[name]) for name in names},
    }


# ---------------------------------------------------------------------------
# Folder scan (spec 5-2)
# ---------------------------------------------------------------------------

def scan(export_dir: str, max_age_days: int, now: Optional[float] = None) -> dict:
    """Read every recent export_chart file; the newest file wins for each chart."""
    now = time.time() if now is None else now
    cutoff = now - max_age_days * 86400
    skipped, unknown, unreadable, found = [], [], [], {}

    for name in sorted(os.listdir(export_dir)):
        path = os.path.join(export_dir, name)
        if not name.lower().startswith(FILE_PREFIX) or not os.path.isfile(path):
            continue
        mtime = os.path.getmtime(path)
        extension = os.path.splitext(name)[1].lower()
        if mtime < cutoff:
            skipped.append({"file": name, "reason": f"older than {max_age_days} days"})
            continue
        if extension == ".csv":
            skipped.append({"file": name, "reason": "csv export format not seen yet; only .xls is read (spec 5-3)"})
            continue
        if extension != ".xls":
            skipped.append({"file": name, "reason": f"unsupported extension {extension or '(none)'}"})
            continue
        try:
            rows = read_rows(path)
            key = identify_chart(rows)
            if key is None:
                unknown.append(name)
                continue
            chart = build_chart(key, rows, name, mtime)
        except (ExportReadError, OSError) as exc:
            unreadable.append(name)
            skipped.append({"file": name, "reason": f"unreadable: {exc}"})
            continue
        found.setdefault(key, []).append((mtime, name, chart))

    charts = {}
    for key in CHARTS:
        if key not in found:
            continue
        copies = sorted(found[key], key=lambda item: (item[0], item[1]), reverse=True)
        charts[key] = copies[0][2]
        for _, name, _ in copies[1:]:
            skipped.append({"file": name, "reason": f"older copy of the {key} chart; used {copies[0][1]}"})

    return {"charts": charts, "unknown_files": unknown, "unreadable_files": unreadable, "skipped": skipped}


def run(export_dir: str, max_age_days: int, settings_from: Optional[dict] = None,
        now: Optional[float] = None) -> dict:
    data = {
        "export_dir": export_dir,
        "max_age_days": max_age_days,
        "settings_from": settings_from or {},
        "read_at": _now_kst(),
        "charts": {},
        "missing_charts": list(CHARTS),
        "unknown_files": [],
        "skipped": [],
        "limitations": LIMITATIONS,
    }
    if not os.path.isdir(export_dir):
        return {"status": "error", "data": data, "error": f"export folder not found: {export_dir}",
                "metadata": _metadata()}

    scanned = scan(export_dir, max_age_days, now=now)
    data.update(charts=scanned["charts"], unknown_files=scanned["unknown_files"], skipped=scanned["skipped"],
                missing_charts=[key for key in CHARTS if key not in scanned["charts"]])

    problems = [f"{key} chart not found" for key in data["missing_charts"]]
    problems += [f"{name} unreadable" for name in scanned["unreadable_files"]]
    if not scanned["charts"]:
        status = "error"
        problems.insert(0, f"no readable {FILE_PREFIX} .xls file from the last {max_age_days} days in {export_dir}")
    elif problems:
        status = "partial"
    else:
        status = "success"
    return {"status": status, "data": data, "error": "; ".join(problems) or None, "metadata": _metadata()}


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _cell(value) -> str:
    return "값 없음" if value is None else f"{value:,}" if isinstance(value, int) else f"{value}"


def _percent(value) -> str:
    return "값 없음" if value is None else f"{value * 100:.2f}%"


def render_markdown(result: dict) -> str:
    data = result["data"] or {}
    lines = [
        "# 서치어드바이저 수치 (내려받은 파일)",
        "",
        f"- 폴더: `{data.get('export_dir', '')}` · 최근 {data.get('max_age_days', '')}일 안의 파일",
        f"- 시각: {data.get('read_at', '')}",
        f"- 상태: `{result['status']}`" + (f" · {result['error']}" if result.get("error") else ""),
    ]
    for key, chart in (data.get("charts") or {}).items():
        names = list(chart["series"])
        is_ctr = key == "impressions_clicks"
        header = [chart["series"][n] for n in names] + (["클릭률"] if is_ctr else [])
        lines += [
            "",
            f"## {chart['label']}",
            "",
            f"- 파일: `{chart['source_file']}` (수정 {chart['source_mtime']})",
            f"- 처음 값이 있는 날짜: {chart['first_data_date'] or '없음 (전부 0)'} · 값이 있는 날 {chart['days_with_data']}일",
            "",
            "| 날짜 | " + " | ".join(header) + " |",
            "|---" * (len(header) + 1) + "|",
        ]
        for day in chart["days"]:
            cells = [_cell(day[n]) for n in names] + ([_percent(day["ctr"])] if is_ctr else [])
            lines.append(f"| {day['date']} | " + " | ".join(cells) + " |")
        totals = [_cell(chart["totals"][n]) for n in names] + ([_percent(chart["totals"]["ctr"])] if is_ctr else [])
        lines.append("| 합계 | " + " | ".join(totals) + " |")
        averages = [_cell(chart["daily_average"][n]) for n in names] + ([""] if is_ctr else [])
        lines.append("| 하루 평균 | " + " | ".join(averages) + " |")

    missing = [CHARTS[key]["label"] for key in data.get("missing_charts") or []]
    if missing:
        lines += ["", "## 못 잰 그래프", ""] + [f"- {label}: 최근 파일 없음" for label in missing]
    if data.get("unknown_files"):
        lines += ["", "## 모르는 파일 (읽지 않음)", ""] + [f"- `{name}`" for name in data["unknown_files"]]
    if data.get("skipped"):
        lines += ["", "## 건너뛴 파일", ""] + [f"- `{s['file']}`: {s['reason']}" for s in data["skipped"]]
    lines += ["", "## 읽는 법 · 한계", ""] + [f"- {note}" for note in data.get("limitations") or LIMITATIONS]
    lines.append("")
    return "\n".join(lines)


def write_outputs(result: dict, output_dir: str) -> list:
    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, "naver-search-advisor.json")
    md_path = os.path.join(output_dir, "NAVER-SEARCH-ADVISOR.md")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(render_markdown(result))
    return [json_path, md_path]


def main() -> int:
    parser = argparse.ArgumentParser(description="Read Naver Search Advisor chart exports (export_chart*.xls)")
    parser.add_argument("--export-dir", help="Folder with the downloaded files "
                                             "(default: search_advisor.export_dir in naver.json, else ~/Downloads)")
    parser.add_argument("--max-age-days", type=int, help="Only read files modified in the last N days "
                                                         f"(default: naver.json, else {DEFAULT_MAX_AGE_DAYS})")
    parser.add_argument("--config", default=CONFIG_PATH, help="naver.json path (default: %(default)s)")
    parser.add_argument("--output-dir", help="Write naver-search-advisor.json and NAVER-SEARCH-ADVISOR.md here")
    parser.add_argument("--json", action="store_true", help="Print the result as JSON")
    args = parser.parse_args()

    try:
        settings = resolve_settings(args.config, export_dir=args.export_dir, max_age_days=args.max_age_days)
    except SettingsError as exc:
        result = {"status": "error", "data": None, "error": str(exc), "metadata": _metadata()}
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1

    result = run(settings["export_dir"], settings["max_age_days"], settings_from=settings["settings_from"])
    written = write_outputs(result, args.output_dir) if args.output_dir else []

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        data = result["data"]
        print(f"Search Advisor exports in {data['export_dir']}: status {result['status']}")
        for chart in data["charts"].values():
            print(f"  {chart['source_file']}: {chart['label']}, {chart['days_with_data']} days with data "
                  f"(first {chart['first_data_date']}), totals {chart['totals']}")
        if result["error"]:
            print(f"  error: {result['error']}")
        for path in written:
            print(f"  wrote {path}")
    return 1 if result["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
