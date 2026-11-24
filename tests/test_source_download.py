from urllib.parse import quote

from fastapi.testclient import TestClient
from indexer.artifact_store import ArtifactStore
from indexer.config import Config
from indexer.ingestion_service import IngestionService

from apps.api.main import create_app

ORIGIN = 'https://testserver'


def test_uploaded_source_downloads_with_original_name(tmp_path):
    storage = ArtifactStore(tmp_path / 'ingestion')
    upload = tmp_path / '공개 PoC 문서.pdf'
    upload.write_bytes(b'%PDF-1.4 original bytes')
    document = storage.register(upload)
    service = IngestionService(storage, None)
    app = create_app(Config(tmp_path, access_key='example', external_processing=True), ingestion_service=service)
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get(f"/v1/ingest/{document['document_id']}/source").status_code == 401
        login = client.post('/v1/auth/login', json={'access_key': 'example'}, headers={'Origin': ORIGIN})
        client.headers.update({'Origin': ORIGIN, 'X-CSRF-Token': login.json()['csrf_token']})
        response = client.get(f"/v1/ingest/{document['document_id']}/source")
        assert response.status_code == 200
        assert response.content == b'%PDF-1.4 original bytes'
        assert response.headers['content-type'] == 'application/pdf'
        assert response.headers['content-disposition'].startswith('attachment;')
        assert quote('공개 PoC 문서.pdf') in response.headers['content-disposition']
        missing = client.get('/v1/ingest/00000000-0000-4000-8000-000000000000/source')
        assert missing.status_code == 404
        assert missing.json()['error']['code'] == 'DOCUMENT_NOT_FOUND'
