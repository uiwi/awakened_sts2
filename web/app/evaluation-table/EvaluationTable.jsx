"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import SiteNav from "../components/SiteNav";

const ALL = "all";
const POOL_ORDER = [
  "ironclad", "silent", "defect", "necrobinder", "regent", "colorless",
  "event", "token", "status", "curse", "quest",
];
const TYPE_ORDER = ["Attack", "Skill", "Power", "Status", "Curse", "Quest"];
const RARITY_ORDER = ["Basic", "Common", "Uncommon", "Rare", "Ancient", "Event", "Token", "Status", "Curse", "Quest"];
const COLLATOR = new Intl.Collator("ko", { numeric: true, sensitivity: "base" });

const SORT_OPTIONS = [
  { value: "pool", label: "직업 / 카드 풀" },
  { value: "type", label: "카드 종류" },
  { value: "name", label: "카드 이름" },
  { value: "rarity", label: "희귀도" },
  { value: "score", label: "효과 점수" },
  { value: "value", label: "가치 지수" },
  { value: "confidence", label: "평가 신뢰도" },
  { value: "stability", label: "순위 안정성" },
];

const BAND_LABELS = {
  very_above_budget: "크게 상회",
  above_budget: "상회",
  on_budget: "기준 범위",
  below_budget: "하회",
  very_below_budget: "크게 하회",
  not_comparable: "비교 제외",
};

const INTERVAL_LABELS = {
  robustly_above_budget: "범위 전체가 기준 상회",
  robustly_below_budget: "범위 전체가 기준 하회",
  contained_on_budget: "범위 전체가 기준 안",
  scenario_overlaps_budget: "시나리오에 따라 판정 변동",
  not_comparable: "기준점 직접 비교 제외",
};

const CONFIDENCE_LABELS = {
  A: "A · 높음",
  B: "B · 중간",
  C: "C · 맥락 의존",
  raw_only: "RAW · 원점수",
};

const STABILITY_LABELS = {
  robust: "안정",
  moderate: "보통",
  sensitive: "민감",
};

function normalize(value) {
  return String(value ?? "").normalize("NFKC").toLocaleLowerCase("ko-KR").trim();
}

function formatScore(value, signed = false) {
  if (value === null || value === undefined) return "—";
  const fixed = Number(value).toFixed(2).replace(/\.00$/, "").replace(/(\.\d)0$/, "$1");
  return signed && value > 0 ? `+${fixed}` : fixed;
}

function formatRange(range) {
  if (!range || range.low === null) return "—";
  if (range.low === range.high) return formatScore(range.baseline);
  return `${formatScore(range.baseline)} · ${formatScore(range.low)}–${formatScore(range.high)}`;
}

function orderIndex(order, value) {
  const index = order.indexOf(value);
  return index === -1 ? order.length : index;
}

function sortValue(card, key) {
  switch (key) {
    case "pool": return orderIndex(POOL_ORDER, card.pool.key);
    case "type": return orderIndex(TYPE_ORDER, card.type.key);
    case "rarity": return orderIndex(RARITY_ORDER, card.rarity.key);
    case "score": return card.score.baseline;
    case "value": return card.value_index.baseline;
    case "confidence": return { A: 0, B: 1, C: 2, raw_only: 3 }[card.confidence.evaluation] ?? 4;
    case "stability": return card.stability.combined;
    default: return card.name.ko;
  }
}

function compareCards(a, b, key, direction) {
  const av = sortValue(a, key);
  const bv = sortValue(b, key);
  if (av === null || av === undefined) return bv === null || bv === undefined ? COLLATOR.compare(a.name.ko, b.name.ko) : 1;
  if (bv === null || bv === undefined) return -1;
  let result = typeof av === "string" ? COLLATOR.compare(av, bv) : av - bv;
  if (result === 0 && key === "pool") result = orderIndex(TYPE_ORDER, a.type.key) - orderIndex(TYPE_ORDER, b.type.key);
  if (result === 0 && key === "type") result = orderIndex(POOL_ORDER, a.pool.key) - orderIndex(POOL_ORDER, b.pool.key);
  if (result === 0) result = COLLATOR.compare(a.name.ko, b.name.ko);
  return result * direction;
}

