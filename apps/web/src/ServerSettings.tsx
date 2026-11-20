import { useEffect, useState } from "react";
import { api, put } from "./api";
import type { Session } from "./api";
import { EmbeddingModels } from "./EmbeddingModels";
import { ParserModels } from "./ParserModels";

type Values = Record<string, string | number | boolean | string[] | { configured: boolean } | null>;
type ServerResponse = { values: Values; defaults: Values; choices?: Record<string, string[]>; restart_required: boolean };
type IndexStatus = {index_version: string | null; chunk_count: number; index_profile: string | null;
  current_profile: string; profile_matches: boolean | null};
type Field = [key: string, label: string, kind: string, hint?: string];
type Group = {id: string; title: string; description: string; fields: (Field | {section: string})[]};

// 처리 흐름 순서로 묶고, 서로 영향을 주는 항목은 같은 묶음에 둔다.
const groups: Group[] = [
  { id: "external", title: "외부 서비스", description: "문서 파싱, 임베딩, 재순위, 대화 답변은 외부 API를 사용합니다. 끄면 외부로 데이터를 보내지 않으며 문서 처리와 대화를 할 수 없고, 이미 준비된 문서의 BM25 검색만 사용할 수 있습니다.", fields: [
    ["external_processing", "외부 서비스 사용", "bool"],
    ["remote_timeout", "외부 요청 제한 시간 (초)", "number", "비동기 파싱 조회, 대화 답변, 원격 Qdrant 요청에 적용"],
  ] },
  { id: "parsing", title: "문서 파싱", description: "파서 제공자를 비우면 문서 처리와 함께 임베딩, 재순위, 벡터 DB 연결도 꺼집니다.", fields: [
    {section: "페이지와 대기 시간"},
    ["max_pages", "PDF 최대 페이지", "number"], ["retry_pages", "빠진 페이지 재시도 최대 페이지", "number"],
    ["job_timeout", "비동기 파싱 최대 대기 시간 (초)", "number", "긴 PDF를 나눠 읽을 때 결과를 기다리는 시간"],
    {section: "PDF 기본값"},
    ["default_ocr_policy", "PDF 기본 OCR", "select"], ["default_dpi", "PDF 기본 DPI (비우면 공급자 기본값)", "nullable-number"],
  ] },
  { id: "indexing", title: "청크·임베딩·벡터 DB", description: "문서를 검색 단위(청크)로 나누고 임베딩해 벡터 DB에 저장합니다. 서버 기본 모델과 아래 모델 목록은 같은 벡터 DB를 사용합니다.", fields: [
    {section: "청크 기본값"},
    ["default_target_chars", "기본 청크 크기 (자)", "number"], ["default_overlap_chars", "기본 겹침 (자)", "number", "청크 크기의 1/4 이하"],
    {section: "서버 기본 임베딩 모델"},
    ["embedding_profile", "서버 기본 모델 ID", "text"],
    ["embedding_base_url", "임베딩 API 주소", "text"], ["embedding_api_key", "임베딩 API 키", "secret"],
    ["embedding_document_model", "문서 임베딩 모델", "text"], ["embedding_query_model", "질의 임베딩 모델", "text"],
    ["embedding_dimensions", "임베딩 차원", "number"],
    ["embedding_max_input_bytes", "임베딩 입력 최대 바이트", "number", "서버 기본 모델의 안전 한도입니다. 다른 모델은 해당 모델의 설정을 사용합니다. 바이트는 토큰 수가 아닙니다."], ["embedding_batch_size", "임베딩 요청당 최대 청크 수", "number"],
    {section: "벡터 DB"},
    ["qdrant_url", "Qdrant 주소 (스킴 생략 시 HTTP, 비우면 로컬)", "text"], ["qdrant_key", "Qdrant API 키", "secret"],
  ] },
  { id: "rerank", title: "검색 재순위", description: "검색 후보의 순서를 다시 매깁니다. 사용하면 검색과 대화 화면에서 '재순위 적용'을 선택할 수 있습니다.", fields: [
    ["reranker_provider", "재순위 제공자", "select"],
    ["reranker_base_url", "재순위 API 주소", "text"], ["reranker_api_key", "재순위 API 키", "secret"],
    ["reranker_model", "재순위 모델", "text"],
  ] },
  { id: "chat", title: "대화 모델", description: "대화에 사용할 모델을 선택하고 서버에서 제공할 모델 목록을 관리합니다.", fields: [
    ["llm_base_url", "LLM 기본 주소", "text"], ["llm_api_key", "LLM API 키", "secret"],
  ] },
  { id: "access", title: "접근과 iframe 연동", description: "로그인 방법과 다른 사이트에 iframe으로 넣을 때 필요한 설정입니다.", fields: [
    ["access_key", "접근 키", "secret"], ["dev_auth", "로컬 개발 인증 허용", "bool", "127.0.0.1에서 접속할 때만"],
    ["public_origin", "공개 출처", "text"], ["frame_origins", "iframe 허용 HTTPS 출처 (공백으로 구분)", "origins"],
    ["embed_secret", "iframe 서명 키", "secret"],
  ] },
  { id: "server", title: "업로드와 저장", description: "업로드 크기와 서버 파일 경로입니다. 데이터 경로를 바꿔도 기존 파일은 옮기지 않습니다.", fields: [
    ["max_upload", "최대 업로드 크기 (바이트)", "number"],
  ] },
];
const choiceLabels: Record<string, string> = {"": "사용 안 함", disabled: "사용 안 함", upstage: "Upstage", paddle_vl: "PaddleOCR-VL", cohere: "Cohere", automatic: "자동", force: "항상 OCR"};

