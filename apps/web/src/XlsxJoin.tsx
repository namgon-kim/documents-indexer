import { useState } from "react";
import { api } from "./api";
import type { Session } from "./api";

type Table = {sheet: string; start: string; end: string; title: string | null; columns: Record<string, string | null>};
type Point = {sheet: string; row: number; cells: string[]; values: string[]};
type Candidate = {left: Point; right: Point; ambiguous?: boolean; conflict?: boolean};
type JoinKey = {left: string; right: string; type: string};
const emptyKey: JoinKey = {left: "", right: "", type: "text"};
const keyTypes: Record<string, string> = {text: "텍스트", number: "숫자", date: "날짜", datetime: "날짜와 시간", boolean: "논리값"};
const cardinalities: Record<string, string> = {one_to_one: "일대일", one_to_many: "일대다 (왼쪽 키 고유)", many_to_one: "다대일 (오른쪽 키 고유)"};
type Joined = {joined_rows: number; ambiguous_pairs: number; unapproved_pairs: number; unmatched_left_rows: number;
  unmatched_right_rows: number; left_key_issues?: Record<string, number>; right_key_issues?: Record<string, number>};
type Candidates = {candidates: Candidate[]; complete?: boolean; total: number; offset: number; join?: Joined};
type Result = {
  complete: boolean; issues: Record<string, number>;
  result: {matched_rows: number; value?: number | string | null; numeric_values?: number; aggregate_unit?: string;
    aggregate_unit_required?: boolean; pair_based_value?: number | string | null};
  join: Joined;
};
const operations: Record<string, string> = {count: "행 개수", sum: "합계", average: "평균", min: "최솟값", max: "최댓값"};
const units: Record<string, string> = {
  auto: "자동 · 중복 반영 시 기준 선택 요청", pairs: "연결된 행 쌍마다", left_rows: "왼쪽 표 행마다 한 번", right_rows: "오른쪽 표 행마다 한 번",
};

