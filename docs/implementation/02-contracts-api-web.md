# 데이터와 API 계약

실행 서버의 `/docs`가 요청 필드와 유효성 검사의 기준이다. 모든 문서는 단일 테넌트의 공유 범위에 속한다. 작업·문서·검색 버전은 UUID로 식별한다. 원본, 파서 응답, 정규화 요소, 청크, 검색 버전은 별도 저장한다.

## API

| 메서드 | 경로 | 용도 |
|---|---|---|
| GET | `/health` | 프로세스 상태 |
| POST | `/v1/auth/login` | 세션과 CSRF 토큰 발급 |
| GET | `/v1/auth/config`, `/v1/auth/session` | 인증 설정 및 세션 확인 |
| POST | `/v1/auth/embed/exchange` | 일회성 iframe 코드 교환 |
| GET | `/v1/processing-options` | 지원 형식, 기본값과 제한 |
| POST | `/v1/ingest` | 파일 등록. multipart `file`, `settings`, `profile`, `embedding_profile_id`, `deferred` |
| GET | `/v1/ingestion-documents` | 문서·작업과 선택 모델의 검색 상태 |
| GET | `/v1/ingest/{document_id}/jobs/{job_id}` | 작업 상태·진행과 범위 |
| POST | `/v1/ingest/{document_id}/jobs` | `base_job_id`, `mode`(`reparse`/`rechunk`), 설정과 모델로 새 작업 생성 |
| POST | `/v1/ingest/{document_id}/jobs/{job_id}/resume` | 저장된 요청 또는 청크부터 재개 |
| GET / POST | `/v1/ingest/{document_id}/jobs/{job_id}/content-review`, `/accept-content-changes` | 재처리 내용 비교와 확인 후 게시 |
| DELETE | `/v1/ingest/{document_id}`, `/v1/ingest/{document_id}/jobs/{job_id}` | 문서 또는 삭제 가능한 작업 삭제 |
| GET | `/v1/ingest/{document_id}/jobs/{job_id}/elements/{element_id}` | 해당 작업의 파싱 원문 요소 |
| GET | `/v1/ingest/{document_id}/jobs/{job_id}/source-details` | 원본 Office 구조 정보 |
| POST | `/v1/retrieve`, `/v1/ingest-chat` | 검색과 문서 기반 대화 |
| GET | `/v1/ingest-index-versions`, `/v1/chat/options` | 검색 버전·대화 모델 목록 |
| GET / PUT | `/v1/settings` | 서버 설정 조회·저장 |
| GET / PUT / DELETE | `/v1/embedding-profiles`, `/v1/embedding-profiles/{profile_id}` | 임베딩 모델 관리 |
| POST | `/v1/embedding-profiles/{profile_id}/validate` | 연결·모델 응답·차원 검사 |
| GET / POST | `/v1/ingest-index-status`, `/v1/ingest-index-rebuild` | 기본 모델 색인 상태와 재구축 |

웹 업로드는 `deferred=true`로 접수한다. 동일 파일은 409 `DUPLICATE_DOCUMENT`와 기존 문서 ID를 반환하며, `allow_duplicate=true`로 명시하면 별도 문서로 등록한다. 일부 결과의 명시적 적용은 `POST /v1/ingest/{document_id}/jobs/{job_id}/accept-partial`을 사용하며 누락 상태와 이전 버전을 유지한다. API의 `deferred` 기본값은 `false`이며 동기 처리 결과를 반환한다. 검색은 `query`, `method`(`bm25`, `dense`, `hybrid`), `limit`(1–20), 선택적 문서·모델·버전 및 `rerank`를 받는다. 응답에는 검색 버전, 청크 텍스트와 `source_refs`의 역할(`evidence`/`context`), `evidence_urls`, `context_urls`가 포함된다. 기존 버전의 미확인 범위는 보수적으로 표시한다. 검색 버전 목록의 `dense_available`은 현재 모델 계약과의 일치 여부를 나타낸다. `POST /v1/ingest-index-rebuild?profile_id=...`는 해당 프로필의 검색 색인을 재구축한다. 대화 이력의 `citation_numbers`는 이전 답변의 출처 번호를 구분하는 데 사용한다.

