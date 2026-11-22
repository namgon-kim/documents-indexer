import json
from uuid import uuid4

import httpx
import pytest
from indexer.artifact_store import ArtifactStore, atomic_json
from indexer.dense_index import EmbeddingClient, EmbeddingProfile
from indexer.index_publication import IndexPublisher
from indexer.ingestion_service import IngestionService
from qdrant_client import QdrantClient


@pytest.fixture
def publication(tmp_path):
    calls = []
    def embed(request):
        body = json.loads(request.content)
        calls.append(body['input'])
        return httpx.Response(200, json={'model': body['model'], 'data': [
            {'index': i, 'embedding': [1.0, 0.5]} for i in range(len(body['input']))]})
    with httpx.Client(transport=httpx.MockTransport(embed)) as client:
        qdrant = QdrantClient(':memory:')
        publisher = IndexPublisher(tmp_path, qdrant, EmbeddingClient(client, 'https://example.test/v1', 'key',
                                   EmbeddingProfile('p', 'doc', 'query', 2)))
        yield publisher, calls
        qdrant.close()


def test_uncommitted_snapshot_is_not_an_indexed_job(publication, monkeypatch):
    publisher, _ = publication
    doc, old_job, new_job = str(uuid4()), str(uuid4()), str(uuid4())
    def chunk(job):
        return {'chunk_id': str(uuid4()), 'document_id': doc, 'job_id': job, 'text': '본문'}
    first = publisher.publish(doc, [chunk(old_job)])
    from indexer import index_publication
    real_write = index_publication.atomic_json
    def crash(path, value):
        if path == publisher.root / publisher.active_name:
            raise KeyboardInterrupt()
        real_write(path, value)
    monkeypatch.setattr(index_publication, 'atomic_json', crash)
    with pytest.raises(KeyboardInterrupt):
        publisher.publish(doc, [chunk(new_job)])
    ghost = next(row for row in publisher.versions() if row['status'] == 'unpublished')
    assert publisher.active()['version'] == first['version']
    assert publisher.get_version(ghost['index_version']) is None
    assert publisher.job_is_indexed(doc, old_job)
    assert not publisher.job_is_indexed(doc, new_job)
    monkeypatch.setattr(index_publication, 'atomic_json', real_write)
    publisher.publish(doc, [chunk(new_job)])
    assert publisher.job_is_indexed(doc, old_job) and publisher.job_is_indexed(doc, new_job)
    assert publisher.get_version(first['version']) is not None


def test_legacy_chunks_without_job_id_are_not_proof_of_a_job(publication):
    publisher, _ = publication
    doc = str(uuid4())
    publisher.publish(doc, [{'chunk_id': str(uuid4()), 'document_id': doc, 'text': '본문'}])
    assert not publisher.job_is_indexed(doc, str(uuid4()))


def test_unknown_legacy_version_protects_known_source_without_claiming_publication(publication):
    publisher, _ = publication
    doc, job = str(uuid4()), str(uuid4())
    version = str(uuid4())
    folder = publisher.root / version
    folder.mkdir()
    atomic_json(folder / 'manifest.json', {'version': version})
    atomic_json(folder / 'chunks.json', [{'document_id': doc, 'job_id': job}])
    assert not publisher.job_is_indexed(doc, job)
    assert IngestionService._job_requires_copy(publisher, doc, job)
    atomic_json(folder / 'chunks.json', [{'document_id': doc}])
    assert not IngestionService._job_requires_copy(publisher, doc, str(uuid4()))
    assert publisher.job_may_be_referenced(doc, job)


