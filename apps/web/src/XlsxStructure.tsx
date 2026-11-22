import { useEffect, useState } from "react";
import { api } from "./api";
import type { Session } from "./api";

type Region = {
  start: string; end: string; kind: string; headers: string[]; title: string | null; uncertain: boolean;
  origin?: string; status?: string; verification?: string; table_name?: string;
};
type Cell = {address: string; display_text: string; hidden_merged: boolean; merged_range: string | null;
  cache_status: string | null; truncated?: boolean; formula_references?: {sheet: string; start: string; end: string}[]};
type SheetSummary = {name: string; visibility: string; bounds: [number, number, number, number]};
type Sheet = SheetSummary & {regions: Region[]; excluded: Record<string, string>;
  charts: {title: string | null; type: string}[]; boundary_candidates: {axis: string; after: number}[];
  analysis_status?: string; suggested_review?: {regions: Region[]; excluded: Record<string, string>};
  stale_review?: {regions: Region[]; excluded: Record<string, string>; reason: string}};
type Grid = {cells: Cell[]; row: number; column: number; last_row: number; last_column: number};
const GRID_ROWS = 10;
const GRID_COLUMNS = 8;
const certainty: Record<string, string> = {
  explicit: "Excel 등록 표", user_verified: "사용자 확인", llm_validated: "모델 제안 · 확인 필요", inferred: "추정 · 확인 필요",
};

function columnLabel(column: number): string {
  const prefix = Math.floor((column - 1) / 26);
  return (prefix ? columnLabel(prefix) : "") + String.fromCharCode(65 + (column - 1) % 26);
}

function containsAddress(region: Region, row: number, column: number) {
  function coordinates(address: string) {
    const match = /^([A-Z]{1,3})([1-9][0-9]{0,6})$/.exec(address);
    if (!match) return undefined;
    const col = [...match[1]].reduce((value, letter) => value * 26 + letter.charCodeAt(0) - 64, 0);
    return {row: Number(match[2]), col};
  }
  const start = coordinates(region.start);
  const end = coordinates(region.end);
  return start && end && row >= start.row && row <= end.row && column >= start.col && column <= end.col;
}

const kinds: Record<string, string> = {table: "표", title: "제목", key_value: "항목·값", code: "코드", text: "텍스트", unknown: "미확정"};
const analysis: Record<string, string> = {
  reviewed: "사용자 검토", cached: "저장된 모델 분석", analyzed: "모델 분석 완료", analyzed_uncached: "모델 분석 완료 · 캐시 저장 실패", disabled: "로컬 추정",
  input_limit: "모델 입력 한도 초과", sheet_limit: "모델 호출 시트 한도 도달", invalid_response: "모델 결과 검증 실패",
  incomplete: "모델 응답 미완료", review_required: "모델의 셀 제외 제안 검토 필요", provider_error: "모델 호출 실패", cache_unavailable: "분석 캐시 사용 불가",
  review_reset: "검토 초기화 · 로컬 추정",
};
const newRegion: Region = {start: "", end: "", kind: "unknown", headers: [], title: null, uncertain: true};

