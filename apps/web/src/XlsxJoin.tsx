import { useState } from "react";
import { api } from "./api";
import type { Session } from "./api";

type Table = {sheet: string; start: string; end: string; title: string | null; columns: Record<string, string | null>};
type Point = {sheet: string; row: number; cells: string[]; values: string[]};
type Candidate = {left: Point; right: Point; ambiguous: boolean; conflict: boolean};
type Candidates = {candidates: Candidate[]; complete: boolean; total: number; offset: number};
type Result = {
  complete: boolean; result: {matched_rows: number; value?: number | string | null; numeric_values?: number};
  join: {joined_rows: number; ambiguous_pairs: number; unapproved_pairs: number; unmatched_left_rows: number; unmatched_right_rows: number};
};
const operations: Record<string, string> = {count: "행 개수", sum: "합계", average: "평균", min: "최솟값", max: "최댓값"};

export function XlsxJoin({session, documentId, version, profile}: {
  session: Session; documentId: string; version: string; profile: string;
}) {
  const base = `/v1/ingest/${documentId}`;
  const [tables, setTables] = useState<Table[]>();
  const [left, setLeft] = useState(0);
  const [right, setRight] = useState(0);
  const [page, setPage] = useState<Candidates>();
  const [approved, setApproved] = useState<Record<string, {left_row: number; right_row: number}>>({});
  const [operation, setOperation] = useState("count");
  const [column, setColumn] = useState("");
  const [side, setSide] = useState<"left" | "right">("right");
  const [result, setResult] = useState<Result>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const lhs = tables?.[left];
  const rhs = tables?.[right];
  const query = {index_version: version, ...(profile ? {profile_id: profile} : {})};

  function changeTable() {
    setPage(undefined); setApproved({}); setResult(undefined); setColumn(""); setError("");
  }

  async function load() {
    setError(""); setBusy(true);
    try {
      const response = await api<{tables: Table[]}>(base + "/xlsx-tables?" + new URLSearchParams(query), session);
      setTables(response.tables);
      setRight(Math.max(0, response.tables.findIndex(table => table.sheet !== response.tables[0]?.sheet)));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "표를 불러오지 못했습니다."); }
    finally { setBusy(false); }
  }

  async function loadCandidates(offset: number) {
    if (!lhs || !rhs) return;
    setError(""); setBusy(true);
    try {
      const params = new URLSearchParams({...query, sheet: lhs.sheet, start: lhs.start,
        right_sheet: rhs.sheet, right_start: rhs.start, offset: String(offset)});
      setPage(await api<Candidates>(base + "/xlsx-join-candidates?" + params, session));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "연결 후보를 불러오지 못했습니다."); }
    finally { setBusy(false); }
  }

  async function run() {
    if (!lhs || !rhs) return;
    setError(""); setBusy(true);
    try {
      setResult(await api<Result>("/v1/xlsx-query", session, {document_id: documentId, ...query,
        sheet: lhs.sheet, start: lhs.start, operation,
        ...(operation === "count" ? {} : {column, column_side: side}),
        join: {sheet: rhs.sheet, start: rhs.start, approved_pairs: Object.values(approved)}}));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "연결한 표를 조회하지 못했습니다."); }
    finally { setBusy(false); }
  }

  function label(point: Point) {
    return `${point.sheet} ${point.cells.join(", ")} · ${point.values.join(" / ")}`;
  }

  return <details className="hint">
    <summary>시트 간 행 연결 검토 및 조회</summary>
    {!tables && <button type="button" disabled={busy} onClick={() => void load()}>게시된 표 불러오기</button>}
    {tables && new Set(tables.map(table => table.sheet)).size < 2 && <p>서로 다른 시트에 확인된 표가 두 개 이상 필요합니다.</p>}
    {tables && new Set(tables.map(table => table.sheet)).size >= 2 && <>
      <label>왼쪽 표 <select value={left} disabled={busy} onChange={event => {setLeft(Number(event.target.value)); changeTable();}}>
        {tables.map((table, index) => <option key={index} value={index}>{table.sheet} {table.start}:{table.end}</option>)}
      </select></label>
      <label>오른쪽 표 <select value={right} disabled={busy} onChange={event => {setRight(Number(event.target.value)); changeTable();}}>
        {tables.map((table, index) => <option key={index} value={index}>{table.sheet} {table.start}:{table.end}</option>)}
      </select></label>
      <button type="button" disabled={busy || lhs?.sheet === rhs?.sheet} onClick={() => void loadCandidates(0)}>연결 후보 보기</button>
    </>}
    {page && <>
      <p>후보 {page.total}쌍 · 이번 조회에 승인한 행 {Object.keys(approved).length}쌍</p>
      {!page.complete && <p>후보가 너무 많습니다. 원본에서 중복 식별자를 정리해 주세요.</p>}
      <ul>{page.candidates.map(item => {
        const key = `${item.left.row}:${item.right.row}`;
        return <li key={key}><label><input type="checkbox" disabled={busy || item.ambiguous || !page.complete}
          checked={Boolean(approved[key])} onChange={event => {
            const checked = event.target.checked;
            setResult(undefined);
            setApproved(old => {
              const next = {...old};
              if (checked) next[key] = {left_row: item.left.row, right_row: item.right.row};
              else delete next[key];
              return next;
            });
          }}/> {label(item.left)} ↔ {label(item.right)}
          {item.ambiguous && (item.conflict ? " · 이름 충돌" : " · 식별자 중복")}
        </label></li>;
      })}</ul>
      <button type="button" disabled={busy || !page.offset} onClick={() => void loadCandidates(page.offset - 100)}>이전 후보</button>
      <button type="button" disabled={busy || page.offset + 100 >= page.total} onClick={() => void loadCandidates(page.offset + 100)}>다음 후보</button>
      <label>집계 <select value={operation} onChange={event => {setOperation(event.target.value); setResult(undefined);}}>
        {Object.entries(operations).map(([key, label]) => <option value={key} key={key}>{label}</option>)}
      </select></label>
      {operation !== "count" && <>
        <label>집계할 쪽 <select value={side} onChange={event => {
          setSide(event.target.value === "left" ? "left" : "right"); setColumn(""); setResult(undefined);
        }}><option value="left">왼쪽 표</option><option value="right">오른쪽 표</option></select></label>
        <label>열 <select value={column} onChange={event => {setColumn(event.target.value); setResult(undefined);}}>
          <option value="">열 선택</option>
          {Object.entries((side === "left" ? lhs : rhs)?.columns ?? {}).map(([key, title]) =>
            <option value={key} key={key}>{key} {title}</option>)}
        </select></label>
      </>}
      <button type="button" disabled={busy || !Object.keys(approved).length || (operation !== "count" && !column)}
        onClick={() => void run()}>선택한 연결로 조회</button>
    </>}
    {result && <div role="status">
      <p>{operations[operation]} · {operation === "count" ? result.result.matched_rows : result.result.value ?? "숫자 없음"}</p>
      <p>결합 {result.join.joined_rows}행 · 충돌 또는 중복 후보 {result.join.ambiguous_pairs}쌍 · 미승인 후보 {result.join.unapproved_pairs}쌍</p>
      <p>미결합 행 · 왼쪽 {result.join.unmatched_left_rows}개, 오른쪽 {result.join.unmatched_right_rows}개{!result.complete && " · 결과 일부 확인 필요"}</p>
    </div>}
    {error && <p role="alert">{error}</p>}
  </details>;
}
