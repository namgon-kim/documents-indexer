import {useEffect, useRef, useState} from 'react';
import {imageBlob} from './api';
import type {Session, VisualItem} from './api';

const labels: Record<string, string> = {image_analysis: '이 이미지의 분석 결과', image_description: '사용자가 작성한 이미지 설명', same_container: '같은 본문 구역',
  nearby: '가까운 위치 · 관계 추정', cell_anchor: '가까운 셀 위치 · 관계 확인 필요'};
export const analysisLabels: Record<string, string> = {completed: '글자 분석 완료', skipped: '분석 안 됨',
  no_text: '분석했으나 글자 미확인', unknown: '분석 결과 미확인', unverified: '분석 여부 확인 필요', suppressed: '분석 결과 보존 · 검색 제외'};

export function VisualAsset({item, session, showAnalysisStatus = true}: {item: VisualItem; session: Session; showAnalysisStatus?: boolean}) {
  const [source, setSource] = useState('');
  const [kind, setKind] = useState('');
  const [error, setError] = useState('');
  useEffect(() => () => {if (source) URL.revokeObjectURL(source);}, [source]);
  async function show() {
    if (source) {setSource(''); return;}
    try {
      const blob = await imageBlob(item.url, session);
      setKind(blob.type); setSource(URL.createObjectURL(blob)); setError('');
    } catch (reason) {setError(reason instanceof Error ? reason.message : '이미지를 불러오지 못했습니다.');}
  }
  return <div className="visual-asset"><button type="button" className="secondary" onClick={() => void show()}>{source ? '이미지 닫기' : item.is_preview ? '차트 미리보기' : '원본 이미지 보기'}</button>
    {item.type && <span className="hint"> {labels[item.type] || '이미지 관계 확인 필요'}</span>}
    {showAnalysisStatus && item.analysis_status && <span className="hint"> {item.format?.toUpperCase()} · {analysisLabels[item.analysis_status] || '확인 필요'}</span>}
    {error && <p className="hint">{error}</p>}
    {source && <p className="hint">{item.is_preview ? '시트에서 생성한 참고 이미지이며 원본과 다를 수 있습니다.' : '문서에 표시된 자르기·회전과 원본 이미지가 다를 수 있습니다.'}</p>}
    {source && (kind.startsWith('image/') ? <img src={source} alt={item.is_preview ? '차트 미리보기' : '문서에서 추출한 원본 이미지'} style={{display: 'block', maxWidth: '100%', maxHeight: 500, objectFit: 'contain', marginTop: 8}}/>
      : <a href={source} download="document-image">원본 파일 다운로드</a>)}
  </div>;
}

// Loads only when scrolled into view so documents with many images stay light.
// Originals the browser cannot draw (EMF, WMF, ...) stay downloadable instead of previewable.
export function ImageThumbnail({item, session, label, expanded, onToggle}: {item: VisualItem; session: Session; label: string;
  expanded: boolean; onToggle: () => void}) {
  const frame = useRef<HTMLButtonElement>(null);
  const [source, setSource] = useState('');
  const [state, setState] = useState<'idle' | 'loading' | 'ready' | 'file' | 'failed'>('idle');
  useEffect(() => () => {if (source) URL.revokeObjectURL(source);}, [source]);
  useEffect(() => {
    const node = frame.current;
    if (!node || state !== 'idle') return;
    const load = () => {
      setState('loading');
      imageBlob(item.url, session)
        .then(blob => {setSource(URL.createObjectURL(blob)); setState(blob.type.startsWith('image/') ? 'ready' : 'file');})
        .catch(() => setState('failed'));
    };
    if (typeof IntersectionObserver === 'undefined') {load(); return;}
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) {observer.disconnect(); load();}
    }, {rootMargin: '120px'});
    observer.observe(node);
    return () => observer.disconnect();
  }, [item.url, session, state]);
  const format = item.format?.toUpperCase() || '';
  function saveOriginal() {
    const link = document.createElement('a');
    link.href = source;
    link.download = `${label}${item.format ? '.' + item.format.toLowerCase() : ''}`;
    link.click();
  }
  if (state === 'file') return <button ref={frame} type="button" className="image-thumb is-file" onClick={saveOriginal}
    title="브라우저에서 표시할 수 없는 형식입니다. 원본 파일을 내려받습니다." aria-label={`${label} 원본 파일 다운로드`}>
    <span className="image-thumb-empty">{format || '파일'}<br/>원본 다운로드</span>
  </button>;
  return <>
    <button ref={frame} type="button" className={'image-thumb' + (expanded ? ' is-open' : '')} onClick={onToggle}
      disabled={state === 'failed'} aria-expanded={expanded} aria-label={`${label} ${expanded ? '크게 보기 닫기' : '크게 보기'}`}>
      {state === 'ready' ? <img src={source} alt="" onError={() => setState('file')}/>
        : <span className="image-thumb-empty">{state === 'failed' ? '미리보기 없음' : format || '이미지'}</span>}
    </button>
    {expanded && state === 'ready' && <figure className="image-preview">
      <img src={source} alt={`${label} ${item.is_preview ? '미리보기' : '원본'}`}/>
      <figcaption>{item.is_preview ? '시트에서 생성한 참고 이미지이며 원본과 다를 수 있습니다.' : '문서에 표시된 자르기·회전과 원본 이미지가 다를 수 있습니다.'}</figcaption>
    </figure>}
  </>;
}

export function VisualAssets({items, session}: {items: VisualItem[]; session: Session}) {
  return <>{items.map((item, index) => <VisualAsset key={`${item.url}-${index}`} item={item} session={session}/>)}</>;
}
