"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import SiteNav from "../components/SiteNav";

const CORE_EFFECTS = [
  "damage_single",
  "damage_all",
  "block",
  "energy",
  "stars",
  "spend_1_star",
  "weak",
  "vulnerable",
  "strength",
  "dexterity",
  "focus",
];

const EFFECT_LABELS = {
  damage_single: "단일 대상 피해 1",
  damage_all: "광역 피해 1",
  block: "방어도 1",
  energy: "에너지 1",
  stars: "별 획득 1",
  spend_1_star: "별 비용 1",
  weak: "약화 1",
  vulnerable: "취약 1",
  strength: "힘 1",
  dexterity: "민첩 1",
  focus: "집중 1",
};

const RARITY_LABELS = {
  Basic: "기본",
  Common: "일반",
  Uncommon: "고급",
  Rare: "희귀",
};

const EFFECT_CONFIDENCE_LABELS = {
  high: "높음",
  medium: "중간",
  low: "낮음",
  low_context: "맥락 의존",
  low_prior: "사전값",
};

const FALLBACK_SUMMARY = {
  card_count: 577,
  source_clause_count: 860,
  effect_family_count: 31,
  unique_rule_count: 297,
  generic_fallback_effects: 0,
  hard_validation_passed: true,
};

function formatNumber(value) {
  if (typeof value !== "number") return value ?? "—";
  return Number.isInteger(value) ? String(value) : value.toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
}

function scoreRange(effect) {
  if (!effect) return "—";
  const baseline = formatNumber(effect.baseline);
  if (effect.low === effect.baseline && effect.high === effect.baseline) return baseline;
  return `${baseline} (${formatNumber(effect.low)}–${formatNumber(effect.high)})`;
}

