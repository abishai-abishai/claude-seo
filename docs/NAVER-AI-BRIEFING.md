# 네이버 AI 브리핑 점검 (직접 읽기)

이 문서가 네이버 AI 브리핑 점검의 정본이다. 코드(`scripts/naver_ai_briefing.py`)는 이 문서를 따른다.
코드와 이 문서가 다르면 코드를 고친다.

## 1. 무엇을 재나

질문 목록을 네이버 검색에 넣고, 질문마다 아래 세 가지를 기록한다.

1. AI 브리핑이 떴는가
2. 답변 본문
3. 답변에 붙은 출처 링크 (화면에 보이는 번호 순서)

지표(5절)는 전부 이 기록을 나눈 값이다.

## 2. 정해진 것 (2026-10-21)

| 항목 | 정한 값 |
|---|---|
| 수집 경로 | 네이버 검색 화면을 직접 읽는다. 유료 수집 대행 API(SerpApi 등)는 쓰지 않는다 |
| 브라우저 | 쓰지 않는다. HTTP 요청만 보낸다 |
| 쿠키 · 로그인 | 쓰지 않는다. 요청마다 쿠키 없이 새로 보낸다 (검색 이력 개인화를 막기 위해) |
| 기기 | 모바일(`mo`)과 PC(`pc`) 둘 다 |
| 요청 간격 | 3초 |
| 질문 · 브랜드 · 도메인 | 설정 파일에 둔다. 이 저장소에는 넣지 않는다 (공개 저장소이기 때문) |
| 원문 보관 | 받은 HTML과 두 번째 요청 응답을 `raw/`에 그대로 저장한다. 판정을 다시 할 때 근거가 된다 |

설정 파일: `~/.config/claude-seo/naver.json`의 `ai_briefing` 칸 (6절 · [NAVER.md](NAVER.md) 10절).
전에는 `naver-ai-briefing.json`이었다. 코드를 이 위치에 맞춰 고친다.

## 3. 읽는 방법

### 3-1. 검색 화면 주소

| 기기 | 주소 |
|---|---|
| `mo` | `https://m.search.naver.com/search.naver?where=m&query=<질문>` |
| `pc` | `https://search.naver.com/search.naver?where=nexearch&query=<질문>` |

### 3-2. AI 브리핑 데이터 찾기

검색 화면 HTML에는 화면 조각마다 아래 모양의 스크립트가 있다.

```
entry.bootstrap(document.getElementById("fdr-<id>"), { ...JSON... })
```

두 번째 값(JSON)을 통째로 읽는다. 그중 `meta.ssuidExtra`가 `fender_renderer-ai_briefing`이거나
`refs.blockId`가 `ai-briefing/`으로 시작하는 것이 AI 브리핑이다. 없으면 그 시각에 AI 브리핑이 안 뜬 것이다.

### 3-3. 틀(templateId) 두 가지

| `body.templateId` | 답변이 있는 곳 | 하는 일 |
|---|---|---|
| `aibShopping` | `body.props.summary.copy` · `body.props.sources` | HTML 안에 다 있다. 그대로 읽는다 |
| `aibAnswerRuntime` | HTML에는 없다. `body.props.apiURL`로 한 번 더 요청해야 온다 | 아래 3-4 |
| 그 밖의 값 | 모른다 | `status: "unknown_template"`으로 기록하고 원문을 저장한다 |

### 3-4. 두 번째 요청 (`aibAnswerRuntime`)

```
GET  body.props.apiURL
     헤더 X-NX-Query-Info = body.props.customHeaders["X-NX-Query-Info"]
     헤더 Referer         = 그 검색 화면 주소
응답 text/event-stream
     event: summary          data: {"markdown": "<글자 조각>"}   여러 번
     event: summary          data: {"copy": "<완성본>"}          마지막 한 번
     event: footnote_sources data: [출처 후보]
     event: sources          data: [화면에 보이는 출처]
     event: rv               data: {"verdict": "show"}
     event: done
```

- `apiURL`은 `https`이고 호스트가 `aib-api.naver.com`일 때만 따라간다. 다른 주소면 요청하지 않고 오류로 기록한다.
- 글자 조각을 이어 붙이지 않는다. 마지막 `copy`(완성본)만 쓴다.
- `rv.verdict`가 `show`가 아니면 화면에 안 보이는 답으로 본다. `status: "hidden"`으로 기록하고 뜬 수에 넣지 않는다.
  근거 (2026-10-21 확인): 네이버 화면 코드 `service-aib.js`는 `rv` 이벤트의 `verdict`가 `"hide"`이면
  그 답을 오류 상태(`isError`)로 바꾼다. 오류 상태에서는 답변 · 출처 영역이 사라지고(`in: !isError`)
  그 자리에 오류 안내(`AibAnswerErrorInfo`)가 들어간다.
  같은 날 `show`로 받은 답은 실제 PC 화면과 첫 문장 · 출처 주소 · 출처 순서가 모두 같았다.
- `done` 이벤트가 오지 않으면 `stream_incomplete` 경고를 남긴다.

### 3-5. 출처 번호

두 틀 모두 `copy` 끝에 아래 목록이 붙어 온다. **이 번호가 화면에 보이는 출처 번호다.**

```
정보출처
[1] <이름>, <URL>
[2] <이름>, <URL>
```

