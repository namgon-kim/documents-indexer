import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { ApiError, api, remove, uploadIngestion } from "./api";
import type { EmbeddingProfile, Options, Session, Settings } from "./api";
import { ProcessProgress } from "./ProcessProgress";
import { ImageThumbnail, analysisLabels } from "./VisualAssets";
import type { VisualItem } from "./api";

type Job = { job_id: string; status: string; stage: string; settings: Settings; profile: string | null;
  parser_provider?: string | null; provider?: string;
  error?: {message: string}; index_version?: string; parsed_available: boolean; mode?: string; created_at?: string;
  embedding_profile_id?: string;
  image_policies_present?: boolean;
  representation_audit?: {unrepresented_elements: string[]; missing_table_cells: unknown[]; missing_text_terms?: unknown[]; shortened_heading_chunks?: unknown[]; shortened_context_elements?: unknown[]};
  existing_index_retained?: boolean;
  can_resume?: boolean;
  can_accept_content_changes?: boolean;
  can_accept_partial?: boolean;
  can_publish_draft?: boolean; pending_review?: boolean;
  content_changes?: {page: number | null; missing_word_count: number; missing_cell_count: number; changed_table_row_count: number}[];
  progress?: {unit?: string; completed: number; total: number | null} | null;
  source_summary?: {format: string; sheets?: {name: string; visibility: string; formula_count: number;
    formula_scan_complete: boolean; missing_cached_values: number}[];
    slides?: {number: number; hidden: boolean; notes_present: boolean | null}[]};
  coverage?: {total_pages: number | null; missing_pages: number[]; unavailable_pages?: number[]; unknown_scope: boolean; provider_incomplete?: boolean; visual_without_text_pages?: number[]; blank_pages?: number[]; terminal_unavailable_pages?: number[];
    warnings?: string[]; limitations?: string[];
    initial_missing_pages?: number[]; retry_page_limit?: number; provider_total_pages?: number | null;
    attempts?: {mode: string; pages: number[] | null; status?: string; reason?: string; ocr?: string; dpi?: number | null; reused?: boolean}[]} };
type Document = { document_id: string; filename: string; format: string; indexed: boolean; deletion_pending: boolean; latest_job: Job; jobs: Job[] };

const labels: Record<string, string> = { queued: "대기 중", processing: "처리 중", completed: "검색 가능", partial: "일부 처리", failed: "처리 실패", parsed: "파싱 완료" };
const parserName = (value?: string | null) => value === 'paddle_vl' ? 'PaddleOCR-VL' : value === 'upstage' ? 'Upstage' : value || '정보 없음';
const fileIcon = <svg viewBox="0 0 22 26"><path d="M3 1.5h10.5L19 7v17.5H3z"/><path d="M13.5 1.5V7H19M6.5 12h9M6.5 15.5h9M6.5 19h6"/></svg>;
const attemptLabels: Record<string, string> = {requested: "요청 중", completed: "완료", failed: "실패", terminated: "원격 처리 종료", skipped: "수행하지 않음"};

function coverageMessage(coverage: NonNullable<Job["coverage"]>) {
  const remaining = coverage.missing_pages.length;
  if (!remaining) return "";
  const initial = coverage.initial_missing_pages?.length;
  const retry = coverage.attempts?.find(attempt => attempt.mode !== "initial");
  if (!initial || !retry) return `${remaining}쪽을 읽지 못했습니다.`;
  if (retry.reason === "results_unavailable") return `빠진 ${initial}쪽의 처리 결과를 아직 확인하지 못했습니다.`;
  if (retry.reason === "retry_outcome_unknown") return "다시 읽기 요청의 접수 여부를 확인할 수 없어 중복 요청하지 않았습니다.";
  if (retry.status === "skipped" && retry.mode === "full" && retry.reason === "retry_limit") {
    return `빠진 ${initial}쪽이 전체의 절반을 넘어 문서 전체 ${coverage.total_pages}쪽을 다시 읽어야 하지만, 자동으로 다시 읽을 수 있는 ${coverage.retry_page_limit ?? 0}쪽을 초과했습니다.`;
  }
  if (retry.status === "skipped") return `빠진 ${initial}쪽을 자동으로 다시 읽지 못했습니다.`;
  if (retry.status === "failed") return `빠진 ${initial}쪽을 다시 읽지 못했습니다. ${remaining}쪽이 남았습니다.`;
  if (retry.status === "completed") {
    const count = retry.pages?.length ?? 0;
    const recovered = Math.max(0, initial - remaining);
    return `빠진 ${initial}쪽 중 ${retry.mode === "full" ? `전체 ${count}쪽` : `${count}쪽`}을 다시 읽어 ${recovered}쪽을 복구했습니다. ${remaining}쪽이 남았습니다.`;
  }
  return `빠진 ${initial}쪽을 확인했으며, 현재 ${remaining}쪽이 남았습니다.`;
}

