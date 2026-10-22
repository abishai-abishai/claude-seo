# 네이버 점검: `/seo audit` 안의 네이버 묶음

이 문서가 네이버 쪽 전체의 정본이다. 스크립트(`scripts/naver_*.py`) · 하위 작업 지시문(`extensions/naver/`) ·
`skills/seo-audit/SKILL.md`의 네이버 단락은 이 문서를 따른다. 코드와 이 문서가 다르면 코드를 고친다.

읽는 법이 긴 항목은 따로 정본을 둔다.

- AI 브리핑 → [NAVER-AI-BRIEFING.md](NAVER-AI-BRIEFING.md)
- 네이버 검색에 잡힌 페이지 → [NAVER-SITE-INDEX.md](NAVER-SITE-INDEX.md)

## 1. 목표

`/seo audit`을 하면 원래 스킬이 하던 점검과 이 문서가 정한 네이버 쪽 수집을 함께 하고,
**원래 스킬이 쓰는 리포트 파일에 같이 적는다.** 네이버 리포트를 따로 만들지 않는다.

## 2. 정해진 것

| 날짜 | 정한 것 |
|---|---|
| 2026-10-21 | 네이버 리포트를 따로 만들지 않는다. 원래 결과물(`FULL-AUDIT-REPORT.md` · `ACTION-PLAN.md` · `audit-data.json` · PDF · seo-cockpit)에 합친다 |
| 2026-10-21 | AI 브리핑 · `site:`는 네이버 검색 화면을 직접 읽는다. 유료 수집 대행 API는 쓰지 않는다 |
| 2026-10-21 | 네이버 자연 검색 = 네이버 검색에서 온 세션 중 도착 주소에 `NaPm=`이 없는 것 (4절) |
| 2026-10-22 | 서치어드바이저 수치는 사람이 내려받은 파일로 읽는다 (5절) |
| 2026-10-22 | 검색어 순위를 넣는다. 검색어 목록 = 사이트 분류 + GA4 네이버 자연 검색 상위 상품 (6절) |
| 2026-10-22 | 네이버 수집 점검(Yeti)을 넣는다 (7절) |
| 2026-10-22 | 원래 GA4 리포트의 Organic 숫자는 고치지 않는다. 네이버 묶음이 「이 중 네이버 광고 클릭 N」 한 줄을 덧붙인다 (4-3) |
| 2026-10-22 | 네이버 묶음은 네이버 설정 파일이 있을 때만 돈다 (3절) |
| 2026-10-22 | 네이버 묶음을 전체 SEO Health Score에 **30%**로 넣는다. 원래 일곱 묶음은 비율대로 줄인다 (9-1) |

## 3. 언제 도나 · 어떻게 붙나

`~/.config/claude-seo/naver.json`(10절)이 있으면 `seo-audit`이 `seo-naver` 하위 작업을 함께 띄운다.
없으면 원래 스킬 그대로 돈다. Matomo 확장(`extensions/matomo/`)과 같은 방식이다.

```
scripts/naver_*.py          파이썬 스크립트
extensions/naver/           seo-naver 하위 작업 지시문 · 설치 파일
skills/seo-audit/SKILL.md   「네이버 설정이 있으면 seo-naver 를 띄운다」 단락
```

항목(4~8절)은 서로 따로 돈다. 하나를 못 재면(키 없음 · 파일 없음 · 네이버 화면 바뀜)
그 항목에 「못 쟀다 + 이유」를 적고 나머지는 계속한다. **못 잰 것을 0으로 적지 않는다.**

| 항목 | 어디서 | 필요한 것 |
|---|---|---|
| 네이버 검색 유입 (4절) | GA4 | 원래 스킬의 GA4 설정 (`google-api.json`의 `ga4_property_id` · 서비스 계정) |
| 서치어드바이저 수치 (5절) | 사람이 내려받은 파일 | 내려받은 파일 |
| 검색어 순위 (6절) | 네이버 검색 화면 | 없음 (목록 후보를 만들 때만 GA4) |
| 수집 점검 (7절) | 사이트에 직접 요청 | 없음 |
| 네이버 검색에 잡힌 페이지 (8절) | 네이버 검색 화면 `site:` | 없음 |
| AI 브리핑 (8절) | 네이버 검색 화면 | 질문 목록 (설정 파일) |

## 4. 네이버 검색 유입 (GA4)

### 4-1. 세는 범위

