# iframe 연동과 검색 평가

[빠른 시작](../../README.md) · [설정과 빌드](01-setup-build.md) · [웹 사용법](02-web-usage.md)

## 호스트에 삽입

같은 origin의 호스트는 세션으로 인증한 뒤 아래 경로를 삽입합니다.

```html
<iframe src="/embed" title="문서 마트" width="100%" height="720"></iframe>
```

다른 origin에 삽입할 때는 HTTPS로 배포하고 `PUBLIC_ORIGIN`, `INDEXER_ACCESS_KEY`, `ALLOWED_FRAME_ANCESTORS`와 `EMBED_SIGNING_SECRET`을 설정합니다. 단일 테넌트의 공유 문서 접근을 허용할 호스트만 등록합니다. 호스트의 백엔드는 로그인한 사용자에게 iframe 사용 권한이 있는지 확인한 뒤 `scripts/issue_embed_code.py`의 `issue_code()`와 같은 방식으로 짧은 수명의 코드를 발급합니다. 서명 비밀은 브라우저에 전달하지 않습니다.

호스트의 화면은 아래 방식으로 nonce를 받아 발급 코드를 전달합니다. 예시에서 `/api/indexer/embed-code`는 호스트가 구현하는 인증된 발급 경로입니다.

```js
const indexerOrigin = "https://indexer.example";
const frame = document.querySelector("iframe");

window.addEventListener("message", async (event) => {
  if (event.origin !== indexerOrigin || event.source !== frame.contentWindow) return;
  if (event.data?.type !== "document-indexer:ready" || typeof event.data.nonce !== "string") return;

  const nonce = event.data.nonce;
  const response = await fetch("/api/indexer/embed-code", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ nonce }),
  });
  if (!response.ok) return;
  const { code } = await response.json();
  frame.contentWindow.postMessage({ type: "document-indexer:code", nonce, code }, indexerOrigin);
});
```

## 검색 품질 평가

평가할 문서를 먼저 적재합니다. 정답 근거를 사람이 확인한 질의를 `cases.json`으로 저장합니다. `document_id`는 API 문서 목록에서 확인할 수 있습니다. 표의 `row`와 `column`은 1부터 시작합니다. `contains_line`은 같은 검색 텍스트 줄에 있어야 할 항목을 적고, 셀 위치를 지정하면 해당 셀의 검색 표현을 확인합니다. 본문 질의는 문서 ID와 페이지로 평가할 수 있습니다.

```json
[
  {
    "id": "table-001",
    "group": "table",
    "query": "2026년 설비비는 얼마인가요?",
    "evidence": [
      {
        "document_id": "실제 문서 UUID로 교체",
        "page": 5,
        "row": 2,
        "column": 4,
        "contains_line": ["설비비", "2026년", "15.25"]
      }
    ]
  }
]
```

```sh
uv run --locked --env-file .env python -m scripts.evaluate cases.json --top-k 5
```

Hit@k는 상위 k개 중 정답 근거를 하나라도 찾은 질의의 비율입니다. Recall@k는 지정한 근거 중 찾아낸 비율, MRR은 첫 정답 근거의 순위를 반영한 평균, p95는 질의 응답 시간의 95백분위수입니다. `group`으로 본문·표·스캔 질의를 나누어 비교할 수 있습니다.

평가 중에는 활성 인덱스를 갱신하지 않습니다. 두 번의 평가를 비교할 때 같은 문서와 질의·근거를 사용합니다. Dense·Hybrid 평가는 선택한 임베딩 서비스에 질의를 보내므로 비용이 발생할 수 있습니다.

## API로 연동

공개 주소의 `/docs`에 요청 입력과 응답 형식이 있습니다.

| 작업 | API |
|---|---|
| 서버 상태 확인 | `GET /health` |
| 설정과 파일 제한 조회 | `GET /v1/processing-options` |
| 파일 접수 | `POST /v1/ingest`에 multipart `file`, `deferred=true`와 선택적 `settings` JSON 문자열 제출 |
| 처리 결과 확인 | `GET /v1/ingest/{document_id}/jobs/{job_id}` |
| 최근 문서 목록 | `GET /v1/ingestion-documents` |
| 검색 | `POST /v1/retrieve`에 `query`, `method`와 선택적 `document_id`·`limit` 제출 |

세션 인증은 `POST /v1/auth/login`으로 받은 쿠키를 사용합니다. 상태를 변경하거나 검색하는 POST 요청에 서버의 공개 주소를 `Origin`으로, 로그인에서 받은 값을 `X-CSRF-Token`으로 보냅니다. iframe의 일회성 코드 교환으로 받은 토큰은 `Authorization: Bearer <token>`으로 보냅니다. 최신 입력·응답 계약은 실행 중인 서버의 `/docs`를 확인합니다.
