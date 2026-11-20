# 저장·인덱스·운영

## 저장 경계

`DATA_DIR`에는 인증 코드 DB, 임시 업로드, 원본 문서, 작업 상태와 파생 파일, 검색 버전 및 임베딩 캐시가 저장된다. 기본 경로는 `data`다.

```text
DATA_DIR/
  auth.sqlite3, instance.lock, server-settings.json, embedding-profiles.json
  staging/                                  # 업로드 임시 파일
  ingestion/documents/<document_id>/
    source.<format>, document.json
    jobs/<job_id>/processing.json           # 상태·설정·범위
    jobs/<job_id>/parser_result.json, parser_elements.json, elements.json, chunks.json
    jobs/<job_id>/parse_attempts.json, source_details.json, provider/
  ingestion/search-indexes/
    active*.json, deleted-documents.json, embedding-cache.sqlite3
    <index_version>/manifest.json, chunks.json, bm25.sqlite3
  qdrant/                                   # QDRANT_URL이 비어 있을 때만
```

실제 생성 파일은 작업 단계와 문서 형식에 따라 달라진다. 원본은 임시 업로드에서 등록 디렉터리로 복사하고 해시를 기록한다. 설정과 모델 파일에는 비밀값이 포함될 수 있다.

## 색인과 삭제

검색 버전마다 `ingestion_<하이픈 없는 버전 UUID>` Qdrant 컬렉션, BM25 파일과 청크·manifest를 만든다. 완성된 색인만 활성 포인터로 게시한다. 문서 추가도 선택 프로필의 전체 검색 버전을 새로 생성하며, 같은 임베딩 계약과 텍스트의 벡터는 캐시에서 재사용한다. 실패하면 기존 활성 버전은 유지된다. 임베딩은 게시 기준 버전의 청크 스냅샷을 확보한 후 잠금 밖에서 수행한다. 게시 직전 활성 버전·임베딩 계약·삭제 상태가 달라졌는지 검사하며, 달라진 경우 기존 버전을 덮어쓰지 않는다. 검색과 원문 근거 조회는 문서 삭제와 동시 실행되지 않도록 보호한다.

문서 삭제는 검색 제외 표시 후 모든 버전의 해당 문서 벡터·BM25·청크와 서버 원본·작업 파일을 지운다. 실패하면 삭제를 다시 시도할 수 있다. 별도 백업, Qdrant 스냅샷, 외부 서비스 보관 결과는 별도로 관리해야 한다. 검색 버전에서 참조 중인 작업은 개별 삭제할 수 없다. 게시된 작업의 파싱 재개는 새 작업 ID에서 이뤄져 기존 버전이 참조하는 요소를 보존한다.

## 실행과 보존

개발용 Compose는 Qdrant만 활성화하며 기본 HTTP 바인딩은 `0.0.0.0:6333`이다. 정식 Compose는 FastAPI와 빌드된 웹을 실행하며 이미 실행 중인 Qdrant에 연결한다. 앱과 Qdrant의 데이터는 각각 기존 호스트 디렉터리와 Qdrant 볼륨에 보존한다. Docker 재생성만으로 스냅샷 백업이 만들어지는 것은 아니다. 파일 상태와 Qdrant를 함께 일관되게 백업해야 한다. [실행·백업 절차](../user-guide/01-setup-build.md)


서버 종료 시 실행 중인 작업이 즉시 끝나지 않아도 종료를 무기한 기다리지 않는다. 작업 스레드가 실제로 끝나기 전에는 데이터 잠금을 유지한다. 서버가 종료되면 다음 시작 시 처리 중이던 작업의 체크포인트를 확인하며, 원격 접수 여부를 확인할 수 없는 동기 파싱은 자동으로 다시 보내지 않는다. 원격 서버의 작업 중단 여부는 별도다.
