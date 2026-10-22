---
name: seo-naver
description: Naver search extension for sites whose main search engine is Naver (Korea). Adds a "Naver Search" block to /seo audit - Naver search inflow from GA4 without Naver ad clicks, Search Advisor exports, keyword ranking on Naver, Yeti crawl checks, site: indexed pages, and Naver AI Briefing citations. Triggers on "Naver", "네이버", "서치어드바이저", "Search Advisor", "Yeti", "AI 브리핑".
metadata:
  version: "2.4.2"
compatibility: "Requires ~/.config/claude-seo/naver.json (see docs/NAVER.md section 10). GA4 inflow reuses the Google API config of seo-google. Run extensions/naver/install.sh to configure."
---

# seo-naver

Naver block for `/seo audit`. The spec is `${CLAUDE_PLUGIN_ROOT}/docs/NAVER.md`;
the AI Briefing and site: items follow `${CLAUDE_PLUGIN_ROOT}/docs/NAVER-AI-BRIEFING.md`
and `${CLAUDE_PLUGIN_ROOT}/docs/NAVER-SITE-INDEX.md`.

## Prerequisites

- Run `extensions/naver/install.sh` or `install.ps1`.
- `~/.config/claude-seo/naver.json` with at least `site`.
- For the GA4 item: the Google API config used by `seo-google`
  (`ga4_property_id` and a service account).
- For the Search Advisor item: chart exports downloaded from Search Advisor
  (file names start with `export_chart`), default folder `~/Downloads`.

## Routing

| Command | Underlying script |
|---|---|
| `/seo naver traffic` | `"${CLAUDE_PLUGIN_ROOT}/scripts/claude-seo" run naver_traffic.py` |
| `/seo naver advisor` | `"${CLAUDE_PLUGIN_ROOT}/scripts/claude-seo" run naver_search_advisor.py` |
| `/seo naver rank` | `"${CLAUDE_PLUGIN_ROOT}/scripts/claude-seo" run naver_keyword_rank.py rank` |
| `/seo naver candidates` | `"${CLAUDE_PLUGIN_ROOT}/scripts/claude-seo" run naver_keyword_rank.py candidates` |
| `/seo naver crawl` | `"${CLAUDE_PLUGIN_ROOT}/scripts/claude-seo" run naver_crawl_check.py` |
| `/seo naver index` | `"${CLAUDE_PLUGIN_ROOT}/scripts/claude-seo" run naver_site_index.py` |
| `/seo naver briefing` | `"${CLAUDE_PLUGIN_ROOT}/scripts/claude-seo" run naver_ai_briefing.py` |

All commands accept `--json` and `--output-dir`.

## When this skill applies

- During `/seo audit`, the orchestrator spawns the `seo-naver` agent when
  `~/.config/claude-seo/naver.json` exists. The Naver Search category then
  counts for 30% of the SEO Health Score (spec 9-1).
- The user asks about Naver search, Search Advisor, Yeti, or Naver AI Briefing.

## Cross-skill delegation

- Google Search Console, CrUX and the original GA4 organic report stay with
  `seo-google`. Its numbers are not changed; the Naver block adds one line
  next to them (spec 4-3).
- AI search readiness for Google and LLMs stays with `seo-geo`.

## What this skill does not do

- No paid scraping APIs. Naver pages are read directly, without cookies, 3
  seconds apart.
- No Search Advisor login cookies. Its numbers come only from files a person
  downloaded.
- No company names, questions, keywords or results in this repository. They
  live in `~/.config/claude-seo/` and in the audit output folder.

## Output Formatting

- Write "not measured" with the reason; never 0 for an item that was not measured.
- Label sources: GA4 Data API (live), Search Advisor export file, Naver search
  page (read time), site request as Yeti.