export function XlsxStructure({session, documentId, jobId, disabled, onChanged}: {
  session: Session; documentId: string; jobId: string; disabled: boolean; onChanged: (jobId: string) => void;
}) {
  const url = `/v1/ingest/${documentId}/jobs/${jobId}/xlsx-structure`;
  const [sheets, setSheets] = useState<SheetSummary[]>();
  const [sheet, setSheet] = useState<Sheet>();
  const [index, setIndex] = useState(0);
  const [regions, setRegions] = useState<Region[]>([]);
  const [excluded, setExcluded] = useState<Record<string, string>>({});
  const [row, setRow] = useState(1);
  const [column, setColumn] = useState(1);
  const [grid, setGrid] = useState<Grid>();
  const [selectedCell, setSelectedCell] = useState<Cell>();
  const [highlight, setHighlight] = useState<number>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const cellsUrl = url.replace("/xlsx-structure", "/xlsx-cells");

  async function select(summary: SheetSummary, position: number) {
    setBusy(true);
    setError("");
    try {
      const detail = await api<Sheet>(url + "?sheet=" + encodeURIComponent(summary.name), session);
      setIndex(position);
      setSheet(detail);
      setRegions(detail.regions.map(region => region.verification === "llm_validated"
        ? {...region, uncertain: true} : region));
      setExcluded(detail.excluded);
      setColumn(detail.bounds[0]);
      setRow(detail.bounds[1]);
      setGrid(undefined);
      setSelectedCell(undefined);
      setHighlight(undefined);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "시트를 열지 못했습니다.");
    } finally { setBusy(false); }
  }

  async function load() {
    setBusy(true);
    setError("");
    try {
      const data = await api<{xlsx_sheets: SheetSummary[]}>(url, session);
      setSheets(data.xlsx_sheets);
      if (data.xlsx_sheets[0]) await select(data.xlsx_sheets[0], 0);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "구조를 불러오지 못했습니다.");
    } finally { setBusy(false); }
  }

  useEffect(() => {
    if (!sheet) return;
    let current = true;
    const query = new URLSearchParams({sheet: sheet.name, row: String(row), column: String(column),
      rows: String(GRID_ROWS), columns: String(GRID_COLUMNS)});
    api<Grid>(cellsUrl + "?" + query, session).then(data => {
      if (current) setGrid(data);
    }).catch(reason => {
      if (current) setError(reason instanceof Error ? reason.message : "셀을 불러오지 못했습니다.");
    });
    return () => { current = false; };
  }, [cellsUrl, column, row, session, sheet]);

  async function openCell(cell: Cell) {
    if (!sheet) return;
    setSelectedCell(cell);
    const query = new URLSearchParams({sheet: sheet.name, address: cell.address});
    try {
      const data = await api<{cells: Cell[]}>(cellsUrl + "?" + query, session);
      setSelectedCell(current => current === cell ? data.cells[0] : current);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "셀 상세를 불러오지 못했습니다.");
    }
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

  async function reset() {
    if (!sheet) return;
    setBusy(true);
    setError("");
    try {
      const result = await api<{job_id: string}>(url + "/reset", session, {sheets: [sheet.name]});
      onChanged(result.job_id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "검토를 초기화하지 못했습니다.");
    } finally { setBusy(false); }
  }

  function update(position: number, fields: Partial<Region>) {
    setRegions(current => current.map((item, index) => index === position ? {...item, ...fields} : item));
  }

  const visible = new Map(grid?.cells.map(cell => [cell.address, cell]));
  const gridRows = grid ? Array.from({length: grid.last_row - grid.row + 1}, (_, index) => grid.row + index) : [];
  const gridColumns = grid ? Array.from({length: grid.last_column - grid.column + 1}, (_, index) => grid.column + index) : [];
  return <details className="hint">
    <summary>Excel 구조 검토</summary>
    {!sheet && <button type="button" className="secondary" disabled={busy} onClick={() => void load()}>시트와 영역 불러오기</button>}
    {sheet && <>
      <label>시트 <select value={index} disabled={busy} onChange={event => {
        const position = Number(event.target.value);
        void select(sheets![position], position);
      }}>{sheets!.map((item, index) => <option key={item.name} value={index}>{item.name}{item.visibility !== "visible" ? " (숨김)" : ""}</option>)}</select></label>
      {sheet.analysis_status && <p>{analysis[sheet.analysis_status] ?? sheet.analysis_status}</p>}
      {!!sheet.charts.length && <p>차트 · {sheet.charts.map(item => item.title || item.type).join(", ")}</p>}
      {!!sheet.boundary_candidates.length && <p>경계 후보 · {sheet.boundary_candidates.map(item => `${item.axis === "row" ? "행" : "열"} ${item.after} 뒤`).join(", ")}</p>}
      {sheet.stale_review && <p role="status">이전 검토가 새 원본과 맞지 않아 적용하지 않았습니다 · {sheet.stale_review.reason}</p>}
      <p>시작·끝 셀은 범위에 포함됩니다. 표 헤더 주소는 쉼표로 구분하세요. 여러 행의 헤더와 병합된 헤더는 위쪽 행부터 모두 지정합니다.</p>
      <fieldset disabled={disabled || busy}>
        {sheet.stale_review && <button type="button" className="secondary" onClick={() => {
          setRegions(sheet.stale_review!.regions.map(region => ({...region, uncertain: true})));
          setExcluded(sheet.stale_review!.excluded);
        }}>이전 검토 불러오기</button>}
        {sheet.analysis_status === "reviewed" && <button type="button" className="secondary"
          onClick={() => void reset()}>이 시트 검토 초기화 후 새 작업 만들기</button>}
        {sheet.suggested_review && <button type="button" className="secondary" onClick={() => {
          setRegions(sheet.suggested_review!.regions.map(region => region.verification === "llm_validated"
            ? {...region, uncertain: true} : region));
          setExcluded(sheet.suggested_review!.excluded);
        }}>모델 제안 불러오기</button>}
        {regions.map((region, position) => <fieldset key={position}>
          <legend>{region.table_name ? `Excel 표 ${region.table_name}` : `${kinds[region.kind]} · ${certainty[region.verification || "inferred"]}`}</legend>
          <button type="button" className="secondary" aria-pressed={highlight === position}
            onClick={() => {
              const match = /^([A-Z]{1,3})([0-9]+)$/.exec(region.start);
              if (!match || !sheet) return;
              const col = [...match[1]].reduce((value, letter) => value * 26 + letter.charCodeAt(0) - 64, 0);
              const targetRow = Number(match[2]);
              if (col < sheet.bounds[0] || col > sheet.bounds[2] || targetRow < sheet.bounds[1] || targetRow > sheet.bounds[3]) return;
              setHighlight(position); setGrid(undefined); setColumn(col); setRow(targetRow);
              if (column === col && row === targetRow) setGrid(grid);
            }}>격자에서 범위 보기</button>
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
      <div className="xlsx-grid-scroll">
        <table className="xlsx-grid" aria-label="원본 셀 격자"><thead><tr><th scope="col">행</th>
          {gridColumns.map(col => <th key={col} scope="col">{columnLabel(col)}</th>)}
        </tr></thead><tbody>{gridRows.map(r => <tr key={r}><th scope="row">{r}</th>
          {gridColumns.map(c => {
            const key = columnLabel(c) + r;
            const cell = visible.get(key);
            const selected = highlight !== undefined && regions[highlight] && containsAddress(regions[highlight], r, c);
            const inRegion = regions.some(region => containsAddress(region, r, c));
            return <td key={key} className={selected ? "xlsx-grid-selected" : inRegion ? "xlsx-grid-region" : ""}>
              {cell && <button type="button" className="text-button" aria-label={`${key} ${cell.display_text.slice(0, 80)}`}
                onClick={() => void openCell(cell)}>{cell.display_text || key}{cell.truncated && "…"}{cell.hidden_merged && " · 병합 내부"}</button>}
            </td>;
          })}</tr>)}</tbody></table>
      </div>
      <div className="xlsx-grid-navigation">
        <button type="button" disabled={row <= sheet.bounds[1]} onClick={() => setRow(Math.max(sheet.bounds[1], row - GRID_ROWS))}>위쪽 행</button>
        <button type="button" disabled={row + GRID_ROWS > sheet.bounds[3]} onClick={() => setRow(row + GRID_ROWS)}>아래쪽 행</button>
        <button type="button" disabled={column <= sheet.bounds[0]} onClick={() => setColumn(Math.max(sheet.bounds[0], column - GRID_COLUMNS))}>왼쪽 열</button>
        <button type="button" disabled={column + GRID_COLUMNS > sheet.bounds[2]} onClick={() => setColumn(column + GRID_COLUMNS)}>오른쪽 열</button>
      </div>
      {selectedCell && <div>
        <p className="result-text">{selectedCell.address} · {selectedCell.display_text}{selectedCell.hidden_merged && ` · 병합 내부 ${selectedCell.merged_range}`}</p>
        {!!selectedCell.formula_references?.length && <p>수식 참조 · {selectedCell.formula_references.map(ref => `${ref.sheet} ${ref.start}:${ref.end}`).join(", ")}</p>}
        <label>{selectedCell.address} 검색 제외 이유 <input maxLength={500} disabled={disabled || busy}
          value={excluded[selectedCell.address] ?? ""} onChange={event => {
            const value = event.target.value;
            setExcluded(current => {
              const next = {...current};
              if (value) next[selectedCell.address] = value;
              else delete next[selectedCell.address];
              return next;
            });
          }}/></label>
      </div>}
    </>}
    {error && <p role="alert">{error}</p>}
  </details>;
}
