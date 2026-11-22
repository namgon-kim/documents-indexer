import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { createRoot } from "react-dom/client";
import { api, ApiError, route } from "./api";
import type { DocumentItem, EmbeddingProfile, Hit, Job, Options, Session } from "./api";
import { ServerSettings } from "./ServerSettings";
import { IngestionDocuments } from "./IngestionDocuments";
import { DocumentChat } from "./DocumentChat";
import { VisualAssets } from "./VisualAssets";
import type { ChatMessage } from "./DocumentChat";
import indukLogo from "./assets/induk-logo.svg";
import "./style.css";

const embed = route === "embed";
const chatPage = route === "chat";
const searchPage = route === "search";
const settingsPage = route === "settings";
const framed = window.parent !== window;
function locationLabel(hit: Hit) {
  function label(role: "evidence" | "context") {
    const refs = hit.source_refs.filter(ref => (ref.role || "evidence") === role);
    const pages = [...new Set(refs.flatMap(ref => ref.page === undefined ? [] : [ref.page]))];
    const locations = refs.flatMap(ref => ref.kind === "xlsx_range" ? [`시트 ${ref.sheet} · ${ref.start}:${ref.end}`]
      : ref.kind === "xlsx_cell" ? [`시트 ${ref.sheet} · ${ref.cell}${ref.status === "unverified" ? " (위치 확인 필요)" : ""}`]
      : ref.kind === "hwpx_section" ? [`HWPX 구역 ${ref.section}`]
      : ref.kind === "docx_block" ? [`DOCX 본문 ${ref.block}`]
      : ref.kind === "hwp_paragraph" ? [`HWP 구역 ${typeof ref.section === "number" ? ref.section + 1 : ref.section} · ${ref.paragraph === undefined ? "문단 위치 미확인" : `추출 문단 ${ref.paragraph + 1}`}`]
      : ref.kind === "pptx_slide" ? [`슬라이드 ${ref.slide} (파서 표시)`] : []);
    return [...(pages.length ? [`${refs[0]?.kind === "pdf_page" ? "페이지" : "파서 기준 페이지"} ${pages.join(", ")}`] : []), ...locations].join(" · ");
  }
  return [label("evidence") && `근거 ${label("evidence")}`, label("context") && `문맥 ${label("context")}`].filter(Boolean).join(" · ") || "위치 미확인";

}

