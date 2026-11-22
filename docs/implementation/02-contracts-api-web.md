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
| GET | `/v1/ingest/{document_id}/pdf-pages` | 게시된 PDF 물리쪽별 청크 직접 조회 |
| GET | `/v1/ingest-index-versions`, `/v1/chat/options` | 검색 버전·대화 모델 목록 |
| GET / PUT | `/v1/settings` | 서버 설정 조회·저장 |
| GET / PUT / DELETE | `/v1/embedding-profiles`, `/v1/embedding-profiles/{profile_id}` | 임베딩 모델 관리 |
| POST | `/v1/embedding-profiles/{profile_id}/validate` | 연결·모델 응답·차원 검사 |
| GET / POST | `/v1/ingest-index-status`, `/v1/ingest-index-rebuild` | 기본 모델 색인 상태와 재구축 |

웹 업로드는 `deferred=true`로 접수한다. 동일 파일은 409 `DUPLICATE_DOCUMENT`와 기존 문서 ID를 반환하며, `allow_duplicate=true`로 명시하면 별도 문서로 등록한다. 일부 결과의 명시적 적용은 `POST /v1/ingest/{document_id}/jobs/{job_id}/accept-partial`을 사용하며 누락 상태와 이전 버전을 유지한다. API의 `deferred` 기본값은 `false`이며 동기 처리 결과를 반환한다. 검색은 `query`, `method`(`bm25`, `dense`, `hybrid`), `limit`(1–20), 선택적 문서·모델·버전 및 `rerank`를 받는다. 응답에는 검색 버전, 청크 텍스트와 `source_refs`의 역할(`evidence`/`context`), `evidence_urls`, `context_urls`가 포함된다. 기존 버전의 미확인 범위는 보수적으로 표시한다. 검색 버전 목록의 `dense_available`은 현재 모델 계약과의 일치 여부를 나타낸다. `POST /v1/ingest-index-rebuild?profile_id=...`는 해당 프로필의 검색 색인을 재구축한다. 대화 이력의 `citation_numbers`는 이전 답변의 출처 번호를 구분하는 데 사용한다.

검색 결과 수 제한은 검색·재순위 결과에 적용하며, 이후 동일 원본 표의 추가 근거가 최대 3개 더 포함될 수 있다. 추가된 결과의 `expansion_reason`은 `same_source_table`, `expanded_from`은 기준 청크 ID다. `table_expansion_complete=false`는 색인에 있는 같은 표의 분할 청크가 일부 제외됐음을 뜻한다. 파싱 자체의 완전성은 보장하지 않는다.

일반 검색의 각 근거에는 같은 문서·작업·원본 요소에 연결된 색인 청크 수와 반환 수를 `element_scopes`로 표시한다. 대화 근거에는 예산 적용 후 전체 텍스트가 모델에 들어간 청크 수와 잘림 여부를 `model_element_scopes`로 추가한다. 이는 게시 색인 내 조각에 대한 정보이며, 원문 추출이나 질문 대상 전체 범위가 완전하다는 뜻이 아니다. 예전 산출물에 요소 식별자가 없으면 범위는 미확인이다. 여러 표가 검색되면 기존 표 확장 예산을 표별로 나눠 적용한다. 일반 텍스트도 같은 문서·작업의 단일 원본 요소에 속한 조각을 최대 3개, 9,000자까지 추가하며 페이지 지정 조회에서는 같은 요청 범위만 허용한다.

일반 대화 응답의 `coverage`는 검색 방식, 범위 판정 상태(`scope_complete=null`은 미확인), 검색·모델 근거 수, 색인 조각이 덜 전달된 원본 요소 수를 제공한다. 모델에는 각 근거에 연결된 원본 요소의 색인 조각 전체 포함 여부를 예산 내에서 제공한다. 색인 조각 일부가 빠진 경우에만 화면에 별도 안내한다. 검색 결과를 필터링하거나 확정 조회로 자동 전환하지 않는다. 확정 범위 계산은 `xlsx_full_table` 또는 XLSX 조회 API에서 사용자가 확인한 표를 지정해 수행한다. 모델 답변의 완전성이나 실제 과거 검색 로그의 복원까지 보장하지 않는다.

