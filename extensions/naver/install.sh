#!/usr/bin/env bash
# Claude SEO: Naver extension installer.
#
# Adds the Naver Search block to /seo audit (spec: docs/NAVER.md). Copies the
# seo-naver skill and agent, and writes a starter ~/.config/claude-seo/naver.json
# only when that file does not exist yet. The file holds no secret.
set -euo pipefail

main() {
    SKILL_DIR="${HOME}/.claude/skills"
    AGENTS_DIR="${HOME}/.claude/agents"
    CONFIG_JSON="${HOME}/.config/claude-seo/naver.json"

    echo "Claude SEO - Naver extension"

    command -v python3 >/dev/null 2>&1 || { echo "Python 3 required."; exit 1; }
    [ ! -d "${SKILL_DIR}/seo" ] && { echo "claude-seo base not installed."; exit 1; }

    SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" >/dev/null 2>&1 && pwd)"

    mkdir -p "${SKILL_DIR}/seo-naver"
    cp "${SOURCE_DIR}/skills/seo-naver/SKILL.md" "${SKILL_DIR}/seo-naver/SKILL.md"
    echo "Installed skill: ${SKILL_DIR}/seo-naver/SKILL.md"

    mkdir -p "${AGENTS_DIR}"
    cp "${SOURCE_DIR}/agents/seo-naver.md" "${AGENTS_DIR}/seo-naver.md"
    echo "Installed agent: ${AGENTS_DIR}/seo-naver.md"

    if [ -f "${CONFIG_JSON}" ]; then
        echo "Kept existing ${CONFIG_JSON}"
    else
        read -rp "Site URL (e.g. https://www.example.com): " NAVER_SITE
        [ -z "${NAVER_SITE}" ] && { echo "Site URL required."; exit 1; }
        NAVER_SITE="${NAVER_SITE}" python3 - "${CONFIG_JSON}" <<'PY'
import json, os, sys, tempfile
path = sys.argv[1]
site = os.environ["NAVER_SITE"]
data = {
    "site": site,
    "ai_briefing": {"devices": ["mo", "pc"], "queries": [], "brands": []},
    "keywords": {"list": [], "candidates": [], "max": 20,
                 "product_patterns": ["goods_view", "goodsNo=", "/product"]},
    "search_advisor": {"export_dir": "~/Downloads", "max_age_days": 7},
    "crawl_check": {"sample_size": 50},
}
directory = os.path.dirname(path)
os.makedirs(directory, exist_ok=True)
fd, tmp = tempfile.mkstemp(dir=directory, prefix=".naver.", suffix=".json")
try:
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
except BaseException:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise
print("Wrote " + path)
PY
    fi

    echo
    echo "Next: add AI Briefing questions and brands under ai_briefing in"
    echo "${CONFIG_JSON} (docs/NAVER.md section 10)."
    echo "Full docs: extensions/naver/docs/NAVER-SETUP.md"
}
main "$@"
