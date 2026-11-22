import type { Hit } from "./api";

export function TableRelations({ hit }: {hit: Hit}) {
  const labeled = hit.table_segments?.filter(segment => segment.label) || [];
  if (!labeled.length) return null;
  return <details><summary>표 라벨 관계 보기</summary>
    <p className="hint">라벨은 셀 내용을 이해하기 위한 연결입니다. 추정 관계는 원문을 확인해 주세요.</p>
    {labeled.map((segment, index) => <p className="hint" key={index}>
      {segment.address || `${segment.row}행 ${segment.column}열`} · {segment.label} · {segment.label_relations?.length
        ? segment.label_relations.map(relation => `${relation.text} (${relation.address || `${relation.row}행 ${relation.column}열`}, ${relation.status === "unverified" ? "추정" : relation.status === "verified" ? "확인됨" : "명시"})`).join(" / ")
        : "관계 출처 미확인"}
    </p>)}
  </details>;
}
