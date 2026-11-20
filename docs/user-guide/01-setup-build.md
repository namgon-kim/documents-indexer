# 설정·실행·백업

## 로컬 실행

Python 3.12, uv, Node.js 22.12 이상과 npm이 필요하다. 저장소에서 실행한다.

```sh
make setup
```

`.env`는 없을 때 예제에서 생성된다. `PARSER_PROVIDER`, `PARSER_BASE_URL`, `PARSER_API_KEY`, `EMBEDDING_BASE_URL`, `EMBEDDING_API_KEY`와 `ALLOW_EXTERNAL_PROCESSING=1`을 설정한다. 대화에는 `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`도 필요하다. 프록시를 사용할 수 있으며 각 주소는 해당 파서·임베딩·Chat Completions API 계약을 제공해야 한다.

```sh
make build
make run
```

`make run`은 기본적으로 `127.0.0.1:8000`에서 FastAPI와 빌드된 웹을 제공한다. 웹 개발 서버를 사용하면 별도 터미널에서 `npm run dev --prefix apps/web`을 실행한다. 이 서버의 `/v1`과 `/health`는 기본적으로 로컬 8000 포트로 전달된다.

`QDRANT_URL`이 비어 있으면 `DATA_DIR/qdrant`에 로컬 Qdrant를 사용한다. 원격 Qdrant는 `QDRANT_URL`과 필요 시 `QDRANT_API_KEY`를 지정한다. 주소에서 스킴을 생략하면 HTTP이며 HTTPS는 명시한다. 첫 검색 색인 게시 시 컬렉션을 자동 생성한다.

웹 설정은 환경 변수를 기본값으로 사용하고 저장된 설정을 우선한다. 연결·처리 설정은 진행 중인 문서 처리가 끝난 뒤 적용된다. 데이터 및 웹 경로는 서버 시작 설정으로 관리한다. [설정 상세](../implementation/05-processing-settings.md)

## Docker Compose 배포: 기존 Qdrant 사용

정식 Compose는 FastAPI와 빌드된 웹을 한 컨테이너에서 실행한다. 서버에서 이미 실행 중인 Qdrant 1.19.1은 별도로 유지하며 이 Compose는 Qdrant 컨테이너나 저장소를 생성·변경하지 않는다. 별도의 웹 서버나 Nginx는 필요하지 않다.

서버에 저장소와 별도로 준비한 설정 파일을 옮긴다. 설정 파일에는 비밀키가 포함되므로 Git에 올리지 않는다. `pipeline/` 안의 설정 파일은 해당 경로에 기존 파일이 없는 새 배포에서만 복사한다. 기존 파일이 있다면 백업 후 내용을 비교해 필요한 값만 반영한다.

```sh
cp /path/to/deployment-settings/.env.production .env.production
chmod 600 .env.production
install -d -m 700 ./data/production/pipeline
cp -n /path/to/deployment-settings/pipeline/*.json ./data/production/pipeline/
chmod 600 ./data/production/pipeline/*.json
```

`.env.production`의 `PUBLIC_ORIGIN`은 브라우저가 접속하는 HTTPS 출처(경로 제외), `INDEXER_ACCESS_KEY`는 로그인에 사용할 값으로 설정한다. 앱은 HTTPS 종료 지점 뒤에서 사용한다. `INDEXER_UID`, `INDEXER_GID`를 호스트 데이터 디렉터리 소유자에 맞춘다. 기본 앱 바인딩은 `0.0.0.0:8000`이며 필요하면 `APP_BIND_ADDRESS`와 `APP_PORT`를 바꾼다. iframe을 쓰는 경우에만 출처와 서명 키를 설정한다.

기본 `QDRANT_URL=http://host.docker.internal:6333`은 **같은 서버**에서 기존 Qdrant가 호스트의 6333 포트로 공개된 경우에 사용한다. 포트가 다르거나 다른 서버라면 실제 주소를 입력한다. `QDRANT_API_KEY`는 기존 Qdrant에 설정된 값과 일치해야 하며 인증이 없다면 비운다. 컨테이너의 `localhost`는 호스트가 아니다. `host.docker.internal`은 Compose가 `host-gateway`로 연결한다. [Docker 네트워크 설명](https://docs.docker.com/compose/how-tos/networking/)

```sh
docker compose --env-file .env.production -f docker-compose.prod.yml config -q
docker compose --env-file .env.production -f docker-compose.prod.yml up -d --build --wait
docker compose --env-file .env.production -f docker-compose.prod.yml ps
```

웹과 API는 같은 포트를 사용한다. `/health`는 앱 프로세스 상태이며 Qdrant 및 외부 모델 연결 성공을 보장하지 않는다. 기존 Qdrant 대시보드는 서버의 기존 포트에서 확인한다. 검색 버전별 컬렉션은 게시 시 자동 생성된다.

[정식 Compose](../../docker-compose.prod.yml) · [환경 변수 예제](../../.env.production.example) · [웹 사용법](02-web-usage.md) · [iframe 연동](03-integration.md)

## 데이터와 백업

앱의 `data/production/pipeline/`에는 원본, 작업 상태, 파싱·청크 결과, 검색 버전 메타데이터, BM25 인덱스와 임베딩 캐시 및 웹 설정이 저장된다. 기존 Qdrant의 벡터 데이터는 **기존 컨테이너가 사용 중인 별도 볼륨**에 그대로 있다. 컨테이너를 재생성해도 같은 디렉터리와 Qdrant를 사용해야 한다.

설정 파일만 복사하면 기존 문서와 검색 버전은 복사되지 않는다. 기존 검색을 이어서 사용하려면 로컬의 해당 `DATA_DIR` 전체(원본·검색 메타데이터 포함)와 그에 대응하는 Qdrant 데이터가 필요하다. 실행 중인 데이터 디렉터리를 두 앱이 동시에 사용해서는 안 된다. 브라우저에 저장된 임베딩 모델 선택은 서버 설정 파일에 포함되지 않으므로 서버 접속 후 선택한다.

백업 시 앱 쓰기를 멈추고 앱 데이터와 기존 Qdrant를 같은 시점으로 보존한다. Qdrant 스냅샷 또는 기존 Qdrant의 백업 절차를 사용하며, 앱 데이터와 설정 파일도 별도로 보관한다. 파일 단위로 Qdrant 저장소를 복사할 때는 Qdrant의 종료 및 복원 절차를 따른다. Compose의 `down -v`나 새 볼륨 생성은 백업이 아니다. [Qdrant 스냅샷 안내](https://qdrant.tech/documentation/concepts/snapshots/)

복원 시에는 대응하는 앱 데이터와 Qdrant 백업을 함께 복구하고, 같은 임베딩 모델 설정과 Qdrant 연결을 사용한다. 기존 디렉터리를 덮어쓰기 전에 별도로 보존한다.
