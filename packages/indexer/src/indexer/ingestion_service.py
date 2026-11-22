"""파일 등록, 처리 그래프 실행과 색인 게시."""

import hashlib
import json
import logging
import re
import shutil
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

from indexer.artifact_store import ArtifactStore, atomic_json
from indexer.content_comparison import compare
from indexer.contracts import IndexerError
from indexer.documents import CHUNK_DEFAULTS, select_profile
from indexer.ingestion_graph import make_graph
from indexer.parsing.contracts import Parser
from indexer.parsing.normalization import cells_from, parse_table_html
from indexer.representation_audit import audit_representation
from indexer.review_status import normalize_review_status
from indexer.visual_relations import xlsx_anchor_matches

LOG = logging.getLogger(__name__)


def resumable_provider_checkpoint(checkpoint, attempt, *, native_visual=False):
    visuals = checkpoint.get('visuals', [])
    if native_visual and attempt == 'initial':
        return all(item.get('status') in {'started', 'completed'} for item in visuals)
    if isinstance(attempt, str) and attempt.startswith('image-'):
        return all(item.get('status') == 'completed' for item in visuals)
    return bool(checkpoint.get('request_id') or (attempt == 'retry' and checkpoint.get('result'))
                or (attempt == 'initial' and visuals and all(item.get('status') == 'completed' for item in visuals)))


def can_resume_native_visual(job):
    return (job.get('settings', {}).get('extraction_mode') == 'native_visual'
            and job.get('mode') not in {'image_actions', 'draft_image_actions', 'rechunk'})


def unresolved_visuals(elements):
    analyzed = {element.get('visual_id') for element in elements if element.get('source_origin') == 'visual_analysis'
                and not element.get('search_exclusion') and (element.get('text') or element.get('html'))}
    return [element for element in elements if element.get('source_origin') == 'native_image'
            and (not element.get('image_policy') or element.get('image_policy') in {'analyze', 'both'})
            and element.get('visual_id') not in analyzed]


def embedding_http_error(error):
    code = error.response.status_code
    try:
        payload = error.response.json()
        detail = payload.get("error", payload) if isinstance(payload, dict) else {}
        message = str(detail.get("message", "")).lower() if isinstance(detail, dict) else ""
    except ValueError:
        message = ""
    if code == 403 and "insufficient credit" in message:
        return {"code": "EMBED_CREDIT_EXHAUSTED", "message": "임베딩 서비스의 크레딧이 부족해 API 키가 정지됐습니다. 결제·크레딧을 확인한 뒤 검색 준비부터 다시 처리해 주세요.", "http_status": code}
    advice = ("임베딩 서비스의 API 키와 모델 접근 권한을 확인해 주세요." if code in {401, 403} else
              "임베딩 서비스 요청 한도에 도달했습니다. 잠시 후 다시 시도해 주세요." if code == 429 else
              "임베딩 서비스 연결과 모델 설정을 확인해 주세요.")
    return {"code": "EMBED_PROVIDER_ERROR", "message": advice, "http_status": code}


class DuplicateDocument(Exception):
    def __init__(self, document):
        self.document = document


class IngestionFailure(Exception):
    def __init__(self, document_id, job_id):
        super().__init__("문서 처리에 실패했습니다")
        self.document_id, self.job_id = document_id, job_id


