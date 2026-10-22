# 네이버 확장 설치

`/seo audit`에 네이버 묶음(Naver Search)을 붙인다. 무엇을 어떻게 재는지는 정본 [docs/NAVER.md](../../../docs/NAVER.md)를 따른다.

## 설치

```bash
./extensions/naver/install.sh
.\extensions\naver\install.ps1
```

하는 일:

1. `seo-naver` 스킬과 하위 작업 지시문을 `~/.claude/skills/` · `~/.claude/agents/`에 복사한다.
2. `~/.config/claude-seo/naver.json`이 없을 때만 사이트 주소를 물어 빈 틀을 만든다. 있으면 건드리지 않는다.

## 설치 뒤 채울 것

| 칸 | 무엇 | 비어 있으면 |
|---|---|---|
| `ai_briefing.queries` · `ai_briefing.brands` | AI 브리핑에 넣을 질문 · 우리 브랜드와 경쟁사 | AI 브리핑은 「못 쟀다」로 남는다 |
| `keywords.list` | 순위를 잴 검색어 | 첫 감사가 후보를 만들어 쓰고 「사람이 확인하지 않은 목록」으로 적는다 |
| GA4 설정 | 원래 스킬의 `google-api.json` (`ga4_property_id` · 서비스 계정) | 네이버 검색 유입은 「못 쟀다」로 남는다 |
| 서치어드바이저 파일 | 서치어드바이저 그래프의 ⋮ > xls 로 내려받은 파일 (`export_chart*.xls`, 7일 안) | 서치어드바이저 수치는 「못 쟀다」로 남는다 |

이 파일에는 비밀 값이 없다. 다만 질문 · 브랜드 · 검색어는 공개 저장소에 넣지 않는다.

## 제거

```bash
./extensions/naver/uninstall.sh
```

스킬과 지시문만 지운다. `naver.json`은 남긴다(직접 넣은 질문 · 브랜드 · 검색어가 들어 있다).