관련 이미지 메타데이터는 검색 결과의 `related_images`에서 확인한다. 이미지 분석 청크는 최대 2개·4,000자까지 별도 근거로 추가될 수 있으며 `expansion_reason=related_image_analysis`와 관계의 출처·확인 상태가 제공된다. 작업의 `GET /v1/ingest/{document_id}/jobs/{job_id}/images`와 요소별 `.../elements/{element_id}/image`는 동일 인증을 요구한다. 이미지 원본은 작업 삭제 시 함께 삭제된다.

`POST /v1/ingest/{document_id}/jobs/{job_id}/images/actions`는 이미지 요소 ID를 `analyze`, `original`, `both`, `exclude`, `reuse`, `describe`에 매핑한 `actions`, `describe`에 필요한 요소별 `descriptions` 및 선택적 `embedding_profile_id`를 받는다. 최대 100개를 선택할 수 있다. `reuse`는 저장된 이미지 분석 결과가 있을 때만 허용하며 외부 분석을 재호출하지 않는다. 미게시 검토 작업이면 같은 작업에 병합하고, 이미 게시된 작업이면 미게시 새 작업으로 분기한다. `describe`는 1~2,000자의 사용자 설명과 원본을 직접 연결한다. 본문 연결이 없는 원본은 설명을 입력해야 한다. 모든 확인 필요 이미지의 처리를 선택한 뒤 명시적으로 게시한다. 검색에서 제외된 분석 결과·관계·원본은 산출물로 유지한다.

세션의 변경 요청은 Origin과 CSRF 토큰을 검증한다. iframe bearer 인증은 서버 설정 변경을 허용하지 않는다. 오류는 `error.code`, `message`, `request_id`를 제공한다. [인증과 iframe](../user-guide/03-integration.md)


## XLSX 구조 계약

