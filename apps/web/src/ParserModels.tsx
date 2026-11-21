import { useEffect, useState } from "react";
import { api, put, remove } from "./api";
import type { Session } from "./api";

type Connection = {name: string; parser_provider: string; parser_base_url: string; parser_model: string;
  parser_mode: string; parser_options: string; parser_sync_timeout: number; api_key_configured?: boolean};
type Profile = Connection & {profile_id: string; active: boolean; applied: boolean};
type Result = {profiles: Profile[]; current: Connection; environment?: (Connection & {active: boolean; applied: boolean}) | null};
const empty: Connection = {name: "", parser_provider: "upstage", parser_base_url: "", parser_model: "document-parse-260930", parser_mode: "enhanced", parser_options: "", parser_sync_timeout: 900};

export function ParserModels({session, disabled, onActivated}: {session: Session; disabled: boolean; onActivated: () => Promise<void>}) {
  const [data, setData] = useState<Result>();
  const [draft, setDraft] = useState<Connection>(empty);
  const [editing, setEditing] = useState("");
  const [key, setKey] = useState("");
  const [clearKey, setClearKey] = useState(false);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const refresh = async () => setData(await api<Result>("/v1/parser-profiles", session));
  useEffect(() => {void refresh().catch(error => setMessage(error.message));}, [session]);
  function reset() { setDraft(empty); setEditing(""); setKey(""); setClearKey(false); }
  async function save() {
    setBusy(true); setMessage("");
    try {
      const id = editing || `parser-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
      const {api_key_configured: _configured, ...body} = draft;
      await put(`/v1/parser-profiles/${encodeURIComponent(id)}`, session,
        {...body, ...(!editing || key || clearKey ? {parser_api_key: clearKey ? "" : key} : {})});
      await refresh(); reset(); setMessage("파서를 저장했습니다. 목록에서 선택하면 적용됩니다.");
    } catch (error) { setMessage(error instanceof Error ? error.message : "파서를 저장하지 못했습니다."); }
    finally { setBusy(false); }
  }
  async function useProfile(profile: {profile_id: string; name: string}) {
    setBusy(true); setMessage("");
    try { await api(`/v1/parser-profiles/${encodeURIComponent(profile.profile_id)}/activate`, session, {}); await onActivated(); await refresh(); setMessage(`${profile.name} 파서를 적용했습니다.`); }
    catch (error) { setMessage(error instanceof Error ? error.message : "파서를 적용하지 못했습니다."); }
    finally { setBusy(false); }
  }
  async function erase(profile: Profile) {
    if (!window.confirm(`'${profile.name}'을 목록에서 삭제하시겠습니까?`)) return;
    setBusy(true); setMessage("");
    try { await remove(`/v1/parser-profiles/${encodeURIComponent(profile.profile_id)}`, session); await refresh(); if (editing === profile.profile_id) reset(); }
    catch (error) { setMessage(error instanceof Error ? error.message : "파서를 삭제하지 못했습니다."); }
    finally { setBusy(false); }
  }
  async function saveCurrent() {
    if (!data?.current.parser_provider) return;
    const existing = data.profiles.find(profile => profile.applied);
    if (existing) { await useProfile(existing); return; }
    setBusy(true); setMessage("");
    try {
      const id = `current-${data.current.parser_provider}`;
      await put(`/v1/parser-profiles/${id}`, session, {name: data.current.parser_provider === "paddle_vl" ? "PaddleOCR-VL" : data.current.parser_provider});
      await refresh(); setMessage("현재 파서를 목록에 저장했습니다.");
    } catch (error) { setMessage(error instanceof Error ? error.message : "현재 파서를 저장하지 못했습니다."); }
    finally { setBusy(false); }
  }
  return <div className="embedding-models"><fieldset><legend>파서 목록</legend>
    <p className="hint">파서별 주소와 키를 등록하고 사용할 파서를 선택합니다. 파서를 바꾸면 이후 새 작업에 적용됩니다.</p>
    {data && <p className="hint">현재 사용: {data.profiles.find(item => item.active)?.name || (data.environment?.active ? "환경 기본 파서" : data.current.parser_provider) || "설정되지 않음"}</p>}
    {data && <label className="stacked-label">사용할 파서<select disabled={busy || disabled} value={data.profiles.find(item => item.active)?.profile_id || (data.environment?.active ? 'environment' : 'current')} onChange={event => {
      const id = event.target.value;
      const profile = id === 'environment' ? {profile_id: id, name: '환경 기본 파서'} : data.profiles.find(item => item.profile_id === id);
      if (profile) void useProfile(profile);
    }}>
      {!data.profiles.some(item => item.active) && !data.environment?.active && <option value="current">현재 설정 · {data.current.parser_provider || '설정되지 않음'}</option>}
      {data.environment && <option value="environment">환경 기본 · {data.environment.parser_provider === 'paddle_vl' ? 'PaddleOCR-VL' : data.environment.parser_provider}</option>}
      {data.profiles.map(profile => <option key={profile.profile_id} value={profile.profile_id}>{profile.name}</option>)}
    </select></label>}
    {data?.current.parser_provider && !data.profiles.some(item => item.active) && <button type="button" className="secondary" disabled={busy || disabled} onClick={() => void saveCurrent()}>현재 파서를 목록에 저장</button>}
    {data?.profiles.map(profile => <div className="model-row" key={profile.profile_id}>
      <div><strong>{profile.name}</strong><p className="hint">{profile.parser_provider === "paddle_vl" ? "PaddleOCR-VL" : profile.parser_provider} · {profile.parser_base_url}</p></div>
      {profile.active ? <span>{profile.applied ? "사용 중" : "적용 필요"}</span> : <button type="button" className="secondary" disabled={busy || disabled} onClick={() => void useProfile(profile)}>선택</button>}
      <button type="button" className="secondary" disabled={busy} onClick={() => { setEditing(profile.profile_id); setDraft({name: profile.name, parser_provider: profile.parser_provider, parser_base_url: profile.parser_base_url, parser_model: profile.parser_model, parser_mode: profile.parser_mode, parser_options: profile.parser_options, parser_sync_timeout: profile.parser_sync_timeout, api_key_configured: profile.api_key_configured}); setKey(""); setClearKey(false); setMessage(""); }}>수정</button>
      <button type="button" className="secondary" disabled={busy} onClick={() => void erase(profile)}>삭제</button>
    </div>)}
    <details key={editing || 'new'} open={editing ? true : undefined}><summary>{editing ? '파서 수정' : '새 파서 추가'}</summary><div className="settings-grid">
      <label>이름<input value={draft.name} onChange={event => setDraft({...draft, name: event.target.value})}/></label>
      <label>파서 종류<select value={draft.parser_provider} onChange={event => setDraft({...draft, parser_provider: event.target.value})}><option value="upstage">Upstage</option><option value="paddle_vl">PaddleOCR-VL</option></select></label>
      <label>API 기본 주소<input value={draft.parser_base_url} placeholder={draft.parser_provider === "paddle_vl" ? "https://example.com/ai/stack" : "https://example.com/v1"} onChange={event => setDraft({...draft, parser_base_url: event.target.value})}/></label>
      <label>API 키<input type="password" autoComplete="new-password" disabled={clearKey} placeholder={draft.api_key_configured ? "설정됨 · 변경할 때 입력" : "필요한 경우 입력"} value={key} onChange={event => setKey(event.target.value)}/>{editing && draft.api_key_configured && <span><input type="checkbox" checked={clearKey} onChange={event => setClearKey(event.target.checked)}/> 저장 시 API 키 삭제</span>}</label>
      {draft.parser_provider !== "paddle_vl" && <><label>파싱 모델<input value={draft.parser_model} onChange={event => setDraft({...draft, parser_model: event.target.value})}/></label><label>파싱 모드<input value={draft.parser_mode} onChange={event => setDraft({...draft, parser_mode: event.target.value})}/></label></>}
      <label>추가 옵션 (key=value;key=value)<input value={draft.parser_options} onChange={event => setDraft({...draft, parser_options: event.target.value})}/></label>
      <label>동기 요청 제한 시간 (초)<input type="number" min={1} max={86400} value={draft.parser_sync_timeout} onChange={event => setDraft({...draft, parser_sync_timeout: Number(event.target.value)})}/></label>
    </div>
    {draft.parser_provider === "paddle_vl" && <p className="hint">기본 요청 경로는 /ocr/vl/layout-parsing입니다. 이미지별 응답 제한은 기본 90초이며 추가 옵션에 image_timeout_seconds=초로 설정할 수 있습니다. 동기 요청 제한 시간을 넘지 않습니다.</p>}
    <div className="actions"><button type="button" className="secondary" disabled={busy || !draft.name.trim() || !draft.parser_base_url} onClick={() => void save()}>{editing ? "파서 수정" : "파서 추가"}</button>{editing && <button type="button" className="text-button" onClick={reset}>수정 취소</button>}</div></details>
    {disabled && <p className="hint">다른 설정의 변경 사항을 저장한 뒤 파서를 선택해 주세요.</p>}
    {message && <p role="status">{message}</p>}
  </fieldset></div>;
}
