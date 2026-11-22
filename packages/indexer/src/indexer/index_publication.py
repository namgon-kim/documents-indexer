"""Dense·BM25 색인을 검증한 후 한 버전으로 게시한다."""

import hashlib
import json
import logging
import shutil
import threading
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from uuid import UUID, uuid4

from qdrant_client import models

from indexer.artifact_store import atomic_json
from indexer.bm25_index import BM25Index
from indexer.dense_index import DenseIndex
from indexer.embedding_adaptation import embed_chunks
from indexer.embedding_cache import EmbeddingCache
from indexer.embedding_input import PREPROCESSING_VERSION
from indexer.search_engine import (
    DEFAULT_RERANK_TOP_N,
    DEFAULT_RETRIEVAL_TOP_N,
    MAX_RETRIEVAL_TOP_N,
    SearchService,
)

LOG = logging.getLogger(__name__)


def _office_source(chunks):
    jobs = {item['job_id'] for item in chunks if item.get('job_id')}
    if len(jobs) != 1:
        return None
    regions = {}
    for chunk in chunks:
        if chunk.get('xlsx_region_start'):
            regions.setdefault((chunk['xlsx_sheet'], chunk['xlsx_region_start']), chunk['element_ids'][0])
    return {'job_id': jobs.pop(), 'regions': [[*key, eid] for key, eid in regions.items()]}


@lru_cache(maxsize=24)
def _legacy_office_source(path, document_id, revision):
    with path.open(encoding='utf-8') as stream:
        return _office_source([chunk for chunk in json.load(stream) if chunk['document_id'] == document_id])

class AllowedChunks:
    def __init__(self, retriever, allowed):
        self.retriever, self.allowed = retriever, allowed

    def search(self, query, limit, document_id=None):
        hits = self.retriever.search(query, limit, document_id)
        allowed = [hit for hit in hits if hit.chunk_id in self.allowed]
        if len(allowed) < limit <= len(hits) and limit < MAX_RETRIEVAL_TOP_N:
            # Deleted or stale entries can temporarily occupy the top ranks during cleanup.
            hits = self.retriever.search(query, MAX_RETRIEVAL_TOP_N, document_id)
            allowed = [hit for hit in hits if hit.chunk_id in self.allowed]
        return allowed[:limit]