- `GET /v1/ingest/{document_id}/jobs/{job_id}/xlsx-structure`는 시트 목록을 반환한다. `sheet`를 지정하면 해당 시트의 영역, 검증 상태, 병합 범위와 제외 이유를 반환한다. `GET .../xlsx-cells`는 시트의 셀 격자 일부를 반환한다. 창은 최대 25행 × 20열이며 셀 미리보기는 200자로 제한한다. `address`로 개별 셀의 전체 값과 다른 시트의 정적 A1 참조를 본다. 외부 통합 문서, 이름 범위, 동적 참조는 해석 범위에서 제외한다.
- 구조 조회 경로에 `POST`로 시트 이름을 키로 하는 수정안을 보낸다. 시트마다 `regions`와 `excluded`를 지정한다. 영역에는 `start`, `end`, `kind`, `headers`, `title`, `uncertain`이 있고, 제외 목록은 셀 주소에서 이유 문자열로 매핑한다. `headers`는 데이터 위의 연속된 최대 5개 행에 있는 헤더 셀 전체다. 병합된 헤더는 병합 범위의 열을 덮고, 아래 행의 헤더가 이를 세분화한다. 열마다 가장 아래 헤더는 최대 하나이며, 헤더가 없는 열은 라벨 없이 남긴다. 헤더 행에 헤더가 아닌 값이 있으면 거부한다. 서버는 좌표, 병합, 선언된 Excel 표, 헤더와 셀 할당을 검증한 뒤 새 작업을 등록한다. `POST .../xlsx-structure/reset`에 `{"sheets": [...]}`를 보내면 해당 시트의 검토를 버리고 로컬 구조 분석을 적용한 새 작업을 등록한다.
- 다시 읽기는 기준 작업의 검토를 새 원본에 다시 검증한다. 맞지 않는 검토는 작업을 실패시키지 않고 시트의 `stale_review`(검토 내용과 이유)와 작업의 `xlsx_review_stale`에 남기며, 해당 시트는 추정 구조로 처리한다. 적용된 검토는 원본 구조 지문 `xlsx_review_basis`와 함께 유지한다. 기준 작업의 검토 이력은 바뀌지 않는다.
- 영역의 `verification` 값은 `explicit` (Excel 등록), `user_verified` (사용자 확인), `llm_validated` (모델 제안의 구조 검사 통과), `inferred` (로컬 추정)이다. 앞의 두 상태가 전체 표 조회 대상이다. 이 규칙은 이미 게시된 v2 결과에도 적용된다.
- `GET /v1/ingest/{document_id}/xlsx-tables`로 게시된 버전에서 조회할 수 있는 표와 열을 확인한다. `POST /v1/xlsx-query`는 `document_id`, 시트 `sheet`, 표 시작 주소 `start` 및 `operation`을 받는다. 연산은 `count`, `distribution`, `date_range`, `rows`, `sum`, `average`, `min`, `max`다. 분포·날짜 범위·수치 집계는 `column`을 사용한다. 개수와 수치 집계에 `group_by`를 지정할 수 있다. 헤더와 선언된 합계 행은 집계 대상에서 빠지며 누락된 계산값과 제외 셀은 결과에 표시한다. 합계 행 수는 Excel 표의 `totalsRowCount`만 따른다. `totalsRowShown`만 있는 표는 마지막 행을 데이터로 유지한다. 개수가 표 범위와 맞지 않으면 `totals_rows_inconsistent`로 표시한다. 합계 행 근거를 확인할 수 없는 이전 산출물은 그 행을 범위에서 빼지 않고 `scope.uncertain_totals_rows`에 기록한다. 조회에 그 행이 필요하면 판정 불가(`totals_row_uncertain`)로 두어 결과를 불완전으로 표시하며, 필터가 이미 그 행을 제외하면 결과에 영향이 없다. 열 매핑 조인에서는 그 행을 키 고유성 검사와 결합에서 빼고 `join.uncertain_totals_rows`에 기록한다. 그 행의 키가 고유해야 하는 쪽의 다른 키와 겹치면 `join.uncertain_key_conflicts`로 표시한다. 확실한 데이터 행끼리의 키 중복은 이전처럼 거부한다. 데이터 행의 한 열 세로 병합은 그룹 라벨로 보고 병합 범위의 행에 원본 값을 적용한다. 응답의 `merged_from`과 `merged_group_cells`로 확인한다. 청크에서는 병합 범위 안의 모든 셀이 그룹 관계를 가지며, 그룹 문맥 텍스트는 청크마다 해당 행의 첫 셀에 한 번 쓴다. 청크 크기 판정도 후보 청크마다 문맥을 한 번만 계산하며, 임베딩 입력 한도로 청크를 다시 나누면 나뉜 청크마다 다시 붙인다.
- 필터는 판정을 참·거짓·판정 불가로 나눈다. 빈 셀은 같음·범위·포함 조건을 만족하지 않으며, `ne`에서는 판정 불가(`blank_filter_value`)로 남긴다. 필터의 `blanks`를 `include` 또는 `exclude`로 지정하면 빈 셀을 명시적으로 처리한다. 숫자는 정수는 정확히, 실수는 Excel의 15자리 유효숫자로 비교한다. 문자열 같음 비교는 정확히 일치해야 하며, 대소문자·전각·앞뒤 공백만 다른 값은 판정 불가(`text_variant`)로 둔다. 판정 불가 행이 있으면 `complete`는 거짓이다. `xlsx-tables`의 `column_formats`는 저장값 종류를 알린다. 예를 들어 `percent_fraction`은 25%를 0.25로 저장한다.
- 조인 집계의 `aggregate_unit`은 `auto`, `pairs`, `left_rows`, `right_rows`다. `pairs`는 결합된 행 쌍마다, 행 단위는 원본 행마다 한 번 집계한다. 행 단위에서 반대쪽 값이 여럿이면 `multiple_join_values`로 표시한다. `auto`는 행 쌍으로 집계하되, 같은 원본 값이 여러 쌍으로 합계·평균·분포에 반복 반영되면 결과를 확정하지 않는다. 이때 `value`와 `distribution`은 비우고, 행 쌍 기준 값을 `pair_based_value`·`pair_based_distribution`에 따로 담으며, `aggregate_unit_required`와 `duplicated_join_values`를 표시한다. `pairs`를 명시하면 행 쌍 기준 결과를 그대로 반환한다. 응답의 `result.aggregate_unit`에 사용한 기준을 기록한다.
- `GET /v1/ingest/{document_id}/xlsx-join-candidates`는 고정된 `index_version`과 `sheet`, `start`, `right_sheet`, `right_start`를 받는다. 조회에 승인한 후보는 `POST /v1/xlsx-query`의 `join`에 `sheet`, `start`, `approved_pairs`로 지정한다. 각 쌍은 `left_row`와 `right_row`를 담는다. 한 요청에서 승인한 쌍만 내부 결합한다. 중복 식별자, 이름 충돌 및 원본과 다른 연결은 거부한다. 필터의 `side`와 집계 열의 `column_side`, 그룹 열의 `group_side`로 좌우 표를 지정한다. 응답에는 미결합·미승인 건수, 조건에 맞은 행 쌍 및 양쪽 원문 범위가 포함된다.
- 열 기반 조인은 `POST /v1/xlsx-join-preview`에서 확인한다. `document_id`, 고정된 `index_version`, 왼쪽 표의 `sheet`·`start`, 오른쪽 표의 `join.sheet`·`join.start`를 지정한다. `join.keys`는 최대 네 개의 `{left, right, type}`으로 구성되며, 타입은 `text`, `number`, `date`, `datetime`, `boolean`이다. 같은 `join`을 `POST /v1/xlsx-query`에 쓰면 일치하는 모든 행을 내부 결합한다. 관계 `cardinality`는 `one_to_one`, `one_to_many`, `many_to_one` 중 하나이며 고유해야 하는 쪽에 중복 키가 있으면 조회를 거부한다. 텍스트에는 숫자 변환이나 공백 제거를 적용하지 않는다. 빈 키, 타입 불일치, 누락된 계산값과 제외 셀은 결합할 수 없는 이유와 행 수를 응답에 기록한다. 미결합 행 수와 양쪽 원본 행 주소도 반환한다.
- `xlsx_structure_version=2`에서 셀의 OOXML 타입과 값, 변환된 타입, 수식, 캐시 상태·값 및 표시용 텍스트를 따로 보존한다. 비어 있는 문자열 캐시와 누락된 캐시는 구분한다. 값 없이 서식만 있는 셀은 셀 기록 대신 같은 스타일 사각형 목록 `style_ranges`로 보존한다. 새 XLSX 청크는 `xlsx-structure-v7`, 공통 HTML 표 청크는 `table-structure-v17`로 표시한다. 확정되지 않은 영역도 원본 셀의 추정 헤더·제목을 검색 문맥으로 쓴다. 이때 라벨 관계는 `unverified`, 청크의 `xlsx_context_status`는 `unverified`로 남고 전체 표 조회 대상이 되지 않는다. HTML 표의 원문 근거에서 줄바꿈과 들여쓰기를 보존하고, 검색값과 헤더 라벨에는 공백을 정규화한 표현을 쓴다. 기존 게시 색인과 새 청크 적용 방법은 저장·운영 문서에 설명한다.