function optionList(cards, selector, order = []) {
  const values = new Map();
  cards.forEach((card) => {
    const item = selector(card);
    values.set(item.value, item.label);
  });
  return [...values.entries()]
    .map(([value, label]) => ({ value, label }))
    .sort((a, b) => {
      const indexed = orderIndex(order, a.value) - orderIndex(order, b.value);
      return indexed || COLLATOR.compare(a.label, b.label);
    });
}

function FilterSelect({ id, label, value, onChange, options }) {
  return (
    <label className="select-field table-filter" htmlFor={id}>
      <span>{label}</span>
      <select id={id} value={value} onChange={(event) => onChange(event.target.value)}>
        <option value={ALL}>전체</option>
        {options.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}
      </select>
    </label>
  );
}

function SortHeader({ label, column, sortKey, direction, onSort, className = "" }) {
  const active = sortKey === column;
  return (
    <th className={className} aria-sort={active ? (direction === 1 ? "ascending" : "descending") : "none"}>
      <button type="button" onClick={() => onSort(column)}>
        {label}<span aria-hidden="true">{active ? (direction === 1 ? " ↑" : " ↓") : " ↕"}</span>
      </button>
    </th>
  );
}

export default function EvaluationTable() {
  const [cards, setCards] = useState([]);
  const [status, setStatus] = useState("loading");
  const [query, setQuery] = useState("");
  const [pool, setPool] = useState(ALL);
  const [type, setType] = useState(ALL);
  const [rarity, setRarity] = useState(ALL);
  const [confidence, setConfidence] = useState(ALL);
  const [band, setBand] = useState(ALL);
  const [sortKey, setSortKey] = useState("pool");
  const [direction, setDirection] = useState(1);
  const [expandedId, setExpandedId] = useState(null);

  useEffect(() => {
    const basePath = process.env.NEXT_PUBLIC_BASE_PATH || "";
    fetch(`${basePath}/data/card-evaluations.json`)
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json();
      })
      .then((data) => {
        setCards(data);
        setStatus("ready");
      })
      .catch(() => setStatus("error"));
  }, []);

  const poolOptions = useMemo(() => optionList(cards, (card) => ({ value: card.pool.key, label: card.pool.name_ko }), POOL_ORDER), [cards]);
  const typeOptions = useMemo(() => optionList(cards, (card) => ({ value: card.type.key, label: card.type.name_ko }), TYPE_ORDER), [cards]);
  const rarityOptions = useMemo(() => optionList(cards, (card) => ({ value: card.rarity.key, label: card.rarity.name_ko }), RARITY_ORDER), [cards]);

  const visibleCards = useMemo(() => {
    const needle = normalize(query);
    return cards
      .filter((card) => {
        const matchesQuery = !needle || [
          card.id, card.name.ko, card.name.en, card.description_ko,
          ...card.keywords.flatMap((item) => [item.id, item.name_ko, item.name_en]),
        ].some((value) => normalize(value).includes(needle));
        return matchesQuery
          && (pool === ALL || card.pool.key === pool)
          && (type === ALL || card.type.key === type)
          && (rarity === ALL || card.rarity.key === rarity)
          && (confidence === ALL || card.confidence.evaluation === confidence)
          && (band === ALL || card.balance_band === band);
      })
      .sort((a, b) => compareCards(a, b, sortKey, direction));
  }, [cards, query, pool, type, rarity, confidence, band, sortKey, direction]);

  const hasFilters = query || [pool, type, rarity, confidence, band].some((value) => value !== ALL);

  function requestSort(column) {
    if (sortKey === column) {
      setDirection((value) => value * -1);
      return;
    }
    setSortKey(column);
    setDirection(["score", "value", "stability"].includes(column) ? -1 : 1);
  }

  function resetFilters() {
    setQuery("");
    setPool(ALL);
    setType(ALL);
    setRarity(ALL);
    setConfidence(ALL);
    setBand(ALL);
  }

  return (
    <main className="evaluation-table-page">
      <SiteNav active="table" />

      <header className="evaluation-hero table-hero">
        <div>
          <p className="eyebrow">577 CARDS · MODEL V5</p>
          <h1>카드 평가표</h1>
          <p className="evaluation-lead">
            효과 점수와 기대 예산의 차이를 비교합니다. 열 제목을 눌러 직업, 카드 종류,
            가치 지수 등으로 정렬할 수 있습니다.
          </p>
        </div>
        <Link className="text-link" href="/evaluation-guide">평가 기준 먼저 읽기 <span aria-hidden="true">→</span></Link>
      </header>

      <section className="evaluation-controls" aria-label="평가표 필터 및 정렬">
        <label className="search-field evaluation-search" htmlFor="evaluation-search">
          <span>카드 이름·설명·키워드</span>
          <div className="search-input-wrap">
            <span aria-hidden="true">⌕</span>
            <input
              id="evaluation-search"
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="예: 별, 방어도, Barricade"
            />
          </div>
        </label>
        <FilterSelect id="evaluation-pool" label="직업 / 카드 풀" value={pool} onChange={setPool} options={poolOptions} />
        <FilterSelect id="evaluation-type" label="카드 종류" value={type} onChange={setType} options={typeOptions} />
        <FilterSelect id="evaluation-rarity" label="희귀도" value={rarity} onChange={setRarity} options={rarityOptions} />
        <FilterSelect
          id="evaluation-confidence"
          label="평가 신뢰도"
          value={confidence}
          onChange={setConfidence}
          options={Object.entries(CONFIDENCE_LABELS).map(([value, label]) => ({ value, label }))}
        />
        <FilterSelect
          id="evaluation-band"
          label="예산 판정"
          value={band}
          onChange={setBand}
          options={Object.entries(BAND_LABELS).map(([value, label]) => ({ value, label }))}
        />
        <label className="select-field table-filter sort-select" htmlFor="evaluation-sort">
          <span>정렬 기준</span>
          <div className="sort-select-row">
            <select id="evaluation-sort" value={sortKey} onChange={(event) => requestSort(event.target.value)}>
              {SORT_OPTIONS.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}
            </select>
            <button
              type="button"
              className="direction-button"
              onClick={() => setDirection((value) => value * -1)}
              aria-label={direction === 1 ? "현재 오름차순, 내림차순으로 변경" : "현재 내림차순, 오름차순으로 변경"}
            >
              {direction === 1 ? "↑" : "↓"}
            </button>
          </div>
        </label>
      </section>

      <section className="evaluation-result-bar" aria-live="polite">
        <p><strong>{visibleCards.length}</strong><span>장의 평가</span></p>
        <div>
          <span className="sort-status">{SORT_OPTIONS.find((option) => option.value === sortKey)?.label} · {direction === 1 ? "오름차순" : "내림차순"}</span>
          {hasFilters && <button type="button" className="reset-button" onClick={resetFilters}>필터 초기화</button>}
        </div>
      </section>

      {status === "loading" && <div className="state-panel">평가 데이터를 불러오는 중…</div>}
      {status === "error" && <div className="state-panel error">평가 데이터를 불러오지 못했습니다.</div>}
      {status === "ready" && visibleCards.length === 0 && (
        <div className="state-panel">조건에 맞는 카드가 없습니다.<button type="button" className="reset-button" onClick={resetFilters}>전체 보기</button></div>
      )}

      {status === "ready" && visibleCards.length > 0 && (
        <section className="evaluation-table-shell" aria-label="전체 카드 평가 결과">
          <table className="evaluation-table">
            <thead>
              <tr>
                <SortHeader label="카드" column="name" sortKey={sortKey} direction={direction} onSort={requestSort} className="card-name-column" />
                <SortHeader label="직업 / 풀" column="pool" sortKey={sortKey} direction={direction} onSort={requestSort} />
                <SortHeader label="종류" column="type" sortKey={sortKey} direction={direction} onSort={requestSort} />
                <SortHeader label="희귀도" column="rarity" sortKey={sortKey} direction={direction} onSort={requestSort} />
                <th>비용</th>
                <SortHeader label="효과 점수" column="score" sortKey={sortKey} direction={direction} onSort={requestSort} className="numeric-column" />
                <th className="numeric-column">기준점</th>
                <SortHeader label="가치 지수" column="value" sortKey={sortKey} direction={direction} onSort={requestSort} className="numeric-column" />
                <th>판정</th>
                <SortHeader label="신뢰도" column="confidence" sortKey={sortKey} direction={direction} onSort={requestSort} />
                <SortHeader label="안정성" column="stability" sortKey={sortKey} direction={direction} onSort={requestSort} />
              </tr>
            </thead>
            <tbody>
              {visibleCards.map((card) => {
                const expanded = expandedId === card.id;
                return (
                  <EvaluationRows
                    key={card.id}
                    card={card}
                    expanded={expanded}
                    onToggle={() => setExpandedId(expanded ? null : card.id)}
                  />
                );
              })}
            </tbody>
          </table>
        </section>
      )}

      <footer>
        <span>MODEL V5 · SORTABLE EVALUATION TABLE</span>
        <span>특수 풀과 교활·사용 불가 카드는 원점수만 표시합니다.</span>
      </footer>
    </main>
  );
}