@pytest.mark.parametrize('recovery', ['request', 'restart'])
def test_accepted_publication_recovers_after_job_status_write_fails(tmp_path, publication, monkeypatch, recovery):
    publisher, calls = publication
    store = ArtifactStore(tmp_path)
    source = tmp_path / 'sample.pdf'
    source.write_bytes(b'%PDF-1.4')
    doc = store.register(source)['document_id']
    job = str(uuid4())
    store.save_job(doc, job, 'chunks', [{'chunk_id': str(uuid4()), 'document_id': doc, 'job_id': job, 'text': '대상 내용'}])
    store.save_job(doc, job, 'elements', [])
    store.save_job(doc, job, 'processing', {'status': 'partial', 'can_accept_partial': True,
                                            'comparison_index_version': None, 'coverage': {'missing_pages': [2]}})
    service = IngestionService(store, None, publisher)
    save = store.save_job
    def fail_final(document_id, job_id, name, value):
        if name == 'processing' and value.get('index_version'):
            raise OSError('disk unavailable')
        return save(document_id, job_id, name, value)
    monkeypatch.setattr(store, 'save_job', fail_final)
    with pytest.raises(OSError):
        service.accept_content_changes(doc, job, accept_partial=True)
    version = publisher.active()['version']
    assert store.load_job(doc, job, 'processing')['publication_intent']
    monkeypatch.setattr(store, 'save_job', save)
    if recovery == 'restart':
        service.recover()
        status = store.load_job(doc, job, 'processing')
    else:
        status = service.accept_content_changes(doc, job, accept_partial=True)
    assert status['index_version'] == version and status['status'] == 'partial'
    assert not status.get('publication_intent') and publisher.active()['version'] == version
    assert calls == [['대상 내용']]


def test_shared_embedding_cache_survives_document_delete(publication):
    publisher, calls = publication
    def chunk(doc, text):
        return {'chunk_id': str(uuid4()), 'document_id': doc, 'text': text}
    publisher.publish('one', [chunk('one', '첫 문서')])
    publisher.publish('two', [chunk('two', '남은 문서')])
    publisher.delete_document('one')
    publisher.publish('three', [chunk('three', '새 문서')])
    assert calls == [['첫 문서'], ['남은 문서'], ['새 문서']]


def test_atomic_json_failure_before_replace_keeps_previous_and_cleans_temporary(tmp_path, monkeypatch):
    import indexer.artifact_store as artifacts
    path = tmp_path / 'state.json'
    atomic_json(path, {'state': 'old'})
    def failed(_):
        raise OSError('fsync failure')
    monkeypatch.setattr(artifacts.os, 'fsync', failed)
    with pytest.raises(OSError):
        atomic_json(path, {'state': 'new'})
    assert json.loads(path.read_text()) == {'state': 'old'}
    assert not list(tmp_path.glob('.writing-*'))


def test_interrupted_draft_restores_snapshot_on_restart(tmp_path):
    store = ArtifactStore(tmp_path)
    source = tmp_path / 'sample.pdf'
    source.write_bytes(b'%PDF-1.4')
    doc, job, edit = store.register(source)['document_id'], str(uuid4()), uuid4().hex
    folder = store.document_dir(doc) / 'jobs' / job
    snapshot = folder / 'draft-edits' / edit
    snapshot.mkdir(parents=True)
    atomic_json(snapshot / 'processing.json', {'status': 'partial', 'pending_review': True})
    atomic_json(snapshot / 'elements.json', [{'text': '이전'}])
    atomic_json(snapshot / 'chunks.json', [{'text': '이전 청크'}])
    store.save_job(doc, job, 'processing', {'status': 'processing', 'mode': 'draft_image_actions', 'image_snapshot': edit})
    store.save_job(doc, job, 'elements', [{'text': '중간'}])
    store.save_job(doc, job, 'chunks', [{'text': '중간 청크'}])
    service = IngestionService(store, None)
    service.recover()
    service.recover()
    assert store.load_job(doc, job, 'elements') == [{'text': '이전'}]
    assert store.load_job(doc, job, 'chunks') == [{'text': '이전 청크'}]
    assert store.load_job(doc, job, 'processing')['error']['code'] == 'DRAFT_INTERRUPTED'