| 칸 | 값 |
|---|---|
| 기간 | 원래 GA4 리포트(`ga4_report.py`)와 같다. 기본 28일, 어제까지 |
| 네이버 검색에서 온 세션 | `sessionSource`가 `m.search.naver.com` · `search.naver.com` · `naver` 중 하나이고, `sessionMedium`이 `referral` · `organic` 중 하나 |
| 그중 광고 클릭 | 도착 주소(`landingPagePlusQueryString`)에 `NaPm=`이 있음 |
| 네이버 자연 검색 | 네이버 검색에서 온 세션 중 광고 클릭이 아닌 것 |
| 구매 | `transactions` |

근거:

- `NaPm`은 네이버 공식 전환추적 안내에 「광고 클릭시 발생하는 NaPm파라미터」로 적혀 있다.
- GA4는 `m.search.naver.com / referral`과 `naver / organic`을 둘 다 Organic Search 채널로 묶는다.
  네이버 광고를 돌리는 사이트에서는 이 두 줄의 대부분이 광고 클릭일 수 있다(2026-10-21 실측).
  빼지 않으면 네이버 자연 검색이 크게 부풀려진다.
- `naver / cpc`(태그를 단 광고) · `naver / blog`(검색이 아님)는 넣지 않는다.

### 4-2. 적는 값

- 네이버 자연 검색 세션 · 구매
- 광고 클릭 세션 (참고로)
- 자연 검색의 처음 본 화면 상위 20 (`landingPage`, 쿼리를 뗀 경로). `(not set)`도 그대로 적는다.

### 4-3. 구글 쪽 숫자에 덧붙이는 한 줄

원래 GA4 리포트는 Organic을 `sessionDefaultChannelGroup == "Organic Search"`로 센다. **이 숫자는 고치지 않는다.**
네이버 묶음이 같은 기간 · 같은 조건에서 도착 주소에 `NaPm=`이 있는 세션 수 N을 세어 아래 한 줄을 적는다.

```
구글 Organic Search {전체} 중 네이버 광고 클릭 {N} ({N ÷ 전체}%), 도착 주소에 NaPm 이 있는 세션
```

### 4-4. 하지 않는 것

- `NaPm` 안의 `tr=` 값으로 광고 종류를 나누지 않는다. 공식 문서에 값의 뜻이 없다.
- GA4로 네이버 검색어를 세지 않는다. 직전 주소에 검색어가 남지 않는다
  (2026-10-21 확인: 직전 주소는 `https://m.search.naver.com/`까지만 있고 `query=`가 붙은 줄이 없었다).

## 5. 서치어드바이저 수치 (내려받은 파일)

### 5-1. 왜 파일인가

서치어드바이저는 공개된 API가 없다(2026-10-22 확인). 화면이 쓰는 내부 주소를 로그인 쿠키로 부르는 방법은 쓰지 않는다.
그 쿠키가 있으면 네이버 계정 전체에 로그인한 상태로 요청할 수 있어서, PC에 보관하면 안 된다.
사람이 서치어드바이저 그래프 오른쪽 위 `⋮` > `xls`로 내려받은 파일을 읽는다.

### 5-2. 어느 파일을 읽나

- 폴더: 설정 `search_advisor.export_dir` (기본 `~/Downloads`)
- 이름이 `export_chart`로 시작하고, 수정 시각이 `search_advisor.max_age_days`(기본 7일) 안인 파일
- 같은 그래프가 여러 개면 가장 최근 파일

### 5-3. 파일 형식 (2026-10-22 실물 확인)

- 확장자는 `.xls`지만 내용은 HTML 표다(시트 이름 `Ark1`). 표만 읽으면 된다.
- 파일 이름은 어느 그래프든 `export_chart.xls`이고 두 번째부터 ` (1)`이 붙는다. **그래서 행 이름으로 그래프를 가린다.**

| 행 이름 | 그래프 |
|---|---|
| `노출` · `클릭` | 콘텐츠 노출/클릭 |
| `수집` · `오류` | 수집 현황 |
| 그 밖 | 모르는 파일. 읽지 않고 파일 이름만 적는다 |

- 첫 행은 날짜다. `10.21`처럼 연도가 없다. 파일 수정 시각의 연도를 붙이고, 그 날짜가 파일 수정 시각보다 뒤면 한 해 전으로 본다.
- `csv` 내보내기는 실물을 아직 보지 않았다. 보기 전까지는 `xls`만 읽는다.

### 5-4. 0을 읽는 법

소유 확인 전 날짜는 0으로 나온다(2026-10-22 확인: GA4에는 그 날들에도 네이버 검색 방문이 있었다).
**처음으로 0이 아닌 날짜 앞의 0은 「값 없음」으로 적고 합계 · 평균에 넣지 않는다.**

### 5-5. 적는 값

| 값 | 계산 |
|---|---|
| 날짜별 노출 · 클릭 | 파일 그대로 |
| 클릭률 | 클릭 ÷ 노출. 노출이 0이면 `null` |
| 날짜별 수집 · 오류 | 파일 그대로 |