export function XlsxJoin({session, documentId, version, profile}: {
  session: Session; documentId: string; version: string; profile: string;
}) {
  const base = `/v1/ingest/${documentId}`;
  const [tables, setTables] = useState<Table[]>();
  const [mapped, setMapped] = useState(true);
  const [keys, setKeys] = useState<JoinKey[]>([{...emptyKey}]);
  const [cardinality, setCardinality] = useState("many_to_one");
  const [left, setLeft] = useState(0);
  const [right, setRight] = useState(0);
  const [page, setPage] = useState<Candidates>();
  const [approved, setApproved] = useState<Record<string, {left_row: number; right_row: number}>>({});
  const [operation, setOperation] = useState("count");
  const [column, setColumn] = useState("");
  const [side, setSide] = useState<"left" | "right">("right");
  const [unit, setUnit] = useState("auto");
  const [result, setResult] = useState<Result>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const lhs = tables?.[left];
  const rhs = tables?.[right];
  const query = {index_version: version, ...(profile ? {profile_id: profile} : {})};

  function resetPreview() {
    setPage(undefined); setApproved({}); setResult(undefined); setError("");
  }

  function changeTable() {
    resetPreview();
    setColumn("");
    setKeys([{...emptyKey}]);
  }

  function changeKey(index: number, values: Partial<JoinKey>) {
    setKeys(current => current.map((key, i) => i === index ? {...key, ...values} : key));
    resetPreview();
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
      if (mapped) {
        setPage(await api<Candidates>("/v1/xlsx-join-preview", session, {document_id: documentId, ...query,
          sheet: lhs.sheet, start: lhs.start, offset, join: {sheet: rhs.sheet, start: rhs.start, keys, cardinality}}));
        return;
      }
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
        sheet: lhs.sheet, start: lhs.start, operation, aggregate_unit: unit,
        ...(operation === "count" ? {} : {column, column_side: side}),
        join: {sheet: rhs.sheet, start: rhs.start, ...(mapped ? {keys, cardinality}
          : {approved_pairs: Object.values(approved)})}}));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "연결한 표를 조회하지 못했습니다."); }
    finally { setBusy(false); }
  }

  function label(point: Point) {
    return `${point.sheet} ${point.cells.join(", ")} · ${point.values.join(" / ")}`;
  }

  function skipped(issues?: Record<string, number>) {
    return Object.values(issues ?? {}).reduce((total, count) => total + count, 0);
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
      <label>연결 방식 <select value={mapped ? "mapped" : "approved"} disabled={busy}
        onChange={event => {setMapped(event.target.value === "mapped"); resetPreview();}}>
        <option value="mapped">열 매핑</option><option value="approved">ID·이름 후보를 선택</option>
      </select></label>
      {mapped && <fieldset disabled={busy}>
        <legend>연결할 열과 키 타입</legend>
        <label>관계 <select value={cardinality} onChange={event => {setCardinality(event.target.value); resetPreview();}}>
          {Object.entries(cardinalities).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
        </select></label>
        {keys.map((key, index) => <div className="xlsx-grid-navigation" key={index}>
          <label>왼쪽 키 <select value={key.left} onChange={event => changeKey(index, {left: event.target.value})}>
            <option value="">열 선택</option>
            {Object.entries(lhs?.columns ?? {}).map(([letter, label]) => <option value={letter} key={letter}>{letter} {label}</option>)}
          </select></label>
          <label>오른쪽 키 <select value={key.right} onChange={event => changeKey(index, {right: event.target.value})}>
            <option value="">열 선택</option>
            {Object.entries(rhs?.columns ?? {}).map(([letter, label]) => <option value={letter} key={letter}>{letter} {label}</option>)}
          </select></label>
          <label>타입 <select value={key.type} onChange={event => changeKey(index, {type: event.target.value})}>
            {Object.entries(keyTypes).map(([code, label]) => <option value={code} key={code}>{label}</option>)}
          </select></label>
          {keys.length > 1 && <button type="button" onClick={() => {
            setKeys(current => current.filter((_, i) => i !== index)); resetPreview();
          }}>키 제거</button>}
        </div>)}
        {keys.length < 4 && <button type="button" onClick={() => {setKeys(current => [...current, {...emptyKey}]); resetPreview();}}>복합 키 추가</button>}
      </fieldset>}
      <button type="button" disabled={busy || lhs?.sheet === rhs?.sheet || (mapped && keys.some(key => !key.left || !key.right))}
        onClick={() => void loadCandidates(0)}>{mapped ? "열 매핑 미리보기" : "연결 후보 보기"}</button>
    </>}
    {page && <>
      {mapped ? <p>키가 일치하는 행 {page.total}쌍 · 키 확인 불가 왼쪽 {skipped(page.join?.left_key_issues)}행, 오른쪽 {skipped(page.join?.right_key_issues)}행</p>
        : <p>후보 {page.total}쌍 · 이번 조회에 승인한 행 {Object.keys(approved).length}쌍</p>}
      {page.complete === false && <p>후보가 너무 많습니다. 원본에서 중복 식별자를 정리해 주세요.</p>}
      <ul>{page.candidates.map(item => {
        const key = `${item.left.row}:${item.right.row}`;
        if (mapped) return <li key={key}>{label(item.left)} ↔ {label(item.right)}</li>;
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
      <label>집계 <select value={operation} disabled={busy} onChange={event => {setOperation(event.target.value); setResult(undefined);}}>
        {Object.entries(operations).map(([key, label]) => <option value={key} key={key}>{label}</option>)}
      </select></label>
      <label>집계 기준 <select value={unit} disabled={busy} onChange={event => {setUnit(event.target.value); setResult(undefined);}}>
        {Object.entries(units).map(([key, label]) => <option value={key} key={key}>{label}</option>)}
      </select></label>
      {operation !== "count" && <>
        <label>집계할 쪽 <select value={side} disabled={busy} onChange={event => {
          setSide(event.target.value === "left" ? "left" : "right"); setColumn(""); setResult(undefined);
        }}><option value="left">왼쪽 표</option><option value="right">오른쪽 표</option></select></label>
        <label>열 <select value={column} disabled={busy} onChange={event => {setColumn(event.target.value); setResult(undefined);}}>
          <option value="">열 선택</option>
          {Object.entries((side === "left" ? lhs : rhs)?.columns ?? {}).map(([key, title]) =>
            <option value={key} key={key}>{key} {title}</option>)}
        </select></label>
      </>}
      <button type="button" disabled={busy || (!mapped && !Object.keys(approved).length) || (operation !== "count" && !column)}
        onClick={() => void run()}>{mapped ? "열 매핑으로 조회" : "선택한 연결로 조회"}</button>
    </>}
    {result && <div role="status">
      {result.result.aggregate_unit_required ? <>
        <p role="alert">{operations[operation]} · 집계 기준 선택 필요</p>
        <p>같은 원본 값 {result.issues.duplicated_join_values}개가 여러 연결 쌍으로 반복 반영되어 결과를 확정하지 않았습니다.
          집계 기준에서 행 쌍마다 또는 한쪽 표의 행마다를 선택해 다시 조회해 주세요.</p>
        <p>참고 · 연결된 행 쌍마다 집계한 값 {result.result.pair_based_value ?? "숫자 없음"} (확정 결과 아님)</p>
      </> : <p>{operations[operation]} · {operation === "count" ? result.result.matched_rows : result.result.value ?? "숫자 없음"}
        {result.result.aggregate_unit && ` · 기준 ${units[result.result.aggregate_unit] ?? result.result.aggregate_unit}`}</p>}
      <p>결합 {result.join.joined_rows}행{mapped
        ? ` · 키 확인 불가 왼쪽 ${skipped(result.join.left_key_issues)}행, 오른쪽 ${skipped(result.join.right_key_issues)}행`
        : ` · 충돌 또는 중복 후보 ${result.join.ambiguous_pairs}쌍 · 미승인 후보 ${result.join.unapproved_pairs}쌍`}</p>
      <p>미결합 행 · 왼쪽 {result.join.unmatched_left_rows}개, 오른쪽 {result.join.unmatched_right_rows}개{!result.complete && " · 결과 일부 확인 필요"}</p>
    </div>}
    {error && <p role="alert">{error}</p>}
  </details>;
}