type ChatOptions = {models: string[]; default_model: string; configured: boolean};
type Props = {session: Session; chatOptions?: ChatOptions; chatModel: string; reasoningEffort: string;
  onModelChange: (value: string) => void; onReasoningChange: (value: string) => void;
  onModelsSaved: () => Promise<void>};

export function ServerSettings({session, chatOptions, chatModel, reasoningEffort, onModelChange, onReasoningChange, onModelsSaved}: Props) {
  const [saved, setSaved] = useState<ServerResponse>();
  const [draft, setDraft] = useState<Values>({});
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [reset, setReset] = useState<string[]>([]);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [rebuilding, setRebuilding] = useState(false);
  const [indexStatus, setIndexStatus] = useState<IndexStatus>();
  useEffect(() => { api<ServerResponse>("/v1/settings", session).then(value => { setSaved(value); setDraft(value.values); }).catch(error => setMessage(error.message)); }, [session]);
  useEffect(() => { api<IndexStatus>("/v1/ingest-index-status", session).then(setIndexStatus).catch(() => setIndexStatus(undefined)); }, [session]);
  function change(key: string, value: Values[string]) { setDraft(current => ({...current, [key]: value})); setReset(current => current.filter(item => item !== key)); }
  function restore(key: string) {
    if (!saved) return;
    setDraft(current => ({...current, [key]: saved.defaults[key]}));
    setSecrets(current => ({...current, [key]: ""}));
    setReset(current => [...new Set([...current, key])]);
  }
  async function save() {
    if (!saved) return;
    setBusy(true); setMessage("");
    try {
      const changes: Record<string, unknown> = {};
      for (const [key, value] of Object.entries(draft)) {
        if (reset.includes(key)) continue;
        if (value !== null && typeof value === "object" && !Array.isArray(value)) continue;
        if (JSON.stringify(value) !== JSON.stringify(saved.values[key])) changes[key] = value;
      }
      for (const [key, value] of Object.entries(secrets)) if (value) changes[key] = value;
      const response = await put<ServerResponse>("/v1/settings", session, {values: changes, reset});
      await onModelsSaved();
      setSaved({...saved, ...response}); setDraft(response.values); setSecrets({}); setReset([]);
      setMessage(response.restart_required ? "저장했습니다. 일부 설정을 적용하지 못했습니다." : "설정을 저장했습니다.");
    } catch (error) { setMessage(error instanceof Error ? error.message : "저장하지 못했습니다."); }
    finally { setBusy(false); }
  }
  async function rebuildIndex() {
    if (!window.confirm(`기존 문서의 검색 내용을 현재 임베딩 모델로 다시 준비합니다. ${indexStatus?.chunk_count ?? 0}개 내용을 외부 서비스로 보내며 비용이 발생할 수 있습니다. 계속하시겠습니까?`)) return;
    setBusy(true); setRebuilding(true); setMessage("");
    try {
      const result = await api<{index_version: string}>("/v1/ingest-index-rebuild", session, {});
      setIndexStatus(await api<IndexStatus>("/v1/ingest-index-status", session));
      setMessage(`검색 준비를 완료했습니다. 새 검색 버전 ${result.index_version.slice(0, 8)}`);
    } catch (error) { setMessage(error instanceof Error ? error.message : "검색 준비에 실패했습니다."); }
    finally { setBusy(false); setRebuilding(false); }
  }
  const models = [...new Set([String(draft.llm_model || ""), ...(Array.isArray(draft.llm_models) ? draft.llm_models : [])])].filter(Boolean);
  const [newModel, setNewModel] = useState("");
  function updateModels(items: string[]) { change("llm_models", items.filter(item => item !== draft.llm_model)); }
  if (!saved) return <section className="card"><h2>서버 설정</h2><p role="status">{message || "설정을 불러오는 중…"}</p></section>;
  const unsaved = reset.length > 0 || Object.values(secrets).some(Boolean) || Object.entries(draft).some(([key, value]) =>
    JSON.stringify(value) !== JSON.stringify(saved.values[key]));
  const isSet = (key: string) => Boolean(secrets[key]) || Boolean((draft[key] as {configured?: boolean} | null)?.configured);
  const external = Boolean(draft.external_processing);
  const parser = String(draft.parser_provider || "");
  const rerankOff = draft.reranker_provider === "disabled";
  // 다른 묶음의 설정 때문에 이 묶음이 동작하지 않는 경우를 알린다.
  function notices(id: string) {
    const items: string[] = [];
    if (!external && ["parsing", "indexing", "rerank", "chat"].includes(id)) items.push("'외부 서비스 사용'이 꺼져 있어 동작하지 않습니다.");
    if (!parser && id === "parsing") items.push("파서 제공자를 선택해야 문서를 처리할 수 있습니다.");
    if (parser === "paddle_vl" && id === "parsing") items.push("PaddleOCR-VL은 PDF를 처리하며 OCR 서버에 설정된 모델을 사용합니다.");
    if (!parser && ["indexing", "rerank"].includes(id)) items.push("'문서 파싱'의 파서 제공자가 비어 있어 사용되지 않습니다.");
    if (id === "chat" && external && chatOptions && !chatOptions.configured) items.push("답변 모델 연결을 설정해 주세요.");
    if (id === "access" && !isSet("access_key") && !draft.dev_auth) items.push("접근 키가 없고 로컬 개발 인증도 꺼져 있어 저장하면 이후 로그인할 수 없습니다.");
    if (id === "access" && Array.isArray(draft.frame_origins) && draft.frame_origins.length && !isSet("embed_secret")) items.push("iframe 서명 키가 없으면 iframe 연동 로그인이 동작하지 않습니다.");
    return items;
  }
  function summary(id: string) {
    if (id === "external") return external ? "사용" : "꺼짐";
    if (id === "parsing") return parser ? (choiceLabels[parser] ?? parser) : "꺼짐";
    if (id === "indexing") return String(draft.embedding_profile || "");
    if (id === "rerank") return rerankOff ? "사용 안 함" : choiceLabels[String(draft.reranker_provider)] ?? String(draft.reranker_provider);
    if (id === "chat") return String(draft.llm_model || "");
    if (id === "access") return isSet("access_key") ? "접근 키 사용" : draft.dev_auth ? "로컬 개발 인증" : "로그인 불가";
    return "설정 보기";
  }
  function fieldDisabled(key: string) { return rerankOff && ["reranker_base_url", "reranker_api_key", "reranker_model"].includes(key); }
  return <section className="server-settings"><div className="settings-heading"><div><span className="eyebrow">WORKSPACE SETTINGS</span><h2>설정</h2>
    <p className="hint">대화와 문서 처리에 사용하는 연결 및 기본값을 관리합니다.</p></div><a className="secondary settings-return" href="chat">대화로 돌아가기</a></div>
    <div className="settings-notice"><strong>서버 설정 적용 안내</strong><p>설정을 저장하면 실행 중인 문서 처리가 끝난 뒤 적용됩니다.</p></div>
    <p className="hint settings-subnote">환경 변수는 기본값입니다. API 키는 다시 표시되지 않습니다. 데이터 및 웹 경로는 서버 시작 설정에서 변경합니다.</p>
    {saved.restart_required && <p className="alert">저장된 설정이 아직 실행 중인 서버에 적용되지 않았습니다.</p>}
    {groups.map(group => { const warnings = notices(group.id); return <details className="settings-panel" key={group.id}>
      <summary><span>{group.title}</span><small className={warnings.length ? "is-warning" : undefined}>{warnings.length ? "확인 필요 · " : ""}{summary(group.id)}</small></summary><fieldset>
      <p className="hint settings-group-description">{group.description}</p>
      {warnings.map(text => <p className="settings-inline-notice" key={text}>{text}</p>)}
      {group.id === "parsing" && <ParserModels session={session} disabled={unsaved} onActivated={async () => { const refreshed = await api<ServerResponse>("/v1/settings", session); setSaved(refreshed); setDraft(refreshed.values); await onModelsSaved(); }}/>}
      {group.id === "chat" && <div className="model-settings">
        <div className="settings-grid">
          <label className="stacked-label">사용할 모델<select value={chatModel} disabled={!chatOptions} onChange={event => onModelChange(event.target.value)}>
            {chatOptions?.models.map(model => <option key={model} value={model}>{model}</option>)}
          </select><span>{chatOptions && chatOptions.models.length < 2 ? "등록된 모델이 하나입니다. 아래에서 모델을 추가하고 저장해 주세요." : "이 브라우저에 바로 적용"}</span></label>
          <label className="stacked-label">추론 수준<select value={reasoningEffort} onChange={event => onReasoningChange(event.target.value)}>
            <option value="">모델 기본값</option><option value="none">없음 (none)</option><option value="minimal">최소 (minimal)</option>
            <option value="low">낮음 (low)</option><option value="medium">보통 (medium)</option><option value="high">높음 (high)</option>
            <option value="xhigh">매우 높음 (xhigh)</option><option value="max">최대 (max)</option>
          </select><span>모델마다 지원하는 수준이 다릅니다 · 이 브라우저에 바로 적용</span></label>
        </div>
        <div className="model-list"><div className="model-list-heading"><strong>서버 모델 목록</strong><span>서버 설정 저장 후 선택 가능</span></div>
          {models.map((model, index) => <div className="model-row" key={model}>
            <input aria-label={`모델 ${index + 1}`} defaultValue={model} onBlur={event => {
              const updated = event.target.value.trim();
              if (!updated || (updated !== model && models.includes(updated))) { event.target.value = model; return; }
              if (model === draft.llm_model) change("llm_model", updated);
              else updateModels(models.map(item => item === model ? updated : item));
            }}/>
            {model === draft.llm_model ? <span className="model-default">기본 모델</span> : <button type="button" className="secondary" onClick={() => { change("llm_model", model); change("llm_models", models.filter(item => item !== model)); }}>기본으로</button>}
            <button type="button" className="secondary" disabled={model === draft.llm_model} onClick={() => updateModels(models.filter(item => item !== model))}>삭제</button>
          </div>)}
          <div className="model-row"><input aria-label="새 모델 ID" value={newModel} placeholder="추가할 모델 ID" onChange={event => setNewModel(event.target.value)}/>
            <button type="button" className="secondary" disabled={!newModel.trim() || models.includes(newModel.trim())} onClick={() => { updateModels([...models, newModel.trim()]); setNewModel(""); }}>모델 추가</button></div>
          <button type="button" className="text-button" onClick={() => { restore("llm_model"); restore("llm_models"); }}>모델 목록 환경 기본값</button>
        </div>
      </div>}
      {group.fields.map(field => {
        if (parser === "paddle_vl" && group.id === "parsing" && !Array.isArray(field) && field.section === "PDF 기본값") return null;
        if (!Array.isArray(field)) return <h3 className="settings-section" key={field.section}>{field.section}</h3>;
        const [key, label, kind, hint] = field;
        if (parser === "paddle_vl" && ["parser_model", "parser_mode", "job_timeout", "default_ocr_policy", "default_dpi"].includes(key)) return null;
        const disabled = fieldDisabled(key);
        return <div className="setting-row" key={key}>
        <label htmlFor={"setting-" + key}>{label}{hint && <small>{parser === "paddle_vl" && key === "parser_sync_timeout" ? "PDF 요청 응답 대기 시간" : hint}</small>}</label>
        {kind === "bool" ? <input id={"setting-" + key} type="checkbox" checked={Boolean(draft[key])} onChange={event => change(key, event.target.checked)}/>
          : kind === "select" ? <select id={"setting-" + key} value={String(draft[key] ?? "")} onChange={event => change(key, event.target.value)}>
              {[...new Set([...(saved.choices?.[key] ?? []), String(draft[key] ?? "")])].map(value => <option key={value} value={value}>{choiceLabels[value] ?? value}</option>)}
            </select>
          : kind === "secret" ? <div><input id={"setting-" + key} type="password" autoComplete="new-password" disabled={disabled} placeholder={(draft[key] as {configured: boolean})?.configured ? "설정됨 · 변경할 때 입력" : "미설정"} value={secrets[key] || ""} onChange={event => {setSecrets(current => ({...current, [key]: event.target.value})); setReset(current => current.filter(item => item !== key));}}/>
            <button type="button" className="text-button" disabled={disabled} onClick={() => { setSecrets(current => ({...current, [key]: ""})); change(key, ""); }}>비우기</button></div>
          : kind === "origins" ? <input id={"setting-" + key} key={(draft[key] as string[]).join(" ")} defaultValue={(draft[key] as string[]).join(" ")}
              onBlur={event => change(key, event.target.value.split(/\s+/).filter(Boolean))}/>
          : <input id={"setting-" + key} disabled={disabled} type={kind === "number" || kind === "nullable-number" ? "number" : "text"} value={String(draft[key] ?? "")}
            onChange={event => change(key, kind === "nullable-number" ? (event.target.value === "" ? null : Number(event.target.value)) : kind === "number" ? (event.target.value === "" ? "" : Number(event.target.value)) : event.target.value)}/>}
        <button type="button" className="text-button" onClick={() => restore(key)}>환경 기본값</button>
      </div>; })}
      {group.id === "indexing" && indexStatus?.index_version && <div className="settings-notice">
        <strong>검색 모델 적용</strong>
        <p>{indexStatus.profile_matches ? "현재 검색 모델과 저장된 검색 내용이 일치합니다." : "현재 검색 모델이 기존 검색 내용과 다릅니다. 이전 문서를 새 모델로 다시 준비해야 합니다."}</p>
        <p className="hint">{indexStatus.chunk_count}개 검색 내용 · 실패하면 이전 검색 버전을 유지합니다.</p>
        {rebuilding && <p role="status">검색 내용을 다시 준비하고 있습니다. 문서 양에 따라 시간이 걸릴 수 있습니다.</p>}
        <button type="button" className="secondary" disabled={busy || unsaved || saved.restart_required || Boolean(indexStatus.profile_matches)} onClick={() => void rebuildIndex()}>현재 모델로 검색 다시 준비</button>
        {unsaved && <p className="hint">먼저 변경한 설정을 저장해 주세요.</p>}
        {saved.restart_required && <p className="hint">설정 적용 상태를 확인해 주세요.</p>}
      </div>}
      {group.id === "indexing" && <div className="embedding-models"><EmbeddingModels session={session}/></div>}
    </fieldset></details>; })}
    {message && <p role="status">{message}</p>}
    <button type="button" className="primary settings-save" disabled={busy} onClick={() => void save()}>{rebuilding ? "검색 준비 중" : busy ? "저장 중" : "서버 설정 저장"}</button>
  </section>;
}
