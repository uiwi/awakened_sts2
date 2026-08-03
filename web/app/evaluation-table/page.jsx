import EvaluationTable from "./EvaluationTable";

export const metadata = {
  title: "카드 평가표 — Spire Archive",
  description: "전체 577장의 효과 점수, 기준점, 가치 지수와 신뢰도를 정렬하고 비교합니다.",
};

export default function EvaluationTablePage() {
  return <EvaluationTable />;
}