### 5-6. 한계

- 서치어드바이저는 등록한 호스트만 센다. 사이트가 다른 호스트(`m.` · `www` 없는 주소)로도 응답하면(7절) 그 몫은 빠져 있다고 적는다.
- 「자세히 보기」(기간 선택 · 검색어별 · 페이지별)는 리포트가 생성된 뒤에 내보내기 형식을 보고 이 절에 더한다.

## 6. 검색어 순위

### 6-1. 검색어 목록

목록 = 사이트 분류 이름 + GA4 네이버 자연 검색(4절)에서 처음 본 화면이 상품 화면인 것의 상위 상품.

- 처음 한 번 스크립트가 후보를 만들어 설정 파일 `keywords.candidates`에 적는다. 상품 이름은 그 화면 `<title>`에서 사이트 이름 앞머리를 뗀 것이다.
- 감사는 사람이 고친 목록 `keywords.list`를 쓴다. 목록을 고정해야 감사끼리 순위를 견줄 수 있다.
- `keywords.list`가 비어 있으면 후보를 그대로 쓰고, 리포트에 「사람이 확인하지 않은 목록」이라고 적는다.
- 상한 `keywords.max` (기본 20).

### 6-2. 재는 곳

| 화면 | 주소 | 적는 값 |
|---|---|---|
| 모바일 통합검색 첫 화면 | `https://m.search.naver.com/search.naver?where=m&query=<검색어>` | 우리 도메인 주소가 나온 영역(`meta.ssuidExtra`)과 그 영역 안 순서. 없으면 「없음」 |
| PC 웹문서 탭 | `https://search.naver.com/search.naver?where=web&query=<검색어>` | 우리 도메인 주소가 처음 나온 순위. 3쪽(60칸)까지 보고 없으면 「60위 밖」 |

- 읽는 법은 AI 브리핑 · `site:`와 같다(화면 HTML의 `entry.bootstrap(...)` JSON). 도메인 판정은 [NAVER-SITE-INDEX.md](NAVER-SITE-INDEX.md) 3-4와 같다.
- 쿠키 · 로그인 없음. 요청 간격 3초.

### 6-3. 한계

- 한 번 잰 값이다. 시각 · IP에 따라 다르다. 감사끼리 추세로 읽는다.
- 공식 경로가 아니다. 화면 구조가 바뀌면 그 검색어는 `error`로 남고 원문을 `raw/`에 저장한다.

## 7. 수집 점검 (네이버 로봇 Yeti)

서치어드바이저 웹마스터 가이드의 항목을 사이트에 직접 요청해 확인한다. 읽기만 한다(GET). 요청 간격 1초.

Yeti 이름 (네이버 안내):

```
Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko; compatible; Yeti/1.1; +https://naver.me/spd) Chrome/W.X.Y.Z Safari/537.36
Mozilla/5.0 (compatible; Yeti/1.1; +https://naver.me/spd)
```

| 점검 | 방법 | 문제로 적는 것 |
|---|---|---|
| Yeti 차단 | 첫 화면을 Yeti 이름 두 가지와 보통 브라우저 이름으로 요청 | Yeti 쪽 응답이 200이 아니거나 브라우저 쪽과 다름 |
| robots.txt | Yeti 이름이 있는 묶음을 모두 모아 합친다. 하나도 없으면 `*` 묶음 | 상품 · 분류 화면 경로가 막힘. `Crawl-delay`는 값만 적는다 (네이버가 따르는지 공식 문서 없음) |
| 호스트 | `www` · `www` 없는 주소 · `m.`을 리다이렉트를 따라가지 않고 요청 | 둘 이상이 각자 200으로 응답함. 각 호스트의 canonical이 어디를 가리키는지 함께 적는다 |
| 사이트맵 호스트 | 사이트맵 `<loc>`를 호스트별로 센다 | 설정 `site`의 호스트와 다른 주소 수 (가이드: 등록 사이트와 호스트가 다르면 수집되지 않는다) |
| 자바스크립트로 보내는 화면 | 사이트맵 상품 주소 표본(`crawl_check.sample_size`, 기본 50)을 Yeti 이름으로 요청 | 200인데 `location.replace` · `location.href=`로 다른 화면으로 보내는 화면. 수와 주소를 적는다 |
| 같은 제목 | 위 표본과 첫 화면의 `<title>` | 첫 화면과 같은 제목을 쓰는 화면 수 |
| 첫 화면 | Yeti 이름으로 받은 첫 화면 | `noindex` · Yeti 전용 meta · `<frameset>` · 자바스크립트로만 된 링크. RSS는 있는지만 적는다 |