공통 HTML 표의 새 청크는 `table_segments`에 검색용 값을 두고 `source_segments`에는 해당 셀의 원문 조각과 위치를 기록한다. 검색 결과의 근거 URL은 `table_cells=row:column:start:end`를 사용한다. 행·열은 1부터 시작하며, 오프셋은 보존된 셀 텍스트에서 0부터 시작하는 문자 인덱스다. 끝 오프셋은 범위에 포함되지 않는다. 지원되는 표의 셀 범위는 임베딩 단계에서 다시 분할될 때도 갱신된다. 기존 게시 청크와 셀 배치를 확인할 수 없는 표의 근거는 요소 단위로 조회한다.

빈 HTML 표에 인접한 캡션만 검색되는 경우 캡션 요소가 원문 근거이며, 빈 표 요소는 문맥으로 연결된다.

표 청크의 `label_relations`는 라벨 셀의 좌표·요소, 관계 유형 및 출처·검증 상태를 기록한다. 추정 관계는 검색에 사용하되 답변에서 확정 관계와 구분하며, 이전 청크에 이 정보가 없으면 관계는 미확인이다. 새 표현은 새 작업으로 재색인할 때 적용된다.

PDF 물리쪽을 명시한 `PDF 9쪽` 또는 `PDF 9쪽과 5쪽` 검색·대화는 지정된 문서와 검색 버전의 해당 쪽 청크를 직접 대상으로 한다. 문서를 지정하지 않았고 PDF가 여러 개면 선택을 요청한다. 페이지 내 BM25로 후보를 선택하며 요청한 재순위가 있으면 한 번 적용한다. `page_scope`는 요청·청크 존재·선택된 쪽과 추출 완전성 미확인을 표시한다. `GET /v1/ingest/{document_id}/pdf-pages?pages=9&pages=5&query=...&index_version=...`도 제공한다. 검색 가능한 청크가 없다는 것은 원본에 내용이 없다는 뜻이 아니다. 인쇄 쪽 번호는 물리쪽으로 자동 변환하지 않는다.