function DocumentCard({item, busy, modelEnabled, embeddingProfile, supportsDpi, parserFormats, nativeVisualSupported, onResume, onReprocess, onDelete, session, onChanged}: {item: Document; busy: boolean; modelEnabled: boolean; embeddingProfile: string; supportsDpi: boolean; parserFormats: string[]; nativeVisualSupported: boolean;
  onResume: (item: Document, job: Job) => void;
  session: Session; onChanged: () => void;
  onReprocess: (item: Document, job: Job, mode: "reparse" | "rechunk" | "complete_images", settings: Partial<Settings>) => void;
  onDelete: (item: Document, job: Job, type: "document" | "job") => void}) {
  const [review, setReview] = useState<{pages: {page: number | null; previous: {element_id: string; job_id: string; text: string; cells: {row: number; column: number; text: string; rowspan: number; colspan: number}[]}[]; current: {element_id: string; job_id: string; text: string; cells: {row: number; column: number; text: string; rowspan: number; colspan: number}[]}[]}[]}>();
  const [reviewError, setReviewError] = useState("");
  const [reviewBusy, setReviewBusy] = useState(false);
  const [images, setImages] = useState<VisualItem[] | null>(null);
  const [imageAnalysisAvailable, setImageAnalysisAvailable] = useState(nativeVisualSupported);
  const [imageChoices, setImageChoices] = useState<Record<string, string>>({});
  const [bulkImageChoice, setBulkImageChoice] = useState('keep');
  const [imageDescriptions, setImageDescriptions] = useState<Record<string, string>>({});
  const [showAllImages, setShowAllImages] = useState(false);
  const [openImage, setOpenImage] = useState<string | null>(null);
  const [imageSubmitting, setImageSubmitting] = useState(false);
  const [selected, setSelected] = useState(item.latest_job.job_id);
  const job = item.jobs.find(value => value.job_id === selected) || item.latest_job;
  const [target, setTarget] = useState(job.settings.target_chars ?? 1200);
  const [overlap, setOverlap] = useState(job.settings.overlap_chars ?? 120);
  const [dpi, setDpi] = useState<number | null>(job.settings.dpi ?? null);
  const [readMode, setReadMode] = useState<Settings["extraction_mode"]>(job.settings.extraction_mode ?? "parser");
  useEffect(() => { setSelected(item.latest_job.job_id); }, [item.latest_job.job_id]);
  useEffect(() => {
    setTarget(job.settings.target_chars ?? 1200); setOverlap(job.settings.overlap_chars ?? 120);
    setDpi(job.settings.dpi ?? null); setReview(undefined); setReviewError("");
    setReadMode(job.settings.extraction_mode ?? "parser"); setImages(null); setImageChoices({});
  }, [job.job_id]);
  const running = item.jobs.some(value => value.status === "queued" || value.status === "processing");
  const chunks = {target_chars: target, overlap_chars: overlap};
  const visibleImages = (images || []).filter(image => showAllImages || !job.pending_review || (image.analysis_status !== 'completed' && (!image.policy || ['analyze', 'both'].includes(image.policy))));
  const coverage = job.coverage;
  const notices: string[] = [];
  if (coverage?.missing_pages.length) notices.push(`${coverageMessage(coverage)} 읽지 못한 페이지: ${coverage.missing_pages.join(", ")}`);
  if (coverage?.unknown_scope) notices.push("전체 페이지의 누락 여부를 확인하지 못했습니다.");
  if (coverage?.provider_incomplete) notices.push("일부 내용이 처리되지 않았거나 결과의 완전성을 확인할 수 없습니다.");
  notices.push(...(coverage?.warnings || []));
  if (coverage?.visual_without_text_pages?.length) notices.push(`그림·도식에서 글자를 확인하지 못한 페이지: ${coverage.visual_without_text_pages.join(", ")}. OCR 실패일 수 있습니다.`);
  if (coverage?.unavailable_pages?.length) notices.push(`결과를 가져오지 못한 페이지: ${coverage.unavailable_pages.join(", ")}.${job.can_resume ? " 이어서 처리하면 기존 요청에서 다시 확인합니다." : ""}`);
  if (coverage?.terminal_unavailable_pages?.length) notices.push(`원격 처리가 종료되어 결과를 받을 수 없는 페이지: ${coverage.terminal_unavailable_pages.join(", ")}.`);
  if (coverage?.attempts?.some(attempt => attempt.status === "skipped" && !["results_unavailable", "retry_outcome_unknown"].includes(attempt.reason || ""))) notices.push("페이지 수 또는 파서 지원 범위로 인해 자동으로 다시 읽지 못했습니다.");
  if (coverage?.attempts?.some(attempt => attempt.status === "terminated")) notices.push("다시 읽기 요청이 종료되어 결과를 받을 수 없습니다.");
  if (coverage?.attempts?.some(attempt => attempt.status === "failed")) notices.push("자동 재시도에 실패하여 최초 파싱 결과를 보존했습니다.");
  const uniqueNotices = [...new Set(notices)];
  return <article className="hit" id={`document-${item.document_id}`}>
    <div className="document-card-head"><div className="document-icon" aria-hidden="true">{fileIcon}</div><div className="document-name"><strong>{item.filename.normalize("NFC")}</strong><small>{item.format.toUpperCase()} · {item.document_id.slice(0, 8)}</small></div><span className={"badge " + job.status}>{labels[job.status] || job.status}</span></div>
    <ProcessProgress job={job} action={!item.deletion_pending && ((job.status === "failed" && ["embedding", "indexing"].includes(job.stage)) || (job.can_resume && ["failed", "partial"].includes(job.status)))
      ? <button type="button" disabled={busy || running} className="secondary" onClick={() => onResume(item, job)}>{job.can_resume ? "이어서 처리" : "검색 준비부터 다시 처리"}</button> : undefined}/>
    <details className="document-details" key={`${job.job_id}-${Boolean(job.pending_review)}`} open={job.pending_review || undefined}><summary>처리 상세 및 작업 관리</summary>
    {item.jobs.length > 1 && <label>작업 기록<select value={job.job_id} onChange={event => setSelected(event.target.value)}>
      {item.jobs.map(value => <option key={value.job_id} value={value.job_id}>{labels[value.status] || value.status} · {value.mode === "rechunk" ? "청킹부터" : (value.mode === "image_actions" || value.mode === "draft_image_actions") ? "이미지별 처리" : value.mode === "complete_images" ? "미처리 이미지 분석" : value.mode === "reparse" ? "다시 파싱" : "최초 처리"} · {value.embedding_profile_id || "모델 정보 없음"} · {value.job_id.slice(0, 8)}</option>)}
    </select></label>}
    {job.embedding_profile_id && <p className="hint">이 작업에 사용한 임베딩 모델 · {job.embedding_profile_id}</p>}
    <p className="hint">문서 읽기 · {job.provider?.startsWith('native_') || (job.settings.extraction_mode && job.settings.extraction_mode !== 'parser') ? `직접 추출${job.settings.extraction_mode === 'native_visual' ? ` · 이미지 분석 ${parserName(job.parser_provider)}` : ''}` : parserName(job.parser_provider || job.provider)}</p>
    {job.parsed_available && <><button type="button" className="secondary image-list-toggle" onClick={async () => {
      if (images !== null) {setImages(null); return;}
      try {const result = await api<{images: VisualItem[]; analysis_available: boolean}>(`/v1/ingest/${item.document_id}/jobs/${job.job_id}/images`, session); setImages(result.images); setImageAnalysisAvailable(result.analysis_available);}
      catch (error) {setReviewError(error instanceof Error ? error.message : '이미지를 불러오지 못했습니다.');}
    }}>{images === null ? '추출 이미지 목록 보기' : '추출 이미지 목록 닫기'}</button>
      {images !== null && (images.length ? <div className="image-review">
        <p className="hint">원본만 포함하면 이미지 자체는 글자 검색되지 않습니다. 차트는 원본 이미지 대신 생성된 미리보기를 사용합니다. 분석은 선택한 이미지에 새로 요청하며, 제외해도 원본 문서는 보존됩니다.</p>
        <div className="image-review-toolbar">
          <label className="image-review-bulk"><span>일괄 선택</span><select value={bulkImageChoice} onChange={event => setBulkImageChoice(event.target.value)}>
            <option value="keep">처리 방식 선택</option><option value="analyze" disabled={!imageAnalysisAvailable}>이미지 분석</option><option value="original">원본만 포함</option>
            <option value="both" disabled={!imageAnalysisAvailable}>분석과 원본 포함</option><option value="exclude">검색에서 제외</option>
          </select></label>
          <button type="button" className="secondary" disabled={bulkImageChoice === 'keep'} onClick={() => setImageChoices(Object.fromEntries(visibleImages.map(image => [image.element_id, bulkImageChoice])))}>표시된 이미지에 반영</button>
          {job.pending_review && <button type="button" className="text-button image-review-filter" onClick={() => setShowAllImages(value => !value)}>{showAllImages ? "확인 필요한 이미지만 보기" : "처리 완료 이미지도 보기"}</button>}
        </div>
        {!visibleImages.length && <p className="hint">확인이 필요한 이미지가 없습니다. 처리한 이미지는 ‘처리 완료 이미지도 보기’에서 확인할 수 있습니다.</p>}
        <div className="image-review-list">{visibleImages.map((image) => <div key={image.element_id} className="image-review-item">
          <ImageThumbnail item={image} session={session} label={`이미지 ${(images || []).indexOf(image) + 1}`}
            expanded={openImage === image.element_id} onToggle={() => setOpenImage(current => current === image.element_id ? null : image.element_id)}/>
          <div className="image-review-meta">
            <div className="image-review-title"><strong>{image.is_preview ? '차트 미리보기' : '이미지'} {(images || []).indexOf(image) + 1}</strong>
              {image.is_preview && <span className="image-policy">시트에서 생성한 참고 이미지 · 원본과 다를 수 있음</span>}
              {image.format && <span className="image-format">{image.format.toUpperCase()}</span>}</div>
            <div className="image-review-tags">
              {image.analysis_status && <span className={`image-status ${image.analysis_status === "completed" ? "is-complete" : ""}`}>{analysisLabels[image.analysis_status] || "확인 필요"}</span>}
              {image.policy && <span className="image-policy">처리: {({'analyze': '이미지 분석', 'original': '원본만 포함', 'both': '분석과 원본 포함', 'exclude': '검색에서 제외', 'describe': '원본과 직접 작성한 설명'} as Record<string, string>)[image.policy] || image.policy}</span>}
            </div>
          </div>
          <label className="image-review-choice"><span className="sr-only">이미지 {(images || []).indexOf(image) + 1} 처리 방식</span><select value={imageChoices[image.element_id] || 'keep'} onChange={event => setImageChoices(current => ({...current, [image.element_id]: event.target.value}))}>
            <option value="keep">처리 방식 선택</option><option value="analyze" disabled={!imageAnalysisAvailable}>이미지 분석</option>
            <option value="original">{image.is_preview ? '미리보기만 포함' : '원본만 포함'}</option><option value="both" disabled={!imageAnalysisAvailable}>{image.is_preview ? '분석과 미리보기 포함' : '분석과 원본 포함'}</option>
            <option value="describe">원본과 설명 직접 입력</option><option value="exclude">검색에서 제외</option>
          </select></label>
          {imageChoices[image.element_id] === "describe" && <label>이미지 설명<textarea maxLength={2000} value={imageDescriptions[image.element_id] || ""} onChange={event => setImageDescriptions(current => ({...current, [image.element_id]: event.target.value}))} placeholder="검색에 사용할 이미지 설명을 입력하세요"/></label>}
        </div>)}</div>
        <button type="button" className="primary" disabled={busy || running || imageSubmitting || !modelEnabled || !Object.values(imageChoices).some(choice => choice !== 'keep') || (!imageAnalysisAvailable && Object.values(imageChoices).some(choice => choice === 'analyze' || choice === 'both'))} onClick={async () => {
          const actions = Object.fromEntries(Object.entries(imageChoices).filter(([, choice]) => choice !== 'keep'));
          const descriptions = Object.fromEntries(Object.keys(actions).filter(key => actions[key] === 'describe').map(key => [key, (imageDescriptions[key] || '').trim()]));
          if (Object.values(descriptions).some(value => !value)) {setReviewError('이미지 설명을 입력해 주세요.'); return;}
          setImageSubmitting(true);
          try {await api(`/v1/ingest/${item.document_id}/jobs/${job.job_id}/images/actions`, session, {actions, descriptions, embedding_profile_id: job.pending_review ? job.embedding_profile_id : embeddingProfile}); setImages(null); setImageChoices({}); setImageDescriptions({}); onChanged();}
          catch (error) {setReviewError(error instanceof Error ? error.message : '이미지 처리를 시작하지 못했습니다.');}
          finally {setImageSubmitting(false);}
        }}>선택한 이미지 처리</button>
      </div> : <p className="hint">이 작업에서 보존된 이미지가 없습니다.{coverage?.warnings?.some(warning => warning.includes("차트는 직접 추출하지 않았습니다") || warning.includes("차트 미리보기를 만들지 못했습니다")) ? " 차트 미리보기가 없어 여기에서 선택하거나 재분석할 수 없습니다. 원본에서 확인해 주세요." : ""}</p>)}</>}
    {job.representation_audit && (job.representation_audit.unrepresented_elements.length > 0 || job.representation_audit.missing_table_cells.length > 0 || (job.representation_audit.missing_text_terms?.length ?? 0) > 0) && <p className="hint">검색 표현 확인 필요 · 연결되지 않은 요소 {job.representation_audit.unrepresented_elements.length}개 · 표 셀 {job.representation_audit.missing_table_cells.length}개 · 본문 확인 {job.representation_audit.missing_text_terms?.length ?? 0}개 (분할로 인한 차이 포함)</p>}
    {job.representation_audit && ((job.representation_audit.shortened_heading_chunks?.length ?? 0) > 0 || (job.representation_audit.shortened_context_elements?.length ?? 0) > 0) && <p className="hint">검색 입력에 맞춰 제목 경로 또는 캡션 문맥이 일부 축약됐습니다. 전체 내용은 원문에서 확인하세요.</p>}
    {uniqueNotices.length > 0 && <section className="review-notices" aria-label="확인할 처리 내용">
      <div className="review-notices-head"><span className="review-notices-icon" aria-hidden="true">!</span><div><strong>확인할 처리 내용</strong><p>{job.pending_review ? "게시 전에 아래 내용을 확인해 주세요." : "문서 처리 시 확인된 내용입니다."}</p></div><span className="review-notices-count">{uniqueNotices.length}건</span></div>
      <ul>{uniqueNotices.map((notice, index) => <li key={`${index}-${notice}`}>{notice}</li>)}</ul>
    </section>}
    {!!job.coverage?.limitations?.length && <section className="extraction-limitations" aria-label="직접 추출 안내">
      <strong>직접 추출 안내</strong><ul>{job.coverage.limitations.map((limit, index) => <li key={`${index}-${limit}`}>{limit}</li>)}</ul>
    </section>}
    {job.coverage?.provider_total_pages != null && <p className="hint">파서가 변환해 처리한 페이지 · {job.coverage.provider_total_pages}쪽. 원본 쪽 번호와 다를 수 있습니다.</p>}
    {!!job.coverage?.blank_pages?.length && <p className="hint">원본에서 빈 페이지로 확인됨 · {job.coverage.blank_pages.join(", ")}</p>}
    {job.can_accept_partial && <button type="button" className="secondary" disabled={busy || running} onClick={async () => {
      if (!window.confirm(coverage?.provider_incomplete || coverage?.missing_pages.length ? "확인되지 않거나 처리되지 않은 내용은 검색에 포함되지 않을 수 있습니다. 현재 결과를 게시하시겠습니까?" : "확인한 내용을 검색에 게시하시겠습니까?")) return;
      try { await api(`/v1/ingest/${item.document_id}/jobs/${job.job_id}/accept-partial`, session, {}); onChanged(); }
      catch (error) { setReviewError(error instanceof Error ? error.message : "부분 결과를 적용하지 못했습니다."); }
    }}>검토 완료 · 게시</button>}
    {job.can_publish_draft && <button type="button" className="primary" disabled={busy || running} onClick={async () => {
      try {await api(`/v1/ingest/${item.document_id}/jobs/${job.job_id}/accept-content-changes`, session, {}); onChanged();}
      catch (error) {setReviewError(error instanceof Error ? error.message : "게시하지 못했습니다.");}
    }}>검토 완료 · 게시</button>}
    {reviewError && !job.content_changes?.length && <p role="alert">{reviewError}</p>}
    {["partial", "failed"].includes(job.status) && !!job.content_changes?.length && <div className="hint"><p>이전 검색 결과와 비교해 일부 본문 또는 표 셀이 줄었습니다. 표현 변경도 차이로 표시될 수 있습니다.</p>
      {job.content_changes.map((change, index) => <p key={index}>{change.page ? `${change.page}쪽` : "페이지 미확인"} · 확인이 필요한 단어 {change.missing_word_count}개 · 표 셀 {change.missing_cell_count}개 · 표 행 관계 {change.changed_table_row_count}개</p>)}
      {job.can_accept_content_changes && <><button type="button" className="secondary" disabled={reviewBusy} onClick={async () => {
        setReviewBusy(true); setReviewError("");
        try { setReview(await api(`/v1/ingest/${item.document_id}/jobs/${job.job_id}/content-review`, session)); }
        catch (error) { setReviewError(error instanceof Error ? error.message : "비교 내용을 불러오지 못했습니다."); }
        finally { setReviewBusy(false); }
      }}>이전·새 내용 비교</button>
      {review && <><div>{review.pages.map((page, index) => <details key={index}><summary>{page.page ? `${page.page}쪽` : "페이지 미확인"} 비교</summary>
        <strong>이전 검색 내용의 원문</strong>{page.previous.map(value => <p className="result-text" key={value.job_id + value.element_id}>{value.cells.length ? value.cells.map(cell => `${cell.row}행 ${cell.column}열${cell.rowspan > 1 || cell.colspan > 1 ? ` (${cell.rowspan}×${cell.colspan} 병합)` : ""}: ${cell.text}`).join("\n") : value.text}</p>)}
        <strong>새 파싱 원문</strong>{page.current.map(value => <p className="result-text" key={value.job_id + value.element_id}>{value.cells.length ? value.cells.map(cell => `${cell.row}행 ${cell.column}열${cell.rowspan > 1 || cell.colspan > 1 ? ` (${cell.rowspan}×${cell.colspan} 병합)` : ""}: ${cell.text}`).join("\n") : value.text}</p>)}
      </details>)}</div><button type="button" className="secondary" disabled={reviewBusy} onClick={async () => {
        if (!window.confirm("확인한 새 내용으로 검색 버전을 갱신합니다. 이전 버전도 검색할 수 있습니다. 계속하시겠습니까?")) return;
        setReviewBusy(true); setReviewError("");
        try { await api(`/v1/ingest/${item.document_id}/jobs/${job.job_id}/accept-content-changes`, session, {}); setReview(undefined); onChanged(); }
        catch (error) { setReviewError(error instanceof Error ? error.message : "새 검색 버전을 적용하지 못했습니다."); }
        finally { setReviewBusy(false); }
      }}>확인한 새 내용 적용</button></>}
      </>}{reviewError && <p role="alert">{reviewError}</p>}</div>}
    {job.source_summary && <details className="hint"><summary>원본 문서 정보</summary>
      {job.source_summary.sheets && <><p>시트 {job.source_summary.sheets.length}개</p>
        {job.source_summary.sheets.map((sheet, index) => <p key={index}>{sheet.name} · {sheet.visibility === "visible" ? "표시" : ["hidden", "veryHidden"].includes(sheet.visibility) ? "숨김" : "상태 미확인"} · 수식 {sheet.formula_count}개{!sheet.formula_scan_complete ? " 이상 · 일부 확인 불가" : ""}{sheet.missing_cached_values ? ` · 저장된 계산값 없음 ${sheet.missing_cached_values}개` : ""}</p>)}</>}
      {job.source_summary.slides && <p>슬라이드 {job.source_summary.slides.length}개 · 숨김 {job.source_summary.slides.filter(slide => slide.hidden).length}개 · 발표자 노트 있음 {job.source_summary.slides.filter(slide => slide.notes_present).length}개{job.source_summary.slides.some(slide => slide.notes_present === null) ? " · 노트 일부 확인 불가" : ""}</p>}
      <p>원본에서 확인한 정보입니다. 검색 결과의 페이지와 시트·슬라이드 번호는 아직 자동 연결되지 않으며, 숨김 내용과 발표자 노트의 파싱 여부는 제공자에 따라 다릅니다.</p>
    </details>}
    {!!job.coverage?.attempts?.length && <details className="hint"><summary>문서 읽기 내역</summary>
      {job.coverage.attempts.map((attempt, index) => <p key={index}>
        {attempt.mode === "initial" ? "처음 읽기" : attempt.mode === "full" ? "전체 다시 읽기" : "일부 페이지 다시 읽기"}
        {attempt.status ? ` · ${attemptLabels[attempt.status] || attempt.status}` : ""}{attempt.reused ? " · 저장된 결과 사용" : ""}
        {attempt.pages?.length ? ` · 페이지 ${attempt.pages.join(", ")}` : ""}
        {attempt.ocr ? ` · OCR ${attempt.ocr}` : ""} · DPI {attempt.dpi ?? "기본값"}
      </p>)}
    </details>}
    {job.error && job.status !== "failed" && <p>{job.error.message}</p>}
    {item.deletion_pending && <p className="alert">삭제가 완료되지 않았습니다. 삭제를 다시 시도해 주세요.</p>}
    {item.indexed && <p><a href={`search?document=${encodeURIComponent(item.document_id)}`}>이 문서에서 검색</a></p>}
    {!item.deletion_pending && <details><summary>다시 처리</summary>
      <p className="hint">선택한 임베딩 모델로 검색을 준비합니다. 이미 읽은 내용을 사용하거나 문서를 다시 읽을 수 있습니다. 다시 읽으면 외부 서비스 비용이 발생할 수 있습니다.</p>
      <label>청크 크기 (자)<input type="number" min={400} max={4000} value={target} onChange={event => {const value = Number(event.target.value); setTarget(value); setOverlap(current => Math.min(current, Math.floor(value / 4)));}}/></label>
      <label>겹침 (자)<input type="number" min={0} max={Math.min(400, Math.floor(target / 4))} value={overlap} onChange={event => setOverlap(Number(event.target.value))}/></label>
      {["xlsx", "pptx", "docx", "hwpx", "hwp"].includes(item.format) && <label>문서를 다시 읽을 방식<select value={readMode} onChange={event => setReadMode(event.target.value as Settings["extraction_mode"])}>
        <option value="parser" disabled={!parserFormats.includes(item.format)}>선택한 파서로 문서 읽기</option>
        <option value="native_text">빠른 처리 · 직접 추출만</option>
        <option value="native_visual" disabled={!nativeVisualSupported}>직접 추출 + 이미지 분석</option>
      </select></label>}
      {supportsDpi && <label>다시 파싱할 DPI<input type="number" min={50} max={600} disabled={item.format !== "pdf"} placeholder="공급자 기본값" value={dpi ?? ""} onChange={event => setDpi(event.target.value ? Number(event.target.value) : null)}/></label>}
      <div className="actions">
        <button type="button" disabled={busy || running || !modelEnabled || !job.parsed_available} className="secondary" onClick={() => onReprocess(item, job, "rechunk", chunks)}>읽은 내용으로 검색 준비</button>
        {!job.pending_review && job.settings.extraction_mode === "native_visual" && job.parsed_available && !job.image_policies_present && <button type="button" disabled={busy || running || !modelEnabled} className="secondary" onClick={() => onReprocess(item, job, "complete_images", chunks)}>미처리 이미지만 분석</button>}
        <button type="button" disabled={busy || running || !modelEnabled} className="secondary" onClick={() => onReprocess(item, job, "reparse", {...chunks, extraction_mode: readMode, dpi: supportsDpi ? dpi : null, ...(supportsDpi ? {} : {ocr_policy: "automatic"})})}>문서 다시 읽기</button>
      </div>
      {item.indexed && <p className="hint">새 색인 게시가 완료될 때까지 기존 버전은 검색할 수 있습니다.</p>}
    </details>}
    <div className="actions">
      <button type="button" disabled={busy || running || item.deletion_pending} className="secondary" onClick={() => onDelete(item, job, "job")}>작업 기록 삭제</button>
      <button type="button" disabled={busy || running} className="secondary" onClick={() => onDelete(item, job, "document")}>{item.deletion_pending ? "문서 삭제 다시 시도" : "문서 삭제"}</button>
    </div>
    </details>
  </article>;
}

