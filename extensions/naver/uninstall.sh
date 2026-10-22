#!/usr/bin/env bash
set -euo pipefail
SKILL_DIR="${HOME}/.claude/skills/seo-naver"
AGENT_FILE="${HOME}/.claude/agents/seo-naver.md"
CONFIG_JSON="${HOME}/.config/claude-seo/naver.json"
[ -d "${SKILL_DIR}" ] && rm -rf "${SKILL_DIR}" && echo "Removed ${SKILL_DIR}"
[ -f "${AGENT_FILE}" ] && rm -f "${AGENT_FILE}" && echo "Removed ${AGENT_FILE}"
# naver.json holds the user's own questions, brands and keyword list, so it is
# kept. Without the agent, /seo audit no longer reads it.
[ -f "${CONFIG_JSON}" ] && echo "Kept ${CONFIG_JSON} (delete it yourself if you no longer need it)"
exit 0
