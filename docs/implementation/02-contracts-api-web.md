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

- `GET /v1/ingest/{document_id}/jobs/{job_id}/xlsx-structure`는 시트 목록을 반환한다. `sheet`를 지정하면 해당 시트의 영역, 검증 상태, 병합 범위와 제외 이유를 반환한다. `GET .../xlsx-cells`는 시트의 셀 격자 일부를 반환한다. 창은 최대 25행 × 20열이며 셀 미리보기는 200자로 제한한다. `address`로 개별 셀의 전체 값과 다른 시트의 정적 A1 참조를 본다. 외부 통합 문서, 이름 범위, 동적 참조는 해석 범위에서 제외한다.
- 구조 조회 경로에 `POST`로 시트 이름을 키로 하는 수정안을 보낸다. 시트마다 `regions`와 `excluded`를 지정한다. 영역에는 `start`, `end`, `kind`, `headers`, `title`, `uncertain`이 있고, 제외 목록은 셀 주소에서 이유 문자열로 매핑한다. 서버는 좌표, 병합, 선언된 Excel 표, 헤더와 셀 할당을 검증한 뒤 새 작업을 등록한다.
- 영역의 `verification` 값은 `explicit` (Excel 등록), `user_verified` (사용자 확인), `llm_validated` (모델 제안의 구조 검사 통과), `inferred` (로컬 추정)이다. 앞의 두 상태가 전체 표 조회 대상이다. 이 규칙은 이미 게시된 v2 결과에도 적용된다.
- `GET /v1/ingest/{document_id}/xlsx-tables`로 게시된 버전에서 조회할 수 있는 표와 열을 확인한다. `POST /v1/xlsx-query`는 `document_id`, 시트 `sheet`, 표 시작 주소 `start` 및 `operation`을 받는다. 연산은 `count`, `distribution`, `date_range`, `rows`, `sum`, `average`, `min`, `max`다. 분포·날짜 범위·수치 집계는 `column`을 사용한다. 개수와 수치 집계에 `group_by`를 지정할 수 있다. 헤더와 선언된 합계 행은 집계 대상에서 빠지며 누락된 계산값과 제외 셀은 결과에 표시한다.
- `GET /v1/ingest/{document_id}/xlsx-join-candidates`는 고정된 `index_version`과 `sheet`, `start`, `right_sheet`, `right_start`를 받는다. 조회에 승인한 후보는 `POST /v1/xlsx-query`의 `join`에 `sheet`, `start`, `approved_pairs`로 지정한다. 각 쌍은 `left_row`와 `right_row`를 담는다. 한 요청에서 승인한 쌍만 내부 결합한다. 중복 식별자, 이름 충돌 및 원본과 다른 연결은 거부한다. 필터의 `side`와 집계 열의 `column_side`, 그룹 열의 `group_side`로 좌우 표를 지정한다. 응답에는 미결합·미승인 건수, 조건에 맞은 행 쌍 및 양쪽 원문 범위가 포함된다.
- 열 기반 조인은 `POST /v1/xlsx-join-preview`에서 확인한다. `document_id`, 고정된 `index_version`, 왼쪽 표의 `sheet`·`start`, 오른쪽 표의 `join.sheet`·`join.start`를 지정한다. `join.keys`는 최대 네 개의 `{left, right, type}`으로 구성되며, 타입은 `text`, `number`, `date`, `datetime`, `boolean`이다. 같은 `join`을 `POST /v1/xlsx-query`에 쓰면 일치하는 모든 행을 내부 결합한다. 관계 `cardinality`는 `one_to_one`, `one_to_many`, `many_to_one` 중 하나이며 고유해야 하는 쪽에 중복 키가 있으면 조회를 거부한다. 텍스트에는 숫자 변환이나 공백 제거를 적용하지 않는다. 빈 키, 타입 불일치, 누락된 계산값과 제외 셀은 결합할 수 없는 이유와 행 수를 응답에 기록한다. 미결합 행 수와 양쪽 원본 행 주소도 반환한다.
- `xlsx_structure_version=2`에서 셀의 OOXML 타입과 값, 변환된 타입, 수식, 캐시 상태·값 및 표시용 텍스트를 따로 보존한다. 비어 있는 문자열 캐시와 누락된 캐시는 구분한다. 새 XLSX 청크는 `xlsx-structure-v3`, 공통 HTML 표 청크는 `table-structure-v13`로 표시한다. HTML 표의 원문 근거에서 줄바꿈과 들여쓰기를 보존하고, 검색값과 헤더 라벨에는 공백을 정규화한 표현을 쓴다. 기존 게시 색인과 새 청크 적용 방법은 저장·운영 문서에 설명한다.

공통 HTML 표의 새 청크는 `table_segments`에 검색용 값을 두고 `source_segments`에는 해당 셀의 원문 조각과 위치를 기록한다. 검색 결과의 근거 URL은 `table_cells=row:column:start:end`를 사용한다. 행·열은 1부터 시작하며, 오프셋은 보존된 셀 텍스트에서 0부터 시작하는 문자 인덱스다. 끝 오프셋은 범위에 포함되지 않는다. 지원되는 표의 셀 범위는 임베딩 단계에서 다시 분할될 때도 갱신된다. 기존 게시 청크와 셀 배치를 확인할 수 없는 표의 근거는 요소 단위로 조회한다.

빈 HTML 표에 인접한 캡션만 검색되는 경우 캡션 요소가 원문 근거이며, 빈 표 요소는 문맥으로 연결된다.
