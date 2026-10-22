# 네이버 검색에 잡힌 페이지 세기 (직접 읽기)

이 문서가 정본이다. 코드(`scripts/naver_site_index.py`)는 이 문서를 따른다.
코드와 이 문서가 다르면 코드를 고친다.

## 1. 무엇을 재나

네이버 웹문서 검색에 `site:<도메인>`을 넣고, 결과 목록을 끝까지 넘기며 그 도메인의 주소를 모은다.

- 모은 주소를 호스트별 · 화면 종류(경로)별로 센다.
- 사이트맵 주소를 주면, 검색에 잡힌 주소와 사이트맵 주소를 대조한다 (4절).

**서치어드바이저의 색인 페이지 수와 같은 값이 아니다.** 네이버가 검색 결과 목록으로 보여 주는 주소만 센다.
서치어드바이저 수치가 아직 없을 때(소유확인 직후) 쓰는 값이고, 수치가 생긴 뒤에는 둘을 나란히 적는다.

## 2. 정해진 것 (2026-10-21)

| 항목 | 정한 값 |
|---|---|
| 수집 경로 | 네이버 검색 화면을 직접 읽는다. 검색 API · 유료 수집 대행 API는 쓰지 않는다 |
| 화면 | PC 웹문서 탭(`where=web`) 하나. 잡힌 문서 목록을 보려는 것이라 기기를 나누지 않는다 |
| 쿠키 · 로그인 | 쓰지 않는다 |
| 요청 간격 | 3초 |
| 쪽 상한 | 기본 50쪽 (한 쪽 20건, 최대 1,000건). 상한에 닿으면 `partial`로 남긴다 |
| 원문 보관 | 받은 쪽마다 HTML을 `raw/`에 저장한다 |

## 3. 읽는 방법

### 3-1. 요청 주소

```
1쪽      https://search.naver.com/search.naver?where=web&query=site%3A<도메인>
2쪽부터  앞 쪽의 「다음」 버튼(class="btn_next")에 적힌 주소를 그대로 따라간다
```

쪽 주소를 직접 만들지 않는다. 네이버가 만든 쪽 주소는 아래 꼴이었다 (2026-10-21 원문 확인).

```
1쪽  ssc=tab.nx.all                       page · start 없음
2쪽  page=2 & start=1  & ssc=tab.ur.all
3쪽  page=3 & start=21 & ssc=tab.ur.all
4쪽  page=4 & start=41 & ssc=tab.ur.all
```

- `page=` 없이 `start=21`만 보내면 네이버가 1쪽을 다시 준다 (20건 전부 1쪽과 같았다).
- 2쪽(`start=1`)은 1쪽과 20건 중 19건이 겹친다. 같은 목록의 앞부분이라서다. 겹친 주소는 한 번만 센다.
- 「다음」 주소의 호스트가 `search.naver.com`이 아니거나 검색어가 `site:<도메인>`이 아니면 따라가지 않고 `error`로 멈춘다.

### 3-2. 결과 한 건

결과는 HTML 안의 `entry.bootstrap(document.getElementById("fdr-…"), {JSON})` 묶음에 들어 있다.
`meta.ssuidExtra`가 `fender_renderer-web`인 묶음의 `data.state.items[]`가 결과 목록이다.

| 칸 | 쓰는 법 |
|---|---|
| `profile.keep.keepTriggerUrl` | **문서 주소.** 이 칸을 먼저 쓴다 |
| `href` | 위 칸이 없을 때 쓴다. 호스트가 `inflow.pay.naver.com`이면 쿼리의 `retUrl` 값이 문서 주소다 |
| `title` | 제목. HTML 태그를 지운다 |

`nlogParams.cr_rank_legacy`는 묶음마다 1부터 다시 시작하므로 순위로 쓰지 않는다. 순위는 나온 순서로 센다.

### 3-3. 멈추는 조건

아래 중 하나가 되면 다음 쪽을 요청하지 않는다.