class IndexPublisher:
    def __init__(self, root: Path, qdrant, embedder, reranker_factory=None, *, active_name="active.json", lock=None, search_lock=None):
        self.root = root.resolve() / "search-indexes"
        self.root.mkdir(parents=True, exist_ok=True)
        self.qdrant, self.embedder = qdrant, embedder
        self.reranker_factory = reranker_factory
        if active_name != "active.json" and not (active_name.startswith("active-") and active_name.endswith(".json")):
            raise ValueError("색인 이름이 올바르지 않습니다")
        self.active_name = active_name
        self.lock = lock or threading.RLock()
        self.search_lock = search_lock or threading.RLock()

    def active(self):
        path = self.root / self.active_name
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def deleted_documents(self):
        path = self.root / "deleted-documents.json"
        return set(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else set()

    def versions(self):
        active = self.active()
        published = self._published_versions()
        rows = []
        for path in self.root.glob("*/manifest.json"):
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
                if str(UUID(manifest["version"])) != path.parent.name:
                    continue
                rows.append({"index_version": manifest["version"], "created_at": manifest.get("created_at"),
                             "status": ("active" if active and active["version"] == manifest["version"] else
                                        "retired" if manifest["version"] in published else
                                        "unpublished" if manifest.get("publication_schema") else "legacy_unknown")})
            except (ValueError, KeyError, OSError):
                continue
        return sorted(rows, key=lambda row: row["created_at"] or "", reverse=True)

    def _published_versions(self):
        # Only an active pointer and its recorded predecessors prove publication.
        # Older snapshots have no predecessor link, so their status is unknown.
        published = set()
        for path in [self.root / 'active.json', *self.root.glob('active-*.json')]:
            if not path.exists():
                continue
            manifest = json.loads(path.read_text(encoding='utf-8'))
            while manifest and manifest.get('version') not in published:
                version = str(UUID(manifest['version']))
                published.add(version)
                previous = manifest.get('previous_version')
                if not previous:
                    break
                previous_path = self.root / str(UUID(previous)) / 'manifest.json'
                manifest = json.loads(previous_path.read_text(encoding='utf-8')) if previous_path.exists() else None
        return published

    def find_publication(self, publication_id):
        for version in self._published_versions():
            manifest = json.loads((self.root / version / 'manifest.json').read_text(encoding='utf-8'))
            if manifest.get('publication_id') == publication_id:
                return manifest
        return None

    def cleanup_orphans(self):
        """Discard only identified new-format builds outside all active histories."""
        with self.search_lock, self.lock:
            published = self._published_versions()
            for folder in self.root.iterdir():
                if folder.is_symlink() or not folder.is_dir():
                    continue
                try:
                    version = str(UUID(folder.name))
                    if version != folder.name or version in published:
                        continue
                    marker = folder / 'build.json'
                    manifest_path = folder / 'manifest.json'
                    manifest = json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else None
                    if manifest and (manifest.get('publication_schema') != 1 or manifest.get('version') != version):
                        continue
                    if not marker.exists() and not manifest:
                        continue
                    if marker.exists():
                        build = json.loads(marker.read_text(encoding='utf-8'))
                        if build != {'version': version, 'schema': 1}:
                            continue
                    collection = 'ingestion_' + version.replace('-', '')
                    if manifest and manifest.get('collection') != collection:
                        continue
                    if self.qdrant.collection_exists(collection):
                        self.qdrant.delete_collection(collection)
                    shutil.rmtree(folder)
                except Exception:
                    LOG.exception('Could not clean incomplete index build %s', folder.name)

    def get_version(self, version):
        if version is None:
            return self.active()
        try:
            version = str(UUID(str(version)))
            path = self.root / version / "manifest.json"
            if not path.exists():
                return None
            manifest = json.loads(path.read_text(encoding="utf-8"))
            if manifest.get('publication_schema') and version not in self._published_versions():
                return None
            return manifest
        except (ValueError, TypeError):
            return None

    def embedding_contract(self):
        profile = self.embedder.profile
        endpoint = getattr(self.embedder, "base_url", None)
        if not endpoint:
            raise ValueError("임베딩 API 주소를 확인할 수 없습니다")
        return {"profile_id": profile.profile_id, "document_model": profile.document_model,
                "query_model": profile.query_model, "dimensions": profile.dimensions,
                "endpoint_sha256": hashlib.sha256(endpoint.encode("utf-8")).hexdigest(),
                "adapter": type(self.embedder).__module__ + "." + type(self.embedder).__qualname__,
                "representation": "chunk-text-v1", "preprocessing": PREPROCESSING_VERSION, "distance": "cosine"}

    def document_chunks(self, document_id, manifest):
        path = self.root / manifest["version"] / "chunks.json"
        return [chunk for chunk in json.loads(path.read_text(encoding="utf-8"))
                if chunk["document_id"] == document_id]

    def xlsx_source(self, document_id, manifest):
        if document_id not in manifest['document_ids']:
            return None
        if source := manifest.get('xlsx_sources', {}).get(document_id):
            return source
        path = self.root / manifest['version'] / 'chunks.json'
        stat = path.stat()
        return _legacy_office_source(path, document_id, (stat.st_ino, stat.st_mtime_ns, stat.st_size))

    def prepare(self, document_id: str, new_chunks: list[dict], progress=None):
        if not new_chunks or any(chunk["document_id"] != document_id for chunk in new_chunks):
            raise ValueError("문서에 속한 검색 청크가 필요합니다")
        with self.lock:
            deleted = self.deleted_documents()
            if document_id in deleted:
                raise ValueError("삭제 중이거나 삭제된 문서입니다")
            previous = self.active()
            contract = self.embedding_contract()
            if previous and previous["embedding_profile"] != contract:
                raise ValueError("임베딩 프로필이 다릅니다. 별도 색인을 구성해 주세요")
            prior = json.loads((self.root / previous["version"] / "chunks.json").read_text(encoding="utf-8")) if previous else []
            chunks = [item for item in prior if item["document_id"] != document_id and item["document_id"] not in deleted] + new_chunks
        profile = self.embedder.profile
        cache = EmbeddingCache(self.root / "embedding-cache.sqlite3", self.embedder, contract)
        chunks, vectors = embed_chunks(cache, chunks, profile.max_input_bytes, profile.batch_size, progress,
                                       tracked_document=document_id, detail_progress=getattr(progress, "document_progress", None))
        return {"chunks": chunks, "vectors": vectors, "contract": contract,
                "previous_version": previous["version"] if previous else None,
                'document_id': document_id,
                'baseline_document': [item for item in prior if item['document_id'] == document_id]}

    def commit(self, prepared):
        original = prepared
        document_id = original.get('document_id')
        target_chunks = [item for item in original['chunks'] if item['document_id'] == document_id] if document_id else None
        for attempt in range(4):
            with self.lock:
                active = self.active()
                if prepared["contract"] != self.embedding_contract():
                    raise ValueError("임베딩 프로필이 변경되었습니다")
                if any(item["document_id"] in self.deleted_documents() for item in prepared["chunks"]):
                    raise ValueError("삭제된 문서가 포함되어 있습니다")
                if (active["version"] if active else None) == prepared["previous_version"]:
                    return self._build(prepared["chunks"], prepared["contract"], prepared["vectors"],
                                       previous_version=prepared['previous_version'], publication_id=prepared.get('publication_id'))
                doc = original.get('document_id')
                if not doc or not active or active['embedding_profile'] != original['contract']:
                    raise ValueError("검색 버전이 변경되었습니다. 저장된 청크로 다시 시도해 주세요")
                if self.document_chunks(doc, active) != original['baseline_document']:
                    raise ValueError("같은 문서의 검색 내용이 변경되었습니다. 다시 확인해 주세요")
            if attempt == 3:
                break
            # Reuse the existing cache outside the lock; a new snapshot may
            # introduce other documents but must never overwrite this document.
            prepared = self.prepare(doc, target_chunks)
            if prepared['baseline_document'] != original['baseline_document']:
                raise ValueError("같은 문서의 검색 내용이 변경되었습니다. 다시 확인해 주세요")
            prepared['publication_id'] = original.get('publication_id')
        raise ValueError("검색 버전이 계속 변경되어 게시하지 못했습니다. 다시 시도해 주세요")

    def publish(self, document_id: str, new_chunks: list[dict], *, publication_id=None):
        prepared = self.prepare(document_id, new_chunks)
        prepared['publication_id'] = publication_id
        return self.commit(prepared)

    def rebuild_with_current_profile(self):
        """저장된 청크를 현재 모델로 다시 색인하고 성공할 때만 새 버전을 게시한다."""
        with self.lock:
            previous = self.active()
            if not previous:
                raise ValueError("다시 만들 검색 색인이 없습니다")
            prior = json.loads((self.root / previous["version"] / "chunks.json").read_text(encoding="utf-8"))
            deleted = self.deleted_documents()
            chunks = [item for item in prior if item["document_id"] not in deleted]
            if not chunks:
                raise ValueError("다시 만들 검색 내용이 없습니다")
            contract = self.embedding_contract()
            previous_version = previous["version"]
        profile = self.embedder.profile
        cache = EmbeddingCache(self.root / "embedding-cache.sqlite3", self.embedder, contract)
        chunks, vectors = embed_chunks(cache, chunks, profile.max_input_bytes, profile.batch_size)
        return self.commit({"chunks": chunks, "vectors": vectors, "contract": contract,
                            "previous_version": previous_version})

    def _build(self, chunks, contract, vectors=None, *, previous_version=None, publication_id=None):
        version = str(uuid4())
        folder = self.root / version
        folder.mkdir()
        collection = "ingestion_" + version.replace("-", "")
        atomic_json(folder / 'build.json', {'version': version, 'schema': 1})
        dense = DenseIndex(self.qdrant, collection, self.embedder)
        dense_built = False
        try:
            if vectors is None:
                profile = self.embedder.profile
                cache = EmbeddingCache(self.root / "embedding-cache.sqlite3", self.embedder, contract)
                chunks, vectors = embed_chunks(cache, chunks, profile.max_input_bytes, profile.batch_size)
            lexical = BM25Index(folder / "bm25.sqlite3")
            by_document = {}
            for item in chunks:
                by_document.setdefault(item["document_id"], []).append(item)
            for key, rows in by_document.items():
                lexical.replace_document(key, rows)
            dense.build(chunks, vectors)
            dense_built = True
            atomic_json(folder / "chunks.json", chunks)
            manifest = {"version": version, "collection": collection, "embedding_profile": contract,
                        "document_ids": sorted(by_document), "chunk_count": len(chunks),
                        "xlsx_sources": {doc: source for doc, rows in by_document.items()
                                         if any(item.get('xlsx_region_start') for item in rows)
                                         and (source := _office_source(rows))},
                        "representation_versions": sorted({item.get("representation_version", "unknown") for item in chunks}),
                        "created_at": datetime.now(UTC).isoformat(), 'publication_schema': 1,
                        'previous_version': previous_version, 'publication_id': publication_id}
            atomic_json(folder / "manifest.json", manifest)
            atomic_json(self.root / self.active_name, manifest)
            return manifest
        except Exception:
            active = self.active()
            if active and active.get('version') == version:
                raise
            if dense_built:
                self.qdrant.delete_collection(collection)
            shutil.rmtree(folder, ignore_errors=True)
            raise

    def search(self, query: str, method: str, limit: int = 5, document_id: str | None = None,
               rerank: bool = False, version: str | None = None, *,
               retrieval_top_n: int = DEFAULT_RETRIEVAL_TOP_N, rerank_top_n: int = DEFAULT_RERANK_TOP_N):
        with self.search_lock:
            return self._search(query, method, limit, document_id, rerank, version, retrieval_top_n, rerank_top_n)

    def _search(self, query, method, limit, document_id, rerank, version, retrieval_top_n, rerank_top_n):
        manifest = self.get_version(version)
        if not manifest:
            raise ValueError("검색 가능한 색인이 없습니다")
        if method != "bm25" and manifest["embedding_profile"] != self.embedding_contract():
            raise ValueError("검색 임베딩 프로필이 색인과 일치하지 않습니다")
        folder = self.root / manifest["version"]
        deleted = self.deleted_documents()
        by_id = {item["chunk_id"]: item for item in json.loads((folder / "chunks.json").read_text())
                 if item["document_id"] not in deleted}
        reranker = self.reranker_factory(lambda chunk_id: by_id[chunk_id]["text"]) if self.reranker_factory else None
        service = SearchService(AllowedChunks(DenseIndex(self.qdrant, manifest["collection"], self.embedder), by_id),
                                AllowedChunks(BM25Index(folder / "bm25.sqlite3"), by_id), reranker)
        from indexer import diagnostics
        diagnostics.record('index', {'index_version': manifest['version'], 'method': method,
                                     'retrieval_top_n': retrieval_top_n, 'rerank_top_n': rerank_top_n if rerank else None,
                                     'requested_limit': limit, 'rerank': rerank,
                                     'embedding_profile_id': manifest.get('embedding_profile', {}).get('profile_id')})
        counts = {}
        hits = service.search(query, method, limit, document_id, rerank,
                              retrieval_top_n=retrieval_top_n, rerank_top_n=rerank_top_n, counts=counts)
        diagnostics.filter_rankings(set(by_id))
        return {"index_version": manifest["version"], "method": method,
                "reranked": rerank, "search_counts": counts,
                "results": [{**by_id[hit.chunk_id], "score": hit.score} for hit in hits]}

    def delete_document(self, document_id: str):
        with self.search_lock, self.lock:
            deleted = self.deleted_documents()
            deleted.add(document_id)
            atomic_json(self.root / "deleted-documents.json", sorted(deleted))
            active_pointers = {}
            for path in [self.root / "active.json", *self.root.glob("active-*.json")]:
                if path.exists():
                    active_pointers[path] = json.loads(path.read_text(encoding="utf-8"))
            for manifest_path in self.root.glob("*/manifest.json"):
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                folder = manifest_path.parent
                if str(UUID(manifest["version"])) != folder.name:
                    raise ValueError("색인 버전 정보를 확인할 수 없습니다")
                chunks_path = folder / "chunks.json"
                chunks = json.loads(chunks_path.read_text(encoding="utf-8"))
                if self.qdrant.collection_exists(manifest["collection"]):
                    self.qdrant.delete(manifest["collection"], points_selector=models.FilterSelector(
                        filter=models.Filter(must=[models.FieldCondition(key="document_id", match=models.MatchValue(value=document_id))])), wait=True)
                if (folder / "bm25.sqlite3").exists():
                    BM25Index(folder / "bm25.sqlite3").replace_document(document_id, [])
                retained = [item for item in chunks if item["document_id"] != document_id]
                atomic_json(chunks_path, retained)
                manifest["document_ids"] = sorted({item["document_id"] for item in retained})
                manifest["chunk_count"] = len(retained)
                manifest.get("xlsx_sources", {}).pop(document_id, None)
                atomic_json(manifest_path, manifest)
                for pointer, active in active_pointers.items():
                    if active["version"] == manifest["version"]:
                        atomic_json(pointer, manifest)
            # Vectors are keyed by embedding contract and text, and can be
            # shared by other documents. Deletion cannot identify exclusive keys.

    def job_is_indexed(self, document_id: str, job_id: str):
        with self.lock:
            for version in self._published_versions():
                path = self.root / version / 'chunks.json'
                chunks = json.loads(path.read_text(encoding="utf-8"))
                if any(item.get("document_id") == document_id and
                       item.get("job_id") == job_id for item in chunks):
                    return True
            return False

    def job_may_be_referenced(self, document_id, job_id, *, include_unattributed=True):
        if self.job_is_indexed(document_id, job_id):
            return True
        # Pre-ledger historical versions cannot be distinguished from old
        # abandoned builds. Protect their possible source jobs from deletion.
        for path in self.root.glob('*/manifest.json'):
            manifest = json.loads(path.read_text(encoding='utf-8'))
            if manifest.get('publication_schema'):
                continue
            chunks = json.loads((path.parent / 'chunks.json').read_text(encoding='utf-8'))
            if any(item.get('document_id') == document_id and
                   (item.get('job_id') == job_id or (include_unattributed and not item.get('job_id'))) for item in chunks):
                return True
        return False
