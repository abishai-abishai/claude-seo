"""Naver Search Advisor chart export reader (docs/NAVER.md section 5)."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import naver_search_advisor as nsa  # noqa: E402

# Same markup as a real export (attributes without spaces, conditional comment, sheet Ark1).
HEAD = (
    '<html xmlns:o="urn:schemas-microsoft-com:office:office"xmlns:x="urn:schemas-microsoft-com:office:excel"'
    'xmlns="http://www.w3.org/TR/REC-html40"><head><!--[if gte mso 9]><xml><x:ExcelWorkbook>'
    "<x:ExcelWorksheets><x:ExcelWorksheet><x:Name>Ark1</x:Name><x:WorksheetOptions><x:DisplayGridlines/>"
    "</x:WorksheetOptions></x:ExcelWorksheet></x:ExcelWorksheets></x:ExcelWorkbook></xml><![endif]-->"
    "<meta name=ProgId content=Excel.Sheet><meta charset=UTF-8></head><body>"
)

NOW = datetime(2026, 10, 22, 9, 30, tzinfo=nsa.KST).timestamp()
DATES = ["10.17", "10.18", "10.19", "10.20", "10.21"]
CLICK_ROWS = [("노출", [0, 0, 100, 0, 50]), ("클릭", [0, 0, 5, 0, 1])]
CRAWL_ROWS = [("수집", [0, 0, 0, 12, 8]), ("오류", [0, 0, 0, 0, 1])]


def export_html(dates: list, rows: list, label_tag: str = "td") -> str:
    head = '<tr><th class="number"></th>' + "".join(f"<th>{d}</th>" for d in dates) + "</tr>"
    body = "".join(
        f'<tr><{label_tag} class="number">{label}</{label_tag}>'
        + "".join(f'<td class="number">{v}</td>' for v in values) + "</tr>"
        for label, values in rows
    )
    return HEAD + "<table>" + head + body + "</table></body></html>"


def write_export(folder: Path, name: str, rows: list, dates: list = DATES, mtime: float = NOW - 60,
                 text: str | None = None, label_tag: str = "td") -> Path:
    path = folder / name
    path.write_text(text if text is not None else export_html(dates, rows, label_tag), encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


# --- reading one file -------------------------------------------------------

@pytest.mark.parametrize("label_tag", ["td", "th"])
def test_read_rows_handles_the_real_markup(tmp_path, label_tag):
    path = write_export(tmp_path, "export_chart.xls", CLICK_ROWS, label_tag=label_tag)
    rows = nsa.read_rows(str(path))
    assert rows[0] == [""] + DATES
    assert rows[1] == ["노출", "0", "0", "100", "0", "50"]
    assert nsa.identify_chart(rows) == "impressions_clicks"


def test_chart_is_chosen_by_row_labels():
    assert nsa.identify_chart([["", "10.21"], ["수집", "1"], ["오류", "0"]]) == "crawl"
    assert nsa.identify_chart([["", "10.21"], ["클릭", "1"], ["노출", "2"]]) == "impressions_clicks"
    assert nsa.identify_chart([["", "10.21"], ["노출", "1"]]) is None
    assert nsa.identify_chart([["", "10.21"], ["방문", "1"], ["클릭", "0"]]) is None


def test_year_comes_from_mtime_and_rolls_back_over_new_year():
    mtime_date = datetime(2027, 1, 2, tzinfo=nsa.KST).date()
    assert nsa.parse_date("12.30", mtime_date).isoformat() == "2026-12-30"
    assert nsa.parse_date("01.02", mtime_date).isoformat() == "2027-01-02"
    assert nsa.parse_date("1.1", mtime_date).isoformat() == "2027-01-01"
    with pytest.raises(nsa.ExportReadError):
        nsa.parse_date("2026-10-21", mtime_date)


def test_values_accept_commas_and_reject_text():
    assert nsa.parse_value("1,234") == 1234
    assert nsa.parse_value("") is None
    with pytest.raises(nsa.ExportReadError):
        nsa.parse_value("n/a")


def test_leading_zeros_are_no_data_and_ctr_skips_zero_impressions():
    rows = [[""] + DATES] + [[label] + [str(v) for v in values] for label, values in CLICK_ROWS]
    chart = nsa.build_chart("impressions_clicks", rows, "export_chart.xls", NOW)
    days = chart["days"]
    assert [d["impressions"] for d in days] == [None, None, 100, 0, 50]
    assert [d["clicks"] for d in days] == [None, None, 5, 0, 1]
    assert [d["ctr"] for d in days] == [None, None, 0.05, None, 0.02]
    assert chart["first_data_date"] == "2026-10-19"
    assert chart["days_with_data"] == 3
    assert chart["totals"] == {"impressions": 150, "clicks": 6, "ctr": 0.04}
    assert chart["daily_average"] == {"impressions": 50.0, "clicks": 2.0}
    assert chart["source_file"] == "export_chart.xls"
    assert chart["source_mtime"] == "2026-10-22T09:30:00+09:00"


def test_first_data_date_uses_either_series():
    # Errors start before crawled pages do: the date still has data for both series.
    rows = [["", "10.20", "10.21", "10.22"], ["수집", "0", "0", "3"], ["오류", "0", "2", "0"]]
    chart = nsa.build_chart("crawl", rows, "export_chart.xls", NOW)
    assert chart["first_data_date"] == "2026-10-21"
    assert [d["crawled"] for d in chart["days"]] == [None, 0, 3]
    assert [d["errors"] for d in chart["days"]] == [None, 2, 0]
    assert chart["totals"] == {"crawled": 3, "errors": 2}


def test_all_zero_chart_has_no_totals_instead_of_zero():
    rows = [["", "10.20", "10.21"], ["노출", "0", "0"], ["클릭", "0", "0"]]
    chart = nsa.build_chart("impressions_clicks", rows, "export_chart.xls", NOW)
    assert chart["first_data_date"] is None and chart["days_with_data"] == 0
    assert chart["totals"] == {"impressions": None, "clicks": None, "ctr": None}


def test_row_length_mismatch_is_an_error():
    with pytest.raises(nsa.ExportReadError):
        nsa.build_chart("crawl", [["", "10.20", "10.21"], ["수집", "1"], ["오류", "0", "0"]], "x.xls", NOW)


# --- folder scan ------------------------------------------------------------

def test_both_charts_read_with_success(tmp_path):
    write_export(tmp_path, "export_chart.xls", CRAWL_ROWS)
    write_export(tmp_path, "export_chart (1).xls", CLICK_ROWS)
    result = nsa.run(str(tmp_path), 7, now=NOW)
    assert result["status"] == "success" and result["error"] is None
    charts = result["data"]["charts"]
    assert charts["crawl"]["source_file"] == "export_chart.xls"
    assert charts["impressions_clicks"]["source_file"] == "export_chart (1).xls"
    assert result["data"]["missing_charts"] == []
    assert any("등록한 호스트만" in note for note in result["data"]["limitations"])
    assert result["metadata"]["source"] == "naver_search_advisor"


def test_newest_copy_of_a_chart_wins(tmp_path):
    write_export(tmp_path, "export_chart.xls", CLICK_ROWS, mtime=NOW - 3600)
    write_export(tmp_path, "export_chart (1).xls", [("노출", [0, 0, 0, 7, 9]), ("클릭", [0, 0, 0, 1, 0])],
                 mtime=NOW - 60)
    result = nsa.run(str(tmp_path), 7, now=NOW)
    chart = result["data"]["charts"]["impressions_clicks"]
    assert chart["source_file"] == "export_chart (1).xls"
    assert chart["totals"]["impressions"] == 16
    assert {"file": "export_chart.xls",
            "reason": "older copy of the impressions_clicks chart; used export_chart (1).xls"} in result["data"]["skipped"]
    assert result["status"] == "partial" and "crawl chart not found" in result["error"]


def test_old_csv_other_and_unknown_files(tmp_path):
    write_export(tmp_path, "export_chart.xls", CLICK_ROWS, mtime=NOW - 8 * 86400)
    write_export(tmp_path, "export_chart (1).xls", CRAWL_ROWS)
    write_export(tmp_path, "export_chart (2).csv", [], text="date,value\n")
    write_export(tmp_path, "export_chart (3).xls", [("방문", [1, 2, 3, 4, 5])])
    write_export(tmp_path, "report.xls", CLICK_ROWS)
    result = nsa.run(str(tmp_path), 7, now=NOW)
    data = result["data"]
    assert list(data["charts"]) == ["crawl"]
    assert data["unknown_files"] == ["export_chart (3).xls"]
    reasons = {s["file"]: s["reason"] for s in data["skipped"]}
    assert reasons["export_chart.xls"] == "older than 7 days"
    assert reasons["export_chart (2).csv"].startswith("csv export format not seen yet")
    assert "report.xls" not in reasons and "report.xls" not in data["unknown_files"]
    assert result["status"] == "partial" and data["missing_charts"] == ["impressions_clicks"]


def test_unreadable_file_is_listed_and_does_not_stop_the_rest(tmp_path):
    write_export(tmp_path, "export_chart.xls", [], text="\xd0\xcf binary workbook")
    write_export(tmp_path, "export_chart (1).xls", CRAWL_ROWS)
    write_export(tmp_path, "export_chart (2).xls", CLICK_ROWS)
    result = nsa.run(str(tmp_path), 7, now=NOW)
    assert result["status"] == "partial"
    assert "export_chart.xls unreadable" in result["error"]
    assert len(result["data"]["charts"]) == 2


def test_no_recent_files_or_missing_folder_is_error(tmp_path):
    result = nsa.run(str(tmp_path), 7, now=NOW)
    assert result["status"] == "error" and "no readable export_chart" in result["error"]
    assert result["data"]["charts"] == {}
    result = nsa.run(str(tmp_path / "nope"), 7, now=NOW)
    assert result["status"] == "error" and "not found" in result["error"]


# --- settings ---------------------------------------------------------------

def test_default_settings_without_config(tmp_path):
    settings = nsa.resolve_settings(str(tmp_path / "naver.json"))
    assert settings["export_dir"] == os.path.abspath(os.path.expanduser("~/Downloads"))
    assert settings["max_age_days"] == 7
    assert settings["settings_from"] == {"export_dir": "default", "max_age_days": "default"}
    assert nsa.CONFIG_PATH.replace("\\", "/").endswith(".config/claude-seo/naver.json")


def test_config_values_expand_home_and_cli_wins(tmp_path):
    config = tmp_path / "naver.json"
    config.write_text(json.dumps({"search_advisor": {"export_dir": "~/sa-exports", "max_age_days": 3}}),
                      encoding="utf-8")
    settings = nsa.resolve_settings(str(config))
    assert settings["export_dir"] == os.path.abspath(os.path.expanduser("~/sa-exports"))
    assert settings["max_age_days"] == 3
    settings = nsa.resolve_settings(str(config), export_dir=str(tmp_path), max_age_days=10)
    assert settings["export_dir"] == str(tmp_path) and settings["max_age_days"] == 10
    assert settings["settings_from"] == {"export_dir": "cli", "max_age_days": "cli"}


def test_bad_config_is_an_error_unless_cli_gives_everything(tmp_path):
    config = tmp_path / "naver.json"
    config.write_text("{broken", encoding="utf-8")
    with pytest.raises(nsa.SettingsError):
        nsa.resolve_settings(str(config))
    assert nsa.resolve_settings(str(config), export_dir=str(tmp_path), max_age_days=7)["max_age_days"] == 7
    config.write_text(json.dumps({"search_advisor": {"max_age_days": "7"}}), encoding="utf-8")
    with pytest.raises(nsa.SettingsError):
        nsa.resolve_settings(str(config))
    with pytest.raises(nsa.SettingsError):
        nsa.resolve_settings(str(tmp_path / "none.json"), max_age_days=0)


# --- report and command line ------------------------------------------------

def test_markdown_report_without_dashes(tmp_path):
    write_export(tmp_path, "export_chart.xls", CLICK_ROWS)
    report = nsa.render_markdown(nsa.run(str(tmp_path), 7, now=NOW))
    assert "## 콘텐츠 노출/클릭" in report and "| 2026-10-17 | 값 없음 | 값 없음 | 값 없음 |" in report
    assert "| 합계 | 150 | 6 | 4.00% |" in report
    assert "## 못 잰 그래프" in report and "수집 현황" in report
    assert "\u2014" not in report and "\u2013" not in report


def test_main_prints_json_and_writes_outputs(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "downloads"
    folder.mkdir()
    write_export(folder, "export_chart.xls", CRAWL_ROWS, mtime=datetime.now(nsa.KST).timestamp() - 60)
    out_dir = tmp_path / "out"
    monkeypatch.setattr(sys, "argv", ["naver_search_advisor.py", "--export-dir", str(folder),
                                      "--config", str(tmp_path / "naver.json"),
                                      "--output-dir", str(out_dir), "--json"])
    assert nsa.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "partial" and "crawl" in result["data"]["charts"]
    assert (out_dir / "naver-search-advisor.json").is_file()
    assert (out_dir / "NAVER-SEARCH-ADVISOR.md").is_file()