### 근거 범위 회귀 검증

기본 CI는 서로 다른 합성 표·목록 및 여섯 항목이 세 청크로 나뉜 경우를 검사한다. 실제 Q13 원본 내용은 저장소에 포함하지 않으므로 기본 CI 결과로 실제 문서의 전달 여부를 보장하지 않는다. 별도 환경에서 증거 ZIP을 안전하게 준비한 후 `POC_Q13_EVIDENCE_ZIP=/path/to/evidence.zip REQUIRE_POC_Q13_EVIDENCE=1 pytest tests/test_table_expansion.py::test_external_q13_evidence_reaches_model_without_copying_source_into_repository`로 원본 작업 청크와 모델 입력을 대조한다. 강제 변수 없이 ZIP이 없을 때는 테스트를 건너뛰며, 강제 변수를 설정하면 누락 시 실패한다. 이 검사는 검색·확장·입력의 재현이며 실제 생성 답변의 완전성이나 과거 요청에서 누락된 단계는 보장하지 않는다.

일반 대화에서 같은 텍스트 요소의 추가 조각은 검색·표·이미지 근거와 해당 근거의 해석 문맥과 표 라벨 관계를 담은 다음 남는 예산에 전체 조각만 추가한다. 공간이 부족하면 추가 조각을 생략하고 범위 미확인을 보고한다. 이 순서는 모든 질문에 적용되며 질의 문구에 따른 추정 분류는 하지 않는다.

### 대화 답변의 근거 계약

일반 RAG 대화는 답변 모델에 질문의 대상·범위, 근거별 대상·값·단위·조건, 변화의 시작과 끝, 전체 목록의 누락, 부재와 직접 확인한 공란, 인용과 주장의 관계를 검토하도록 요청한다. 검색 근거와 확인된 Excel 직접 조회를 구분하며, 추론과 근거가 부족한 범위도 설명하도록 한다. 이 지시는 의미적 정확성이나 항목 누락 방지를 보장하지 않는다.

`answer_verification`은 `citation_identifiers_only`로 인용 번호가 전달된 근거 범위 안에 있는지만 확인한다. 유효한 번호나 경고가 없는 상태는 주장과 인용의 의미적 일치를 검증했다는 뜻이 아니다. 의미적 답변 품질은 별도의 검색 단계 추적 및 평가가 필요하다. 확인된 Excel 표 직접 집계는 기존 직접 조회 경로를 사용한다.

### 관리자 검색·대화 진단

`POST /v1/diagnostics/retrieve`와 `POST /v1/diagnostics/chat`은 설정 관리와 같은 세션·CSRF 권한을 요구하며 iframe 토큰을 허용하지 않는다. 각각 기존 `/v1/retrieve`, `/v1/ingest-chat` 요청 필드에 선택적 `probes`를 받는다. probe는 `document_id`, 물리 `page`, 선택적 `row`·`column` 및 `contains_line` 문자열 배열이다. 반환된 결과에는 임의의 `trace_id`, 고정 색인 버전, BM25 채널·dense·결합·재순위·선택 결과의 청크 ID/순위/점수, 표 확장 결정, 모델 입력 청크와 인용 번호, 예산·개수 제한 사유, 답변에 등장한 유효 인용 번호가 담긴다. 실행하지 않은 단계는 기록하지 않는다. PDF 물리쪽 조회는 별도의 쪽 내 검색이므로 일반 dense/RRF 단계가 없다. 확인된 Excel 직접 조회는 `direct_xlsx`에 계획·완료 상태를 기록하며 청크 기반 경로와 구분한다.