class IngestionService:
    def __init__(self, storage: ArtifactStore, parser: Parser, publisher=None, *, max_pages=2000, retry_pages=200, parser_factory=None, xlsx_analyzer=None):
        self.storage = storage
        self.parser = parser
        self.max_pages, self.retry_pages = max_pages, retry_pages
        self.parser_factory = parser_factory
        self.xlsx_analyzer = xlsx_analyzer
        self.graph = self._graph(parser)
        self.publisher = publisher
        self.embedding_profiles = None

    def _deleted_documents(self):
        return self.publisher.deleted_documents() if self.publisher and hasattr(self.publisher, 'deleted_documents') else set()

    def _ensure_not_deleted(self, document_id):
        if document_id in self._deleted_documents():
            raise ValueError('삭제 중이거나 삭제된 문서입니다')

    @staticmethod
    def _job_requires_copy(publisher, document_id, job_id):
        if not publisher:
            return False
        if hasattr(publisher, 'job_may_be_referenced'):
            return publisher.job_may_be_referenced(document_id, job_id, include_unattributed=False)
        return bool(hasattr(publisher, 'job_is_indexed') and publisher.job_is_indexed(document_id, job_id))

    def _restore_draft_snapshot(self, document_id, job_id, edit_id, error):
        if not re.fullmatch(r'[0-9a-f]{32}', str(edit_id)):
            raise ValueError('검토 작업의 기준 기록을 확인할 수 없습니다')
        folder = self.storage.document_dir(document_id) / 'jobs' / job_id
        snapshot = folder / 'draft-edits' / edit_id
        previous = json.loads((snapshot / 'processing.json').read_text(encoding='utf-8'))
        for name in ('parser_elements', 'parser_initial_elements', 'parser_result', 'elements', 'chunks'):
            path = snapshot / (name + '.json')
            if path.exists():
                self.storage.save_job(document_id, job_id, name, json.loads(path.read_text(encoding='utf-8')))
            else:
                (folder / (name + '.json')).unlink(missing_ok=True)
        # Publish the restored status last so interrupted restoration is repeatable.
        self.storage.save_job(document_id, job_id, 'processing', {**previous, 'error': error,
            'last_image_failure': error, 'pending_review': True})

    def _graph(self, parser):
        return make_graph(parser, self._parsed, self._represented, self._quality, self.max_pages, self.retry_pages,
                          self._provider_event, self._progress, self._inspected, self._embed_node, self._index_node,
                          self._image, self.xlsx_analyzer)

    def _image(self, state, image_id, data):
        self.storage.save_image(state['document_id'], state['job_id'], image_id, data)

    def _save_parser(self, document_id, job_id, parser):
        parser_config = getattr(parser, "config", None)
        if self.parser_factory and is_dataclass(parser_config):
            self.storage.save_job(document_id, job_id, "parser_config", {
                "config": asdict(parser_config), "remote_timeout": getattr(parser, "remote_timeout", None),
                "job_timeout": getattr(parser, "job_timeout", None)})

    def _job_parser(self, document_id, job_id, status):
        if self.parser_factory:
            try:
                snapshot = self.storage.load_job(document_id, job_id, "parser_config")
            except FileNotFoundError:
                snapshot = None
            if snapshot:
                parser = self.parser_factory(snapshot)
                if status.get("parser_contract") != self.parser_contract(parser):
                    raise ValueError("저장된 파서 설정이 작업과 일치하지 않습니다")
                return parser
        if status.get("parser_contract") == self.parser_contract():
            return self.parser
        raise ValueError("이전 작업의 파서 설정을 확인할 수 없습니다. 원래 파서를 선택하거나 새로 파싱해 주세요")

    def _can_use_job_parser(self, document_id, job_id, status):
        try:
            self._job_parser(document_id, job_id, status)
            return True
        except (ValueError, TypeError, KeyError):
            return False

    def publisher_for(self, profile_id=None, *, for_ingest=False):
        return self.embedding_profiles.publisher(profile_id, for_ingest=for_ingest) if self.embedding_profiles else self.publisher

    @staticmethod
    def ensure_index_contract(publisher):
        if publisher and hasattr(publisher, "active") and hasattr(publisher, "embedding_contract"):
            active = publisher.active()
            if active and active.get("embedding_profile") != publisher.embedding_contract():
                raise IndexerError("INDEX_REBUILD_REQUIRED", "현재 임베딩 계약이 기존 검색 색인과 다릅니다. 설정에서 색인을 다시 준비한 후 문서를 처리해 주세요.", 409)

    def _parsed(self, state, result):
        self.storage.save_job(state["document_id"], state["job_id"], "parser_result", result.raw)
        self.storage.save_job(state["document_id"], state["job_id"], "parser_elements", result.elements)
        self.storage.save_job(state["document_id"], state["job_id"], "parser_initial_elements", result.elements)
        status = self.storage.load_job(state["document_id"], state["job_id"], "processing")
        self.storage.save_job(state["document_id"], state["job_id"], "processing", {
            **status, "stage": "quality", "provider": result.provider,
            "provider_failed": sorted(result.failed), "provider_unavailable": sorted(result.unavailable),
            "provider_incomplete": result.incomplete,
            "provider_terminal_unavailable": sorted(result.terminal_unavailable), "progress": None,
            "profile": state.get("profile"), "profile_origin": state.get("profile_origin"),
            **({'xlsx_structure_version': result.raw['xlsx_structure_version']}
               if 'xlsx_structure_version' in result.raw else {}),
            **self._xlsx_review_status(status, result.raw)})

    @staticmethod
    def _xlsx_review_status(status, raw):
        """Keep only reviews that still match the new parse; record the others as stale."""
        state = raw.get('xlsx_review_state') if isinstance(raw, dict) else None
        reviews = status.get('xlsx_review') or {}
        if not state or not reviews:
            return {}
        stale = {name: {'review': reviews[name], 'reason': item.get('reason')}
                 for name, item in state.items() if item['status'] == 'stale' and name in reviews}
        updates = {'xlsx_review_basis': {**(status.get('xlsx_review_basis') or {}),
                                         **{name: item['basis'] for name, item in state.items()
                                            if item['status'] == 'applied' and name in reviews}}}
        if stale:
            # The base job keeps its review history; this job asks for a new review.
            updates['xlsx_review'] = {name: review for name, review in reviews.items() if name not in stale}
            updates['xlsx_review_stale'] = {**(status.get('xlsx_review_stale') or {}), **stale}
            updates['xlsx_review_basis'] = {name: basis for name, basis in updates['xlsx_review_basis'].items()
                                            if name not in stale}
        return updates

    def _provider_event(self, state, attempt, event, data):
        self.storage.save_provider_event(state["document_id"], state["job_id"], attempt, event, data)

    def _inspected(self, state, details):
        document_id, job_id = state["document_id"], state["job_id"]
        self.storage.save_job(document_id, job_id, "source_details", details)
        status = self.storage.load_job(document_id, job_id, "processing")
        if details["format"] == "xlsx":
            summary = {"format": "xlsx", "sheets": [{"name": sheet["name"], "visibility": sheet["visibility"],
                "formula_count": len(sheet["formulas"]), "formula_scan_complete": sheet["formula_scan_complete"],
                "missing_cached_values": sum(item["cached_value"] is None for item in sheet["formulas"])}
                for sheet in details["sheets"]]}
        else:
            summary = {"format": "pptx", "slides": [{"number": slide["number"], "hidden": slide["hidden"],
                       "notes_present": slide["notes_present"]} for slide in details["slides"]]}
        self.storage.save_job(document_id, job_id, "processing", {**status, "source_summary": summary})

    def _progress(self, state, completed, total):
        document_id, job_id = state["document_id"], state["job_id"]
        status = self.storage.load_job(document_id, job_id, "processing")
        progress = {"completed": completed, "total": total}
        if state.get('settings', {}).get('extraction_mode') == 'native_visual' or state.get('image_edit'):
            progress['unit'] = 'images'
        self.storage.save_job(document_id, job_id, "processing", {**status, "progress": progress})

    def _quality(self, state, elements, coverage, attempts):
        document_id, job_id = state["document_id"], state["job_id"]
        self.storage.save_job(document_id, job_id, "parser_elements", elements)
        if attempts is not None:
            self.storage.save_job(document_id, job_id, "parse_attempts", attempts)
        status = self.storage.load_job(document_id, job_id, "processing")
        retrying = bool(coverage.get("attempts") and coverage["attempts"][-1].get("status") == "requested")
        self.storage.save_job(document_id, job_id, "processing", {
            **status, "stage": "retrying" if retrying else "representing", "coverage": coverage, "progress": None})

    def _represented(self, state, elements, chunks):
        for name, value in (("elements", elements), ("chunks", chunks)):
            self.storage.save_job(state["document_id"], state["job_id"], name, value)
        status = self.storage.load_job(state["document_id"], state["job_id"], "processing")
        audit = audit_representation(elements, chunks)
        self.storage.save_job(state["document_id"], state["job_id"], "processing", {**status, "stage": "indexing", "representation_audit": audit})

    @staticmethod
    def embedding_policy(publisher):
        profile = getattr(getattr(publisher, "embedder", None), "profile", None)
        return {"max_input_bytes": profile.max_input_bytes, "batch_size": profile.batch_size} if profile else None

    def enqueue(self, source: Path, profile=None, settings=None, embedding_profile_id=None, *, allow_duplicate=False):
        selection = select_profile(source, profile)
        supplied = settings or {}
        defaults = CHUNK_DEFAULTS[selection.profile]
        settings = {**defaults, **supplied}
        if "target_chars" in supplied and "overlap_chars" not in supplied:
            settings["overlap_chars"] = min(defaults["overlap_chars"], supplied["target_chars"] // 4)
        if not allow_duplicate:
            with source.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            size = source.stat().st_size
            deleted = self._deleted_documents()
            for existing in self.storage.list_documents():
                if existing['document_id'] in deleted:
                    continue
                original = self.storage.document_dir(existing["document_id"]) / ("source." + existing["format"])
                if existing.get("sha256") == digest and original.is_file() and original.stat().st_size == size:
                    raise DuplicateDocument(existing)
        publisher = self.publisher_for(embedding_profile_id, for_ingest=True)
        self.ensure_index_contract(publisher)
        document = self.storage.register(source)
        document_id = document["document_id"]
        job_id = str(uuid4())
        status = {"status": "queued", "stage": "queued", "settings": settings or {}, "profile": profile,
                  "parser_contract": self.parser_contract(), "mode": "initial",
                  "created_at": datetime.now(UTC).isoformat()}
        if publisher and self.embedding_policy(publisher):
            status["embedding_policy"] = self.embedding_policy(publisher)
        if self.embedding_profiles:
            status["embedding_profile_id"] = publisher.embedder.profile.profile_id
            status["embedding_contract"] = publisher.embedding_contract()
        self._save_parser(document_id, job_id, self.parser)
        self.storage.save_job(document_id, job_id, "processing", status)
        return {"document_id": document_id, "job_id": job_id, **status}

    def parser_contract(self, parser=None):
        parser = parser or self.parser
        config = getattr(parser, "config", None)
        fields = {name: getattr(config, name, None) for name in ("provider", "base_url", "model", "mode", "options")}
        fields["adapter"] = type(parser).__module__ + "." + type(parser).__qualname__
        return hashlib.sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def enqueue_reprocess(self, document_id: str, base_job_id: str, mode: str, settings: dict, embedding_profile_id=None,
                          *, xlsx_review=None, xlsx_review_reset=None):
        if mode not in {"reparse", "rechunk", "complete_images"}:
            raise ValueError("다시 처리할 단계를 확인해 주세요")
        self.storage.load_document(document_id)
        if self.publisher and document_id in self.publisher.deleted_documents():
            raise ValueError("삭제 중인 문서는 처리할 수 없습니다")
        jobs = self.storage.list_jobs(document_id)
        if any(job["status"] in {"queued", "processing"} for job in jobs):
            raise ValueError("이 문서의 다른 작업이 진행 중입니다")
        base = self.storage.load_job(document_id, base_job_id, "processing")
        reviews = dict(base.get('xlsx_review') or {})
        basis = dict(base.get('xlsx_review_basis') or {})
        stale = dict(base.get('xlsx_review_stale') or {})
        if xlsx_review and xlsx_review_reset:
            raise ValueError('구조 검토와 초기화를 함께 요청할 수 없습니다')
        if xlsx_review or xlsx_review_reset:
            from indexer.xlsx_structure import STRUCTURE_VERSION, review_basis, validate

            if mode != 'rechunk' or settings.get('extraction_mode') not in {'native_text', 'native_visual'}:
                raise ValueError('XLSX 직접 추출 작업에서 구조를 검토할 수 있습니다')
            raw = self.storage.load_job(document_id, base_job_id, 'parser_result')
            if raw.get('xlsx_structure_version') != STRUCTURE_VERSION:
                raise ValueError('문서를 다시 읽은 뒤 구조를 검토해 주세요')
            sheets = {sheet['name']: sheet for sheet in raw['xlsx_sheets']}
            requested = set(xlsx_review or xlsx_review_reset)
            if requested - sheets.keys():
                raise ValueError('검토할 시트를 찾을 수 없습니다')
            for name, proposal in (xlsx_review or {}).items():
                validate(sheets[name], proposal, 'user')
                basis[name] = review_basis(sheets[name])
            if xlsx_review:
                reviews.update({name: proposal.model_dump() for name, proposal in xlsx_review.items()})
            else:
                reviews = {name: review for name, review in reviews.items() if name not in requested}
                basis = {name: value for name, value in basis.items() if name not in requested}
            stale = {name: value for name, value in stale.items() if name not in requested}
        if mode == 'complete_images' and base.get('pending_review'):
            raise ValueError('검토 중인 작업은 이미지 목록에서 처리할 항목을 선택해 주세요')
        selected_id = embedding_profile_id or base.get("embedding_profile_id")
        publisher = self.publisher_for(selected_id, for_ingest=True)
        self.ensure_index_contract(publisher)
        image_checkpoints = []
        if mode in {"rechunk", "complete_images"}:
            base_parser = self._job_parser(document_id, base_job_id, base)
            if any((base.get("settings", {}).get(key, 'parser' if key == 'extraction_mode' else None)
                    != settings.get(key, 'parser' if key == 'extraction_mode' else None))
                   for key in ("dpi", "ocr_policy", "extraction_mode")):
                raise ValueError("OCR 또는 DPI가 변경되면 다시 파싱해야 합니다")
            self.storage.load_job(document_id, base_job_id, "parser_elements")
            self.storage.load_job(document_id, base_job_id, "parser_result")
            if not base.get("provider"):
                raise ValueError("기준 작업의 파서 출처를 확인할 수 없습니다")
            if mode == 'complete_images' and (base.get('settings', {}).get('extraction_mode') != 'native_visual'
                    or not str(base.get('provider', '')).startswith('native_')):
                raise ValueError('기존 이미지 분석 작업에서만 미처리 이미지를 이어서 분석할 수 있습니다')
            if mode == 'complete_images' and any(item.get('_image_policy') for item in self.storage.load_job(document_id, base_job_id, 'parser_elements')):
                raise ValueError('이미지별 설정이 있는 작업은 이미지 목록에서 미처리 항목을 선택해 주세요')
            if mode == 'complete_images':
                image_checkpoints = self.storage.provider_checkpoint(document_id, base_job_id, 'initial').get('visuals', [])
                if any(item.get('status') != 'completed' for item in image_checkpoints):
                    raise ValueError('이전 이미지 요청의 완료 여부를 확인할 수 없어 자동으로 다시 요청할 수 없습니다')
        job_id = str(uuid4())
        job_parser = base_parser if mode in {"rechunk", "complete_images"} else self.parser
        status = {"status": "queued", "stage": "queued", "mode": mode, "base_job_id": base_job_id,
                  "settings": settings, "profile": base.get("profile"), "parser_contract": self.parser_contract(job_parser),
                  'force_review': mode == 'complete_images',
                  **({'xlsx_review_edit': True} if xlsx_review else {}),
                  **({'xlsx_review_reset': sorted(xlsx_review_reset)} if xlsx_review_reset else {}),
                  'image_policies_present': base.get('image_policies_present', False) if mode == 'rechunk' else False,
                  "created_at": datetime.now(UTC).isoformat()}
        if publisher and self.embedding_policy(publisher):
            status["embedding_policy"] = self.embedding_policy(publisher)
        if self.embedding_profiles:
            status["embedding_profile_id"] = publisher.embedder.profile.profile_id
            status["embedding_contract"] = publisher.embedding_contract()
        if settings.get('extraction_mode') in {'native_text', 'native_visual'}:
            # A reparse revalidates inherited reviews against the new source; reviews
            # that no longer match are recorded as stale instead of failing the job.
            status.update({key: value for key, value in (('xlsx_review', reviews), ('xlsx_review_basis', basis),
                                                         ('xlsx_review_stale', stale)) if value})
        self._save_parser(document_id, job_id, job_parser)
        if mode == 'complete_images':
            for item in image_checkpoints:
                self.storage.save_provider_event(document_id, job_id, 'initial', 'visual', item)
        self.storage.save_job(document_id, job_id, "processing", status)
        return {"document_id": document_id, "job_id": job_id, **status}

    def ingest(self, source: Path, profile=None, settings=None, embedding_profile_id=None, *, allow_duplicate=False):
        job = self.enqueue(source, profile, settings, embedding_profile_id, allow_duplicate=allow_duplicate)
        return self.run_job(job["document_id"], job["job_id"])

    def enqueue_image_actions(self, document_id, base_job_id, actions, embedding_profile_id=None, descriptions=None):
        from indexer.parsing.image_actions import IMAGE_ACTIONS

        self.storage.load_document(document_id)
        if self.publisher and document_id in self.publisher.deleted_documents():
            raise ValueError('삭제 중인 문서는 처리할 수 없습니다')
        if any(job['status'] in {'queued', 'processing'} for job in self.storage.list_jobs(document_id)):
            raise ValueError('이 문서의 다른 작업이 진행 중입니다')
        base = self.storage.load_job(document_id, base_job_id, 'processing')
        raw = self.storage.load_job(document_id, base_job_id, 'parser_elements')
        self.storage.load_job(document_id, base_job_id, 'parser_result')
        normalized = self.storage.load_job(document_id, base_job_id, 'elements')
        images = {item['element_id']: item for item in normalized if item.get('source_origin') == 'native_image'}
        raw_ids = {item.get('_visual_id') for item in raw if item.get('_origin') == 'native_image'}
        if not actions or len(actions) > 100 or any(key not in images or value not in IMAGE_ACTIONS for key, value in actions.items()):
            raise ValueError('선택한 이미지 또는 처리 방식을 확인해 주세요')
        selected = {images[key].get('visual_id'): value for key, value in actions.items()}
        descriptions = descriptions or {}
        if set(descriptions) - set(actions) or any(
            not isinstance(descriptions.get(key), str) or not 1 <= len(descriptions[key].strip()) <= 2000
            for key, action in actions.items() if action == 'describe'
        ):
            raise ValueError('이미지 설명을 1~2,000자로 입력해 주세요')
        selected_descriptions = {images[key]['visual_id']: descriptions[key].strip()
                                 for key, action in actions.items() if action == 'describe'}
        chunks = self.storage.load_job(document_id, base_job_id, 'chunks')
        linked = set()
        for element in normalized:
            if not element.get('text') or element.get('search_exclusion') or element.get('source_origin') in {'visual_analysis', 'visual_description'}:
                continue
            related_chunks = [chunk for chunk in chunks if element['element_id'] in chunk.get('element_ids', [])]
            for link in element.get('related_images', []):
                image = images.get(link.get('element_id'), {})
                if link.get('type') == 'cell_anchor':
                    anchor = image.get('layout') or {}
                    if not xlsx_anchor_matches(anchor, (cell for chunk in related_chunks for cell in chunk.get('table_cells', []))):
                        continue
                if related_chunks:
                    linked.add(link.get('element_id'))
        if any(action == 'original' and key not in linked for key, action in actions.items()):
            raise ValueError('연결할 본문이 없는 이미지는 원본과 설명 직접 입력을 선택해 주세요')
        if None in selected or len(selected) != len(actions) or not set(selected) <= raw_ids:
            raise ValueError('기준 작업의 이미지 연결을 확인할 수 없습니다')
        existing_analysis = {item.get('_visual_id') for item in raw if item.get('_origin') == 'visual_analysis'}
        if any(value == 'reuse' and key not in existing_analysis for key, value in selected.items()):
            raise ValueError('재사용할 이미지 분석 결과가 없습니다')
        base_publisher = self.publisher_for(base.get('embedding_profile_id'))
        published = self._job_requires_copy(base_publisher, document_id, base_job_id)
        draft = bool((base.get('pending_review') or (not base.get('index_version') and
                     (base.get('can_accept_partial') or base.get('can_accept_content_changes')))) and not published)
        action_parser = self._job_parser(document_id, base_job_id, base) if draft else self.parser
        analyzing = {key for key, value in selected.items() if value in {'analyze', 'both'}}
        if analyzing and getattr(getattr(action_parser, 'config', None), 'provider', '') != 'paddle_vl':
            raise ValueError('이미지 분석은 PaddleOCR-VL 파서를 선택해야 합니다')
        checkpoints = self.storage.provider_checkpoint(document_id, base_job_id, 'initial').get('visuals', [])
        prior_events = list(checkpoints)
        provider_folder = self.storage.document_dir(document_id) / 'jobs' / base_job_id / 'provider'
        for attempt_folder in provider_folder.glob('image-*'):
            if re.fullmatch(r'image-[0-9a-f]{32}', attempt_folder.name):
                prior_events.extend(self.storage.provider_checkpoint(document_id, base_job_id, attempt_folder.name).get('visuals', []))
        # Selecting analyze/both here is the user's explicit authorization to
        # issue a new OCR request, including when an earlier request is unresolved.
        # Keep the old provider checkpoints intact; each image action gets a new
        # attempt namespace so the uncertain request remains auditable.
        for image in images.values():
            if not self.storage.image_path(document_id, base_job_id, image['asset_id']).is_file():
                raise ValueError('기준 작업의 원본 이미지를 찾을 수 없습니다')
        selected_id = embedding_profile_id or base.get('embedding_profile_id')
        publisher = self.publisher_for(selected_id, for_ingest=True)
        self.ensure_index_contract(publisher)
        if draft:
            if self.embedding_profiles and selected_id != base.get('embedding_profile_id'):
                raise ValueError('검토 중인 작업의 임베딩 모델은 변경할 수 없습니다')
            edit_id = uuid4().hex
            folder = self.storage.document_dir(document_id) / 'jobs' / base_job_id
            snapshot = folder / 'draft-edits' / edit_id
            try:
                snapshot.mkdir(parents=True)
                for name in ('parser_elements.json', 'parser_initial_elements.json', 'parser_result.json', 'elements.json', 'chunks.json', 'processing.json'):
                    if (folder / name).exists():
                        shutil.copy2(folder / name, snapshot / name)
                status = {**base, 'status': 'queued', 'stage': 'queued', 'mode': 'draft_image_actions',
                          'image_actions': selected, 'image_descriptions': selected_descriptions, 'image_attempt': 'image-' + edit_id,
                          'image_snapshot': edit_id, 'force_review': True, 'pending_review': True,
                          'image_policies_present': True, 'can_accept_partial': False,
                          'can_accept_content_changes': False, 'can_publish_draft': False, 'error': None}
                self.storage.save_job(document_id, base_job_id, 'processing', status)
            except Exception:
                shutil.rmtree(snapshot, ignore_errors=True)
                raise
            return {'document_id': document_id, 'job_id': base_job_id, **status}
        job_id = str(uuid4())
        folder = self.storage.document_dir(document_id) / 'jobs' / job_id
        status = {'status': 'queued', 'stage': 'queued', 'mode': 'image_actions', 'base_job_id': base_job_id,
                  'settings': base.get('settings', {}), 'profile': base.get('profile'),
                  'image_policies_present': True, 'force_review': True,
                  **{key: base[key] for key in ('xlsx_review', 'xlsx_review_basis', 'xlsx_review_stale') if base.get(key)},
                  'parser_contract': self.parser_contract(self.parser), 'image_actions': selected, 'image_descriptions': selected_descriptions,
                  'created_at': datetime.now(UTC).isoformat()}
        if publisher and self.embedding_policy(publisher):
            status['embedding_policy'] = self.embedding_policy(publisher)
        if self.embedding_profiles:
            status['embedding_profile_id'] = publisher.embedder.profile.profile_id
            status['embedding_contract'] = publisher.embedding_contract()
        try:
            shutil.copytree(self.storage.document_dir(document_id) / 'jobs' / base_job_id / 'images', folder / 'images')
            self._save_parser(document_id, job_id, self.parser)
            for item in checkpoints:
                if f"image-{item.get('index')}" not in analyzing:
                    self.storage.save_provider_event(document_id, job_id, 'initial', 'visual', item)
            self.storage.save_job(document_id, job_id, 'processing', status)
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        return {'document_id': document_id, 'job_id': job_id, **status}

    def content_loss(self, publisher, active, document_id, current):
        if not active or document_id not in active.get("document_ids", []) or not hasattr(publisher, "document_chunks"):
            return []
        chunks = publisher.document_chunks(document_id, active)
        by_job = {}
        for chunk in chunks:
            job = chunk.get("job_id")
            if not job:
                raise ValueError("기존 검색 내용의 출처를 확인할 수 없습니다")
            by_job.setdefault(job, set()).update([*chunk.get("element_ids", []), *chunk.get("heading_element_ids", []), *chunk.get("context_element_ids", [])])
        old = []
        for job, ids in by_job.items():
            elements = self.storage.load_job(document_id, job, "elements")
            found = {item["element_id"] for item in elements}
            if not ids <= found:
                raise ValueError("기존 검색 내용의 원문을 확인할 수 없습니다")
            old.extend(item for item in elements if item["element_id"] in ids)
        return compare(old, current)

    def _embed_node(self, state):
        document_id, job_id = state["document_id"], state["job_id"]
        status = self.storage.load_job(document_id, job_id, "processing")
        publisher = self.publisher_for(status.get("embedding_profile_id"), for_ingest=True)
        coverage = state.get("coverage", {})
        document = self.storage.load_document(document_id)
        partial = bool(coverage.get("missing_pages") or coverage.get("provider_incomplete")
                       or (document["format"] == "pdf" and coverage.get("unknown_scope"))
                       or unresolved_visuals(state.get('normalized', [])))
        active = publisher.active() if publisher and hasattr(publisher, "active") else None
        if state.get("resume_index") and active and hasattr(publisher, "document_chunks"):
            active_chunks = publisher.document_chunks(document_id, active)
            if active_chunks and all(chunk.get("job_id") == job_id for chunk in active_chunks):
                return {"publication": {"partial": partial, "changes": [], "preserve": False,
                                        "active_version": None, "manifest": active, "already_published": True}}
        if state.get("resume_index") and self._job_requires_copy(publisher, document_id, job_id):
            raise ValueError("이미 게시된 작업입니다. 새 작업에서 검색 준비를 요청해 주세요")
        changes = (self.content_loss(publisher, active, document_id, state["normalized"])
                   if publisher and status.get('mode') not in {'image_actions', 'draft_image_actions'} else [])
        preserve = bool(partial or (active and document_id in active.get("document_ids", []) and changes))
        if not state["chunks"] and not partial:
            raise IndexerError('NO_SEARCHABLE_CONTENT', '검색할 내용이 없습니다. 원본이나 추출 설정을 확인하고 새 작업으로 다시 읽어 주세요.', 422)
        data = {"partial": partial, "changes": changes, "preserve": preserve,
                "active_version": active.get("version") if preserve and active else None,
                'existing_document': bool(active and document_id in active.get('document_ids', []))}
        if publisher and not preserve and state["chunks"]:
            self.storage.save_job(document_id, job_id, "processing", {**status, "stage": "embedding"})
            if hasattr(publisher, "prepare"):
                def embedding_progress(completed, total):
                    current = self.storage.load_job(document_id, job_id, "processing")
                    self.storage.save_job(document_id, job_id, "processing", {**current, "progress": {
                        "unit": "chunks", "completed": completed, "total": total}})
                def document_progress(done, total, corpus_done, corpus_total):
                    current = self.storage.load_job(document_id, job_id, "processing")
                    self.storage.save_job(document_id, job_id, "processing", {**current, "progress": {
                        "unit": "chunks", "completed": done, "total": total,
                        "corpus_completed": corpus_done, "corpus_total": corpus_total}})
                embedding_progress.document_progress = document_progress
                prepared = publisher.prepare(document_id, state["chunks"], progress=embedding_progress)
                final_chunks = [chunk for chunk in prepared["chunks"] if chunk["document_id"] == document_id]
                audit = audit_representation(state["normalized"], final_chunks)
                self.storage.save_job(document_id, job_id, "chunks", final_chunks)
                current = self.storage.load_job(document_id, job_id, "processing")
                self.storage.save_job(document_id, job_id, "processing", {**current, "representation_audit": audit})
                data["prepared"] = prepared
        return {"publication": data}

    def _index_node(self, state):
        document_id, job_id = state["document_id"], state["job_id"]
        data = dict(state["publication"])
        publisher = self.publisher_for(self.storage.load_job(document_id, job_id, "processing").get("embedding_profile_id"), for_ingest=True)
        if publisher and not data["preserve"] and not data.get("already_published") and state["chunks"]:
            status = self.storage.load_job(document_id, job_id, "processing")
            self.storage.save_job(document_id, job_id, "processing", {**status, "stage": "indexing", "progress": None})
            data["manifest"] = publisher.commit(data.pop("prepared")) if "prepared" in data else publisher.publish(document_id, state["chunks"])
        return {"publication": data}

    def claim_job(self, document_id: str, job_id: str):
        self._ensure_not_deleted(document_id)
        self.storage.load_document(document_id)
        status = self.storage.load_job(document_id, job_id, "processing")
        if status.get("status") != "queued":
            raise ValueError("대기 중인 작업이 아닙니다")
        if (status.get("recovery_mode") in {"initial", "retry"}
            and self._job_requires_copy(self.publisher, document_id, job_id)):
            raise ValueError("이미 게시된 작업은 새 작업으로 이어서 처리해야 합니다")
        claim = uuid4().hex
        status = {**status, "status": "processing", "stage": "representing" if status.get("mode") == "rechunk" else "parsing", 'claim_id': claim}
        self.storage.save_job(document_id, job_id, "processing", status)
        return claim

    def run_job(self, document_id: str, job_id: str, *, claim_id=None):
        claim_id = claim_id or self.claim_job(document_id, job_id)
        self._ensure_not_deleted(document_id)
        document = self.storage.load_document(document_id)
        stored = self.storage.document_dir(document_id) / ("source." + document["format"])
        status = self.storage.load_job(document_id, job_id, "processing")
        if status.get('status') != 'processing' or status.get('claim_id') != claim_id:
            raise ValueError('작업 예약을 확인할 수 없습니다')
        profile, settings = status.get("profile"), status.get("settings") or {}
        try:
            try:
                publisher = self.publisher_for(status.get("embedding_profile_id"), for_ingest=True)
            except ValueError as error:
                raise IndexerError("EMBEDDING_PROFILE_INVALID", str(error), 409) from error
            if self.embedding_profiles and status.get("embedding_contract") != publisher.embedding_contract():
                raise IndexerError("EMBEDDING_PROFILE_CHANGED", "임베딩 설정이 변경되어 새 작업이 필요합니다.", 409)
            if status.get("embedding_policy") and status["embedding_policy"] != self.embedding_policy(publisher):
                raise ValueError("임베딩 입력 설정이 변경되었습니다. 새 작업을 요청해 주세요")
            job_parser = self._job_parser(document_id, job_id, status)
            image_attempt = status.get('image_attempt') if status.get('mode') == 'draft_image_actions' else None
            initial_checkpoint = self.storage.provider_checkpoint(document_id, job_id, image_attempt or 'initial')
            inputs = {"path": str(stored), "requested_profile": profile,
                      "document_id": document_id, "job_id": job_id, "settings": settings or {},
                      "embedding_max_input_bytes": (self.embedding_policy(publisher) or {}).get("max_input_bytes", 3800),
                      "recovery_mode": status.get("recovery_mode"),
                      "xlsx_review": status.get("xlsx_review", {}),
                      'provider_attempt': image_attempt or 'initial',
                      "provider_checkpoints": {'initial': initial_checkpoint,
                                               'retry': self.storage.provider_checkpoint(document_id, job_id, 'retry')}}
            if status.get("recovery_mode") == "index":
                inputs.update({"resume_index": True, "chunks": self.storage.load_job(document_id, job_id, "chunks"),
                               "normalized": self.storage.load_job(document_id, job_id, "elements"),
                               "coverage": status.get("coverage", {}), "profile": status.get("profile") or "general",
                               "profile_origin": status.get("profile_origin") or "default",
                               "parser_provider": status.get("provider")})
            if status.get("recovery_mode") == "retry":
                inputs["recovered_parse"] = {"provider": status["provider"],
                    "elements": self.storage.load_job(document_id, job_id, "parser_initial_elements"),
                    "raw": self.storage.load_job(document_id, job_id, "parser_result"),
                    "failed": status.get("provider_failed", []), "unavailable": status.get("provider_unavailable", []),
                    "incomplete": status.get("provider_incomplete", False),
                    "terminal_unavailable": status.get("provider_terminal_unavailable", [])}
            if status.get("mode") == "rechunk":
                base_id = status["base_job_id"]
                base = self.storage.load_job(document_id, base_id, "processing")
                try:
                    previous_elements = self.storage.load_job(document_id, base_id, 'elements')
                except FileNotFoundError:
                    previous_elements = []
                base_images = self.storage.document_dir(document_id) / 'jobs' / base_id / 'images'
                if base_images.is_dir():
                    destination = self.storage.document_dir(document_id) / 'jobs' / job_id / 'images'
                    shutil.copytree(base_images, destination, dirs_exist_ok=True)
                inputs["reused_parse"] = {"provider": base["provider"],
                                          "elements": self.storage.load_job(document_id, base_id, "parser_elements"),
                                          "raw": self.storage.load_job(document_id, base_id, "parser_result"),
                                          'previous_elements': previous_elements,
                                          "coverage": base.get("coverage"),
                                          "incomplete": base.get("provider_incomplete", False)}
                if status.get('xlsx_review_edit'):
                    from indexer.parsing.native_documents import apply_xlsx_review

                    reused = inputs['reused_parse']
                    reused['raw'], reused['elements'] = apply_xlsx_review(reused['raw'], reused['elements'], status['xlsx_review'])
                    reused['previous_elements'] = []
                elif status.get('xlsx_review_reset'):
                    from indexer.parsing.native_documents import reset_xlsx_review

                    reused = inputs['reused_parse']
                    reused['raw'], reused['elements'] = reset_xlsx_review(reused['raw'], reused['elements'],
                                                                          status['xlsx_review_reset'])
                    reused['previous_elements'] = []
            if status.get('mode') == 'image_actions':
                base_id = status['base_job_id']
                base = self.storage.load_job(document_id, base_id, 'processing')
                inputs['image_edit'] = {'provider': base['provider'],
                    'elements': self.storage.load_job(document_id, base_id, 'parser_elements'),
                    'raw': self.storage.load_job(document_id, base_id, 'parser_result'),
                    'previous_elements': self.storage.load_job(document_id, base_id, 'elements'),
                    'coverage': base.get('coverage'), 'incomplete': base.get('provider_incomplete', False),
                    'actions': status['image_actions'], 'descriptions': status.get('image_descriptions', {}),
                    'image_path': lambda asset_id: self.storage.image_path(document_id, job_id, asset_id)}
            elif status.get('mode') == 'draft_image_actions':
                edit_id = str(status.get('image_snapshot', ''))
                if not re.fullmatch(r'[0-9a-f]{32}', edit_id):
                    raise ValueError('검토 작업의 기준 기록을 확인할 수 없습니다')
                snapshot = self.storage.document_dir(document_id) / 'jobs' / job_id / 'draft-edits' / edit_id
                def saved(name):
                    return json.loads((snapshot / (name + '.json')).read_text(encoding='utf-8'))
                base = saved('processing')
                inputs['image_edit'] = {'provider': base['provider'], 'elements': saved('parser_elements'),
                    'raw': saved('parser_result'), 'previous_elements': saved('elements'),
                    'coverage': base.get('coverage'), 'incomplete': base.get('provider_incomplete', False),
                    'actions': status['image_actions'], 'descriptions': status.get('image_descriptions', {}),
                    'image_path': lambda asset_id: self.storage.image_path(document_id, job_id, asset_id)}
            state = (self.graph if job_parser is self.parser else self._graph(job_parser)).invoke(inputs)
            status = self.storage.load_job(document_id, job_id, "processing")
            status = {**status, "profile": state["profile"], "profile_origin": state["profile_origin"], "provider": state["parser_provider"]}
            coverage = state.get("coverage", {})
            publication = state["publication"]
            partial, preserve, changes = publication["partial"], publication["preserve"], publication["changes"]
            manifest = publication.get("manifest")
            resumable_attempt = None
            if coverage.get("unavailable_pages"):
                terminal = set(coverage.get("terminal_unavailable_pages", []))
                retry_unavailable = any(set(item.get("unavailable_pages", [])) - set(item.get("terminal_unavailable_pages", [])) for item in coverage.get("attempts", []) if item.get("mode") != "initial")
                options = (["initial"] if set(coverage.get("initial_unavailable_pages", [])) - terminal else []) + (["retry"] if retry_unavailable else [])
                for attempt in options:
                    if self.storage.provider_checkpoint(document_id, job_id, attempt).get("request_id"):
                        resumable_attempt = attempt
                        break
            status = {**status, "status": "partial" if partial or preserve else "completed" if manifest else "parsed",
                      "stage": "completed" if manifest or preserve or partial else "parsed", "existing_index_retained": bool(preserve and publication.get('existing_document')),
                      'pending_review': bool(preserve), "content_changes": changes, "comparison_index_version": publication.get('active_version') if preserve else None,
                      "can_accept_content_changes": bool(changes and preserve and not partial),
                      "can_accept_partial": bool(partial and preserve and state["chunks"] and publisher),
                      'can_publish_draft': bool(preserve and not partial and state['chunks'] and publisher and status.get('force_review')),
                      "can_resume": bool(resumable_attempt), "recovery_mode": resumable_attempt, "progress": None, "error": None}
            if partial and not state['chunks']:
                status.update(pending_review=False, can_accept_partial=False, can_publish_draft=False,
                              error={'code': 'NO_SEARCHABLE_CONTENT',
                                     'message': '현재 검색할 내용이 없습니다. 미완료 처리를 이어서 확인하거나 새 작업으로 다시 읽어 주세요.'})
            if manifest:
                status["index_version"] = manifest["version"]
            self.storage.save_job(document_id, job_id, "processing", status)
            return {"document_id": document_id, "job_id": job_id, **status}
        except Exception as error:
            status = self.storage.load_job(document_id, job_id, "processing")
            cause = error
            while cause and not isinstance(cause, httpx.HTTPStatusError):
                cause = cause.__cause__
            http_status = cause.response.status_code if isinstance(cause, httpx.HTTPStatusError) else None
            event = {"event": "ingestion_failed", "document_id": document_id, "job_id": job_id,
                     "stage": status.get("stage"), "error_type": type(error).__name__}
            if http_status is not None:
                event["http_status"] = http_status
            if isinstance(error, IndexerError):
                event["error_code"] = error.code
            LOG.error(json.dumps(event))
            if isinstance(error, IndexerError):
                message, error_code = error.message, error.code
            elif status.get("stage") == "embedding" and isinstance(error, httpx.HTTPStatusError):
                details = embedding_http_error(error)
                message, error_code = details["message"], details["code"]
            elif status.get("stage") in {"embedding", "indexing"}:
                message, error_code = "검색 준비에 실패했습니다. 설정을 확인한 후 저장된 청크부터 다시 시도해 주세요.", "INDEXING_FAILED"
            else:
                message, error_code = "문서 처리에 실패했습니다. 파서 연결과 입력 파일을 확인해 주세요.", "PROCESSING_FAILED"
            attempt = (status.get('image_attempt') if status.get('mode') == 'draft_image_actions' else
                       "retry" if status.get("stage") in {"retrying", "quality"} and status.get("provider") else "initial")
            checkpoint = self.storage.provider_checkpoint(document_id, job_id, attempt)
            can_resume = (not (isinstance(error, IndexerError) and error.code in {"PARSE_REQUEST_TERMINATED", "PARSER_PAGE_LIMIT"})
                          and resumable_provider_checkpoint(checkpoint, attempt, native_visual=can_resume_native_visual(status))
                          and status.get("stage") in {"parsing", "retrying", "quality"})
            error_info = {"code": error_code, "message": message}
            if error_code == 'NO_SEARCHABLE_CONTENT':
                status = {**status, 'stage': 'completed'}
            if http_status is not None:
                error_info["http_status"] = http_status
            if status.get('mode') == 'draft_image_actions' and re.fullmatch(r'[0-9a-f]{32}', str(status.get('image_snapshot', ''))):
                self._restore_draft_snapshot(document_id, job_id, status['image_snapshot'], error_info)
                raise IngestionFailure(document_id, job_id) from error
            self.storage.save_job(document_id, job_id, "processing", {**status, "status": "failed", "can_resume": can_resume,
                                   "recovery_mode": attempt if can_resume else None, "error": error_info})
            raise IngestionFailure(document_id, job_id) from error

    def claim_next(self, *, exclude_documents=()):
        for document in self.storage.list_documents():
            if document['document_id'] in exclude_documents or document['document_id'] in self._deleted_documents():
                continue
            for job in self.storage.list_jobs(document["document_id"]):
                if job.get("status") == "queued":
                    if (job.get("recovery_mode") in {"initial", "retry"}
                        and self._job_requires_copy(self.publisher, document["document_id"], job["job_id"])):
                        self.storage.save_job(document["document_id"], job["job_id"], "processing", {
                            **job, "status": "partial", "stage": "completed", "can_resume": True})
                        try:
                            self.resume_processing(document["document_id"], job["job_id"])
                        except ValueError as error:
                            current = self.storage.load_job(document["document_id"], job["job_id"], "processing")
                            self.storage.save_job(document["document_id"], job["job_id"], "processing", {
                                **current, "can_resume": False, "error": {"message": str(error)}})
                        return True
                    return (document['document_id'], job['job_id'], self.claim_job(document['document_id'], job['job_id']))
        return None

    def process_next(self):
        claimed = self.claim_next()
        if isinstance(claimed, tuple):
            try:
                self.run_job(claimed[0], claimed[1], claim_id=claimed[2])
            except IngestionFailure:
                pass
        return bool(claimed)

    def recover(self, *, exclude_jobs=(), exclude_documents=()):
        for document in self.storage.list_documents():
            document_id = document["document_id"]
            if document_id in exclude_documents:
                continue
            if document_id in self._deleted_documents():
                continue
            for job in self.storage.list_jobs(document_id):
                if (document_id, job['job_id']) in exclude_jobs:
                    continue
                stored = self.storage.load_job(document_id, job['job_id'], 'processing')
                if stored.get('mode') == 'draft_image_actions' and stored.get('status') == 'processing':
                    self._restore_draft_snapshot(document_id, job['job_id'], stored.get('image_snapshot'),
                        {'code': 'DRAFT_INTERRUPTED', 'message': '이미지 수정 중 서버가 중단되어 이전 검토 상태를 복원했습니다.'})
                    continue
                intent = stored.get('publication_intent')
                if intent and stored.get('status') == 'partial':
                    try:
                        publisher = self.publisher_for(stored.get('embedding_profile_id'), for_ingest=True)
                        active = publisher.active() if publisher else None
                        if active and hasattr(publisher, 'find_publication') and publisher.find_publication(intent.get('id')):
                            self.accept_content_changes(document_id, job['job_id'], accept_partial=intent['accept_partial'])
                            continue
                    except (ValueError, FileNotFoundError, KeyError, IngestionFailure):
                        LOG.warning(json.dumps({'event': 'publication_recovery_deferred', 'document_id': document_id,
                                                'job_id': job['job_id']}))
                normalized = normalize_review_status(stored)
                if (normalized is not stored and job.get('mode') == 'draft_image_actions'
                    and job.get('status') == 'partial' and job.get('pending_review')
                    and not normalized.get('coverage', {}).get('provider_incomplete')
                    and not job.get('content_changes') and not job.get('index_version')
                    and not any(other.get('status') in {'queued', 'processing'} for other in self.storage.list_jobs(document_id))):
                    try:
                        publisher = self.publisher_for(job.get('embedding_profile_id'), for_ingest=True)
                        active = publisher.active() if publisher else None
                        same_version = (active.get('version') if active else None) == job.get('comparison_index_version')
                        new_document = (not job.get('existing_index_retained') and
                                        (not active or document_id not in active.get('document_ids', [])))
                        eligible = (publisher and (same_version or new_document)
                                    and (not self.embedding_profiles or job.get('embedding_contract') == publisher.embedding_contract())
                                    and (not job.get('embedding_policy') or job['embedding_policy'] == self.embedding_policy(publisher))
                                    and not unresolved_visuals(self.storage.load_job(document_id, job['job_id'], 'elements'))
                                    and bool(self.storage.load_job(document_id, job['job_id'], 'chunks')))
                        if eligible:
                            self.storage.save_job(document_id, job['job_id'], 'processing', {**normalized,
                                'status': 'queued', 'stage': 'queued', 'recovery_mode': 'index',
                                'force_review': False, 'can_accept_partial': False, 'error': None})
                            continue
                    except (ValueError, FileNotFoundError, KeyError):
                        LOG.warning(json.dumps({'event': 'draft_auto_publish_deferred', 'document_id': document_id,
                                                'job_id': job['job_id']}))
                if (job.get('status') == 'failed' and job.get('stage') == 'parsing'
                        and can_resume_native_visual(job) and not job.get('parsed_available')):
                    checkpoint = self.storage.provider_checkpoint(document_id, job['job_id'], 'initial')
                    if (any(item.get('status') == 'started' for item in checkpoint.get('visuals', []))
                            and resumable_provider_checkpoint(checkpoint, 'initial', native_visual=True)
                            and self._can_use_job_parser(document_id, job['job_id'], job)):
                        self.storage.save_job(document_id, job['job_id'], 'processing', {
                            **job, 'status': 'queued', 'stage': 'queued', 'recovery_mode': 'initial',
                            'previous_error': job.get('error'), 'error': None, 'can_resume': False, 'progress': None})
                        continue
                if job.get("status") == "processing":
                    attempt = (job.get('image_attempt') if job.get('mode') == 'draft_image_actions' else
                               "retry" if job.get("stage") in {"retrying", "quality"} and job.get("provider") else "initial")
                    checkpoint = self.storage.provider_checkpoint(document_id, job["job_id"], attempt)
                    resumable = resumable_provider_checkpoint(checkpoint, attempt, native_visual=can_resume_native_visual(job)) and self._can_use_job_parser(document_id, job["job_id"], job)
                    recovery_mode = attempt if resumable else None
                    if job.get("stage") in {"embedding", "indexing"} and self._can_use_job_parser(document_id, job["job_id"], job):
                        try:
                            self.storage.load_job(document_id, job["job_id"], "chunks")
                            self.storage.load_job(document_id, job["job_id"], "elements")
                            resumable, recovery_mode = True, "index"
                        except (ValueError, FileNotFoundError):
                            pass
                    published_parse = (resumable and recovery_mode in {"initial", "retry"}
                                       and self._job_requires_copy(self.publisher, document_id, job["job_id"]))
                    self.storage.save_job(document_id, job["job_id"], "processing", {
                        **job, "status": "partial" if published_parse else "queued" if resumable else "failed",
                        "stage": "completed" if published_parse else job.get("stage"), "recovery_mode": recovery_mode,
                        "can_resume": resumable, "error": {"message": "서버가 중단되어 작업을 확인해야 합니다."} if not resumable else None})
                    if published_parse:
                        try:
                            self.resume_processing(document_id, job["job_id"])
                        except ValueError as error:
                            current = self.storage.load_job(document_id, job["job_id"], "processing")
                            self.storage.save_job(document_id, job["job_id"], "processing", {
                                **current, "can_resume": False, "error": {"message": str(error)}})

    def resume_processing(self, document_id: str, job_id: str):
        self._ensure_not_deleted(document_id)
        status = self.storage.load_job(document_id, job_id, "processing")
        if status.get("status") not in {"failed", "partial"} or not status.get("can_resume"):
            raise ValueError("이 작업은 저장된 처리 기록에서 재개할 수 없습니다")
        self._job_parser(document_id, job_id, status)
        publisher = self.publisher_for(status.get("embedding_profile_id"), for_ingest=True)
        if self.embedding_profiles and status.get("embedding_contract") != publisher.embedding_contract():
            raise ValueError("임베딩 설정이 변경되어 기존 작업을 이어서 처리할 수 없습니다")
        if any(job.get("status") in {"queued", "processing"} for job in self.storage.list_jobs(document_id)):
            raise ValueError("이 문서의 다른 작업이 진행 중입니다")
        attempt = status.get("recovery_mode")
        checkpoint = self.storage.provider_checkpoint(document_id, job_id, attempt) if attempt else {}
        if not resumable_provider_checkpoint(checkpoint, attempt, native_visual=can_resume_native_visual(status)):
            raise ValueError("저장된 처리 요청을 확인할 수 없습니다")
        published = self._job_requires_copy(publisher, document_id, job_id)
        resumed = {**status, "status": "queued", "stage": "queued", "progress": None,
                   "previous_error": status.get("error") or status.get("previous_error"),
                   "error": None, "can_resume": False, "can_accept_content_changes": False,
                   "can_accept_partial": False}
        if published:
            new_job_id = str(uuid4())
            jobs_dir = self.storage.document_dir(document_id) / "jobs"
            temporary = jobs_dir / (".resume-" + new_job_id)
            target = jobs_dir / new_job_id
            try:
                shutil.copytree(jobs_dir / job_id, temporary)
                resumed = {**resumed, "base_job_id": job_id, "created_at": datetime.now(UTC).isoformat()}
                resumed.pop("index_version", None)
                resumed.pop("partial_accepted", None)
                resumed.pop("content_changes_accepted", None)
                atomic_json(temporary / "processing.json", resumed)
                temporary.rename(target)
            except Exception:
                shutil.rmtree(temporary, ignore_errors=True)
                raise
            job_id = new_job_id
        else:
            self.storage.save_job(document_id, job_id, "processing", resumed)
        return {"document_id": document_id, "job_id": job_id, **resumed}

    def delete_document(self, document_id: str):
        document = self.storage.load_document(document_id)
        if (document_id not in self._deleted_documents() and
                any(job.get("status") in {"queued", "processing"} for job in self.storage.list_jobs(document_id))):
            raise ValueError("진행 중인 작업이 있어 삭제할 수 없습니다")
        if self.publisher:
            self.publisher.delete_document(document_id)
        folder = self.storage.document_dir(document_id)
        metadata = folder / "document.json"
        try:
            for path in folder.iterdir():
                if path == metadata:
                    continue
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            metadata.unlink()
            folder.rmdir()
        except Exception:
            if folder.exists() and not metadata.exists():
                atomic_json(metadata, document)
            raise

    def delete_job(self, document_id: str, job_id: str):
        self.storage.load_job(document_id, job_id, "processing")
        jobs = self.storage.list_jobs(document_id)
        if any(job.get("status") in {"queued", "processing"} for job in jobs):
            raise ValueError("진행 중인 작업이 있어 삭제할 수 없습니다")
        if len(jobs) < 2:
            raise ValueError("마지막 작업은 삭제할 수 없습니다. 문서 삭제를 이용해 주세요")
        if self.publisher and (self.publisher.job_may_be_referenced(document_id, job_id)
                               if hasattr(self.publisher, 'job_may_be_referenced')
                               else self.publisher.job_is_indexed(document_id, job_id)):
            raise ValueError("검색 버전에서 참조하는 작업은 삭제할 수 없습니다")
        shutil.rmtree(self.storage.document_dir(document_id) / "jobs" / job_id)

    def content_review(self, document_id, job_id):
        status = self.storage.load_job(document_id, job_id, "processing")
        if not status.get("can_accept_content_changes"):
            raise ValueError("검토할 내용이 없습니다")
        publisher = self.publisher_for(status.get("embedding_profile_id"))
        manifest = publisher.get_version(status["comparison_index_version"])
        if not manifest:
            raise ValueError("비교할 검색 버전을 찾을 수 없습니다")
        previous = {}
        for chunk in publisher.document_chunks(document_id, manifest):
            previous.setdefault(chunk["job_id"], set()).update([*chunk.get("element_ids", []), *chunk.get("heading_element_ids", []), *chunk.get("context_element_ids", [])])
        old_elements = []
        for source_job, ids in previous.items():
            old_elements.extend({**element, "job_id": source_job} for element in self.storage.load_job(document_id, source_job, "elements") if element["element_id"] in ids)
        new_elements = self.storage.load_job(document_id, job_id, "elements")
        def details(elements, page):
            result = []
            for element in elements:
                if element.get("page") != page or not element.get("text"):
                    continue
                table = parse_table_html(element.get("html") or "").find("table")
                cells = cells_from(table) if table and not table.find("table") else []
                result.append({"element_id": element["element_id"], "job_id": element.get("job_id", job_id),
                               "kind": element["kind"], "text": element["text"],
                               "cells": [{"row": c.row + 1, "column": c.column + 1, "text": c.text,
                                          "rowspan": c.rows, "colspan": c.columns} for c in cells]})
            return result
        return {"index_version": manifest["version"], "pages": [
            {**change, "previous": details(old_elements, change["page"]), "current": details(new_elements, change["page"])}
            for change in status["content_changes"]]}

    def accept_content_changes(self, document_id, job_id, *, accept_partial=False):
        self._ensure_not_deleted(document_id)
        stored_status = self.storage.load_job(document_id, job_id, "processing")
        status = normalize_review_status(stored_status)
        legacy_normalized = status is not stored_status
        if (status.get('content_changes_accepted') and status.get('index_version')
                and status.get('partial_accepted', False) == accept_partial):
            publisher = self.publisher_for(status.get('embedding_profile_id'), for_ingest=True)
            manifest = publisher.get_version(status['index_version']) if hasattr(publisher, 'get_version') else None
            if manifest and hasattr(publisher, 'document_chunks'):
                published = publisher.document_chunks(document_id, manifest)
                if published and all(chunk.get('job_id') == job_id for chunk in published):
                    return {'document_id': document_id, 'job_id': job_id, **status}
        allowed = (status.get("can_accept_partial") if accept_partial else
                   status.get("can_accept_content_changes") or status.get('can_publish_draft'))
        if not allowed or status.get("status") != "partial":
            raise ValueError("검토 후 적용할 수 있는 작업이 아닙니다")
        if any(job.get("status") in {"queued", "processing"} for job in self.storage.list_jobs(document_id)):
            raise ValueError("이 문서의 다른 작업이 진행 중입니다")
        publisher = self.publisher_for(status.get("embedding_profile_id"), for_ingest=True)
        if self.embedding_profiles and status.get("embedding_contract") != publisher.embedding_contract():
            raise ValueError("임베딩 설정이 변경되어 새 작업이 필요합니다")
        active = publisher.active()
        intent = status.get('publication_intent')
        if intent and intent.get('accept_partial') != accept_partial:
            raise ValueError('이전 게시 요청과 다른 승인 방식입니다')
        published_manifest = (publisher.find_publication(intent.get('id')) if intent and active
                              and hasattr(publisher, 'find_publication') else None)
        already_published = bool(published_manifest)
        if not already_published and (active.get('version') if active else None) != status.get("comparison_index_version"):
            raise ValueError("비교한 검색 버전이 변경되었습니다. 새 작업으로 다시 확인해 주세요")
        chunks = self.storage.load_job(document_id, job_id, "chunks")
        elements = self.storage.load_job(document_id, job_id, 'elements')
        if unresolved_visuals(elements):
            raise ValueError('확인이 필요한 이미지의 처리 방식을 모두 선택해 주세요')
        if already_published:
            published_chunks = publisher.document_chunks(document_id, published_manifest)
            if not published_chunks or any(chunk.get('job_id') != job_id for chunk in published_chunks):
                raise ValueError('게시된 작업의 출처를 확인할 수 없습니다')
            manifest = published_manifest
        else:
            if hasattr(publisher, '_published_versions'):
                if not intent:
                    intent = {'id': str(uuid4()), 'accept_partial': accept_partial}
                    status = {**status, 'publication_intent': intent}
                    self.storage.save_job(document_id, job_id, 'processing', status)
                kwargs = {'publication_id': intent['id']}
            else:
                kwargs = {}
            try:
                manifest = publisher.publish(document_id, chunks, **kwargs)
            except Exception as error:
                raise IngestionFailure(document_id, job_id) from error
        coverage = status.get('coverage') or {}
        remains_partial = bool(coverage.get('provider_incomplete') or coverage.get('missing_pages')
                               or coverage.get('unavailable_pages') or (coverage.get('unknown_scope') and
                                   self.storage.load_document(document_id).get('format') == 'pdf'))
        resolved_legacy_notice = legacy_normalized and not remains_partial
        status = {**status, "status": "partial" if accept_partial and not resolved_legacy_notice else "completed", "stage": "completed", "existing_index_retained": False,
                  "can_accept_partial": False, "partial_accepted": accept_partial, 'pending_review': False, 'can_publish_draft': False,
                  "can_resume": status.get("can_resume", False),
                  "can_accept_content_changes": False, "content_changes_accepted": True,
                  "index_version": manifest["version"], "error": None}
        status.pop('publication_intent', None)
        self.storage.save_job(document_id, job_id, "processing", status)
        return {"document_id": document_id, "job_id": job_id, **status}

    def resume_index(self, document_id: str, job_id: str):
        self._ensure_not_deleted(document_id)
        status = self.storage.load_job(document_id, job_id, "processing")
        if any(job.get('status') in {'queued', 'processing'} and job['job_id'] != job_id
               for job in self.storage.list_jobs(document_id)):
            raise ValueError('이 문서의 다른 작업이 진행 중입니다')
        publisher = self.publisher_for(status.get("embedding_profile_id"), for_ingest=True)
        if status.get("status") != "failed" or status.get("stage") not in {"embedding", "indexing"} or not publisher:
            raise ValueError("저장된 청크에서 재개할 수 없는 작업입니다")
        if self.embedding_profiles and status.get("embedding_contract") != publisher.embedding_contract():
            raise ValueError("임베딩 설정이 변경되어 새 작업이 필요합니다")
        if status.get("embedding_policy") and status["embedding_policy"] != self.embedding_policy(publisher):
            raise ValueError("임베딩 입력 설정이 변경되어 새 작업이 필요합니다")
        chunks = self.storage.load_job(document_id, job_id, "chunks")
        current_elements = self.storage.load_job(document_id, job_id, "elements")
        self.storage.save_job(document_id, job_id, "processing", {
            **status, "status": "processing", "stage": "embedding", "progress": None,
            "previous_error": status.get("error") or status.get("previous_error"), "error": None,
            "can_accept_content_changes": False, "can_accept_partial": False})
        try:
            state = self.graph.invoke({"resume_index": True, "document_id": document_id, "job_id": job_id,
                                       "chunks": chunks, "normalized": current_elements, "coverage": status.get("coverage", {})})
        except Exception as error:
            current = self.storage.load_job(document_id, job_id, "processing")
            details = embedding_http_error(error) if current.get("stage") == "embedding" and isinstance(error, httpx.HTTPStatusError) else {
                "code": "INDEXING_FAILED", "message": "검색 준비에 실패했습니다. 저장된 청크부터 다시 시도해 주세요."}
            self.storage.save_job(document_id, job_id, "processing", {**current, "status": "failed", "error": details})
            LOG.error(json.dumps({"event": "ingestion_failed", "document_id": document_id, "job_id": job_id,
                "stage": current.get("stage"), "error_type": type(error).__name__,
                "error_code": details["code"], **({"http_status": details["http_status"]} if "http_status" in details else {})}))
            raise IngestionFailure(document_id, job_id) from error
        publication = state["publication"]
        partial, preserve, changes = publication["partial"], publication["preserve"], publication["changes"]
        manifest = publication.get("manifest")
        status = self.storage.load_job(document_id, job_id, "processing")
        status = {**status, "status": "partial" if partial or preserve else "completed", "stage": "completed",
                  "existing_index_retained": bool(preserve and publication.get('existing_document')), 'pending_review': bool(preserve), "content_changes": changes,
                  "comparison_index_version": publication.get("active_version"),
                  "can_accept_content_changes": bool(changes and preserve and not partial),
                  "can_accept_partial": bool(partial and preserve and chunks),
                  'can_publish_draft': bool(preserve and not partial and chunks and status.get('force_review')), "error": None}
        if manifest:
            status["index_version"] = manifest["version"]
        self.storage.save_job(document_id, job_id, "processing", status)
        return {"document_id": document_id, "job_id": job_id, **status}
