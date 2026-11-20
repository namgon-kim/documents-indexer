import type { CSSProperties, ReactNode } from "react";

export type ProcessState = {
  status: string; stage: string; mode?: string;
  progress?: {unit?: string; completed: number; total: number | null; corpus_completed?: number; corpus_total?: number} | null;
  coverage?: {missing_pages: number[]; warnings?: string[]; attempts?: {mode: string; pages: number[] | null}[]};
  existing_index_retained?: boolean;
  pending_review?: boolean;
  error?: {message: string};
};
type StepState = "upcoming" | "waiting" | "active" | "done" | "reused" | "warn" | "failed" | "skipped";
type Tone = "ok" | "done" | "warn" | "danger" | "neutral";
type View = {steps: {state: StepState; detail: string}[]; percent: number; tone: Tone; running: boolean; compact: boolean;
  label: string; detail: string; note: string; noteTone?: "warn" | "danger"};

const steps = [
  {label: "문서 입력", stages: ["queued"]},
  {label: "문서 읽기", stages: ["parsing"]},
  {label: "누락 확인", stages: ["quality", "retrying"]},
  {label: "내용 정리", stages: ["representing"]},
  {label: "임베딩", stages: ["embedding"]},
  {label: "검색 준비", stages: ["indexing"]},
];
// 전체 진행률에서 stage가 차지하는 구간. 진행값이 오는 stage만 구간 안을 채운다.
const ranges: Record<string, [number, number]> = {queued: [0, 0], parsing: [4, 48], quality: [48, 52], retrying: [52, 60],
  representing: [60, 66], embedding: [66, 94], indexing: [94, 99]};
const stageText: Record<string, string> = {queued: "대기 중", parsing: "문서 읽는 중", quality: "빠진 페이지 확인 중", retrying: "빠진 페이지 다시 읽는 중",
  representing: "검색할 내용 정리 중", embedding: "임베딩 생성 중", indexing: "검색 준비 중"};
// 청킹부터 다시 처리하면 입력·읽기·누락 확인은 이전 작업 결과를 쓴다.
const reusedSteps = 3;

function countText(job: ProcessState) {
  const progress = job.progress;
  if (progress?.total) return progress.unit === "images"
    ? `${progress.completed.toLocaleString()}/${progress.total.toLocaleString()}개 확인${progress.completed < progress.total ? ` · ${progress.completed + 1}번째 응답 대기` : ""}`
    : `${progress.completed.toLocaleString()}/${progress.total.toLocaleString()}${progress.unit === "chunks" ? "개 (이 문서, 캐시 포함)" : "쪽"}${progress.corpus_total ? ` · 전체 색인 ${progress.corpus_completed}/${progress.corpus_total}개` : ""}`;
  if (job.stage === "retrying") {
    const retry = job.coverage?.attempts?.filter(attempt => attempt.mode !== "initial").at(-1);
    if (retry?.pages?.length) return `${retry.pages.length}쪽 다시 읽기`;
  }
  return "";
}

function percentOf(job: ProcessState) {
  const range = ranges[job.stage];
  if (!range) return 0;
  const progress = job.progress;
  const ratio = progress?.total ? Math.min(1, progress.completed / progress.total) : 0;
  return Math.floor(range[0] + (range[1] - range[0]) * ratio);
}