`sources` · `footnote_sources`는 같은 URL의 제목 · 플랫폼 이름을 붙이는 데만 쓴다.
`copy`가 없으면 `sources`를 `footnoteSourceIndex` 순서로 쓰고 `copy_missing` 경고를 남긴다.

## 4. 판정

| 무엇 | 어떻게 |
|---|---|
| 본문 | `copy`에서 `정보출처` 줄 앞까지 |
| 브랜드 언급 | 본문에서 브랜드 이름 · 별칭을 글자로 찾는다. 영문 이름은 앞뒤가 영문 · 숫자가 아닐 때만 센다 (`ABC`가 `XABC` 안에서 세지지 않게) |
| 인용 | 출처 URL의 호스트가 브랜드 `domains`와 같거나 그 하위 도메인이면 그 브랜드의 인용이다. `domains`에 `/`가 있으면(예: `blog.naver.com/아이디`) 호스트+경로 앞부분으로 맞춘다 |

출처 종류(`source_type`)는 아래 순서로 처음 맞는 것 하나를 붙인다.

| 값 | 기준 |
|---|---|
| `own` | 자사 브랜드(`is_ours: true`)의 `domains` |
| `competitor` | 경쟁사 브랜드의 `domains` |
| `naver_ugc` | `blog.naver.com` · `cafe.naver.com` · `in.naver.com` · `kin.naver.com` · `post.naver.com` (모바일 주소 포함) |
| `naver_shopping` | `smartstore.naver.com` · `brand.naver.com` · `shopping.naver.com` |
| `community` | 커뮤니티 · 영상 사이트 목록 (코드의 `COMMUNITY_HOSTS`) |
| `third_party` | 그 밖의 전부 |

## 5. 지표

AI 브리핑이 뜬 답변 하나를 "답변 1건"으로 센다. 읽지 못한 건(`status: "error"`)은 떴는지 모르므로
모든 분모에서 빼고 `errors`에 따로 센다.

| 지표 | 계산식 | 칸 이름 |
|---|---|---|
| 노출률 | AI 브리핑이 뜬 수 ÷ 검색한 수 (오류 제외) | `shown_rate` |
| 노출도 점수 | 자사 브랜드가 본문에 언급된 답변 수 ÷ 답변 수 | `exposure_score` |
| 인용 점유율 | 자사 링크가 출처에 있는 답변 수 ÷ 출처가 하나라도 있는 답변 수 | `citation_share` |
| 언급 점유율 | 자사 언급 횟수 ÷ (자사 + 경쟁사 언급 횟수) | `mention_share` |
| 인용 → 브랜드 귀속률 | 자사 링크가 인용된 답변 중 본문에도 자사 브랜드가 있는 비율 | `attribution_rate` |

분모가 0이면 값은 `null`이다 (0%로 적지 않는다).

## 6. 설정 파일

`~/.config/claude-seo/naver.json` 중 이 점검이 읽는 칸이다. 파일 전체 모양은 [NAVER.md](NAVER.md) 10절.

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
  }
}
```

명령줄의 `--query`(여러 번 가능) · `--device`가 있으면 설정 파일 값보다 앞선다.

## 7. 결과 JSON

저장소의 다른 스크립트와 같이 `status` · `data` · `error` · `metadata` 네 칸으로 돌려준다.
`status`는 전부 성공이면 `success`, 일부 질문이 오류면 `partial`, 전부 오류면 `error`다.

```
data.site
data.run_at                      실행 시각 (KST, ISO 8601)
data.answers[]                   질문 x 기기 한 건씩
  .query  .device  .search_url  .searched_at
  .status                        ok | no_briefing | hidden | unknown_template | error
  .shown                         AI 브리핑이 떴는가
  .template  .aib_type  .verdict
  .answer_text                   본문 (정보출처 앞까지)
  .sources[]                     .rank .name .title .url .domain .platform .source_type .brand
  .own_cited  .own_cited_ranks[]
  .mentions{브랜드: 횟수}  .own_mentioned
  .warnings[]  .error  .raw_files[]
data.summary                     전체
data.summary_by_device.mo / .pc  기기별
  .attempts .errors .shown .shown_rate
  .answers_with_sources .own_mentioned_answers .own_cited_answers .own_cited_and_mentioned
  .exposure_score .citation_share .own_mentions .competitor_mentions .mention_share .attribution_rate
  .mention_totals{브랜드: 횟수}  .top_domains[[도메인, 횟수]]  .source_type_counts{종류: 횟수}
```

`--output-dir`를 주면 그 폴더에 `naver-ai-briefing.json` · `NAVER-AI-BRIEFING.md` · `raw/`를 쓴다.

## 8. 한계

- **공식 경로가 아니다.** 네이버가 화면 구조를 바꾸면 읽기가 깨진다. 깨지면 그 건은 `error` 또는
  `unknown_template`으로 남고 원문이 `raw/`에 저장된다. 조용히 0으로 적지 않는다.
- **한 번 잰 값이다.** 같은 질문에도 AI 브리핑은 떴다 안 떴다 하고, 시각 · IP에 따라 답이 다르다.
  하루 한 번씩 쌓아 추세로 읽는다.
  2026-10-21에는 같은 질문이 1시간 20분 사이에 출처 6개 `hide`에서 출처 4개 `show`로 바뀌었다.
  출처 6개 중 3개만 남고 1개가 새로 들어왔으며, 본문 첫 문장도 달랐다.
- **요청이 많으면 막힐 수 있다.** 간격 3초를 줄이지 않는다.
