import { useEffect, useState } from "react";
import { api, put, remove } from "./api";
import type { EmbeddingProfile, Session } from "./api";

type Draft = {profile_id: string; name: string; base_url: string; document_model: string;
  query_model: string; dimensions: number; max_input_bytes: number | ""; batch_size: number; enabled: boolean; api_key: string};
const empty: Draft = {profile_id: "", name: "", base_url: "", document_model: "", query_model: "", dimensions: 1024, max_input_bytes: "", batch_size: 24, enabled: true, api_key: ""};

export function EmbeddingModels({session}: {session: Session}) {
  const [profiles, setProfiles] = useState<EmbeddingProfile[]>([]);
  const [draft, setDraft] = useState<Draft>(empty);
  const [editing, setEditing] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [clearKey, setClearKey] = useState(false);
  const refresh = () => api<{profiles: EmbeddingProfile[]}>("/v1/embedding-profiles", session).then(value => setProfiles(value.profiles));
  useEffect(() => { void refresh().catch(() => setMessage("임베딩 모델 연결을 설정한 뒤 사용할 수 있습니다.")); }, [session]);
  function edit(profile: EmbeddingProfile) {
    setEditing(profile.profile_id);
    setClearKey(false);
    setDraft({profile_id: profile.profile_id, name: profile.name, base_url: profile.base_url,
      document_model: profile.document_model, query_model: profile.query_model, dimensions: profile.dimensions, max_input_bytes: profile.max_input_bytes, batch_size: profile.batch_size,
      enabled: profile.enabled, api_key: ""});
    setMessage("");
  }
  function reset() { setEditing(""); setDraft(empty); setClearKey(false); }
  async function save() {
    setBusy(true); setMessage("");
    const {api_key, ...fields} = draft;
    try {
      await put(`/v1/embedding-profiles/${encodeURIComponent(draft.profile_id)}`, session,
        {...fields, ...(!editing || api_key || clearKey ? {api_key: clearKey ? "" : api_key} : {})});
      await refresh(); reset(); setMessage("모델을 저장했습니다.");
    } catch (error) { setMessage(error instanceof Error ? error.message : "모델을 저장하지 못했습니다."); }
    finally { setBusy(false); }
  }
  async function erase(profile: EmbeddingProfile) {
    if (!window.confirm(`'${profile.name}' 모델을 삭제하시겠습니까? 검색 버전에서 사용 중인 모델은 삭제할 수 없습니다.`)) return;
    setBusy(true); setMessage("");
    try {
      await remove(`/v1/embedding-profiles/${encodeURIComponent(profile.profile_id)}`, session);
      await refresh(); if (editing === profile.profile_id) reset(); setMessage("모델을 삭제했습니다.");
    } catch (error) { setMessage(error instanceof Error ? error.message : "모델을 삭제하지 못했습니다."); }
    finally { setBusy(false); }
  }
  async function validate(profile: EmbeddingProfile) {
    if (!window.confirm(`'${profile.name}' 모델에 짧은 문장 두 개를 보내 연결과 차원을 확인합니다. 외부 서비스 비용이 발생할 수 있습니다. 계속하시겠습니까?`)) return;
    setBusy(true); setMessage("");
    try {
      await api(`/v1/embedding-profiles/${encodeURIComponent(profile.profile_id)}/validate`, session, {});
      setMessage(`${profile.name}: 문서·질문 모델의 응답 형식과 차원을 확인했습니다. 검색 품질은 별도로 평가해야 합니다.`);
    } catch (error) { setMessage(error instanceof Error ? error.message : "모델을 확인하지 못했습니다."); }
    finally { setBusy(false); }
  }
  async function rebuild(profile: EmbeddingProfile) {
    if (!window.confirm(`'${profile.name}'의 기존 검색 내용을 현재 설정으로 다시 임베딩합니다. 외부 서비스 비용이 발생할 수 있습니다. 계속하시겠습니까?`)) return;
    setBusy(true); setMessage("");
    try {
      const result = await api<{index_version: string}>(`/v1/ingest-index-rebuild?profile_id=${encodeURIComponent(profile.profile_id)}`, session, {});
      setMessage(`${profile.name}: 검색 준비 완료 · ${result.index_version.slice(0, 8)}`);
      await refresh();
    } catch (error) { setMessage(error instanceof Error ? error.message : "검색 준비에 실패했습니다."); }
    finally { setBusy(false); }
  }
  const selected = profiles.find(profile => profile.profile_id === editing);
  return <fieldset><legend>임베딩 모델 목록</legend>
    <p className="hint">문서를 검색용 숫자로 바꾸는 모델입니다. 모델마다 검색 내용이 따로 저장되며, 이미 사용한 모델의 주소·모델 ID·차원은 새 ID로 등록해야 합니다.</p>
    <p className="hint">현재는 설정한 주소의 /embeddings 요청과 응답 형식이 호환되는 모델을 사용할 수 있습니다. 주소만 바꿔 모든 서비스가 동작하는 것은 아닙니다.</p>
    {profiles.map(profile => <div className="model-row" key={profile.profile_id}>
      <div><strong>{profile.name}</strong><p className="hint">{profile.profile_id} · {profile.document_model} / {profile.query_model} · {profile.dimensions}차원{profile.default ? " · 서버 기본" : profile.enabled ? "" : " · 새 문서 사용 중지"}</p></div>
      <button type="button" className="secondary" disabled={busy} onClick={() => void validate(profile)}>연결 확인</button>
      {profile.has_index && <button type="button" className="secondary" disabled={busy || !profile.enabled} onClick={() => void rebuild(profile)}>색인 다시 준비</button>}
      {!profile.default && <><button type="button" className="secondary" disabled={busy} onClick={() => edit(profile)}>수정</button>
        <button type="button" className="secondary" disabled={busy} onClick={() => void erase(profile)}>삭제</button></>}
    </div>)}
    <div className="settings-grid">
      <label>모델 ID<input value={draft.profile_id} disabled={Boolean(editing)} placeholder="예: korean-search" onChange={event => setDraft({...draft, profile_id: event.target.value})}/></label>
      <label>표시 이름<input value={draft.name} placeholder="예: 한국어 검색" onChange={event => setDraft({...draft, name: event.target.value})}/></label>
      <label>임베딩 API 주소<input value={draft.base_url} disabled={selected?.has_index} placeholder="https://example.com/v1" onChange={event => setDraft({...draft, base_url: event.target.value})}/></label>
      <label>API 키<input type="password" autoComplete="new-password" value={draft.api_key} disabled={clearKey} placeholder={selected?.api_key_configured ? "설정됨 · 변경할 때 입력" : "필요한 경우 입력"} onChange={event => setDraft({...draft, api_key: event.target.value})}/>{editing && selected?.api_key_configured && <span><input type="checkbox" checked={clearKey} onChange={event => setClearKey(event.target.checked)}/> 저장 시 API 키 삭제</span>}</label>
      <label>문서 모델<input value={draft.document_model} disabled={selected?.has_index} onChange={event => setDraft({...draft, document_model: event.target.value})}/></label>
      <label>질문 모델<input value={draft.query_model} disabled={selected?.has_index} onChange={event => setDraft({...draft, query_model: event.target.value})}/></label>
      <label>벡터 차원<input type="number" min={1} max={65536} value={draft.dimensions} disabled={selected?.has_index} onChange={event => setDraft({...draft, dimensions: Number(event.target.value)})}/></label>
      <label>한 입력 최대 바이트<input type="number" min={256} max={1000000} value={draft.max_input_bytes} placeholder="직접 설정" onChange={event => setDraft({...draft, max_input_bytes: event.target.value === "" ? "" : Number(event.target.value)})}/></label>
      <label>한 요청 최대 청크 수<input type="number" min={1} max={512} value={draft.batch_size} onChange={event => setDraft({...draft, batch_size: Number(event.target.value)})}/></label>
      <label><input type="checkbox" checked={draft.enabled} onChange={event => setDraft({...draft, enabled: event.target.checked})}/> 새 문서 처리에 사용</label>
    </div>
    <p className="hint">바이트 제한은 직접 정하는 서버 측 안전 기준이며 모델의 토큰 제한과 다릅니다. 모델의 입력 한도를 보장하는 값이 아니므로 사용 환경에 맞게 설정하세요.</p>
    {selected?.has_index && <p className="hint">기존 검색 버전을 보존하기 위해 주소, 모델과 차원은 수정할 수 없습니다. 다른 설정은 새 모델 ID로 등록해 주세요.</p>}
    <div className="actions"><button type="button" className="secondary" disabled={busy || !draft.profile_id || !draft.name || !draft.base_url || !draft.document_model || !draft.query_model || draft.max_input_bytes === "" || draft.max_input_bytes < 256 || draft.max_input_bytes > 1000000} onClick={() => void save()}>{editing ? "모델 수정" : "모델 추가"}</button>
      {editing && <button type="button" className="text-button" onClick={reset}>수정 취소</button>}</div>
    {message && <p role="status">{message}</p>}
  </fieldset>;
}