export function describeProgress(job: ProcessState): View {
  const rechunk = job.mode === "rechunk";
  const missing = job.coverage?.missing_pages.length ?? 0;
  const view: View = {steps: steps.map(() => ({state: "upcoming", detail: ""})), percent: 0, tone: "ok", running: false, compact: false, label: "", detail: "", note: ""};
  const done = (index: number) => { view.steps[index] = rechunk && index < reusedSteps ? {state: "reused", detail: "재사용"} : {state: "done", detail: "완료"}; };

  if (job.status === "completed") {
    steps.forEach((_, index) => done(index));
    return {...view, percent: 100, tone: "done", compact: true, label: "처리 완료", detail: rechunk ? "읽은 내용으로 다시 준비함" : `${steps.length}단계 완료`};
  }
  if (job.status === "partial") {
    steps.forEach((_, index) => done(index));
    const warningCount = job.coverage?.warnings?.length ?? 0;
    view.steps[2] = {state: job.pending_review && !missing && !warningCount ? "done" : "warn", detail: missing ? `${missing}쪽 누락` : warningCount ? "확인 필요" : job.pending_review ? "확인 완료" : "확인 필요"};
    if (job.pending_review) view.steps[4] = view.steps[5] = {state: "waiting", detail: "게시 대기"};
    return {...view, percent: job.pending_review ? ranges.embedding[0] : 100, tone: "warn", label: job.pending_review ? "게시 전 확인 필요" : "일부 처리 완료", detail: missing ? `${missing}쪽을 읽지 못함` : warningCount ? `확인 필요 ${warningCount}건` : job.pending_review ? "확인 후 게시" : "처리 범위 확인 필요",
      note: job.pending_review ? (job.existing_index_retained ? "새 결과는 검토 후 게시됩니다. 기존 검색은 유지됩니다." : "아직 검색에 게시되지 않았습니다. 확인 후 게시해 주세요.") : "확인된 내용이 검색에 반영되어 있습니다.", noteTone: "warn"};
  }
  if (job.status === "parsed") {
    for (let index = 0; index <= 3; index++) done(index);
    return {...view, percent: ranges.embedding[0], tone: "neutral", label: "파싱 완료", detail: "검색 준비 안 됨",
      note: "검색 반영을 설정하지 않아 내용 정리까지만 진행했습니다."};
  }
  let current = Math.max(0, steps.findIndex(step => step.stages.includes(job.stage)));
  if (rechunk && current < reusedSteps) current = reusedSteps;
  for (let index = 0; index < current; index++) done(index);
  if (missing && current > 2) view.steps[2] = {state: "warn", detail: `${missing}쪽 누락`};
  if (job.status === "failed") {
    view.steps[current] = {state: "failed", detail: "중단"};
    return {...view, percent: percentOf(job), tone: "danger", label: `${steps[current].label} 단계에서 중단`, detail: countText(job),
      note: job.error?.message ?? "", noteTone: "danger"};
  }
  if (job.status === "queued") {
    view.steps[current] = {state: "waiting", detail: "대기 중"};
    return {...view, percent: rechunk ? ranges.representing[0] : 0, label: "대기 중", detail: "앞선 작업이 끝나면 시작합니다"};
  }
  view.steps[current] = {state: "active", detail: countText(job) || "진행 중"};
  return {...view, percent: percentOf(job), running: true, label: job.progress?.unit === "images" ? "이미지 분석 중" : stageText[job.stage] ?? job.stage, detail: countText(job)};
}

const marks: Partial<Record<StepState, ReactNode>> = {
  done: <path d="M2.5 6.2l2.3 2.3 4.7-5"/>,
  reused: <><path d="M9.6 6.4A3.7 3.7 0 1 1 8.4 3.2"/><path d="M8.9 1.4v2.3H6.6"/></>,
  warn: <path d="M6 2.6v4.2M6 9.3v.1"/>,
  failed: <path d="M3.4 3.4l5.2 5.2M8.6 3.4l-5.2 5.2"/>,
  skipped: <path d="M3 6h6"/>,
};

