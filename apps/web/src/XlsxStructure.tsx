import { useState } from "react";
import { api } from "./api";
import type { Session } from "./api";

type Region = {
  start: string; end: string; kind: string; headers: string[]; title: string | null; uncertain: boolean;
  origin?: string; status?: string; table_name?: string;
};
type Cell = {has_content: boolean; address: string; display_text: string; hidden_merged: boolean; merged_range: string | null; cache_status: string | null};
type Sheet = {name: string; visibility: string; regions: Region[]; cells: Cell[]; excluded: Record<string, string>;
  hidden_merged_cells: Cell[]; charts: {title: string | null; type: string}[]; boundary_candidates: {axis: string; after: number}[];
  analysis_status?: string; suggested_review?: {regions: Region[]; excluded: Record<string, string>}};
const kinds: Record<string, string> = {table: "표", title: "제목", key_value: "항목·값", code: "코드", text: "텍스트", unknown: "미확정"};
const analysis: Record<string, string> = {
  reviewed: "사용자 검토", cached: "저장된 모델 분석", analyzed: "모델 분석 완료", analyzed_uncached: "모델 분석 완료 · 캐시 저장 실패", disabled: "로컬 추정",
  input_limit: "모델 입력 한도 초과", sheet_limit: "모델 호출 시트 한도 도달", invalid_response: "모델 결과 검증 실패",
  incomplete: "모델 응답 미완료", review_required: "모델의 셀 제외 제안 검토 필요", provider_error: "모델 호출 실패", cache_unavailable: "분석 캐시 사용 불가",
};
const newRegion: Region = {start: "", end: "", kind: "unknown", headers: [], title: null, uncertain: true};

