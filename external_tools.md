# 외부 구성요소와 연결 계약

실제 적용값은 환경 변수와 서버 설정에서 결정한다. 연결 가능 여부, 모델 사용 권한, 데이터 처리 조건과 대상 문서의 검색 품질은 배포 환경에서 확인한다.

| 영역 | 현재 구성 | 역할과 경계 |
|---|---|---|
| 문서 파서 | Upstage Document Parse | `PARSER_PROVIDER=upstage`. 동기·장문 비동기 요청, OCR, DPI, 결과 저장. 다른 파서는 어댑터 등록이 필요하다. |
| 임베딩 | OpenAI 호환 `/embeddings` 형식 | 기본 프로필은 `solar-embedding-2-passage` / `solar-embedding-2-query`, 1,024차원. URL, 키와 모델을 별도로 설정한다. 형식이 다른 API는 별도 어댑터가 필요하다. |
| 벡터 저장 | Qdrant | 로컬 내장 또는 원격 서버. Compose 이미지는 `qdrant/qdrant:v1.19.1`이다. 벡터만으로 원본·BM25·작업 상태를 복원할 수 없다. |
| 키워드 검색 | SQLite FTS5 | 각 검색 버전의 동일 청크에 BM25를 적용한다. 한국어 형태소 분석의 품질은 별도 평가 대상이다. |
| 재순위 | Cohere v2 또는 미사용 | `RERANKER_PROVIDER=cohere` 설정 후 검색·대화에서 선택한다. 기본값은 `disabled`다. |
| 답변 모델 | OpenAI Responses API 형식 | `LLM_BASE_URL` 뒤에 `/responses`를 호출한다. 기본 모델 ID는 설정 예제 기준 `gpt-6.1-terra`이며 실제 사용 가능 여부는 연결 환경에 달려 있다. |
| 프레임워크 | FastAPI, LangGraph, LangChain Core | API, 문서 처리 그래프, 대화 검색·답변 연결에 사용한다. |
| 웹 | React, TypeScript, Vite | 독립 화면과 iframe 경로를 제공한다. 정식 이미지에서는 빌드된 정적 파일을 FastAPI가 제공한다. |

파서·임베딩·재순위·LLM의 키와 주소는 서로 독립적이다. `PARSER_BASE_URL`, `EMBEDDING_BASE_URL`, `LLM_BASE_URL`은 프록시 주소로 설정할 수 있지만 각 프록시가 해당 API 계약을 지원해야 한다. 바이트 제한은 토큰 한도가 아니다. 임베딩 모델과 토크나이저가 확실할 때만 토큰 기준 검사를 추가할 수 있다.

VLM을 이용한 별도 도식 관계 보강은 현재 구현하지 않는다. 특정 상용 모델이나 DB가 이 프로젝트의 문서에서 우수하다는 비교 결과도 아직 없다. [설정 상세](docs/implementation/05-processing-settings.md) · [평가](docs/user-guide/03-integration.md)