def test_pending_deletion_blocks_resume_and_processing_but_can_be_retried(tmp_path, publication):
    publisher, _ = publication
    store = ArtifactStore(tmp_path)
    source = tmp_path / 'file.pdf'
    source.write_bytes(b'%PDF-1.4')
    doc, job = store.register(source)['document_id'], str(uuid4())
    store.save_job(doc, job, 'processing', {'status': 'queued', 'stage': 'queued'})
    atomic_json(publisher.root / 'deleted-documents.json', [doc])
    service = IngestionService(store, None, publisher)
    assert service.process_next() is False
    service.recover()
    with pytest.raises(ValueError, match='삭제'):
        service.resume_processing(doc, job)
    with pytest.raises(ValueError, match='삭제'):
        service.resume_index(doc, job)
    with pytest.raises(ValueError, match='삭제'):
        service.run_job(doc, job)
    service.delete_document(doc)
    assert not store.document_dir(doc).exists()


def test_worker_failure_is_visible_and_retries_with_backoff(tmp_path):
    import threading

    from fastapi.testclient import TestClient
    from indexer.config import Config

    from apps.api.main import create_app
    failed = threading.Event()
    class BrokenWorker:
        publisher = None
        storage = ArtifactStore(tmp_path)
        def recover(self):
            pass
        def process_next(self):
            failed.set()
            raise OSError('injected')
    app = create_app(Config(tmp_path), ingestion_service=BrokenWorker())
    with TestClient(app) as client:
        assert failed.wait(2)
        health = client.get('/health').json()
        assert health['status'] == 'ok'
        assert health['worker']['status'] == 'degraded'
        assert health['worker']['error_type'] == 'OSError'


def test_complete_empty_result_does_not_offer_index_resume(tmp_path):
    from io import BytesIO

    from indexer.ingestion_service import IngestionFailure
    from indexer.parsing.contracts import ParseResult
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    raw = BytesIO()
    writer.write(raw)
    source = tmp_path / 'empty.pdf'
    source.write_bytes(raw.getvalue())
    class Parser:
        def parse(self, request):
            # A page marker proves the page was read while producing no text.
            rows = [{'category': 'page_number', 'page': 1, 'content': {'text': '1'}}]
            return ParseResult('fake', rows, {'elements': rows})
    store = ArtifactStore(tmp_path)
    service = IngestionService(store, Parser())
    job = service.enqueue(source)
    with pytest.raises(IngestionFailure):
        service.run_job(job['document_id'], job['job_id'])
    status = store.load_job(job['document_id'], job['job_id'], 'processing')
    assert status['error']['code'] == 'NO_SEARCHABLE_CONTENT'
    assert status['stage'] == 'completed' and not status['can_resume']
    with pytest.raises(ValueError):
        service.resume_index(job['document_id'], job['job_id'])


def test_accepted_request_is_idempotent_after_success(tmp_path, publication):
    publisher, calls = publication
    store = ArtifactStore(tmp_path)
    source = tmp_path / 'sample.pdf'
    source.write_bytes(b'%PDF-1.4')
    doc, job = store.register(source)['document_id'], str(uuid4())
    store.save_job(doc, job, 'chunks', [{'chunk_id': str(uuid4()), 'document_id': doc, 'job_id': job, 'text': '내용'}])
    store.save_job(doc, job, 'elements', [])
    store.save_job(doc, job, 'processing', {'status': 'partial', 'can_accept_partial': True,
                                            'comparison_index_version': None, 'coverage': {'missing_pages': [2]}})
    service = IngestionService(store, None, publisher)
    first = service.accept_content_changes(doc, job, accept_partial=True)
    second = service.accept_content_changes(doc, job, accept_partial=True)
    assert first['index_version'] == second['index_version'] == publisher.active()['version']
    assert calls == [['내용']]