검색 결과 수 제한은 검색·재순위 결과에 적용하며, 이후 동일 원본 표의 추가 근거가 최대 3개 더 포함될 수 있다. 추가된 결과의 `expansion_reason`은 `same_source_table`, `expanded_from`은 기준 청크 ID다. `table_expansion_complete=false`는 색인에 있는 같은 표의 분할 청크가 일부 제외됐음을 뜻한다. 파싱 자체의 완전성은 보장하지 않는다.

관련 이미지 메타데이터는 검색 결과의 `related_images`에서 확인한다. 이미지 분석 청크는 최대 2개·4,000자까지 별도 근거로 추가될 수 있으며 `expansion_reason=related_image_analysis`와 관계의 출처·확인 상태가 제공된다. 작업의 `GET /v1/ingest/{document_id}/jobs/{job_id}/images`와 요소별 `.../elements/{element_id}/image`는 동일 인증을 요구한다. 이미지 원본은 작업 삭제 시 함께 삭제된다.

`POST /v1/ingest/{document_id}/jobs/{job_id}/images/actions`는 이미지 요소 ID를 `analyze`, `original`, `both`, `exclude`, `reuse`, `describe`에 매핑한 `actions`, `describe`에 필요한 요소별 `descriptions` 및 선택적 `embedding_profile_id`를 받는다. 최대 100개를 선택할 수 있다. `reuse`는 저장된 이미지 분석 결과가 있을 때만 허용하며 외부 분석을 재호출하지 않는다. 미게시 검토 작업이면 같은 작업에 병합하고, 이미 게시된 작업이면 미게시 새 작업으로 분기한다. `describe`는 1~2,000자의 사용자 설명과 원본을 직접 연결한다. 본문 연결이 없는 원본은 설명을 입력해야 한다. 모든 확인 필요 이미지의 처리를 선택한 뒤 명시적으로 게시한다. 검색에서 제외된 분석 결과·관계·원본은 산출물로 유지한다.

세션의 변경 요청은 Origin과 CSRF 토큰을 검증한다. iframe bearer 인증은 서버 설정 변경을 허용하지 않는다. 오류는 `error.code`, `message`, `request_id`를 제공한다. [인증과 iframe](../user-guide/03-integration.md)


## XLSX 구조 계약

- `GET /v1/ingest/{document_id}/jobs/{job_id}/xlsx-structure`는 직접 추출 결과의 셀, 스타일, 명시 표, 경계 후보, 검증 상태, 제외 이유, 차트 정의 및 시트 간 행 연결 후보를 반환한다.
- 같은 경로에 `POST`로 시트 이름을 키로 하는 수정안을 보낸다. 시트마다 `regions`와 `excluded`를 지정한다. 영역에는 `start`, `end`, `kind`, `headers`, `title`, `uncertain`이 있고, 제외 목록은 셀 주소에서 이유 문자열로 매핑한다. 서버는 좌표, 병합, 선언된 Excel 표, 헤더와 셀 할당을 검증한 뒤 새 작업을 등록한다.
- `GET /v1/ingest/{document_id}/xlsx-tables`로 게시된 버전에서 검증된 표와 열을 확인한다. `POST /v1/xlsx-query`는 `document_id`, 시트 `sheet`, 표 시작 주소 `start` 및 `operation`을 받는다. 연산은 `count`, `distribution`, `date_range`, `rows`다. 분포와 날짜 범위는 `column`이 필요하며 `filters`는 열 주소, 허용 연산자와 타입이 맞는 값을 사용한다. 집계는 헤더 및 선언된 합계 행을 제외하고 원본 범위를 순회한다. 행 조회는 `offset`과 `limit`으로 나누며 응답에는 고정된 색인 버전, 범위와 불완전성이 포함된다.
- `xlsx_structure_version=2`, 청크 `xlsx-structure-v2` 구조는 직접 추출 또는 같은 버전의 구조 수정 작업에 적용된다. 이전 게시 색인은 기존 검색과 복구를 지원한다. 이전 파싱 산출물에 새 구조를 적용할 때는 청크 재생성 대신 직접 추출로 새 작업을 만든다.
