"use client";

import { useEffect, useMemo, useState } from "react";

const ALL = "all";

const POOL_ORDER = [
  "ironclad",
  "silent",
  "defect",
  "necrobinder",
  "regent",
  "colorless",
  "event",
  "token",
  "status",
  "curse",
  "quest",
];

const RARITY_ORDER = [
  "Basic",
  "Common",
  "Uncommon",
  "Rare",
  "Ancient",
  "Event",
  "Token",
  "Status",
  "Curse",
  "Quest",
];

function normalize(value) {
  return String(value ?? "").normalize("NFKC").toLocaleLowerCase("ko-KR").trim();
}

function uniqueOptions(cards, keySelector, labelSelector, order = []) {
  const options = new Map();
  cards.forEach((card) => {
    const key = keySelector(card);
    if (key) options.set(key, labelSelector(card));
  });
  return [...options.entries()]
    .map(([value, label]) => ({ value, label }))
    .sort((a, b) => {
      const ai = order.indexOf(a.value);
      const bi = order.indexOf(b.value);
      if (ai !== -1 || bi !== -1) {
        return (ai === -1 ? 999 : ai) - (bi === -1 ? 999 : bi);
      }
      return a.label.localeCompare(b.label, "ko");
    });
}

function energyLabel(card) {
  if (card.cost?.is_x_cost) return "X";
  if (card.cost?.energy === null || card.cost?.energy === undefined) return "—";
  return card.cost.energy;
}

function CardImage({ card, upgraded = false, className = "" }) {
  const [failed, setFailed] = useState(false);
  const preferred = upgraded
    ? card.image?.full_ko_upgraded_url
    : card.image?.full_ko_url;
  const src = failed || !preferred ? card.image?.art_url : preferred;

  if (!src) {
    return <div className={`image-placeholder ${className}`}>이미지 없음</div>;
  }

  return (
    <img
      className={className}
      src={src}
      alt={`${card.name.ko}${upgraded ? " 강화" : ""} 카드`}
      loading="lazy"
      onError={() => setFailed(true)}
    />
  );
}

function SelectFilter({ id, label, value, onChange, options }) {
  return (
    <label className="select-field" htmlFor={id}>
      <span>{label}</span>
      <select id={id} value={value} onChange={(event) => onChange(event.target.value)}>
        <option value={ALL}>전체</option>
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}

function DetailModal({ card, onClose }) {
  const [upgraded, setUpgraded] = useState(false);

  useEffect(() => {
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const closeOnEscape = (event) => event.key === "Escape" && onClose();
    window.addEventListener("keydown", closeOnEscape);
    return () => {
      document.body.style.overflow = previous;
      window.removeEventListener("keydown", closeOnEscape);
    };
  }, [onClose]);

  const selectedText = upgraded
    ? card.text.ko.upgrade_description_plain || card.text.ko.description_plain
    : card.text.ko.description_plain;

  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <section
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="detail-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <button className="modal-close" type="button" onClick={onClose} aria-label="닫기">
          ×
        </button>
        <div className="modal-art">
          <CardImage card={card} upgraded={upgraded} className="detail-image" />
        </div>
        <div className="modal-copy">
          <p className="eyebrow">{card.id}</p>
          <h2 id="detail-title">{card.name.ko}</h2>
          <p className="english-name">{card.name.en}</p>
          <div className="detail-badges">
            <span className={`badge pool-${card.pool.key}`}>{card.pool.name_ko}</span>
            <span className="badge">{card.type.name_ko}</span>
            <span className={`badge rarity-${card.rarity.key.toLowerCase()}`}>
              {card.rarity.name_ko}
            </span>
            <span className="badge energy">비용 {energyLabel(card)}</span>
          </div>
          <p className="detail-description">{selectedText || "설명 없음"}</p>
          {card.keywords.length > 0 && (
            <div className="keyword-row" aria-label="키워드">
              {card.keywords.map((keyword) => (
                <span className="keyword" key={keyword.id}>
                  {keyword.name_ko}
                </span>
              ))}
            </div>
          )}
          <dl className="stats">
            {card.stats.damage !== null && (
              <>
                <dt>피해</dt>
                <dd>{card.stats.damage}</dd>
              </>
            )}
            {card.stats.block !== null && (
              <>
                <dt>방어도</dt>
                <dd>{card.stats.block}</dd>
              </>
            )}
            {card.stats.cards_draw !== null && (
              <>
                <dt>드로우</dt>
                <dd>{card.stats.cards_draw}</dd>
              </>
            )}
          </dl>
          {card.text.ko.upgrade_description_plain && (
            <button
              className={`upgrade-toggle ${upgraded ? "active" : ""}`}
              type="button"
              onClick={() => setUpgraded((value) => !value)}
            >
              {upgraded ? "일반 카드 보기" : "강화 카드 보기"}
            </button>
          )}
        </div>
      </section>
    </div>
  );
}