def test_download_timeout_is_bounded_by_remaining_deadline(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from indexer.parsing import upstage_adapter
    from indexer.parsing.contracts import ParserConfig, ParseRequest
    from indexer.parsing.upstage_adapter import UpstageParser
    now = [0.0]
    monkeypatch.setattr(upstage_adapter, 'time', SimpleNamespace(monotonic=lambda: now[0], sleep=lambda _: None))
    timeouts = []
    def handler(request):
        timeouts.append(request.extensions['timeout']['read'])
        if '/requests/' in request.url.path:
            now[0] = 7.5
            return httpx.Response(200, json={'status': 'completed', 'total_pages': 1, 'batches': [
                {'start_page': 1, 'end_page': 1, 'status': 'completed', 'download_url': 'https://downloads.example/part'}]})
        return httpx.Response(200, json={'elements': [{'page': 1}]})
    source = tmp_path / 'file.pdf'
    source.write_bytes(b'%PDF-1.4')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        parser = UpstageParser(ParserConfig('upstage', 'https://parser.example', 'key'), client,
                               remote_timeout=90, job_timeout=10)
        result = parser._parse_long(ParseRequest(source, 'pdf', page_count=1, checkpoint={'request_id': 'job'}), {})
    assert result.elements == [{'page': 1}]
    assert timeouts == [10, 2.5]


@pytest.mark.parametrize('filename', ['a\x00.pdf', '가' * 85 + '.pdf'])
def test_invalid_upload_filename_returns_4xx(tmp_path, filename):
    from fastapi.testclient import TestClient
    from indexer.config import Config

    from apps.api.main import create_app
    service = IngestionService(ArtifactStore(tmp_path), None)
    app = create_app(Config(tmp_path, access_key='example', external_processing=True), ingestion_service=service)
    with TestClient(app, base_url='https://testserver') as client:
        login = client.post('/v1/auth/login', json={'access_key': 'example'}, headers={'Origin': 'https://testserver'})
        client.headers.update({'Origin': 'https://testserver', 'X-CSRF-Token': login.json()['csrf_token']})
        if '\x00' in filename:
            body = (b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="a\x00.pdf"\r\n'
                    b'Content-Type: application/pdf\r\n\r\n%PDF-1.4\r\n--boundary--\r\n')
            response = client.post('/v1/ingest', content=body, headers={'Content-Type': 'multipart/form-data; boundary=boundary'})
        else:
            response = client.post('/v1/ingest', files={'file': (filename, b'%PDF-1.4')})
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'INVALID_FILENAME'


def test_failure_after_active_pointer_write_keeps_searchable_publication(publication, monkeypatch):
    publisher, _ = publication
    doc, job = str(uuid4()), str(uuid4())
    from indexer import index_publication
    original = index_publication.atomic_json
    def fail_after_replace(path, value):
        original(path, value)
        if path == publisher.root / publisher.active_name:
            raise OSError('directory sync failed after replace')
    monkeypatch.setattr(index_publication, 'atomic_json', fail_after_replace)
    with pytest.raises(OSError):
        publisher.publish(doc, [{'chunk_id': str(uuid4()), 'document_id': doc, 'job_id': job, 'text': '대학 교육'}], publication_id='attempt')
    assert publisher.find_publication('attempt')['version'] == publisher.active()['version']
    assert publisher.job_is_indexed(doc, job)
    assert publisher.search('대학', 'bm25')['results']


def test_incomplete_empty_result_is_not_described_as_published(tmp_path):
    from io import BytesIO

    from indexer.parsing.contracts import ParseResult
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    raw = BytesIO()
    writer.write(raw)
    source = tmp_path / 'empty.pdf'
    source.write_bytes(raw.getvalue())
    class Parser:
        def parse(self, request):
            return ParseResult('fake', [], {'elements': []}, incomplete=True, unavailable={1})
    result = IngestionService(ArtifactStore(tmp_path), Parser(), retry_pages=0).ingest(source)
    assert result['status'] == 'partial' and result['error']['code'] == 'NO_SEARCHABLE_CONTENT'
    assert not result['pending_review'] and not result['can_accept_partial'] and not result.get('index_version')


def test_restore_historical_publication_after_same_document_changes(tmp_path, publication, monkeypatch):
    publisher, calls = publication
    store = ArtifactStore(tmp_path)
    source = tmp_path / 'source.pdf'
    source.write_bytes(b'%PDF-1.4')
    doc, first_job, second_job = store.register(source)['document_id'], str(uuid4()), str(uuid4())
    store.save_job(doc, first_job, 'chunks', [{'chunk_id': str(uuid4()), 'document_id': doc, 'job_id': first_job, 'text': '이전 내용'}])
    store.save_job(doc, first_job, 'elements', [])
    store.save_job(doc, first_job, 'processing', {'status': 'partial', 'can_accept_partial': True,
        'comparison_index_version': None, 'coverage': {'missing_pages': [2]}})
    service = IngestionService(store, None, publisher)
    save = store.save_job
    def fail_final(doc_id, job_id, name, value):
        if name == 'processing' and value.get('index_version'):
            raise OSError('injected')
        return save(doc_id, job_id, name, value)
    monkeypatch.setattr(store, 'save_job', fail_final)
    with pytest.raises(OSError):
        service.accept_content_changes(doc, first_job, accept_partial=True)
    version_one = publisher.active()['version']
    monkeypatch.setattr(store, 'save_job', save)
    version_two = publisher.publish(doc, [{'chunk_id': str(uuid4()), 'document_id': doc,
                                           'job_id': second_job, 'text': '최신 내용'}])['version']
    service.recover()
    assert store.load_job(doc, first_job, 'processing')['index_version'] == version_one
    assert publisher.active()['version'] == version_two
    assert publisher.document_chunks(doc, publisher.active())[0]['job_id'] == second_job
    assert calls == [['이전 내용'], ['최신 내용']]


def test_orphan_cleanup_is_retryable_and_preserves_published_versions(publication, monkeypatch):
    publisher, _ = publication
    doc, job = str(uuid4()), str(uuid4())
    chunk = {'chunk_id': str(uuid4()), 'document_id': doc, 'job_id': job, 'text': '본문'}
    first = publisher.publish(doc, [chunk])
    from indexer import index_publication
    atomic = index_publication.atomic_json
    def crash(path, value):
        if path == publisher.root / publisher.active_name:
            raise KeyboardInterrupt()
        atomic(path, value)
    monkeypatch.setattr(index_publication, 'atomic_json', crash)
    with pytest.raises(KeyboardInterrupt):
        publisher.publish(doc, [chunk])
    orphan = next(row['index_version'] for row in publisher.versions() if row['status'] == 'unpublished')
    collection = 'ingestion_' + orphan.replace('-', '')
    delete = publisher.qdrant.delete_collection
    def fail(_):
        raise RuntimeError('temporary failure')
    monkeypatch.setattr(publisher.qdrant, 'delete_collection', fail)
    publisher.cleanup_orphans()
    assert (publisher.root / orphan).exists()
    monkeypatch.setattr(publisher.qdrant, 'delete_collection', delete)
    publisher.cleanup_orphans()
    publisher.cleanup_orphans()
    assert not (publisher.root / orphan).exists() and not publisher.qdrant.collection_exists(collection)
    assert publisher.get_version(first['version']) and publisher.active()['version'] == first['version']


def test_worker_releases_operation_lock_during_remote_parse(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from io import BytesIO

    from fastapi.testclient import TestClient
    from indexer.config import Config
    from indexer.parsing.contracts import ParseResult
    from pypdf import PdfWriter

    from apps.api.main import create_app
    entered, release = threading.Event(), threading.Event()
    class Parser:
        def parse(self, request):
            entered.set()
            assert release.wait(8)
            rows = [{'page': 1, 'category': 'paragraph', 'content': {'text': '자료'}}]
            return ParseResult('fake', rows, {'elements': rows})
    def pdf(width):
        writer = PdfWriter()
        writer.add_blank_page(width=width, height=100)
        output = BytesIO()
        writer.write(output)
        return output.getvalue()
    service = IngestionService(ArtifactStore(tmp_path), Parser())
    app = create_app(Config(tmp_path, access_key='example', external_processing=True), ingestion_service=service)
    with TestClient(app, base_url='https://testserver') as client:
        login = client.post('/v1/auth/login', json={'access_key': 'example'}, headers={'Origin': 'https://testserver'})
        client.headers.update({'Origin': 'https://testserver', 'X-CSRF-Token': login.json()['csrf_token']})
        first = client.post('/v1/ingest', data={'deferred': 'true'}, files={'file': ('one.pdf', pdf(100))})
        assert first.status_code == 200 and entered.wait(3)
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                second = executor.submit(client.post, '/v1/ingest', data={'deferred': 'true'},
                                         files={'file': ('two.pdf', pdf(150))}).result(timeout=2)
            assert second.status_code == 200
            doc = first.json()['document_id']
            assert client.delete(f'/v1/ingest/{doc}').status_code == 409
            changed = client.put('/v1/settings', json={'values': {'remote_timeout': 60}})
            assert changed.status_code == 409 and changed.json()['error']['code'] == 'JOB_IN_PROGRESS'
        finally:
            release.set()


def test_synchronous_upload_releases_lock_during_parse(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from io import BytesIO

    from fastapi.testclient import TestClient
    from indexer.config import Config
    from indexer.parsing.contracts import ParseResult
    from pypdf import PdfWriter

    from apps.api.main import create_app
    entered, release = threading.Event(), threading.Event()
    class Parser:
        def parse(self, request):
            entered.set()
            assert release.wait(8)
            rows = [{'page': 1, 'category': 'paragraph', 'content': {'text': '자료'}}]
            return ParseResult('fake', rows, {'elements': rows})
    def pdf(width):
        writer = PdfWriter()
        writer.add_blank_page(width=width, height=100)
        output = BytesIO()
        writer.write(output)
        return output.getvalue()
    service = IngestionService(ArtifactStore(tmp_path), Parser())
    app = create_app(Config(tmp_path, access_key='example', external_processing=True), ingestion_service=service)
    with TestClient(app, base_url='https://testserver') as client, ThreadPoolExecutor(max_workers=2) as executor:
        login = client.post('/v1/auth/login', json={'access_key': 'example'}, headers={'Origin': 'https://testserver'})
        client.headers.update({'Origin': 'https://testserver', 'X-CSRF-Token': login.json()['csrf_token']})
        first = executor.submit(client.post, '/v1/ingest', files={'file': ('one.pdf', pdf(100))})
        try:
            assert entered.wait(3)
            second = executor.submit(client.post, '/v1/ingest', data={'deferred': 'true'},
                                     files={'file': ('two.pdf', pdf(150))}).result(timeout=2)
            assert second.status_code == 200
        finally:
            release.set()
        assert first.result(timeout=5).status_code == 200


def test_prepared_documents_rebase_but_same_document_conflict_is_rejected(publication):
    publisher, calls = publication
    def chunk(doc, text):
        return {'chunk_id': str(uuid4()), 'document_id': doc, 'text': text}
    one = publisher.prepare('one', [chunk('one', '첫 번째')])
    two = publisher.prepare('two', [chunk('two', '두 번째')])
    publisher.commit(one)
    publisher.commit(two)
    assert set(publisher.active()['document_ids']) == {'one', 'two'}
    assert calls == [['첫 번째'], ['두 번째']]
    previous = publisher.active()['version']
    old = publisher.prepare('one', [chunk('one', '오래된 수정')])
    new = publisher.prepare('one', [chunk('one', '새 수정')])
    publisher.commit(new)
    with pytest.raises(ValueError, match='같은 문서'):
        publisher.commit(old)
    assert publisher.active()['version'] != previous
    assert publisher.document_chunks('one', publisher.active())[0]['text'] == '새 수정'


def test_two_simultaneous_uploads_both_reach_published_index(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from io import BytesIO

    from fastapi.testclient import TestClient
    from indexer.config import Config
    from indexer.parsing.contracts import ParseResult
    from pypdf import PdfReader, PdfWriter

    from apps.api.main import create_app
    barrier = threading.Barrier(2)
    calls = []
    def embed(request):
        body = json.loads(request.content)
        calls.extend(body['input'])
        if any(text in {'자료 하나', '자료 둘'} for text in body['input']):
            barrier.wait(timeout=5)
        return httpx.Response(200, json={'model': body['model'], 'data': [
            {'index': i, 'embedding': [1.0, 0.5]} for i in range(len(body['input']))]})
    class Parser:
        def parse(self, request):
            width = float(PdfReader(request.path).pages[0].mediabox.width)
            rows = [{'page': 1, 'category': 'paragraph', 'content': {'text': '자료 하나' if width == 100 else '자료 둘'}}]
            return ParseResult('fake', rows, {'elements': rows})
    def pdf(width):
        writer = PdfWriter()
        writer.add_blank_page(width=width, height=100)
        out = BytesIO()
        writer.write(out)
        return out.getvalue()
    with httpx.Client(transport=httpx.MockTransport(embed)) as embedding_http:
        qdrant = QdrantClient(':memory:')
        publisher = IndexPublisher(tmp_path, qdrant, EmbeddingClient(embedding_http, 'https://example.test/v1', 'key',
                                   EmbeddingProfile('p', 'doc', 'query', 2)))
        service = IngestionService(ArtifactStore(tmp_path), Parser(), publisher)
        with TestClient(create_app(Config(tmp_path, access_key='example', external_processing=True), ingestion_service=service),
                        base_url='https://testserver') as client, ThreadPoolExecutor(max_workers=2) as pool:
            login = client.post('/v1/auth/login', json={'access_key': 'example'}, headers={'Origin': 'https://testserver'})
            client.headers.update({'Origin': 'https://testserver', 'X-CSRF-Token': login.json()['csrf_token']})
            futures = [pool.submit(client.post, '/v1/ingest', files={'file': (f'{n}.pdf', pdf(width))})
                       for n, width in enumerate((100, 150))]
            results = [future.result(timeout=10) for future in futures]
            assert [result.status_code for result in results] == [200, 200]
            assert all(result.json()['status'] == 'completed' for result in results)
            assert set(publisher.active()['document_ids']) == {result.json()['document_id'] for result in results}
            assert calls.count('자료 하나') == calls.count('자료 둘') == 1
        qdrant.close()


@pytest.mark.parametrize('operation', ['accept-content-changes', 'accept-partial', 'resume', 'rebuild'])
def test_long_review_resume_and_rebuild_do_not_hold_api_lock(tmp_path, monkeypatch, operation):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from io import BytesIO
    from types import SimpleNamespace

    from fastapi.testclient import TestClient
    from indexer.config import Config
    from pypdf import PdfWriter

    from apps.api.main import create_app
    entered, release = threading.Event(), threading.Event()
    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(8)
        return {'version': 'v', 'chunk_count': 1}
    publisher = SimpleNamespace(deleted_documents=lambda: set(), rebuild_with_current_profile=delayed)
    store = ArtifactStore(tmp_path)
    source = tmp_path / 'first.pdf'
    source.write_bytes(b'%PDF-1.4')
    doc, job = store.register(source)['document_id'], str(uuid4())
    store.save_job(doc, job, 'processing', {'status': 'failed', 'stage': 'indexing', 'can_resume': False})
    service = IngestionService(store, None, publisher)
    monkeypatch.setattr(service, 'accept_content_changes', delayed)
    monkeypatch.setattr(service, 'resume_index', delayed)
    app = create_app(Config(tmp_path, access_key='example', external_processing=True), ingestion_service=service)
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    output = BytesIO()
    writer.write(output)
    path = '/v1/ingest-index-rebuild' if operation == 'rebuild' else f'/v1/ingest/{doc}/jobs/{job}/{operation}'
    with TestClient(app, base_url='https://testserver') as client, ThreadPoolExecutor(max_workers=2) as pool:
        login = client.post('/v1/auth/login', json={'access_key': 'example'}, headers={'Origin': 'https://testserver'})
        client.headers.update({'Origin': 'https://testserver', 'X-CSRF-Token': login.json()['csrf_token']})
        running = pool.submit(client.post, path)
        try:
            assert entered.wait(3)
            upload = pool.submit(client.post, '/v1/ingest', data={'deferred': 'true'},
                                 files={'file': ('other.pdf', output.getvalue())}).result(timeout=2)
            assert upload.status_code == 200
            if operation != 'rebuild':
                assert client.delete(f'/v1/ingest/{doc}').status_code == 409
            changed = client.put('/v1/settings', json={'values': {'remote_timeout': 60}})
            assert changed.status_code == 409
        finally:
            release.set()
        assert running.result(timeout=5).status_code == 200