export function XlsxStructure({session, documentId, jobId, disabled, onChanged}: {
  session: Session; documentId: string; jobId: string; disabled: boolean; onChanged: (jobId: string) => void;
}) {
  const url = `/v1/ingest/${documentId}/jobs/${jobId}/xlsx-structure`;
  const [sheets, setSheets] = useState<Sheet[]>();
  const [index, setIndex] = useState(0);
  const [regions, setRegions] = useState<Region[]>([]);
  const [excluded, setExcluded] = useState<Record<string, string>>({});
  const [cellFilter, setCellFilter] = useState("");
  const [page, setPage] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const sheet = sheets?.[index];

  function select(sheet: Sheet, position: number) {
    setIndex(position);
    setRegions(sheet.regions);
    setExcluded(sheet.excluded);
    setCellFilter("");
    setPage(0);
    setError("");
  }

  async function load() {
    setBusy(true);
    setError("");
    try {
      const data = await api<{xlsx_sheets: Sheet[]}>(url, session);
      setSheets(data.xlsx_sheets);
      if (data.xlsx_sheets[0]) select(data.xlsx_sheets[0], 0);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "구조를 불러오지 못했습니다.");
    } finally { setBusy(false); }
  }

  async function save() {
    if (!sheet) return;
    setBusy(true);
    setError("");
    try {
      const proposal = regions.map(({start, end, kind, headers, title, uncertain}) =>
        ({start, end, kind, headers: headers.filter(Boolean), title, uncertain}));
      const result = await api<{job_id: string}>(url, session, {[sheet.name]: {regions: proposal, excluded}});
      onChanged(result.job_id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "구조를 저장하지 못했습니다.");
    } finally { setBusy(false); }
  }

  function update(position: number, fields: Partial<Region>) {
    setRegions(current => current.map((item, index) => index === position ? {...item, ...fields} : item));
  }

  const source = sheet ? [...sheet.hidden_merged_cells, ...sheet.cells].filter(cell =>
    cell.has_content && cell.address.includes(cellFilter.trim().toUpperCase())) : [];
  return <details className="hint">
    <summary>Excel 구조 검토</summary>
    {!sheets && <button type="button" className="secondary" disabled={busy} onClick={() => void load()}>시트와 영역 불러오기</button>}
    {sheet && <>
      <label>시트 <select value={index} onChange={event => {
        const position = Number(event.target.value);
        select(sheets![position], position);
      }}>{sheets!.map((item, index) => <option key={item.name} value={index}>{item.name}{item.visibility !== "visible" ? " (숨김)" : ""}</option>)}</select></label>
      {sheet.analysis_status && <p>{analysis[sheet.analysis_status] ?? sheet.analysis_status}</p>}
      {!!sheet.charts.length && <p>차트 · {sheet.charts.map(item => item.title || item.type).join(", ")}</p>}
      {!!sheet.boundary_candidates.length && <p>경계 후보 · {sheet.boundary_candidates.map(item => `${item.axis === "row" ? "행" : "열"} ${item.after} 뒤`).join(", ")}</p>}
      <p>시작·끝 셀은 범위에 포함됩니다. 표 헤더 주소는 열마다 하나씩 쉼표로 구분하세요.</p>
      <fieldset disabled={disabled || busy}>
        {sheet.suggested_review && <button type="button" className="secondary" onClick={() => {
          setRegions(sheet.suggested_review!.regions);
          setExcluded(sheet.suggested_review!.excluded);
        }}>모델 제안 불러오기</button>}
        {regions.map((region, position) => <fieldset key={position}>
          <legend>{region.table_name ? `Excel 표 ${region.table_name}` : `${kinds[region.kind]} · ${region.status === "verified" ? "검증됨" : "검토 필요"}`}</legend>
          <label>시작 셀 <input aria-label="시작 셀" size={8} value={region.start} disabled={!!region.table_name}
            onChange={event => update(position, {start: event.target.value.trim().toUpperCase()})}/></label>
          <label>끝 셀 <input size={8} value={region.end} disabled={!!region.table_name}
            onChange={event => update(position, {end: event.target.value.trim().toUpperCase()})}/></label>
          <label>영역 종류 <select value={region.kind} disabled={!!region.table_name}
            onChange={event => update(position, {kind: event.target.value, headers: []})}>
            {Object.entries(kinds).map(([value, label]) => <option value={value} key={value}>{label}</option>)}
          </select></label>
          {region.kind === "table" && <label>헤더 주소 <input value={region.headers.join(",")} disabled={!!region.table_name}
            onChange={event => update(position, {headers: event.target.value.toUpperCase().replaceAll(" ", "").split(",")})}/></label>}
          <label>제목 주소 <input size={8} value={region.title ?? ""}
            onChange={event => update(position, {title: event.target.value.trim().toUpperCase() || null})}/></label>
          <label><input type="checkbox" checked={region.uncertain} onChange={event => update(position, {uncertain: event.target.checked})}/> 경계 또는 역할 미확정</label>
          {!region.table_name && <button type="button" className="secondary"
            onClick={() => setRegions(current => current.filter((_, index) => index !== position))}>영역 삭제</button>}
        </fieldset>)}
        <button type="button" className="secondary" onClick={() => setRegions(current => [...current, {...newRegion}])}>영역 추가</button>
        <button type="button" onClick={() => void save()}>현재 시트 수정 후 새 작업 만들기</button>
      </fieldset>
      <details><summary>원본 셀 · 제외 이유 편집</summary>
        <label>셀 주소 필터 <input value={cellFilter} onChange={event => { setCellFilter(event.target.value); setPage(0); }}/></label>
        <table><thead><tr><th>셀</th><th>값</th><th>검색 제외 이유</th></tr></thead><tbody>
          {source.slice(page * 50, (page + 1) * 50).map(cell => <tr key={cell.address}>
            <th scope="row">{cell.address}{cell.hidden_merged && <small> · 병합 내부 {cell.merged_range}</small>}</th>
            <td style={{whiteSpace: "pre-wrap"}}>{cell.display_text}</td>
            <td><input aria-label={`${cell.address} 제외 이유`} disabled={disabled || busy} value={excluded[cell.address] ?? ""}
              onChange={event => {
                const value = event.target.value;
                setExcluded(current => {
                  const next = {...current};
                  if (value) next[cell.address] = value;
                  else delete next[cell.address];
                  return next;
                });
              }}/></td>
          </tr>)}
        </tbody></table>
        <button type="button" disabled={page === 0} onClick={() => setPage(page - 1)}>이전 셀</button>
        <button type="button" disabled={(page + 1) * 50 >= source.length} onClick={() => setPage(page + 1)}>다음 셀</button>
      </details>
    </>}
    {error && <p role="alert">{error}</p>}
  </details>;
}