probe 결과는 게시 청크에서의 후보 일치, 반환·모델 입력에서의 일치와 답변 전체의 문구 포함 여부를 구분한다. 모델 입력은 실제 전송한 근거 JSON의 `text`, `context`, 구조화 메타데이터를 구분해 `model_input_matches`에 필드명과 청크 ID만 표시한다. 답변은 문장 번호별 인용 번호와 청크 ID 및 probe가 문자 그대로 일치한 문장 번호만 제공한다. 이 매핑은 의미적 지지 여부를 판정하지 않는다. 입력과 답변 원문은 요청 처리 중에만 사용하고 진단 응답에 포함하지 않는다. 반환·모델 입력 검사는 예산으로 잘린 실제 텍스트를 대상으로 하며, 후보 단계는 게시 청크의 페이지·셀 메타데이터를 사용한다. 행·열이 지정된 probe는 셀의 위치와 모델에 전달된 표 본문이 서로 일치하고 중복 없이 연결되는 경우에만 본문 전달을 확인한다. 연결이 불명확하면 `cell_model_input_unverified_chunk_ids`에 표시하며, 잘린 청크는 기존 `truncated_cell_match_unverified_chunk_ids`에도 표시한다. 답변 일치는 문구 포함 여부일 뿐 대상·관계·의미 또는 실제 인용의 정확성을 검증하지 않는다. 진단은 기존 검색 및 모델 호출을 추가 재실행하지 않으며, 서버에 별도 추적 파일을 쓰지 않는다. 응답과 평가 보고서에는 질문·근거·답변 본문이나 인증키를 포함하지 않지만, 진단 요청 자체에는 질문 및 probe 문구가 전송되므로 프록시·접근 로그 설정과 보고서의 청크 식별자 접근 권한을 관리해야 한다.

`GET /v1/diagnostics/artifacts/{document_id}/{job_id}?index_version=...`는 같은 작업의 초기·최종 파서 요소, 정규화 요소, 작업·게시 청크의 가용성·개수 및 위치 기준 변화 수와 청크 ID 차이를 반환한다. 정규화 전후 값 비교는 원본 `content.text`와 정규화 `text`의 문자 비교이며 의미적 변경 판정은 아니다. 게시 버전 또는 파일이 없으면 `published_snapshot_unavailable`, 문서는 있으나 다른 작업이 게시된 것이 확인되면 `job_not_in_snapshot`, 이전 형식에 작업 ID가 없으면 `job_membership_unknown`을 반환한다. 저장되지 않은 옛 검색·모델 입력이나 원문을 다른 자료로 추정해 복원하지 않는다.

`scripts/evaluate.py CASES --trace search --index-version UUID`는 진단 검색을 각 평가 사례당 한 번 실행하고 색인 버전을 고정한다. `--trace chat`은 사례당 답변 모델을 실제 한 번 호출하므로 비용이 발생하며, `--rerank`는 서버 재순위 설정을 사용한다. chat 진단의 기존 `recall`은 확인 가능한 인용 근거 일치의 하한이며 잘린 셀을 누락으로 확정하지 않는다. 이와 별도로 `model_input_recall`은 본문·해석 문맥의 문자 일치만 집계하며 `model_text_recall`, `model_context_recall`과 `model_metadata_recall`을 각각 제공한다. 셀 위치를 모델 텍스트에 연결할 수 없으면 본문 전달률은 `unknown`이고, 메타데이터 검사 자체가 적용되지 않은 경우 별도 지표는 `null`이다. 답변의 의미적 정답 여부는 별도 검토가 필요하다.

### 검색 대상과 대화 이력

BM25는 복합 숫자·영숫자 식별자를 별도로 보존해 정확히 일치하는 후보를 우선한다. 기존 토큰·부분문자열 경로와 색인은 유지하며, 오래된 토큰 전용 색인에서도 식별자 토큰 구문을 사용한다. 한국어 표현 차이에 대한 부분일치는 두 개 이상의 trigram 근거를 요구하고 정확 일치보다 낮은 비율로 일치 범위를 평가해 다른 후보와 함께 순위를 매긴다. 느슨한 유사 후보도 결과에 포함될 수 있으므로 실제 질의로 품질을 평가해야 한다. 이 보완은 부분문자열 색인이 없는 구버전에는 적용되지 않는다. 검색 후보 수와 hybrid RRF 설정은 변경하지 않는다.