function App() {
  const [session, setSession] = useState<Session>();
  const [authStatus, setAuthStatus] = useState("확인 중");
  const [accessKey, setAccessKey] = useState("");
  const [error, setError] = useState("");
  const [options, setOptions] = useState<Options>();
  const [documents, setDocuments] = useState<DocumentItem[]>([]);
  const [query, setQuery] = useState("");
  const [xlsxFullTable, setXlsxFullTable] = useState(false);
  const [resultLimit, setResultLimit] = useState("5");
  const [searchMethod, setSearchMethod] = useState<"hybrid" | "bm25" | "dense">("hybrid");
  const [rerank, setRerank] = useState(false);
  const [embeddingProfiles, setEmbeddingProfiles] = useState<EmbeddingProfile[]>([]);
  const [embeddingProfile, setEmbeddingProfile] = useState("");
  const embeddingProfileRef = useRef("");
  const [reasoningEffort, setReasoningEffort] = useState(() => localStorage.getItem("indexer.reasoning") || "");
  const [chatModel, setChatModel] = useState(() => localStorage.getItem("indexer.model") || "");
  const [chatOptions, setChatOptions] = useState<{models: string[]; default_model: string; configured: boolean}>();
  const [scope, setScope] = useState(() => new URLSearchParams(location.search).get("document") || "");
  const [versions, setVersions] = useState<{ index_version: string; created_at: string; status: string; dense_available?: boolean; unavailable_reason?: string | null }[]>([]);
  const [version, setVersion] = useState("");
  const selectedVersion = version ? versions.find(item => item.index_version === version) : versions.find(item => item.status === "active");
  const [messages, setMessages] = useState<ChatMessage[]>(() => {
    try { const saved = JSON.parse(sessionStorage.getItem("indexer.chat") || "[]"); return Array.isArray(saved) ? saved : []; }
    catch { return []; }
  });
  const [results, setResults] = useState<Hit[]>();
  const [searchedVersion, setSearchedVersion] = useState("");
  const [searching, setSearching] = useState(false);
  const [expanded, setExpanded] = useState<{ id: string; text: string }>();

  useEffect(() => {
    try { sessionStorage.setItem("indexer.chat", JSON.stringify(messages)); } catch { /* storage can be unavailable */ }
  }, [messages]);

  function showError(reason: unknown) {
    if (reason instanceof ApiError && reason.status === 401) {
      setSession(undefined);
      setAuthStatus("로그인");
      setError("접근이 만료되었습니다. 다시 인증해 주세요.");
      return;
    }
    setError(reason instanceof Error ? reason.message : "요청을 처리할 수 없습니다.");
  }

  useEffect(() => {
    let disposed = false;
    let pendingNonce = "";
    let trusted: string[] = [];
    const requestCode = () => {
      pendingNonce = crypto.randomUUID();
      for (const origin of trusted) {
        window.parent.postMessage({ type: "document-indexer:ready", nonce: pendingNonce }, origin);
      }
    };
    const receive = async (event: MessageEvent) => {
      if (event.source !== window.parent || !trusted.includes(event.origin)
          || event.data?.type !== "document-indexer:code"
          || typeof event.data.code !== "string"
          || event.data.nonce !== pendingNonce || !pendingNonce) return;
      const nonce = pendingNonce;
      pendingNonce = "";
      try {
        const grant = await api<{ access_token: string; expires_in: number }>(
          "/v1/auth/embed/exchange", undefined,
          { code: event.data.code, nonce, parent_origin: event.origin },
        );
        if (!disposed) setSession({ token: grant.access_token });
        timer = window.setTimeout(requestCode, Math.max(1, grant.expires_in - 30) * 1000);
      } catch (reason) {
        if (!disposed) showError(reason);
      }
    };
    let timer: number | undefined;
    if (embed && framed) window.addEventListener("message", receive);
    async function connect() {
      try {
        const current = await api<{ csrf_token: string }>("/v1/auth/session");
        if (!disposed) setSession({ csrf: current.csrf_token });
      } catch {
        const config = await api<{ dev_login: boolean; frame_origins: string[] }>("/v1/auth/config");
        if (disposed) return;
        if (embed && framed && config.frame_origins.length) {
          trusted = config.frame_origins;
          setAuthStatus("호스트에 연결하는 중");
          requestCode();
        } else if (config.dev_login) {
          const current = await api<{ csrf_token: string }>("/v1/auth/login", undefined, {});
          if (!disposed) setSession({ csrf: current.csrf_token });
        } else setAuthStatus("로그인");
      }
    }
    void connect().catch(showError);
    return () => {
      disposed = true;
      window.removeEventListener("message", receive);
      window.clearTimeout(timer);
    };
  }, []);

  function changeEmbeddingProfile(value: string) {
    embeddingProfileRef.current = value;
    setEmbeddingProfile(value); setScope(""); setVersion(""); setResults(undefined); setExpanded(undefined);
    localStorage.setItem("indexer.embeddingProfile", value);
  }

  async function list(current: Session, next?: string, ingestionEnabled = Boolean(options?.ingestion_enabled), profileId = embeddingProfileRef.current) {
    if (!ingestionEnabled) return;
    const suffix = profileId ? "?profile_id=" + encodeURIComponent(profileId) : "";
    const response = await api<{documents: {document_id: string; filename: string; format: string; indexed: boolean; latest_job: Job}[]}>("/v1/ingestion-documents" + suffix, current);
    if (profileId && profileId !== embeddingProfileRef.current) return;
    setDocuments(response.documents.map(item => ({...item, source_format: item.format, source_version: 1, active_result: item.indexed ? {} : null} as DocumentItem)));
    const indexes = await api<{versions: {index_version: string; created_at: string; status: string}[]}>("/v1/ingest-index-versions" + suffix, current);
    if (!profileId || profileId === embeddingProfileRef.current) setVersions(indexes.versions);
  }

  useEffect(() => {
    if (!session) return;
    void api<{models: string[]; default_model: string; configured: boolean}>("/v1/chat/options", session).then(value => {
      setChatOptions(value);
      setChatModel(current => value.models.includes(current) ? current : value.default_model);
    }).catch(showError);
    void api<Options>("/v1/processing-options", session).then(async value => {
      setOptions(value);
      if (value.search_methods && !value.search_methods.includes(searchMethod)) setSearchMethod("bm25");
      if (value.ingestion_enabled) {
        const models = await api<{default_profile_id: string; profiles: EmbeddingProfile[]}>("/v1/embedding-profiles", session);
        setEmbeddingProfiles(models.profiles);
        const stored = localStorage.getItem("indexer.embeddingProfile") || "";
        const chosen = models.profiles.some(item => item.profile_id === stored) ? stored : models.default_profile_id;
        embeddingProfileRef.current = chosen; setEmbeddingProfile(chosen);
        await list(session, undefined, true, chosen);
      }
    }).catch(showError);
  }, [session]);

  useEffect(() => {
    if (!session || !options?.ingestion_enabled || !embeddingProfile) return;
    void list(session, undefined, true, embeddingProfile).catch(showError);
    const timer = window.setInterval(() => void list(session, undefined, true, embeddingProfile).catch(showError), 5000);
    return () => window.clearInterval(timer);
  }, [session, options?.ingestion_enabled, embeddingProfile]);

  useEffect(() => {
    if (selectedVersion?.dense_available === false) setSearchMethod("bm25");
  }, [version, versions]);

  async function login(event: FormEvent) {
    event.preventDefault();
    setError("");
    try {
      const current = await api<{ csrf_token: string }>("/v1/auth/login", undefined, { access_key: accessKey });
      setSession({ csrf: current.csrf_token });
      setAccessKey("");
    } catch (reason) { showError(reason); }
  }

  const xlsxScope = documents.some(item => item.document_id === scope && item.source_format === "xlsx");

  async function search(event: FormEvent) {
    event.preventDefault();
    if (!session || !query.trim()) return;
    setSearching(true);
    setError("");
    setExpanded(undefined);
    try {
      const question = query.trim();
      const previous = messages.map(({role, content, sources}) => ({role, content: content.slice(0, 4000),
        ...(role === "assistant" && sources ? {citation_numbers: sources.map((_, index) => index + 1)} : {})})).slice(-10);
      setMessages(items => [...items, {role: "user", content: question}]);
      setQuery("");
      const response = await api<{ answer: string; sources: Hit[]; index_version: string; search_counts?: ChatMessage["search_counts"] }>("/v1/ingest-chat", session, {
        message: question, history: previous, ...(xlsxScope && xlsxFullTable ? {xlsx_full_table: true} : {}), ...(chatModel ? { model: chatModel } : {}), ...(reasoningEffort ? { reasoning_effort: reasoningEffort } : {}), ...(scope ? { document_id: scope } : {}), ...(version ? { index_version: version } : {}),
        ...(options?.ingestion_enabled ? {method: searchMethod, rerank: rerank && Boolean(options.rerank_available), profile_id: embeddingProfile} : {}),
      });
      setMessages(items => [...items, {role: "assistant", content: response.answer, sources: response.sources, index_version: response.index_version, search_counts: response.search_counts}]);
    } catch (reason) { setQuery(query); setMessages(items => items.slice(0, -1)); showError(reason); }
    finally { setSearching(false); }
  }

  async function searchDocuments(event: FormEvent) {
    event.preventDefault();
    if (!session || !query.trim()) return;
    if (!/^[0-9]+$/.test(resultLimit) || Number(resultLimit) < 1 || Number(resultLimit) > 20) {
      setError("표시 결과 수는 1~20 사이의 정수로 입력해 주세요.");
      return;
    }
    setSearching(true); setError(""); setResults(undefined); setExpanded(undefined);
    try {
      const response = await api<{results: Hit[]; index_version: string}>("/v1/retrieve", session, {
        query: query.trim(), ...(options?.ingestion_enabled ? {limit: Number(resultLimit), method: searchMethod, rerank: rerank && Boolean(options.rerank_available), profile_id: embeddingProfile} : {top_k: Number(resultLimit)}), ...(scope ? {document_id: scope} : {}),
        ...(version ? {index_version: version} : {}),
      });
      setResults(response.results); setSearchedVersion(response.index_version);
    } catch (reason) { showError(reason); }
    finally { setSearching(false); }
  }

  async function viewEvidence(hit: Hit, context = false) {
    if (!session) return;
    const id = hit.unit_id + (context ? ":context" : "");
    if (expanded?.id === id) return setExpanded(undefined);
    try {
      const portions = await Promise.all((context ? hit.context_urls || [] : hit.evidence_urls).map(url =>
        api<{ content: { text: string } }>(url, session),
      ));
      setExpanded({ id, text: portions.map(item => item.content.text).join("\n") });
    } catch (reason) { showError(reason); }
  }

  return <main className={embed ? "shell embedded" : "shell"}>
    <header className="brand">
      <a className="brand-home" href="./" aria-label="문서 마트 홈">
        <img className="university-logo" src={indukLogo} alt="인덕대학교"/>
        <span className="brand-divider" aria-hidden="true"/>
        <span className="brand-title"><strong>문서 마트</strong><small>DOCUMENT MART</small></span>
      </a>
      <nav className="navigation" aria-label="주 메뉴">
        <a href="./" aria-current={!chatPage && !searchPage && !settingsPage ? "page" : undefined}>문서 관리</a>
        <a href="search" aria-current={searchPage ? "page" : undefined}>문서 검색</a>
        <a href="chat" aria-current={chatPage ? "page" : undefined}>문서 대화</a>
        <a href="settings" aria-current={settingsPage ? "page" : undefined}>설정</a>
      </nav>
    </header>
    {session && !settingsPage && <div className="page-intro">
      <div><span className="eyebrow">{chatPage ? "DOCUMENT CHAT" : searchPage ? "KNOWLEDGE SEARCH" : "DOCUMENT WORKSPACE"}</span>
        <h1>{chatPage ? "문서와 대화하기" : searchPage ? "필요한 내용을 빠르게" : "문서 관리"}</h1>
        <p>{chatPage ? "문서를 바탕으로 질문하고, 답변의 근거까지 확인하세요." : searchPage ? "등록한 문서에서 원하는 내용과 원문 근거를 찾아보세요." : "문서를 등록하고 검색 준비 상태를 확인하세요."}</p>
      </div><div className="intro-accent" aria-hidden="true"><span/><span/><span/></div>
    </div>}
    {error && <div className="alert" role="alert">{error}<button onClick={() => setError("")} aria-label="닫기">×</button></div>}
    {!session ? <section className="card login">
      <h2>{authStatus}</h2>
      {authStatus === "로그인" && <form onSubmit={login}>
        <label htmlFor="key">서버 접근 키</label>
        <input id="key" type="password" value={accessKey} onChange={event => setAccessKey(event.target.value)} autoComplete="current-password"/>
        <button className="primary">로그인</button>
      </form>}
    </section> : <>
      {!chatPage && !searchPage && !settingsPage && options?.ingestion_enabled && <IngestionDocuments session={session} options={options} embeddingProfiles={embeddingProfiles} embeddingProfile={embeddingProfile} onEmbeddingProfileChange={changeEmbeddingProfile} onChange={() => void list(session, undefined, true).catch(showError)}/>}
      {!chatPage && !searchPage && !settingsPage && options && !options.ingestion_enabled && <section className="card"><p>문서 파서와 임베딩 연결을 설정해 주세요.</p><a href="settings">설정 열기</a></section>}
      {settingsPage && <ServerSettings session={session} chatOptions={chatOptions} chatModel={chatModel}
        reasoningEffort={reasoningEffort} onModelChange={value => {setChatModel(value); localStorage.setItem("indexer.model", value);}}
        onReasoningChange={value => {setReasoningEffort(value); localStorage.setItem("indexer.reasoning", value);}}
        onModelsSaved={async () => {
          const value = await api<{models: string[]; default_model: string; configured: boolean}>("/v1/chat/options", session);
          setChatOptions(value);
          setChatModel(current => value.models.includes(current) ? current : value.default_model);
          const processing = await api<Options>("/v1/processing-options", session);
          setOptions(processing);
          if (processing.ingestion_enabled) {
            const profiles = await api<{default_profile_id: string; profiles: EmbeddingProfile[]}>("/v1/embedding-profiles", session);
            setEmbeddingProfiles(profiles.profiles);
            const selected = profiles.profiles.some(item => item.profile_id === embeddingProfileRef.current)
              ? embeddingProfileRef.current : profiles.default_profile_id;
            embeddingProfileRef.current = selected;
            setEmbeddingProfile(selected);
          }
        }}/>}
      {searchPage && <section className="card search-card" aria-labelledby="document-search-title">
        <div className="section-heading"><div><span className="section-kicker">SEARCH</span><h2 id="document-search-title">문서 검색</h2></div><p className="hint">내용을 입력해 검색을 시작하세요.</p></div>
        <form className="search-form" onSubmit={searchDocuments}>
          <div className="search-bar"><label htmlFor="document-search-query" className="sr-only">검색어</label>
            <input id="document-search-query" type="search" value={query} placeholder="문서에서 찾을 내용을 입력하세요" onChange={event => setQuery(event.target.value)} maxLength={2000}/>
            <button className="primary" disabled={!query.trim() || searching || (options?.ingestion_enabled && !embeddingProfile)}>{searching ? "검색 중" : "검색"}</button>
          </div>
          <details className="search-filters"><summary>검색 조건 <span>모델 · 방식 · 문서 범위</span></summary><div className="search">
          {options?.ingestion_enabled && <label>임베딩 모델 <select value={embeddingProfile} disabled={!embeddingProfiles.length || searching} onChange={event => changeEmbeddingProfile(event.target.value)}>{embeddingProfiles.map(item => <option key={item.profile_id} value={item.profile_id}>{item.name}{item.enabled ? "" : " · 새 문서 사용 중지"}</option>)}</select></label>}
          {options?.ingestion_enabled && <label>검색 방식 <select value={searchMethod} onChange={event => setSearchMethod(event.target.value as typeof searchMethod)}><option value="hybrid" disabled={!options.search_methods?.includes("hybrid") || selectedVersion?.dense_available === false}>Hybrid</option><option value="dense" disabled={!options.search_methods?.includes("dense") || selectedVersion?.dense_available === false}>Dense</option><option value="bm25">BM25</option></select></label>}
          {options?.ingestion_enabled && <label title={options.rerank_available ? undefined : "설정에서 재순위 제공자를 연결하면 사용할 수 있습니다"}><input type="checkbox" checked={rerank && Boolean(options.rerank_available)} disabled={!options.rerank_available} onChange={event => setRerank(event.target.checked)}/> 재순위 적용</label>}
          <label>표시 결과 수 <input type="number" min={1} max={20} step={1} value={resultLimit}
            onChange={event => setResultLimit(event.target.value)}/><small>서버 후보 수와 리랭킹 결과 수 이내에서 표시합니다.</small></label>
          <label>검색 버전 <select value={version} onChange={event => setVersion(event.target.value)}>
            <option value="">현재 검색 버전</option>
            {versions.map(item => <option key={item.index_version} value={item.index_version}>{new Date(item.created_at).toLocaleString("ko-KR")} · {item.status === "active" ? "현재" : "이전"} · {item.index_version.slice(0, 8)}{item.dense_available === false ? " · BM25만 가능" : ""}</option>)}
          </select></label>
          <label>문서 <select value={scope} onChange={event => setScope(event.target.value)}>
            <option value="">모든 문서</option>
            {documents.filter(item => item.active_result).map(item => <option key={item.document_id} value={item.document_id}>{item.filename.normalize("NFC")} · {item.document_id.slice(0, 8)}</option>)}
          </select></label>
          </div></details>
        </form>
        {options?.ingestion_enabled && versions.length === 0 && <p className="hint">선택한 모델에 검색 가능한 색인이 없습니다. 문서를 처리한 뒤 검색해 주세요.</p>}
        {selectedVersion?.dense_available === false && <p className="hint">이 검색 버전은 임베딩 계약이 달라 BM25만 사용할 수 있습니다. Dense·Hybrid 검색은 설정에서 색인을 다시 준비해 주세요.</p>}
        {results && <p className="hint">검색 인덱스 · {searchedVersion.slice(0, 8)}{versions.find(item => item.index_version === searchedVersion)?.status === "retired" ? " · 이전 버전" : " · 현재 버전"}</p>}
        {results?.length === 0 && <p className="hint">일치하는 결과가 없습니다.</p>}
        {results?.map(hit => <article className="hit" key={hit.unit_id}>
          <strong>{hit.filename.normalize("NFC")}</strong>
          <p className="hint">문서 {hit.document_id.slice(0, 8)} · 작업 {hit.job_id.slice(0, 8)} · {locationLabel(hit)}{hit.quality === "unverified" ? " · 구조 확인 필요" : ""}{hit.heading_path_origin === "inferred" ? " · 제목 계층 추정" : ""}</p>
          <p className="result-text">{hit.search_text}</p>
          {!!hit.related_images?.length && session && <VisualAssets items={hit.related_images} session={session}/>}
          {(hit.context_before || hit.context_after) && <details><summary>앞뒤 문맥 보기</summary>{hit.context_before && <p className="result-text">앞 · {hit.context_before}</p>}{hit.context_after && <p className="result-text">뒤 · {hit.context_after}</p>}</details>}
          <button className="secondary" onClick={() => void viewEvidence(hit)}>근거 원문 보기</button>
          {!!hit.context_urls?.length && <button className="secondary" onClick={() => void viewEvidence(hit, true)}>문맥 원문 보기</button>}
          {expanded?.id === hit.unit_id + ":context" && <div className="evidence"><p className="hint">제목·캡션·인접 본문 등 문맥입니다. 표 연결은 추정일 수 있습니다.</p><p className="result-text">{expanded.text}</p></div>}
          {expanded?.id === hit.unit_id && <div className="evidence"><p className="hint">검색 결과에 연결된 원문 근거입니다.</p><p className="result-text">{expanded.text}</p></div>}
        </article>)}
      </section>}
      {chatPage && <DocumentChat messages={messages} session={session!} searching={searching} query={query} onQueryChange={setQuery} onSubmit={search}
        onClear={() => setMessages([])} canSend={Boolean(query.trim()) && Boolean(chatOptions?.configured) && !searching && !(options?.ingestion_enabled && !embeddingProfile)}
        model={chatModel} models={chatOptions?.models ?? []} reasoning={reasoningEffort} connected={Boolean(chatOptions?.configured)}
        onModelChange={value => {setChatModel(value); localStorage.setItem("indexer.model", value);}}
        onReasoningChange={value => {setReasoningEffort(value); localStorage.setItem("indexer.reasoning", value);}}
        scopeLabel={scope ? documents.find(item => item.document_id === scope)?.filename.normalize("NFC") ?? "선택한 문서" : "모든 문서"}
        locationLabel={locationLabel} expanded={expanded} onEvidence={hit => void viewEvidence(hit)} onContext={hit => void viewEvidence(hit, true)}
        scopeFields={<>
          {options?.ingestion_enabled && <label>검색 방식<select value={searchMethod} onChange={event => setSearchMethod(event.target.value as typeof searchMethod)}><option value="hybrid" disabled={!options.search_methods?.includes("hybrid") || selectedVersion?.dense_available === false}>Hybrid</option><option value="dense" disabled={!options.search_methods?.includes("dense") || selectedVersion?.dense_available === false}>Dense</option><option value="bm25">BM25</option></select></label>}
          <label>검색 버전<select value={version} onChange={event => setVersion(event.target.value)}>
            <option value="">현재 검색 버전</option>
            {versions.map(item => <option key={item.index_version} value={item.index_version}>
              {new Date(item.created_at).toLocaleString("ko-KR")} · {item.status === "active" ? "현재" : "이전"} · {item.index_version.slice(0, 8)}{item.dense_available === false ? " · BM25만 가능" : ""}
            </option>)}
          </select></label>
          {options?.ingestion_enabled && <label className="check" title={options.rerank_available ? undefined : "설정에서 재순위 제공자를 연결하면 사용할 수 있습니다"}><input type="checkbox" checked={rerank && Boolean(options.rerank_available)} disabled={!options.rerank_available} onChange={event => setRerank(event.target.checked)}/> 재순위 적용</label>}
          <label className="wide">문서<select value={scope} onChange={event => {setScope(event.target.value); setXlsxFullTable(false);}}>
            <option value="">모든 문서</option>
            {documents.filter(item => item.active_result).map(item =>
              <option key={item.document_id} value={item.document_id}>{item.filename.normalize("NFC")} · {item.document_id.slice(0, 8)}</option>)}
          </select></label>
          {xlsxScope && <label className="check wide"><input type="checkbox" checked={xlsxFullTable}
            onChange={event => setXlsxFullTable(event.target.checked)}/> 확인된 Excel 표 전체에서 개수·분포·날짜·수치 집계</label>}
        </>}/>}
    </>}
  </main>;
}

createRoot(document.getElementById("root")!).render(<App />);