export function IngestionDocuments({session, options, embeddingProfiles, embeddingProfile, onEmbeddingProfileChange, onChange}: {
  session: Session; options: Options; embeddingProfiles: EmbeddingProfile[]; embeddingProfile: string;
  onEmbeddingProfileChange: (value: string) => void; onChange: () => void}) {
  const [items, setItems] = useState<Document[]>([]);
  const [file, setFile] = useState<File>();
  const [dragging, setDragging] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const [settings, setSettings] = useState(options.defaults);
  const [profile, setProfile] = useState("");
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState<number | null>(null);
  const [message, setMessage] = useState("");
  const [duplicate, setDuplicate] = useState<string>();
  const profileRef = useRef(embeddingProfile);
  profileRef.current = embeddingProfile;
  function chunkDefaults(selected: string, filename?: string) {
    const format = filename?.split(".").at(-1)?.toLowerCase();
    const resolved = selected || (format === "xlsx" ? "spreadsheet" : format === "pptx" ? "slides" : "general");
    return options.chunk_profile_defaults?.[resolved] ?? {target_chars: options.defaults.target_chars, overlap_chars: options.defaults.overlap_chars};
  }
  const refresh = useCallback(async () => {
    const suffix = embeddingProfile ? "?profile_id=" + encodeURIComponent(embeddingProfile) : "";
    const result = await api<{documents: Document[]}>("/v1/ingestion-documents" + suffix, session);
    if (profileRef.current === embeddingProfile) setItems(result.documents);
  }, [session, embeddingProfile]);

  useEffect(() => {
    void refresh().catch(error => setMessage(error.message));
    const timer = window.setInterval(() => { void refresh().catch(() => setMessage("작업 상태를 다시 확인하고 있습니다.")); }, 2000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  async function submit(event?: FormEvent, allowDuplicate = false) {
    event?.preventDefault();
    if (!file || busy) return;
    const format = file.name.split(".").at(-1)?.toLowerCase() || "";
    if (!options.formats.includes(format) || file.size > options.max_upload_bytes) {
      setMessage("지원하지 않는 형식이거나 파일 크기 제한을 초과했습니다."); return;
    }
    setBusy(true); setMessage(""); setSent(null);
    try {
      const effectiveSettings = options.supports_dpi === false ? {...settings, dpi: null, ocr_policy: "automatic" as const} : settings;
      await uploadIngestion(file, effectiveSettings, profile, session, embeddingProfile, setSent, allowDuplicate);
      setDuplicate(undefined);
      setFile(undefined); if (fileInput.current) fileInput.current.value = ""; await refresh(); onChange();
    } catch (error) {
      if (error instanceof ApiError && error.code === "DUPLICATE_DOCUMENT") setDuplicate(error.documentId);
      setMessage(error instanceof Error ? error.message : "업로드하지 못했습니다.");
    }
    finally { setBusy(false); }
  }

  function selectFile(next?: File) {
    setMessage(""); setDuplicate(undefined);
    if (!next) { setFile(undefined); return; }
    const format = next.name.split(".").at(-1)?.toLowerCase() || "";
    if (!options.formats.includes(format) || next.size > options.max_upload_bytes) {
      setFile(undefined);
      if (fileInput.current) fileInput.current.value = "";
      setMessage(!options.formats.includes(format) ? "지원하지 않는 문서 형식입니다." : "파일 크기 제한을 초과했습니다.");
      return;
    }
    setFile(next);
    const effectiveProfile = format !== "pdf" && profile === "scanned" ? "" : profile;
    if (format !== "pdf") {
      setSettings(current => ({...current, dpi: null, ocr_policy: "automatic",
        ...(!profile || profile === "scanned" ? chunkDefaults(effectiveProfile, next.name) : {}),
        extraction_mode: ["xlsx", "pptx", "docx", "hwpx", "hwp"].includes(format) && !options.parser_formats?.includes(format)
          ? (options.native_visual_supported ? "native_visual" : "native_text") : "parser"}));
      setProfile(current => current === "scanned" ? "" : current);
    } else setSettings(current => ({...current, extraction_mode: "parser", ...(!profile ? chunkDefaults("", next.name) : {})}));
  }

  async function resume(item: Document, job: Job) {
    setBusy(true); setMessage("");
    try {
      await api(`/v1/ingest/${item.document_id}/jobs/${job.job_id}/resume`, session, {});
      await refresh(); onChange();
    } catch (error) { setMessage(error instanceof Error ? error.message : "이어서 처리하지 못했습니다."); }
    finally { setBusy(false); }
  }

  async function reprocess(item: Document, job: Job, mode: "reparse" | "rechunk" | "complete_images", changes: Partial<Settings>) {
    setBusy(true); setMessage("");
    try {
      await api(`/v1/ingest/${item.document_id}/jobs`, session, {base_job_id: job.job_id, mode, settings: changes, embedding_profile_id: embeddingProfile});
      await refresh(); onChange();
    } catch (error) { setMessage(error instanceof Error ? error.message : "다시 처리하지 못했습니다."); }
    finally { setBusy(false); }
  }

  async function deleteItem(item: Document, job: Job, type: "document" | "job") {
    const prompt = type === "document" ? "서버의 원본 파일, 작업 기록 및 모든 검색 버전에서 이 문서의 데이터를 삭제합니다. 외부 서비스의 처리 결과와 별도 백업은 따로 삭제해야 합니다. 계속하시겠습니까?"
      : "선택한 작업 기록을 삭제합니다. 검색 버전이 참조하는 작업은 삭제할 수 없습니다. 계속하시겠습니까?";
    if (!window.confirm(prompt)) return;
    setBusy(true); setMessage("");
    try {
      await remove(type === "document" ? `/v1/ingest/${item.document_id}` : `/v1/ingest/${item.document_id}/jobs/${job.job_id}`, session);
      await refresh(); onChange();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "삭제하지 못했습니다.");
      await refresh();
    } finally { setBusy(false); }
  }

  return <div className="grid">
    <section className="card upload-card"><div className="section-heading"><div><span className="section-kicker">NEW DOCUMENT</span><h2>문서 업로드</h2></div><span className="section-number">01</span></div>
      <p className="hint">파일을 등록하면 검색할 수 있도록 내용을 준비합니다.</p>
      {options.pdf_max_pages && <p className="hint">현재 연결된 파서는 PDF를 최대 {options.pdf_max_pages}쪽까지 처리합니다.</p>}
      <form onSubmit={event => void submit(event)}>
        <label className={"file-drop" + (dragging ? " is-dragging" : "")}
          onDragOver={event => { event.preventDefault(); if (!busy) setDragging(true); }}
          onDragLeave={event => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false); }}
          onDrop={event => { event.preventDefault(); setDragging(false); if (!busy) selectFile(event.dataTransfer.files[0]); }}>
          <input ref={fileInput} type="file" accept={options.formats.map(format => `.${format}`).join(",")} disabled={busy} onChange={event => selectFile(event.target.files?.[0])}/>
          <span className="upload-symbol" aria-hidden="true"><svg viewBox="0 0 26 26"><path d="M3 15h5l2 3.5h6l2-3.5h5v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path className="open" d="M13 3v10M9 7l4-4 4 4"/></svg></span>
          <strong>{file ? file.name.normalize("NFC") : "파일을 선택하거나 끌어다 놓으세요"}</strong>
          <span>{file ? `${(file.size / 1024 / 1024).toFixed(2)} MB · 파일을 바꾸려면 다시 선택하세요` : `${options.formats.map(format => format.toUpperCase()).join(", ")} · 최대 ${Math.round(options.max_upload_bytes / 1024 / 1024)} MB`}</span>
        </label>
        <label>검색에 사용할 임베딩 모델<select value={embeddingProfile} disabled={!embeddingProfiles.length || busy} onChange={event => onEmbeddingProfileChange(event.target.value)}>
          {embeddingProfiles.map(item => <option key={item.profile_id} value={item.profile_id}>{item.name}{item.enabled ? "" : " · 새 문서 사용 중지"}</option>)}
        </select></label>
        <p className="hint">현재 문서 파서 · {parserName(options.current_parser?.name || options.current_parser?.provider)} · <a href="settings">파서 설정</a></p>
        {embeddingProfiles.find(item => item.profile_id === embeddingProfile)?.enabled === false && <p className="hint">사용 중지된 모델의 문서는 검색할 수 있지만 새로 처리할 수 없습니다.</p>}
        <label>문서 처리 유형<select value={profile} onChange={event => { const next = event.target.value; setProfile(next); setSettings(current => ({...current, ...chunkDefaults(next, file?.name)})); }}>
          <option value="">자동 선택</option><option value="general">일반</option><option value="report">보고서·계획서</option>
          <option value="manual">매뉴얼</option><option value="spreadsheet">표 중심</option><option value="slides">슬라이드 중심</option>
          {options.force_ocr_formats.includes("pdf") && <option value="scanned" disabled={!file?.name.toLowerCase().endsWith(".pdf")}>스캔 PDF</option>}
        </select></label>
        <p className="hint">목표 청크 크기와 겹침은 문자 단위입니다. 선택한 임베딩 입력 안전 한도는 {embeddingProfiles.find(item => item.profile_id === embeddingProfile)?.max_input_bytes?.toLocaleString() || "확인되지 않음"}바이트이며 제목과 표 문맥도 포함합니다. 바이트는 모델의 토큰 한도가 아닙니다.</p>
        {file && ["xlsx", "pptx", "docx", "hwpx", "hwp"].includes(file.name.split(".").at(-1)?.toLowerCase() || "") && <label>문서 읽기 방식
          <select value={settings.extraction_mode} onChange={event => setSettings({...settings, extraction_mode: event.target.value as Settings["extraction_mode"]})}>
            <option value="parser" disabled={!options.parser_formats?.includes(file.name.split(".").at(-1)?.toLowerCase() || "")}>선택한 파서로 문서 읽기</option>
            <option value="native_text">빠른 처리 · 직접 추출만</option>
            <option value="native_visual" disabled={!options.native_visual_supported}>직접 추출 + 이미지 분석</option>
          </select><span className="hint">직접 추출은 일부 표·도형을 놓칠 수 있습니다. 이미지 분석은 PaddleOCR-VL을 사용합니다.</span></label>}
        <details className="settings"><summary>고급 처리 설정 <span>청크 크기 · 겹침{options.supports_dpi !== false ? " · DPI" : ""}</span></summary>
          <p className="hint">{options.force_ocr_formats.includes("pdf") ? "스캔 PDF는 OCR을 강제합니다. " : ""}유형을 바꾸면 해당 유형의 청크 기본값이 적용되며 이후 직접 조정할 수 있습니다. 자동 선택은 XLSX와 PPTX에 각각 표·슬라이드 기본값을 적용하고 나머지는 범용 기본값을 사용합니다.</p>
          <label>청크 크기 (자)<input type="number" min={400} max={4000} value={settings.target_chars} onChange={event => {
            const size = Number(event.target.value); setSettings({...settings, target_chars: size, overlap_chars: Math.min(settings.overlap_chars, Math.floor(size / 4))});
          }}/></label>
          <label>겹침 (자)<input type="number" min={0} max={Math.min(400, Math.floor(settings.target_chars / 4))} value={settings.overlap_chars} onChange={event => setSettings({...settings, overlap_chars: Number(event.target.value)})}/></label>
          {options.supports_dpi !== false && <label>DPI (PDF, 비우면 기본값)<input type="number" min={50} max={600} disabled={!file?.name.toLowerCase().endsWith(".pdf")} value={settings.dpi ?? ""} onChange={event => setSettings({...settings, dpi: event.target.value ? Number(event.target.value) : null})}/></label>}
        </details>
        <button className="primary" disabled={!file || busy || !embeddingProfile || embeddingProfiles.find(item => item.profile_id === embeddingProfile)?.enabled === false}>{busy ? "요청 중" : "업로드 및 처리"}</button>
        {busy && sent !== null && <p role="status">업로드 {Math.round(sent * 100)}%</p>}
      </form>
      {message && <p className="alert" role="alert">{message}</p>}
      {duplicate && <div className="actions"><a href={`#document-${duplicate}`}>기존 문서 확인</a><button type="button" className="secondary" disabled={busy} onClick={() => { if (window.confirm("같은 파일을 새 문서로 처리하면 파싱 비용과 검색 결과가 중복될 수 있습니다. 계속하시겠습니까?")) void submit(undefined, true); }}>새 문서로 추가</button></div>}
    </section>
    <section className="card documents-card"><div className="section-heading"><div><span className="section-kicker">YOUR DOCUMENTS</span><h2>문서와 처리 상태 <span className="count-pill">{items.length}</span></h2></div><span className="section-number">02</span></div>
      {!items.length && <div className="empty-state"><span className="empty-icon" aria-hidden="true">{fileIcon}</span><strong>아직 등록된 문서가 없습니다</strong><p>왼쪽에서 문서를 등록하면<br/>처리 상태를 여기에서 확인할 수 있습니다.</p></div>}
      {items.map(item => <DocumentCard key={item.document_id} item={item} session={session} onChanged={() => {void refresh(); onChange();}} busy={busy} supportsDpi={options.supports_dpi !== false} parserFormats={options.parser_formats || []} nativeVisualSupported={Boolean(options.native_visual_supported)} embeddingProfile={embeddingProfile} modelEnabled={Boolean(embeddingProfiles.find(value => value.profile_id === embeddingProfile)?.enabled)}
        onResume={(document, job) => void resume(document, job)}
        onReprocess={(document, job, mode, changes) => void reprocess(document, job, mode, changes)}
        onDelete={(document, job, type) => void deleteItem(document, job, type)}/>)}
    </section>
  </div>;
}
