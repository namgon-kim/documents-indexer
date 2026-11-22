import { useEffect, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import type { Hit } from "./api";
import type { Session } from "./api";
import { VisualAssets } from "./VisualAssets";
import { TableRelations } from "./TableRelations";

export type ChatMessage = {role: "user" | "assistant"; content: string; sources?: Hit[]; index_version?: string; coverage?: {full_scope_requested: boolean; scope_complete: boolean | null; partial_element_count: number}; page_scope?: {requested_pages: number[]; pages_without_chunks: number[]; all_page_chunks_selected: boolean}; search_counts?: {evidence_limited: boolean; evidence_omitted_count: number; context_omitted_count: number; evidence_truncated_count: number}};
type Props = {
  session: Session;
  messages: ChatMessage[]; searching: boolean; query: string; canSend: boolean;
  onQueryChange: (value: string) => void; onSubmit: (event: FormEvent) => void; onClear: () => void;
  model: string; models: string[]; reasoning: string; connected: boolean; scopeLabel: string; scopeFields: ReactNode;
  onModelChange: (value: string) => void; onReasoningChange: (value: string) => void;
  locationLabel: (hit: Hit) => string; expanded?: {id: string; text: string}; onEvidence: (hit: Hit) => void; onContext: (hit: Hit) => void;
};

const suggestions = ["이 문서의 핵심 내용을 요약해 줘", "주요 일정과 기한을 정리해 줘", "표에 있는 수치를 비교해 줘"];
const documentIcon = <svg viewBox="0 0 24 24"><rect x="4" y="2.5" width="16" height="19" rx="2.5"/><path d="M8 8h8M8 12h8M8 16h5"/></svg>;

// 답변 모델은 근거 번호를 [1] 형식으로 붙인다. 근거가 있는 번호만 버튼으로 바꾼다.
function withCitations(text: string, count: number, onCite: (number: number) => void) {
  return text.split(/(\[\d+\])/).map((part, index) => {
    const number = /^\[(\d+)\]$/.exec(part)?.[1];
    if (!number || Number(number) < 1 || Number(number) > count) return part;
    return <button type="button" className="cite" key={index} aria-label={`근거 ${number} 보기`} onClick={() => onCite(Number(number))}>{number}</button>;
  });
}

export function DocumentChat(props: Props) {
  const {messages, searching, query, canSend, onQueryChange, onSubmit, onClear, model, models, reasoning, connected, scopeLabel, scopeFields, onModelChange, onReasoningChange,
    locationLabel, expanded, onEvidence, onContext} = props;
  const thread = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const scope = useRef<HTMLDetailsElement>(null);
  const [open, setOpen] = useState<number[]>([]);
  const [flash, setFlash] = useState("");

  useEffect(() => { thread.current?.scrollTo({top: thread.current.scrollHeight}); }, [messages.length, searching]);
  useEffect(() => {
    const field = input.current;
    if (!field) return;
    field.style.height = "auto";
    field.style.height = `${Math.min(field.scrollHeight, 160)}px`;
  }, [query]);
  useEffect(() => {
    const close = (event: MouseEvent) => { if (scope.current?.open && !scope.current.contains(event.target as Node)) scope.current.open = false; };
    document.addEventListener("click", close);
    return () => document.removeEventListener("click", close);
  }, []);

  function toggle(index: number, value = !open.includes(index)) {
    setOpen(current => value ? [...new Set([...current, index])] : current.filter(item => item !== index));
  }
  function cite(index: number, number: number) {
    toggle(index, true);
    const id = `chat-source-${index}-${number}`;
    setFlash(id);
    window.setTimeout(() => document.getElementById(id)?.scrollIntoView({block: "nearest"}), 0);
    window.setTimeout(() => setFlash(current => current === id ? "" : current), 1200);
  }

  return <section className="chat" aria-labelledby="chat-title">
    <div className="chat-top">
      <span className="chat-bot" aria-hidden="true">{documentIcon}</span>
      <div className="chat-title"><h2 id="chat-title">문서 대화</h2>
        <div className="chat-meta chat-model-controls">
          <label><span>답변 모델</span><select aria-label="답변 모델" value={model} disabled={!connected || !models.length || searching} onChange={event => onModelChange(event.target.value)}>
            {!models.length && <option value="">모델 확인 중</option>}{models.map(value => <option key={value} value={value}>{value}</option>)}
          </select></label>
          <label><span>추론 수준</span><select aria-label="추론 수준" value={reasoning} disabled={searching} onChange={event => onReasoningChange(event.target.value)}>
            <option value="">모델 기본값</option><option value="none">없음</option><option value="minimal">최소</option><option value="low">낮음</option>
            <option value="medium">보통</option><option value="high">높음</option><option value="xhigh">매우 높음</option><option value="max">최대</option>
          </select></label>
        </div></div>
      <button type="button" className="icon-button" title="대화 지우기" aria-label="대화 지우기" disabled={searching || !messages.length} onClick={() => { setOpen([]); onClear(); }}>
        <svg viewBox="0 0 24 24"><path d="M4 7h16M9 7V4.5h6V7M6.5 7l1 13h9l1-13M10 11v5M14 11v5"/></svg></button>
      <a className="icon-button" href="settings" title="설정" aria-label="설정">
        <svg viewBox="0 0 24 24"><path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/></svg></a>
    </div>

    <div className="chat-thread" ref={thread} aria-live="polite">
      {!messages.length && !searching && <div className="chat-empty">
        <span className="chat-bot" aria-hidden="true">{documentIcon}</span>
        <strong>문서에 대해 질문해 보세요</strong><p>답변과 함께 검색된 근거를 확인할 수 있습니다.</p>
        <div className="chat-suggest">{suggestions.map(text => <button type="button" key={text} onClick={() => { onQueryChange(text); input.current?.focus(); }}>{text}</button>)}</div>
      </div>}
      {messages.map((message, index) => message.role === "user"
        ? <div className="chat-row user" key={index}><div className="chat-stack"><p className="chat-bubble">{message.content}</p></div></div>
        : <div className="chat-row assistant" key={index}><span className="chat-bot" aria-hidden="true">{documentIcon}</span><div className="chat-stack">
          <p className="chat-bubble">{withCitations(message.content, message.sources?.length ?? 0, number => cite(index, number))}</p>
          {message.coverage?.full_scope_requested && !message.coverage.scope_complete && <p className="hint" role="status">검색된 일부 근거를 바탕으로 답변했습니다. 질문 대상의 전체 범위와 원문 추출의 완전성은 확인되지 않았습니다. Excel에서는 확인된 표 전체 조회도 사용할 수 있습니다.</p>}
          {message.page_scope && <p className="hint" role="status">PDF 물리쪽 {message.page_scope.requested_pages.join(", ")} · 페이지 내 BM25 조회 · 인쇄 쪽 번호와 다를 수 있으며 원문 추출 완전성은 미확인입니다.{!message.page_scope.all_page_chunks_selected ? " 일부 청크만 답변 근거로 선택됐습니다." : ""}{message.page_scope.pages_without_chunks.length ? ` 검색 가능한 청크 없음: ${message.page_scope.pages_without_chunks.join(", ")}쪽 (원본·추출 상태 확인 필요).` : ""}</p>}
          {message.search_counts?.evidence_limited && <p className="hint" role="status">답변 근거 예산으로 검색 근거 {message.search_counts.evidence_omitted_count}개와 문맥 {message.search_counts.context_omitted_count}개가 제외됐고, 근거 {message.search_counts.evidence_truncated_count}개는 일부만 사용됐습니다.</p>}
          {!!message.sources?.length && <div className={"chat-sources" + (open.includes(index) ? " is-open" : "")}>
            <div className="chat-source-strip">
              <button type="button" className="chat-source-toggle" aria-expanded={open.includes(index)} onClick={() => toggle(index)}>
                근거 {message.sources.length}개<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M3 4.5l3 3 3-3"/></svg></button>
              {message.sources.map((hit, number) => <span className="chat-source-chip" key={hit.unit_id}><b>{number + 1}</b><span>{hit.filename.normalize("NFC")} · {locationLabel(hit)}</span></span>)}
            </div>
            {open.includes(index) && <div className="chat-source-list">
              <p className="hint">검색된 후보이며 답변의 인용이 정확한지는 원문에서 확인하세요. · 검색 인덱스 {message.index_version?.slice(0, 8)}</p>
              {message.sources.map((hit, number) => <article key={hit.unit_id} id={`chat-source-${index}-${number + 1}`}
                className={"chat-source" + (flash === `chat-source-${index}-${number + 1}` ? " is-flash" : "")}>
                <header><b>[{number + 1}]</b><strong>{hit.filename.normalize("NFC")}</strong></header>
                <p className="chat-source-location">{locationLabel(hit)}{hit.quality === "unverified" ? " · 구조 확인 필요" : ""}{hit.heading_path_origin === "inferred" ? " · 제목 계층 추정" : ""}</p>
                <p className="chat-source-text">{hit.search_text}</p>
          <TableRelations hit={hit}/>
                {!!hit.related_images?.length && <VisualAssets items={hit.related_images} session={props.session}/>}
                {(hit.context_before || hit.context_after) && <details><summary>앞뒤 문맥 보기</summary>
                  {hit.context_before && <p className="result-text">앞 · {hit.context_before}</p>}{hit.context_after && <p className="result-text">뒤 · {hit.context_after}</p>}</details>}
                {!!hit.evidence_urls.length && <button type="button" className="link-button" onClick={() => onEvidence(hit)}>{expanded?.id === hit.unit_id ? "파싱 원문 닫기" : "파싱 원문 보기"}</button>}
                {!!hit.context_urls?.length && <button type="button" className="link-button" onClick={() => onContext(hit)}>문맥 원문 보기</button>}
                {expanded?.id === hit.unit_id + ":context" && <p className="evidence result-text">문맥 · {expanded.text}</p>}
                {expanded?.id === hit.unit_id && <p className="evidence result-text">{expanded.text}</p>}
              </article>)}
            </div>}
          </div>}
        </div></div>)}
      {searching && <div className="chat-row assistant"><span className="chat-bot" aria-hidden="true">{documentIcon}</span><div className="chat-stack">
        <p className="chat-bubble"><span className="chat-typing" role="status"><span className="chat-dots" aria-hidden="true"><i/><i/><i/></span>문서를 찾고 답변을 쓰는 중</span></p>
      </div></div>}
    </div>

    <form className="chat-composer" onSubmit={onSubmit}>
      <details className="chat-scope" ref={scope}>
        <summary><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h16M7 12h10M10 18h4"/></svg>검색 범위 · <b>{scopeLabel}</b></summary>
        <div className="chat-scope-panel">{scopeFields}</div>
      </details>
      <div className="chat-box">
        <label htmlFor="chat-query" className="sr-only">질문</label>
        <textarea id="chat-query" ref={input} rows={1} value={query} placeholder="문서에 대해 질문하세요" maxLength={2000} onChange={event => onQueryChange(event.target.value)}
          onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }}/>
        <button className="chat-send" aria-label={searching ? "답변 중" : "보내기"} disabled={!canSend}>
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5M6 11l6-6 6 6"/></svg></button>
      </div>
      <div className="chat-compose-foot"><span>검색된 문서 내용과 대화가 답변 모델로 전송됩니다 · 대화 기록은 현재 브라우저 탭에 보관됩니다</span>
        <span><kbd>Enter</kbd> 보내기 · <kbd>Shift</kbd>+<kbd>Enter</kbd> 줄바꿈</span></div>
    </form>
  </section>;
}