function BenchmarkTable({ title, rows }) {
  const costs = [...new Set(rows.map((row) => row.cost))].sort(
    (a, b) => Number(a.slice(1)) - Number(b.slice(1)),
  );
  const rarities = ["Basic", "Common", "Uncommon", "Rare"].filter((rarity) =>
    rows.some((row) => row.rarity === rarity),
  );

  return (
    <div className="benchmark-card">
      <div className="section-heading compact">
        <p className="eyebrow">BASE SCORE</p>
        <h3>{title}</h3>
      </div>
      <div className="table-scroll compact-table">
        <table>
          <thead>
            <tr>
              <th>희귀도</th>
              {costs.map((cost) => <th key={cost}>{cost}</th>)}
            </tr>
          </thead>
          <tbody>
            {rarities.map((rarity) => (
              <tr key={rarity}>
                <th>{RARITY_LABELS[rarity]}</th>
                {costs.map((cost) => {
                  const cell = rows.find((row) => row.rarity === rarity && row.cost === cost);
                  return <td key={cost}>{cell ? formatNumber(cell.points) : "—"}</td>;
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function EvaluationGuide() {
  const [model, setModel] = useState(null);

  useEffect(() => {
    const basePath = process.env.NEXT_PUBLIC_BASE_PATH || "";
    fetch(`${basePath}/data/evaluation-model.json`)
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json();
      })
      .then(setModel)
      .catch(() => setModel({ summary: FALLBACK_SUMMARY, effect_scores: [], benchmarks: [] }));
  }, []);

  const summary = model?.summary || FALLBACK_SUMMARY;
  const effects = useMemo(() => {
    const byId = new Map((model?.effect_scores || []).map((effect) => [effect.effect, effect]));
    return CORE_EFFECTS.map((id) => byId.get(id)).filter(Boolean);
  }, [model]);
  const characterBenchmarks = (model?.benchmarks || []).filter((row) => row.scope === "character");
  const colorlessBenchmarks = (model?.benchmarks || []).filter((row) => row.scope === "colorless");

  return (
    <main>
      <SiteNav active="guide" />

      <header className="evaluation-hero guide-hero">
        <div>
          <p className="eyebrow">CARD VALUE MODEL · V5</p>
          <h1>카드 평가는<br />어떻게 계산했나</h1>
          <p className="evaluation-lead">
            카드의 모든 효과를 같은 점수축에 올리고, 희귀도와 에너지 비용이 허용하는
            기대 예산과 비교합니다. 결과는 정답이 아니라 비교 가능한 출발점입니다.
          </p>
          <Link className="primary-link" href="/evaluation-table">
            전체 카드 평가표 보기 <span aria-hidden="true">→</span>
          </Link>
        </div>
        <div className="formula-panel" aria-label="카드 평가 공식">
          <span className="formula-kicker">VALUE INDEX</span>
          <strong>V = Σ 효과 점수 − B</strong>
          <p>효과 합계에서 직업·희귀도·에너지 기준점을 뺍니다.</p>
          <dl>
            <div><dt>−2 ≤ V ≤ 2</dt><dd>기준 범위</dd></div>
            <div><dt>V &gt; 2</dt><dd>예산 상회</dd></div>
            <div><dt>V &lt; −2</dt><dd>예산 하회</dd></div>
          </dl>
        </div>
      </header>

      <section className="model-stats" aria-label="모델 검증 요약">
        <div><strong>{summary.card_count}</strong><span>전체 카드</span></div>
        <div><strong>{summary.source_clause_count}</strong><span>분리된 효과 문장</span></div>
        <div><strong>{summary.effect_family_count}</strong><span>효과 대분류</span></div>
        <div><strong>{summary.unique_rule_count}</strong><span>점수 규칙</span></div>
        <div><strong>{summary.generic_fallback_effects}</strong><span>범용 fallback</span></div>
      </section>

      <section className="guide-section">
        <div className="section-heading">
          <p className="eyebrow">01 · EFFECT SCORE</p>
          <h2>효과를 분리하고 모두 더합니다</h2>
          <p>
            피해, 방어도, 상태, 자원, 키워드와 조건을 원자 효과로 나눕니다. 이득은 양수,
            페널티와 인쇄된 별 비용은 음수입니다. 괄호는 보수–낙관 범위입니다.
          </p>
        </div>
        <div className="effect-value-grid">
          {effects.length > 0 ? effects.map((effect) => (
            <article key={effect.effect} className={effect.effect === "spend_1_star" ? "negative-effect" : ""}>
              <span>{EFFECT_LABELS[effect.effect]}</span>
              <strong>{scoreRange(effect)}</strong>
              <small className={`confidence-dot confidence-${effect.confidence}`}>{EFFECT_CONFIDENCE_LABELS[effect.confidence] || effect.confidence}</small>
            </article>
          )) : <div className="inline-loading">효과 점수표를 불러오는 중…</div>}
        </div>
        <aside className="model-note star-note">
          <span aria-hidden="true">✦</span>
          <div>
            <strong>별 비용은 기준점이 아닙니다.</strong>
            <p>별 획득은 1개당 +1.5점, 카드에 인쇄된 별 지불은 1개당 −2점인 별도 효과입니다. X별 비용은 X=1/2/4로 범위를 만듭니다.</p>
          </div>
        </aside>
      </section>

      <section className="guide-section">
        <div className="section-heading">
          <p className="eyebrow">02 · BASE SCORE</p>
          <h2>같은 조건의 기대 예산과 비교합니다</h2>
          <p>
            기준점 B는 카드 풀, 희귀도, 에너지 비용의 조합입니다. 별 비용은 여기에
            더하지 않습니다. 무색 카드는 캐릭터 카드와 별도 곡선을 사용합니다.
          </p>
        </div>
        {model && (
          <div className="benchmark-grid">
            <BenchmarkTable title="캐릭터 카드" rows={characterBenchmarks} />
            <BenchmarkTable title="무색 카드" rows={colorlessBenchmarks} />
          </div>
        )}
        <p className="benchmark-footnote">E5의 33점은 Meteor Strike에서 직접 관측한 희귀 카드 기준입니다. 직접 앵커가 없는 더 높은 비용은 외삽하지 않습니다.</p>
      </section>

      <section className="guide-section two-column-guide">
        <div>
          <div className="section-heading">
            <p className="eyebrow">03 · CONFIDENCE</p>
            <h2>점수와 신뢰도를 함께 봅니다</h2>
          </div>
          <div className="confidence-list">
            <article><span className="grade grade-a">A</span><div><strong>높은 신뢰도</strong><p>직접 수치화되고 기준점 근거도 충분합니다.</p></div></article>
            <article><span className="grade grade-b">B</span><div><strong>중간 신뢰도</strong><p>교차 보정 또는 제한된 시나리오가 포함됩니다.</p></div></article>
            <article><span className="grade grade-c">C</span><div><strong>맥락 의존</strong><p>덱 구성, 전투 길이와 대상 수에 민감합니다.</p></div></article>
            <article><span className="grade grade-raw">RAW</span><div><strong>기준 비교 제외</strong><p>교활·사용 불가·특수 풀 등은 원점수만 제공합니다.</p></div></article>
          </div>
        </div>
        <div>
          <div className="section-heading">
            <p className="eyebrow">04 · READING</p>
            <h2>평가표 읽는 순서</h2>
          </div>
          <ol className="reading-steps">
            <li><span>1</span><p><strong>가치 지수</strong>로 기준 예산 대비 위치를 확인합니다.</p></li>
            <li><span>2</span><p><strong>점수 범위</strong>가 ±2 기준선을 넘나드는지 봅니다.</p></li>
            <li><span>3</span><p><strong>평가 신뢰도</strong>가 낮으면 단일 수치보다 범위를 우선합니다.</p></li>
            <li><span>4</span><p><strong>순위 안정성</strong>으로 가정 변화에 강한 평가인지 확인합니다.</p></li>
          </ol>
        </div>
      </section>

      <section className="validation-banner">
        <div>
          <p className="eyebrow">VALIDATION</p>
          <h2>{summary.hard_validation_passed ? "전체 검증 통과" : "검증 결과 확인 필요"}</h2>
          <p>유한·정렬된 점수 범위, 원문 문장 커버리지, 근거 합계 재현과 fallback 0을 검사했습니다.</p>
        </div>
        <span className={summary.hard_validation_passed ? "validation-pass" : "validation-fail"}>
          {summary.hard_validation_passed ? "PASS" : "CHECK"}
        </span>
      </section>

      <footer>
        <span>MODEL V5 · 577 CARDS</span>
        <span>평가값은 밸런스 탐색을 위한 비교 모델입니다.</span>
      </footer>
    </main>
  );
}
