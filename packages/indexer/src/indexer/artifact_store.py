"""원본과 처리 산출물 저장소."""

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from uuid import UUID, uuid4

from indexer.review_status import normalize_review_status


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".writing-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = root.resolve() / "ingestion"
        self.root.mkdir(parents=True, exist_ok=True)

    def document_dir(self, document_id: str) -> Path:
        return self.root / "documents" / str(UUID(document_id))

    def image_path(self, document_id: str, job_id: str, asset_id: str) -> Path:
        if not re.fullmatch(r'[0-9a-f]{64}', asset_id):
            raise ValueError('이미지 식별자가 올바르지 않습니다')
        return self.document_dir(document_id) / 'jobs' / str(UUID(job_id)) / 'images' / asset_id

    def save_image(self, document_id: str, job_id: str, asset_id: str, data: bytes):
        if hashlib.sha256(data).hexdigest() != asset_id:
            raise ValueError('이미지 내용이 식별자와 일치하지 않습니다')
        path = self.image_path(document_id, job_id, asset_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != asset_id:
                raise ValueError('저장된 이미지의 내용이 올바르지 않습니다')
            return
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.writing-')
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def register(self, source: Path) -> dict:
        document_id = str(uuid4())
        folder = self.document_dir(document_id)
        folder.mkdir(parents=True)
        suffix = source.suffix.lower()
        destination = folder / ("source" + suffix)
        try:
            shutil.copyfile(source, destination)
            with destination.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            metadata = {"document_id": document_id, "filename": source.name,
                        "format": suffix.lstrip("."), "sha256": digest}
            atomic_json(folder / "document.json", metadata)
            return metadata
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise

    def load_document(self, document_id: str):
        return json.loads((self.document_dir(document_id) / "document.json").read_text(encoding="utf-8"))

    def list_documents(self):
        result = []
        for path in (self.root / "documents").glob("*/document.json"):
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
                if self.document_dir(document["document_id"]) == path.parent:
                    result.append(document)
            except (ValueError, KeyError, OSError):
                continue
        return result

    def list_jobs(self, document_id: str):
        jobs = []
        for path in (self.document_dir(document_id) / "jobs").glob("*/processing.json"):
            try:
                job_id = str(UUID(path.parent.name))
                item = json.loads(path.read_text(encoding="utf-8"))
                item = normalize_review_status(item)
                try:
                    snapshot = json.loads((path.parent / 'parser_config.json').read_text(encoding='utf-8'))
                    parser_provider = snapshot.get('config', {}).get('provider')
                except (OSError, ValueError, AttributeError):
                    parser_provider = None
                if 'pending_review' not in item and not item.get('index_version') and (item.get('can_accept_partial') or item.get('can_accept_content_changes')):
                    item['pending_review'] = True
                jobs.append({**item, "job_id": job_id, "document_id": document_id, 'parser_provider': parser_provider,
                             "parsed_available": bool(item.get("provider") and
                                 (path.parent / "parser_elements.json").is_file() and
                                 (path.parent / "parser_result.json").is_file())})
            except (ValueError, OSError):
                continue
        return sorted(jobs, key=lambda job: job.get("created_at", ""), reverse=True)

    def save_job(self, document_id: str, job_id: str, name: str, value):
        if name not in {"parser_result", "parser_elements", "parser_initial_elements", "parse_attempts", "source_details", "elements", "chunks", "processing", "parser_config"}:
            raise ValueError("알 수 없는 산출물 유형입니다")
        path = self.document_dir(document_id) / "jobs" / str(UUID(job_id)) / (name + ".json")
        atomic_json(path, value)

    def load_job(self, document_id: str, job_id: str, name: str):
        if name not in {"parser_result", "parser_elements", "parser_initial_elements", "parse_attempts", "source_details", "elements", "chunks", "processing", "parser_config"}:
            raise ValueError("알 수 없는 산출물 유형입니다")
        path = self.document_dir(document_id) / "jobs" / str(UUID(job_id)) / (name + ".json")
        return json.loads(path.read_text(encoding="utf-8"))

    def provider_checkpoint(self, document_id: str, job_id: str, attempt: str):
        if attempt not in {"initial", "retry"} and not re.fullmatch(r'image-[0-9a-f]{32}', attempt):
            raise ValueError("알 수 없는 파싱 단계입니다")
        folder = self.document_dir(document_id) / "jobs" / str(UUID(job_id)) / "provider" / attempt
        state = json.loads((folder / "state.json").read_text(encoding="utf-8")) if (folder / "state.json").exists() else {}
        state["batches"] = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(folder.glob("batch-*.json"))]
        state['visuals'] = [json.loads(path.read_text(encoding='utf-8')) for path in sorted(folder.glob('visual-*.json'))]
        if (folder / "result.json").exists():
            state["result"] = json.loads((folder / "result.json").read_text(encoding="utf-8"))
        return state

    def save_provider_event(self, document_id: str, job_id: str, attempt: str, event: str, data: dict):
        if attempt not in {"initial", "retry"} and not re.fullmatch(r'image-[0-9a-f]{32}', attempt):
            raise ValueError("알 수 없는 파싱 단계입니다")
        folder = self.document_dir(document_id) / "jobs" / str(UUID(job_id)) / "provider" / attempt
        if event == "batch":
            start, end = data.get("start"), data.get("end")
            if type(start) is not int or type(end) is not int or not 1 <= start <= end <= 2000:
                raise ValueError("파싱 범위가 올바르지 않습니다")
            atomic_json(folder / f"batch-{start:04d}-{end:04d}.json", data)
        elif event == 'visual' and (attempt == 'initial' or attempt.startswith('image-')):
            index = data.get('index')
            if type(index) is not int or not 0 <= index < 100 or data.get('status') not in {'started', 'completed'}:
                raise ValueError('이미지 분석 상태가 올바르지 않습니다')
            atomic_json(folder / f'visual-{index:04d}.json', data)
        elif event == "result" and attempt == "retry":
            atomic_json(folder / "result.json", data)
        elif event in {"started", "request_id", "plan", "submission_started"}:
            path = folder / "state.json"
            state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            state.update({"started": True} if event == "started" else {"submission_started": True} if event == "submission_started" else data)
            atomic_json(path, state)
        else:
            raise ValueError("알 수 없는 파싱 이벤트입니다")