function EvaluationRows({ card, expanded, onToggle }) {
  const value = card.value_index.baseline;
  return (
    <>
      <tr className={`evaluation-row pool-row-${card.pool.key} ${expanded ? "expanded" : ""}`}>
        <td className="card-name-cell">
          <button type="button" className="row-toggle" onClick={onToggle} aria-expanded={expanded}>
            <span><strong>{card.name.ko}</strong><small>{card.name.en}</small></span>
            <span aria-hidden="true">{expanded ? "−" : "+"}</span>
          </button>
        </td>
        <td><span className={`pool-label pool-${card.pool.key}`}>{card.pool.name_ko}</span></td>
        <td>{card.type.name_ko}</td>
        <td>{card.rarity.name_ko}</td>
        <td className="cost-cell">{card.cost_label}</td>
        <td className="number-cell"><strong>{formatScore(card.score.baseline)}</strong><small>{card.score.low !== card.score.high ? `${formatScore(card.score.low)}–${formatScore(card.score.high)}` : "고정"}</small></td>
        <td className="number-cell">{formatScore(card.benchmark)}</td>
        <td className={`number-cell value-cell ${value > 2 ? "positive" : value < -2 ? "negative" : "neutral"}`}>
          <strong>{formatScore(value, true)}</strong>
        </td>
        <td><span className={`band band-${card.balance_band}`}>{BAND_LABELS[card.balance_band]}</span></td>
        <td><span className={`grade grade-${card.confidence.evaluation.toLowerCase()}`}>{card.confidence.evaluation === "raw_only" ? "RAW" : card.confidence.evaluation}</span></td>
        <td className="stability-cell"><strong>{STABILITY_LABELS[card.stability.label]}</strong><small>{formatScore(card.stability.combined * 100)}%</small></td>
      </tr>
      {expanded && (
        <tr className="evaluation-detail-row">
          <td colSpan="11">
            <div className="evaluation-detail">
              <div>
                <p className="eyebrow">CARD TEXT</p>
                <p className="detail-card-text">{card.description_ko || "설명 없음"}</p>
                {card.keywords.length > 0 && <div className="keyword-row">{card.keywords.map((keyword) => <span className="keyword" key={keyword.id}>{keyword.name_ko}</span>)}</div>}
              </div>
              <dl>
                <div><dt>효과 점수 범위</dt><dd>{formatRange(card.score)}</dd></div>
                <div><dt>가치 지수 범위</dt><dd>{card.benchmark_comparable ? formatRange(card.value_index) : "기준 비교 제외"}</dd></div>
                <div><dt>구간 판정</dt><dd>{INTERVAL_LABELS[card.interval_class]}</dd></div>
                <div><dt>효과 / 기준 신뢰도</dt><dd>{card.confidence.effect} / {card.confidence.benchmark}</dd></div>
                <div><dt>평가 규칙</dt><dd className="rule-list">{card.rules.join(" · ")}</dd></div>
              </dl>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}
