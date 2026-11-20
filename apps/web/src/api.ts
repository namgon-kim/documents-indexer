export type Session = { csrf: string } | { token: string };

export interface EmbeddingProfile {
  profile_id: string;
  name: string;
  base_url: string;
  document_model: string;
  query_model: string;
  dimensions: number;
  max_input_bytes: number;
  batch_size: number;
  enabled: boolean;
  default: boolean;
  api_key_configured: boolean;
  has_index: boolean;
}

export interface Settings {
  extraction_mode: "parser" | "native_text" | "native_visual";
  ocr_policy: "automatic" | "force";
  dpi: number | null;
  target_chars: number;
  overlap_chars: number;
}

export interface Options {
  current_parser?: {provider: string; name: string};
  ingestion_enabled?: boolean;
  pdf_max_pages?: number | null;
  formats: string[];
  parser_formats?: string[];
  native_visual_supported?: boolean;
  max_upload_bytes: number;
  rerank_available?: boolean;
  external_processing?: boolean;
  search_methods?: ("hybrid" | "dense" | "bm25")[];
  profiles: { profile_id: string; name: string }[];
  defaults: Settings;
  chunk_profile_defaults: Record<string, {target_chars: number; overlap_chars: number}>;
  limits: { target_chars: [number, number]; overlap_chars: [number, number]; overlap_ratio: number };
  force_ocr_formats: string[];
  supports_dpi?: boolean;
}

type Status = "queued" | "processing" | "completed" | "partial" | "failed";

export interface Coverage {
  total_pages: number | null;
  searchable_pages: number[];
  missing_pages: number[];
  unavailable_pages?: number[];
  missing_elements?: { element_id: string; page: number; code: string }[];
  unknown_scope: boolean;
  incomplete: boolean;
}

export interface Job {
  job_id: string;
  document_id: string;
  status: Status;
  stage: string | null;
  attempts?: { activity: string; ocr?: string; source_pages?: number[] }[];
  progress?: { unit: string; completed: number; total: number | null } | null;
  coverage?: Coverage;
  active_result?: Active | null;
  effective_settings: Settings;
  error?: { code: string; message: string };
  updated_at: string;
}

export interface Active {
  job_id: string;
  status: "completed" | "partial";
  coverage: Coverage;
  collection_name: string;
}

export interface DocumentItem {
  document_id: string;
  filename: string;
  source_format: string;
  source_version: number;
  latest_job: Job;
  active_result: Active | null;
}

export interface Hit {
  unit_id: string;
  document_id: string;
  job_id: string;
  filename: string;
  search_text: string;
  context_before?: string;
  context_after?: string;
  source_refs: { page?: number; kind: string; role?: "evidence" | "context"; table_rows?: [number, number]; sheet?: string; cell?: string; start?: string; end?: string; slide?: number; section?: string | number; block?: number; paragraph?: number }[];
  evidence_urls: string[];
  context_urls?: string[];
  related_images?: VisualItem[];
  source_url: string;
  quality: string;
  heading_path_origin?: string;
}

export interface VisualItem {url: string; element_id: string; type?: string; origin?: string; status?: string; format?: string; analysis_status?: string; policy?: string | null; has_analysis?: boolean; is_preview?: boolean}

export class ApiError extends Error {
  constructor(message: string, public status: number, public code?: string, public documentId?: string) {
    super(message);
  }
}

// The app root as the browser sees it; a proxy may mount the app under a path prefix (server sets <base>).
export const appBase = new URL(".", document.baseURI).pathname;
export function appUrl(path: string) {
  return path.startsWith("/") ? appBase + path.slice(1) : path;
}
// Page name relative to the app root: "", "chat", "search", "settings" or "embed".
export const route = (location.pathname.startsWith(appBase) ? location.pathname.slice(appBase.length)
  : location.pathname.replace(/^\/+/, "")).replace(/\/+$/, "");

function headers(session?: Session) {
  if (!session) return {};
  return "token" in session
    ? { Authorization: "Bearer " + session.token }
    : { "X-CSRF-Token": session.csrf };
}

export async function imageBlob(path: string, session: Session): Promise<Blob> {
  if (!path.startsWith('/v1/ingest/')) throw new Error('이미지 주소를 확인할 수 없습니다.');
  const response = await fetch(appUrl(path) + (path.includes('?') ? '&' : '?') + 'preview=true',
    {credentials: 'token' in session ? 'omit' : 'same-origin', headers: headers(session) as HeadersInit});
  if (!response.ok) throw new Error('이미지를 불러오지 못했습니다.');
  return response.blob();
}

export async function api<T>(path: string, session?: Session, body?: unknown, key?: string): Promise<T> {
  const response = await fetch(appUrl(path), {
    method: body === undefined ? "GET" : "POST",
    credentials: session && "token" in session ? "omit" : "same-origin",
    headers: { ...headers(session), ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      ...(key ? { "Idempotency-Key": key } : {}) } as HeadersInit,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new ApiError(payload.error?.message || "요청을 처리할 수 없습니다.", response.status);
  }
  return payload as T;
}

export async function remove(path: string, session: Session): Promise<void> {
  const response = await fetch(appUrl(path), { method: "DELETE", credentials: "token" in session ? "omit" : "same-origin", headers: headers(session) as HeadersInit });
  const payload = await response.json();
  if (!response.ok) throw new ApiError(payload.error?.message || "삭제하지 못했습니다.", response.status);
}

export async function put<T>(path: string, session: Session, body: unknown): Promise<T> {
  const requestHeaders = new Headers();
  for (const [key, value] of Object.entries(headers(session))) if (value) requestHeaders.set(key, value);
  requestHeaders.set("Content-Type", "application/json");
  const response = await fetch(appUrl(path), { method: "PUT", credentials: "token" in session ? "omit" : "same-origin",
    headers: requestHeaders, body: JSON.stringify(body) });
  const payload = await response.json();
  if (!response.ok) throw new ApiError(payload.error?.message || "설정을 저장하지 못했습니다.", response.status);
  return payload as T;
}

export function uploadIngestion(file: File, settings: Settings, profile: string, session: Session,
                                embeddingProfile: string,
                                progress: (value: number | null) => void, allowDuplicate = false): Promise<{document_id: string; job_id: string}> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", appUrl("/v1/ingest"));
    request.responseType = "json";
    for (const [name, value] of Object.entries(headers(session))) if (value) request.setRequestHeader(name, value);
    request.upload.onprogress = event => progress(event.lengthComputable ? event.loaded / event.total : null);
    request.onload = () => request.status === 200 ? resolve(request.response)
      : reject(new ApiError(request.response?.error?.message || "업로드가 완료되지 않았습니다.", request.status, request.response?.error?.code, request.response?.document_id));
    request.onerror = () => reject(new ApiError("연결이 끊겼습니다. 문서 목록을 확인한 후 다시 시도해 주세요.", 0));
    const form = new FormData();
    form.set("file", file); form.set("settings", JSON.stringify(settings)); form.set("deferred", "true");
    if (allowDuplicate) form.set("allow_duplicate", "true");
    if (profile) form.set("profile", profile);
    if (embeddingProfile) form.set("embedding_profile_id", embeddingProfile);
    request.send(form);
  });
}

export async function download(path: string, session: Session, filename: string) {
  const response = await fetch(appUrl(path), { headers: headers(session) as HeadersInit });
  if (!response.ok) throw new ApiError("원본 파일을 내려받을 수 없습니다.", response.status);
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