export default function CardBrowser() {
  const [cards, setCards] = useState([]);
  const [status, setStatus] = useState("loading");
  const [query, setQuery] = useState("");
  const [keyword, setKeyword] = useState(ALL);
  const [pool, setPool] = useState(ALL);
  const [rarity, setRarity] = useState(ALL);
  const [selectedCard, setSelectedCard] = useState(null);

  useEffect(() => {
    let active = true;
    const basePath = process.env.NEXT_PUBLIC_BASE_PATH || "";
    fetch(`${basePath}/data/cards.json`)
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json();
      })
      .then((data) => {
        if (!active) return;
        setCards(data);
        setStatus("ready");
      })
      .catch(() => active && setStatus("error"));
    return () => {
      active = false;
    };
  }, []);

  const poolOptions = useMemo(
    () =>
      uniqueOptions(
        cards,
        (card) => card.pool.key,
        (card) => card.pool.name_ko,
        POOL_ORDER,
      ),
    [cards],
  );

  const rarityOptions = useMemo(
    () =>
      uniqueOptions(
        cards,
        (card) => card.rarity.key,
        (card) => card.rarity.name_ko,
        RARITY_ORDER,
      ),
    [cards],
  );

  const keywordOptions = useMemo(
    () =>
      uniqueOptions(
        cards.flatMap((card) =>
          card.keywords.map((item) => ({ keyword: item })),
        ),
        (item) => item.keyword.id,
        (item) => item.keyword.name_ko,
      ),
    [cards],
  );

  const filteredCards = useMemo(() => {
    const needle = normalize(query);
    return cards.filter((card) => {
      const matchesQuery =
        !needle ||
        [
          card.name.ko,
          card.name.en,
          card.id,
          ...card.keywords.flatMap((item) => [item.name_ko, item.name_en, item.id]),
        ].some((value) => normalize(value).includes(needle));
      const matchesKeyword = keyword === ALL || card.keyword_ids.includes(keyword);
      const matchesPool = pool === ALL || card.pool.key === pool;
      const matchesRarity = rarity === ALL || card.rarity.key === rarity;
      return matchesQuery && matchesKeyword && matchesPool && matchesRarity;
    });
  }, [cards, query, keyword, pool, rarity]);

  const hasFilters = query || keyword !== ALL || pool !== ALL || rarity !== ALL;

  function resetFilters() {
    setQuery("");
    setKeyword(ALL);
    setPool(ALL);
    setRarity(ALL);
  }

  return (
    <main>
      <header className="hero">
        <div className="hero-mark" aria-hidden="true">Ⅱ</div>
        <div>
          <p className="eyebrow">THE SPIRE ARCHIVE</p>
          <h1>카드 기록 보관소</h1>
          <p className="hero-copy">
            슬레이 더 스파이어 2의 카드 <strong>{cards.length || 577}장</strong>을
            이름, 키워드, 직업, 희귀도로 탐색하세요.
          </p>
        </div>
      </header>

      <section className="filters" aria-label="카드 검색 필터">
        <label className="search-field" htmlFor="card-search">
          <span>카드 이름 또는 키워드</span>
          <div className="search-input-wrap">
            <span aria-hidden="true">⌕</span>
            <input
              id="card-search"
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="예: 소멸, Barricade, FTL"
              autoComplete="off"
            />
          </div>
        </label>
        <SelectFilter
          id="keyword-filter"
          label="키워드"
          value={keyword}
          onChange={setKeyword}
          options={keywordOptions}
        />
        <SelectFilter
          id="pool-filter"
          label="직업 / 카드 풀"
          value={pool}
          onChange={setPool}
          options={poolOptions}
        />
        <SelectFilter
          id="rarity-filter"
          label="희귀도"
          value={rarity}
          onChange={setRarity}
          options={rarityOptions}
        />
      </section>

      <section className="results-bar" aria-live="polite">
        <p>
          <strong>{filteredCards.length}</strong>
          <span>장의 카드</span>
        </p>
        {hasFilters && (
          <button type="button" className="reset-button" onClick={resetFilters}>
            필터 초기화
          </button>
        )}
      </section>

      {status === "loading" && <div className="state-panel">카드 기록을 불러오는 중…</div>}
      {status === "error" && (
        <div className="state-panel error">
          카드 데이터를 불러오지 못했습니다. 정적 서버에서 실행했는지 확인해 주세요.
        </div>
      )}
      {status === "ready" && filteredCards.length === 0 && (
        <div className="state-panel">
          <span className="empty-rune">∅</span>
          조건에 맞는 카드가 없습니다.
          <button type="button" className="reset-button" onClick={resetFilters}>
            전체 카드 보기
          </button>
        </div>
      )}

      {status === "ready" && filteredCards.length > 0 && (
        <section className="card-grid" aria-label="카드 검색 결과">
          {filteredCards.map((card) => (
            <button
              className={`card-tile pool-border-${card.pool.key}`}
              key={card.id}
              type="button"
              onClick={() => setSelectedCard(card)}
            >
              <div className="card-image-wrap">
                <CardImage card={card} className="card-image" />
                <span className="energy-orb">{energyLabel(card)}</span>
              </div>
              <div className="card-copy">
                <div className="card-heading">
                  <div>
                    <h2>{card.name.ko}</h2>
                    <p>{card.name.en}</p>
                  </div>
                  <span className={`rarity-dot rarity-${card.rarity.key.toLowerCase()}`} />
                </div>
                <div className="card-meta">
                  <span>{card.pool.name_ko}</span>
                  <span>{card.type.name_ko}</span>
                  <span>{card.rarity.name_ko}</span>
                </div>
                <p className="card-description">{card.text.ko.description_plain}</p>
                {card.keywords.length > 0 && (
                  <div className="keyword-row">
                    {card.keywords.map((item) => (
                      <span className="keyword" key={item.id}>
                        {item.name_ko}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            </button>
          ))}
        </section>
      )}

      <footer>
        <span>577 CARDS · STABLE ARCHIVE</span>
        <span>데이터 및 이미지 © Mega Crit Games</span>
      </footer>

      {selectedCard && (
        <DetailModal card={selectedCard} onClose={() => setSelectedCard(null)} />
      )}
    </main>
  );
}
