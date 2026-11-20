# 문서 마트 · DocumentIndexer

PDF, HWP, HWPX, XLSX, PPTX를 등록하고 검색 및 문서 기반 대화에 사용하는 단일 테넌트 PoC입니다. Upstage Document Parse로 문서를 읽고, LangGraph가 처리 단계를 연결합니다. 문서 검색은 BM25·Dense·Hybrid를 선택하며 Qdrant에 벡터를 저장합니다.

## 시작하기

Python 3.12, `uv`, Node.js 22.12 이상과 npm이 필요합니다. 저장소에서 다음을 실행합니다.

```sh
make setup
```

생성된 `.env`에 파서와 임베딩 연결을 설정하고 `ALLOW_EXTERNAL_PROCESSING=1`로 변경합니다. 문서 대화에는 LLM 연결도 필요합니다. `QDRANT_URL`을 비우면 `DATA_DIR` 아래 로컬 Qdrant를 사용합니다.

```sh
make build
make run
```

[웹 화면](http://127.0.0.1:8000/)에서 문서를 등록합니다. [API 문서](http://127.0.0.1:8000/docs) · [상태 확인](http://127.0.0.1:8000/health)

- [설정·실행·백업](docs/user-guide/01-setup-build.md)
- [문서 관리·검색·대화 사용법](docs/user-guide/02-web-usage.md)
- [iframe 연동·검색 평가](docs/user-guide/03-integration.md)
- [설계 원칙](initial_design.md) · [외부 구성요소](external_tools.md) · [구현 문서](docs/implementation/README.md)

검사는 `make check`로 실행합니다. 실제 외부 서비스 사용에는 해당 연결과 데이터 전송 설정이 필요합니다.