const art: ReactNode[] = [
  <svg viewBox="0 0 72 56"><g><path className="pp-f" d="M26 4h15l6 6v22H26z"/><path className="pp-s" d="M41 4v6h6"/><path className="pp-t" d="M30 16h12M30 21h12M30 26h8"/></g>
    <path className="pp-f" d="M12 31h12l4 7h16l4-7h12v14a4 4 0 0 1-4 4H16a4 4 0 0 1-4-4z"/></svg>,
  <svg viewBox="0 0 72 56"><rect className="pp-f" x="20" y="4" width="32" height="46" rx="3"/>
    <g><path className="pp-s pp-ln" d="M26 13h20"/><path className="pp-s pp-ln" d="M26 19h15"/><path className="pp-s pp-ln" d="M26 25h20"/>
      <path className="pp-s pp-ln" d="M26 31h12"/><path className="pp-s pp-ln" d="M26 37h18"/><path className="pp-s pp-ln" d="M26 43h9"/></g>
    <g className="pp-beam"><rect className="pp-beam-glow" x="17" y="3" width="38" height="7"/><rect className="pp-beam-bar" x="15" y="8.5" width="42" height="2.5" rx="1.25"/></g></svg>,
  <svg viewBox="0 0 72 56"><rect className="pp-f" x="10" y="6" width="14" height="18" rx="2"/><rect className="pp-f" x="28" y="6" width="14" height="18" rx="2"/>
    <rect className="pp-f" x="46" y="6" width="14" height="18" rx="2"/><rect className="pp-f" x="10" y="30" width="14" height="18" rx="2"/>
    <rect className="pp-f pp-miss" x="28" y="30" width="14" height="18" rx="2"/><rect className="pp-f" x="46" y="30" width="14" height="18" rx="2"/>
    <path className="pp-t" d="M13 12h8M31 12h8M49 12h8M13 36h8M49 36h8"/>
    <g className="pp-lens"><circle className="pp-lens-glass" r="6.5"/><path className="pp-s" style={{strokeWidth: 2.6}} d="M4.8 4.8l5.4 5.4"/></g></svg>,
  <svg viewBox="0 0 72 56"><rect className="pp-f" x="5" y="5" width="27" height="44" rx="3"/>
    <rect className="pp-solid pp-src" x="9" y="10" width="14" height="4" rx="1"/><path className="pp-t pp-src pp-src2" d="M9 19.5h19M9 24.5h15"/>
    <g className="pp-src pp-src3"><rect className="pp-s" x="9" y="30" width="19" height="13" rx="1"/><path className="pp-t" d="M9 36.5h19M15.5 30v13M21.8 30v13"/></g>
    <g className="pp-chunk"><rect className="pp-f" x="39" y="6" width="28" height="11" rx="2"/><rect className="pp-solid" x="42.5" y="10" width="10" height="3" rx="1"/></g>
    <g className="pp-chunk pp-k2"><rect className="pp-f" x="39" y="21" width="28" height="11" rx="2"/><path className="pp-t" d="M42.5 25h20M42.5 28.5h13"/></g>
    <g className="pp-chunk pp-k3"><rect className="pp-f" x="39" y="36" width="28" height="11" rx="2"/><path className="pp-t" d="M42.5 41.5h21M50 38.5v6M57 38.5v6"/></g></svg>,
  <svg viewBox="0 0 72 56"><rect className="pp-f" x="5" y="12" width="24" height="30" rx="3"/><path className="pp-t" d="M9.5 19h15M9.5 24h15M9.5 29h10M9.5 34h13"/>
    <path className="pp-s pp-arrow" d="M33 27h7M37 23.5l3.5 3.5-3.5 3.5"/>
    <rect className="pp-solid pp-vec" x="45" y="24" width="4.5" height="20" rx="1.2"/><rect className="pp-solid pp-vec pp-v2" x="51.5" y="14" width="4.5" height="30" rx="1.2"/>
    <rect className="pp-solid pp-vec pp-v3" x="58" y="29" width="4.5" height="15" rx="1.2"/><rect className="pp-solid pp-vec pp-v4" x="64.5" y="20" width="4.5" height="24" rx="1.2"/>
    <path className="pp-t" d="M43 47.5h28"/></svg>,
  <svg viewBox="0 0 72 56"><rect className="pp-solid pp-drop" x="27" y="2" width="6" height="4" rx="1"/><rect className="pp-solid pp-drop pp-d2" x="36" y="0" width="6" height="4" rx="1"/>
    <rect className="pp-solid pp-drop pp-d3" x="31.5" y="3" width="6" height="4" rx="1"/>
    <path className="pp-f" d="M19 26v16c0 3 7.6 5.5 17 5.5s17-2.5 17-5.5V26"/><path className="pp-t pp-band" d="M19 34c0 3 7.6 5.5 17 5.5s17-2.5 17-5.5"/>
    <ellipse className="pp-f" cx="36" cy="26" rx="17" ry="5.5"/></svg>,
];

export function ProcessProgress({job, action}: {job: ProcessState; action?: ReactNode}) {
  const view = describeProgress(job);
  const valueText = `${view.label}${view.detail ? `, ${view.detail}` : ""}, ${view.percent}%`;
  if (view.compact) return <div className="pp is-compact" data-tone={view.tone}>
    <span className="pp-dots" aria-hidden="true">{view.steps.map((step, index) => <i key={index} className={step.state === "reused" ? "is-reused" : undefined}/>)}</span>
    <span className="pp-stage" role="status">{view.label}</span><span className="pp-detail">{view.detail}</span><span className="pp-pct">{view.percent}%</span>
  </div>;
  const reached = view.steps.reduce((last, step, index) => step.state === "upcoming" || step.state === "skipped" ? last : index, 0);
  return <div className={"pp" + (view.running ? " is-running" : "")} data-tone={view.tone}>
    <div className="pp-track"><span className="pp-line" aria-hidden="true" style={{"--f": reached / (steps.length - 1)} as CSSProperties}/>
    <ol className="pp-steps" aria-label="처리 단계">
      {view.steps.map((step, index) => <li key={index} className={"pp-step is-" + step.state} data-stage={step.state === "active" ? job.stage : undefined}
        aria-current={step.state === "active" ? "step" : undefined}>
        <div className="pp-art" aria-hidden="true">{art[index]}
          {marks[step.state] && <span className="pp-mark"><svg viewBox="0 0 12 12">{marks[step.state]}</svg></span>}</div>
        <span className="pp-label">{steps[index].label}</span><span className="pp-state">{step.detail}</span>
      </li>)}
    </ol></div>
    <div className="pp-bar">
      <div className="pp-head"><span className="pp-stage" role="status">{view.label}</span><span className="pp-detail">{view.detail}</span><span className="pp-pct">{view.percent}%</span></div>
      <div className="pp-meter" role="progressbar" aria-label="처리 진행률" aria-valuemin={0} aria-valuemax={100} aria-valuenow={view.percent} aria-valuetext={valueText}>
        <div className="pp-fill" style={{width: `${view.percent}%`}}/>
      </div>
    </div>
    {(view.note || action) && <div className="pp-foot">
      {view.note ? <p className={"pp-note" + (view.noteTone ? " is-" + view.noteTone : "")}>{view.note}</p> : <span/>}
      {action}
    </div>}
  </div>;
}
