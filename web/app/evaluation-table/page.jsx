import EvaluationTable from "./EvaluationTable";

export const metadata = {
  title: "카드 평가표 — Spire Archive",
  description: "기본 타격·수비를 제외한 카드의 효과 점수, 기준점, 가치 지수와 티어를 정렬하고 비교합니다.",
};

export default function EvaluationTablePage() {
  return <EvaluationTable />;
}