| 조건 | 남는 값 `stop_reason` |
|---|---|
| 다음 버튼(`class="btn_next"`)이 없거나, `aria-disabled="true"`이거나, 주소(`href`)가 없음 | `last_page` |
| 그 쪽에 결과가 0건 | `empty_page` |
| **두 쪽 연달아** 새 주소가 0건 (그 사이트 주소가 전부 이미 모은 것) | `no_new_urls` |
| 쪽 상한에 닿음 | `max_pages` |
| 요청 실패 | `error` |

`no_new_urls`를 한 쪽이 아니라 두 쪽 연달아로 두는 것은, 2쪽이 1쪽과 거의 같은 주소를 다시 보여 주기 때문이다 (3-1).

결과 묶음 표시(`fender_renderer-web`)는 있는데 JSON을 읽지 못하면 0건으로 세지 않고 `error`로 멈춘다.

### 3-4. 도메인 판정

호스트가 `<도메인>`이거나 `.<도메인>`으로 끝나면 그 사이트 주소로 센다(`www.` · `m.` 포함).
아니면 `other_domain`으로 따로 센다.

## 4. 사이트맵 대조

`--sitemap <주소>`를 주면 사이트맵을 받아 `<loc>` 주소를 모은다 (`&amp;`는 `&`로 되돌린다).
사이트맵 색인 파일(`sitemapindex`)이면 그 안의 사이트맵을 한 단계만 따라간다.

대조 기준은 **호스트를 뺀 경로 + 쿼리**다. 쿼리는 이름순으로 정렬한다.
같은 화면이 `www.` · `m.` · 호스트 없는 주소로 나뉘어 있어도 한 화면으로 본다.

| 묶음 | 뜻 |
|---|---|
| `in_both` | 검색에 잡혔고 사이트맵에도 있다 |
| `search_only` | 검색에 잡혔는데 사이트맵에 없다 |
| `sitemap_only` | 사이트맵에 있는데 이번 검색 결과에 없다 |

`sitemap_only`는 "색인이 안 됐다"는 뜻이 아니다. 쪽 상한이나 네이버가 목록에 다 보여 주지 않아서일 수 있다.

## 5. 결과 JSON

```
status                         success | partial | error
data
  .domain .searched_at .pages_fetched .stop_reason .max_pages
  .last_page_items               마지막으로 읽은 쪽의 결과 건수 (20이면 6절 상한일 수 있다)
  .urls[]                        .rank .page .url .host .path .title
  .summary
    .total_urls                  모은 주소 수 (겹친 것 뺌)
    .other_domain                다른 도메인 주소 수
    .by_host                     {호스트: 수}
    .by_path                     {경로: 수}   경로 = 호스트 뒤 경로
  .sitemap                       (--sitemap 줬을 때만)
    .url .total .in_both .search_only .sitemap_only
    .search_only_urls[] .sitemap_only_urls[]
  .raw_files[]
error
metadata                       .source .spec .timestamp
```

## 6. 한계

- **공식 경로가 아니다.** 화면 구조가 바뀌면 `error`로 남고 원문이 `raw/`에 저장된다. 조용히 0으로 적지 않는다.
- **네이버가 `site:` 결과를 전부 보여 준다는 보장이 없다.** 이 값은 "적어도 이만큼은 잡혀 있다"로 읽는다.
- **네이버는 10쪽(200칸)까지만 보여 주는 것으로 보인다.** 2026-10-21 확인: 10쪽이 20건 꽉 찬 채로
  다음 버튼이 꺼졌고, 11쪽 주소를 직접 요청하면 0건이었다. 200칸 안에서도 앞 쪽 주소가 다시 나와서
  서로 다른 주소는 그보다 적다. 마지막 쪽이 20건이면(`last_page_items` = 20) 리포트에 상한일 수 있다고 적는다.
- **한 번 잰 값이다.** 날을 바꿔 쌓아 추세로 읽는다.