## 8. 네이버 검색에 잡힌 페이지 · AI 브리핑

각자의 정본을 따른다.

- 잡힌 페이지: [NAVER-SITE-INDEX.md](NAVER-SITE-INDEX.md). 사이트맵 대조(`--sitemap`)를 함께 돌린다. 서치어드바이저 수치가 있으면 둘을 나란히 적는다.
- AI 브리핑: [NAVER-AI-BRIEFING.md](NAVER-AI-BRIEFING.md). 질문 · 브랜드는 설정 파일의 `ai_briefing` 칸에 둔다.

## 9. 리포트에 적는 자리

| 결과물 | 무엇을 |
|---|---|
| `findings/naver.md` | seo-naver가 쓰는 항목별 결과 (Matomo의 `findings/matomo.md`와 같은 자리) |
| `audit-data.json`의 `categories[]` | `"name": "Naver Search"` 한 묶음 (`score` · `what_works` · `findings[]`). PDF(`google_report.py`)와 seo-cockpit은 이름과 상관없이 `categories[]`를 그리므로 그 코드는 고치지 않는다 |
| `FULL-AUDIT-REPORT.md` | 「네이버 검색」 절 |
| `ACTION-PLAN.md` | 네이버 묶음의 할 일을 같은 우선순위(Critical > High > Medium > Low)에 섞는다 |
| 구글 쪽 GA4 숫자 옆 | 4-3의 한 줄 |

### 9-1. 점수

Naver Search 묶음도 0~100 점수를 매기고, **전체 SEO Health Score에 30%로 넣는다**(2026-10-22 확정).
이 포크를 쓰는 곳은 네이버가 검색 유입이 가장 큰 플랫폼이기 때문이다. 원래 일곱 묶음은 비율대로 줄여 합을 100%로 맞춘다.

| 묶음 | 네이버 설정이 있을 때 | 원래 (네이버 설정이 없을 때) |
|---|---|---|
| Naver Search | 30% | 없음 |
| Content Quality | 16% | 23% |
| Technical SEO | 15% | 22% |
| On-Page SEO | 14% | 20% |
| Schema / Structured Data | 7% | 10% |
| Performance (CWV) | 7% | 10% |
| AI Search Readiness | 7% | 10% |
| Images | 4% | 5% |
| 합 | 100% | 100% |

네이버 설정이 없으면 원래 표를 그대로 쓴다. 네이버 묶음의 항목이 일부만 재졌으면 잰 항목으로만 점수를 매기고,
못 잰 항목을 리포트에 적는다 (3절).

## 10. 설정 파일 `~/.config/claude-seo/naver.json`

이 저장소에는 넣지 않는다. 질문 · 브랜드 · 검색어가 회사 정보이기 때문이다.

```json
{
  "site": "https://www.example.com",
  "ai_briefing": {
    "devices": ["mo", "pc"],
    "queries": ["질문 1", "질문 2"],
    "brands": [
      {"name": "우리브랜드", "aliases": [], "domains": ["example.com"], "is_ours": true},
      {"name": "경쟁사A", "aliases": [], "domains": ["competitor-a.com"], "is_ours": false}
    ]
  },
  "keywords": {"list": [], "candidates": [], "max": 20},
  "search_advisor": {"export_dir": "~/Downloads", "max_age_days": 7},
  "crawl_check": {"sample_size": 50}
}
```

- 전에 쓰던 `naver-ai-briefing.json`은 이 파일의 `ai_briefing` 칸으로 옮긴다. `scripts/naver_ai_briefing.py`가 이 파일을 읽게 고친다.
- GA4 속성 번호와 서비스 계정은 원래 스킬의 `google-api.json`을 그대로 쓴다. 여기 다시 적지 않는다.

## 11. 하지 않는 것

- 서치어드바이저 로그인 쿠키로 내부 주소 부르기 (5-1)
- 유료 수집 대행 API (SerpApi 등)
- `tr=` 값으로 광고 종류 나누기 (4-4)
- 원래 스킬 스크립트의 숫자 바꾸기 (4-3)
- 회사 이름 · 질문 · 검색어 · 결과를 이 저장소에 넣기

## 12. 아직 정하지 않은 것

- Naver Search 묶음 안에서 항목별 점수 매기는 법 (전체 점수에서의 비중은 9-1에서 확정)
- 검색어 순위: 통합검색 영역 값(`meta.ssuidExtra`)을 사람이 읽는 이름으로 바꾸는 표
- 서치어드바이저 「자세히 보기」 내보내기 형식 (리포트 생성 뒤)
- 서치어드바이저 `csv` 형식