대화 검색은 현재 질문을 기본으로 한다. 지시어 또는 대상이 생략된 짧은 후속 질문은 최근 사용자 질문 최대 두 개와 최근 답변 일부를 보완하며, 번호 목록의 순서 지칭을 유일한 항목에 연결하면 해당 항목만 검색 문맥으로 사용한다. 항목이 중복되거나 복수 지칭이면 기존 문맥을 유지한다. 짧은 단독 질문은 대상이 생략됐을 수 있으므로 모호할 때 기존 문맥을 사용한다. 질문 안의 앞선 내용에 명시적으로 연결되는 지시어는 이전 이력을 덧붙이는 근거로 사용하지 않는다. 이 결정은 제한적인 휴리스틱이므로 모든 문맥이나 의미적 선행 대상을 정확하게 판별하지는 못한다. 서로 다른 선행 대상이 여러 개라 판정하기 어려우면 추론을 확정하지 않고 현재 질문으로 검색한다. 진단의 `retrieval_query`에는 참조별 판정 상태(`local`, `external`, `ambiguous`), 이력 존재·선택·실제 사용 여부와 질의 크기만 기록하고 질문과 이력 원문은 기록하지 않는다. 답변용 이력은 최근 최대 50개를 받을 수 있고 최신 대화를 우선한다. 운영자는 사용 가능한 모델 중 가장 작은 문맥 창을 `llm_context_tokens` 또는 `LLM_CONTEXT_TOKENS`로 설정한다(기본 131072). 출력용으로 문맥 창의 4분의 1 또는 최대 4096 토큰과 요청 여유를 남기고 시스템 지시, 현재 질문, 검색 근거, 이력을 함께 계산하며 이력에 별도 256 KiB 상한도 적용한다. 입력 크기는 UTF-8 바이트를 토큰의 보수적 대용으로 사용하므로 모델별 토크나이저와 공급자 오버헤드에 따른 실제 적합성을 보장하지 않는다. 이전 답변에서 확인된 인용 번호는 현재 근거 번호와 혼동되지 않도록 제거하며, 그 밖의 숫자 괄호는 일반 텍스트로 보존한다. 예산으로 이력이 생략되면 `search_counts.history_omitted_count`에 표시한다. 바이트 예산은 모델의 토큰 창 보장을 뜻하지 않는다.

서버 설정 `chat_query_rewrite_enabled`(환경 변수 `CHAT_QUERY_REWRITE_ENABLED`, 기본 꺼짐)을 켜면 이력이 있는 대화에서 검색 전에 선택된 대화 모델을 한 번 더 호출한다. 최근 최대 6개 메시지를 최대 8 KiB까지 전송하며 추가 지연과 비용이 발생한다. 독립 질문은 원문을 유지하고, 후속 질문은 모델이 이력에서 찾은 원문 대상과 재작성 질의를 제안한다. 코드에서 대상 출처, 숫자·단위, 영문 식별자, 질문의 주요 문자열과 길이를 확인한다. 모델 오류·잘못된 JSON·검증 실패·외부 처리 비활성화 시 검색 질의는 원문으로 돌아간다. 이 검사는 모든 의미 변경을 판별하지 못하므로 재작성의 정확성을 보장하지 않는다. 재작성 상태는 대화 응답의 `search_counts.query_rewrite_status`와 진단에 기록하며, 실패 시 화면에서 원문 검색 사실을 안내한다. 설정을 끄면 추가 모델 호출 없이 기존의 보수적인 질의 구성을 사용한다.

단일·쉼표 목록·범위 인용은 동일한 파서로 검증하고 유효한 개별 인용으로 표시한다. 잘못된 번호·역순·과도한 범위는 근거 확인 불가로 표시한다. 이는 번호 검증이며 인용과 주장의 의미적 일치 검증이 아니다. 제목 경로의 각 계층은 기존 바이트 한도에서 나누어 보존한다. 새 표현 버전은 재청킹한 작업에만 적용되며 기존 게시 버전은 변경되지 않는다.

이 변경의 합성 비교에서는 동일 구성 토큰을 가진 `1-2-1`과 `1-1-2` 중 기존 lexical 결과가 식별자와 다른 항목을 먼저 반환하는 사례를 재현했고, 새 경로는 정확한 식별자를 우선했다. 조사 결합 입력에서도 기존 lexical 후보가 없던 사례에 원형 및 띄어쓰기 변이 후보를 보완했다. 긴 다단계 제목은 기존 중간 절단에서 가운데 계층이 빠지는 것을 재현했다. 해당 결과는 합성 회귀에 한정되며, Q5·Q9·Q11 당시의 게시 색인과 단계별 기록이 없어 과거 오답의 원인이나 실제 전후 검색 성능을 확정하지 않는다.
