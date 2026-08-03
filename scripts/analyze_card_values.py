#!/usr/bin/env python3
"""Build the first-pass card effect inventory and value model.

The model intentionally separates a card's estimated effect value from the
rarity/cost budget it is compared against.  It is an exploratory calibration
tool, not a claim that every bespoke card can already be reduced to one scalar.
"""

from __future__ import annotations

import csv
import json
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "web/public/data/cards.json"
OUTPUT = ROOT / "analysis/card_value_v1"

PLAYABLE_RARITIES = {"Basic", "Common", "Uncommon", "Rare"}
SPECIAL_POOLS = {"event", "curse", "status", "token", "quest"}

# The first pass is deliberately anchored to the two most common immediate
# effects.  Multipliers are empirical comparison factors, not enemy-count
# assumptions (an ALL-enemies attack does not literally get multiplied by 3).
DAMAGE_POINT = 0.50
BLOCK_POINT = 0.60
AOE_MULTIPLIER = 1.30
DELAY_MULTIPLIER = 0.80
KEYWORD_POINTS: dict[str, float | None] = {
    "EXHAUST": -1.00,
    "ETHEREAL": -1.00,
    "INNATE": 0.50,
    "RETAIN": 0.50,
    "ETERNAL": 0.00,
    # Sly changes the effective play cost; Unplayable changes the card's role.
    # Neither should be forced into a flat additive number.
    "SLY": None,
    "UNPLAYABLE": None,
}


FAMILY_LABELS = {
    "damage": ("피해", "직접·고정·반사·조건부 피해"),
    "block": ("방어도", "즉시·지연·지속 방어도"),
    "draw": ("카드 뽑기", "즉시·지연·조건부 드로우"),
    "discard": ("버리기", "선택·무작위·손 전체 버리기"),
    "exhaust": ("소멸시키기", "자신 또는 다른 카드를 전투 중 제거"),
    "card_create": ("카드 생성", "손·뽑기·버린 카드 더미에 카드 추가/복사"),
    "card_move": ("카드 이동/탐색", "더미 간 이동, 서치, 회수, 맨 위 배치"),
    "card_play": ("추가 사용/재사용", "자동 사용, 반복 사용, Replay"),
    "card_upgrade": ("강화", "전투 중 카드 강화"),
    "card_transform": ("변화", "카드를 다른 카드로 변화"),
    "card_cost": ("비용 변화", "비용 감소·증가·무료화"),
    "retain": ("보존", "카드 또는 손패 보존"),
    "energy": ("에너지", "에너지 획득·손실·제한"),
    "stars": ("별", "별 획득·소모 연동"),
    "hp": ("체력", "체력 손실·회복·최대 체력"),
    "buff": ("강화 효과", "힘·민첩·집중·활력·가시·도금·무형 등"),
    "debuff": ("약화 효과", "약화·취약·중독·파멸·힘 감소 등"),
    "orb": ("구체", "집중·구체 생성·발동·밀어내기·슬롯"),
    "osty": ("오스티/소환", "소환, 오스티 공격·회복·죽음·연동"),
    "forge": ("제련", "제련 및 군주의 칼날 연동"),
    "trigger": ("지속/발동기", "턴 시작·종료 또는 Whenever/Every 발동"),
    "restriction": ("사용 제약", "사용 조건, 사용 불가, 턴 종료, 획득 제한"),
    "enemy_control": ("적 제어", "기절·처치·의도·행동 방해"),
    "ally": ("협동", "다른 플레이어 또는 모든 플레이어 대상"),
    "reward_meta": ("전투 외/보상", "골드·카드 보상·덱 제거·이벤트·퀘스트"),
    "scaling": ("가변/스케일링", "X, 더미/횟수/상태에 비례하는 값"),
    "status_penalty": ("상태/저주 페널티", "손패 오염, 턴 종료 피해 등"),
    "keyword": ("카드 키워드", "본문 밖 카드 속성"),
    "bespoke_rule": ("고유 규칙", "단독 계열로 두기 어려운 카드 고유 규칙"),
    "empty": ("효과 없음", "본문이 비어 있는 상태/저주 카드"),
}


# A clause may map to several atomic families.  For example, "draw 2 and
# discard 1" emits both draw and discard.  The raw/normalized clause remains
# attached so a later parser can split it more finely without losing evidence.
FAMILY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ally", re.compile(r"\b(another player|other players|all players|all allies|allies)\b", re.I)),
    ("damage", re.compile(r"\b(deal|deals|damage|take \d+ damage|attacked|reflect)\b", re.I)),
    ("block", re.compile(r"\bblock\b", re.I)),
    ("draw", re.compile(r"\b(draw|cards until|hand is full)\b", re.I)),
    ("discard", re.compile(r"\bdiscard", re.I)),
    ("exhaust", re.compile(r"\bexhaust", re.I)),
    ("card_upgrade", re.compile(r"\bupgrade", re.I)),
    ("card_transform", re.compile(r"\btransform", re.I)),
    ("retain", re.compile(r"\bretain", re.I)),
    ("card_cost", re.compile(r"\b(costs?|free to play|free this|reduce the cost|increase the cost)\b", re.I)),
    ("card_create", re.compile(r"\b(add|create|copy|copies|fill your hand)\b.*\b(card|cards|shiv|shivs|soul|souls|wound|wounds|dazed|burn|void|debris|fuel|slimed|hand|pile)\b", re.I)),
    ("card_move", re.compile(r"\b(put|return|shuffle|choose|top card|top of|from your (?:draw|discard|exhaust) pile|into your hand)\b", re.I)),
    ("card_play", re.compile(r"\b(play|played|replay|hits? twice|additional time|extra time|triggered .*additional)\b", re.I)),
    ("energy", re.compile(r"\benergy\b", re.I)),
    ("stars", re.compile(r"\b(star|stars)\b", re.I)),
    ("hp", re.compile(r"\b(hp|max hp|heal|lose that much hp|loses? \d+ hp)\b", re.I)),
    ("orb", re.compile(r"\b(orb|orbs|channel|evoke|focus)\b", re.I)),
    ("osty", re.compile(r"\b(osty|summon|minion)\b", re.I)),
    ("forge", re.compile(r"\b(forge|forges|sovereign blade)\b", re.I)),
    ("debuff", re.compile(r"\b(apply|weak|vulnerable|poison|doom|frail|enemy loses|artifact)\b", re.I)),
    ("buff", re.compile(r"\b(strength|dexterity|vigor|thorns|plating|intangible|buffer)\b", re.I)),
    ("enemy_control", re.compile(r"\b(stun|kill enemies|enemy intends|enemy gains|cannot act|die)\b", re.I)),
    ("reward_meta", re.compile(r"\b(gold|card reward|rest site|next act|deck after|remove a card from your deck|special event|combats)\b", re.I)),
    ("restriction", re.compile(r"\b(can only|cannot|must be played|end your turn|unplayable|take double damage|die)\b", re.I)),
    ("scaling", re.compile(r"\b(equal to|for each|every .* times|x times|\bx\b|additional damage|double|half|that many|that much|number of)\b", re.I)),
    ("trigger", re.compile(r"^(at the (?:start|end)|whenever|every|next turn|at the end|the first|if |when )", re.I)),
    ("status_penalty", re.compile(r"\b(status|curse|burn|dazed|wound|void|slimed|bad luck|regret)\b", re.I)),
]


EFFECT_SCORE_ROWS = [
    ("피해", "단일 적 즉시 피해", "피해 1", "0.50", "높음", "타격 횟수만큼 합산"),
    ("피해", "모든 적 피해", "피해 1", "0.65", "중간", "단일 피해 0.50 × 광역 1.30"),
    ("피해", "무작위 적 피해", "피해 1", "0.45", "낮음", "집중 불가 할인; 다수전 기대값은 별도 보정"),
    ("피해", "오스티 피해", "피해 1", "0.45", "낮음", "오스티 생존·행동 조건 반영"),
    ("피해", "지연 피해", "피해 1", "0.40", "낮음", "즉시 피해 × 0.80"),
    ("방어", "즉시 방어도", "방어도 1", "0.60", "높음", "Defend/Leap/Iron Wave 앵커"),
    ("방어", "다음 턴 방어도", "방어도 1", "0.48", "중간", "즉시 방어 × 0.80"),
    ("자원", "카드 뽑기", "1장", "2.00", "중간", "순환과 순카드이득을 합친 평균값"),
    ("자원", "선택 버리기", "1장", "-0.50", "낮음", "교활/버리기 시너지 전의 독립값"),
    ("자원", "무작위 버리기", "1장", "-1.00", "낮음", "선택권 부재"),
    ("자원", "에너지 획득", "1", "2.50", "중간", "Adrenaline 등으로 교차 점검"),
    ("자원", "별 획득", "1", "1.50", "중간", "Venerate/Solar Strike/Glow 앵커"),
    ("디버프", "약화", "1턴", "1.50", "중간", "Sucker Punch/Go for the Eyes 앵커"),
    ("디버프", "취약", "1턴", "1.50", "중간", "Beam Cell/Fear 앵커"),
    ("디버프", "중독", "1", "1.00", "중간", "Deadly Poison/Poisoned Stab 앵커"),
    ("디버프", "파멸", "1", "0.25", "중간", "Scourge/Blight Strike 앵커"),
    ("버프", "힘 또는 민첩", "1", "3.25", "중간", "Inflame/Footwork 앵커; 지속값"),
    ("버프", "이번 턴 힘·민첩·집중", "1", "0.80", "낮음", "일회성 할인"),
    ("버프", "활력", "1", "0.50", "낮음", "다음 공격에만 적용"),
    ("버프", "도금", "1", "1.50", "낮음", "Stone Armor 기준"),
    ("카드 생성", "단도 생성", "1장", "2.00", "중간", "0코스트 4피해 토큰 기준"),
    ("레전트", "제련", "1", "0.25", "높음", "Wrought in War/Spoils of Battle 교차 앵커"),
    ("네크로바인더", "소환", "1", "1.00", "낮음", "오스티 생존과 후속 공격에 따라 편차 큼"),
    ("키워드", "소멸", "카드 속성", "-1.00", "낮음", "재사용 상실; 덱 압축 시너지는 상호작용으로 환급"),
    ("키워드", "휘발성", "카드 속성", "-1.00", "낮음", "미사용 시 소멸 위험"),
    ("키워드", "선천성", "카드 속성", "+0.50", "낮음", "초반 확정성; 손패 혼잡 가능"),
    ("키워드", "보존", "카드 속성", "+0.50", "낮음", "사용 시점 선택권"),
    ("키워드", "교활", "카드 속성", "상호작용", "낮음", "버리기 시 무료 사용이므로 코스트 기준선 자체를 전환"),
    ("키워드", "사용불가", "카드 속성", "상호작용", "낮음", "상태/저주/트리거 카드 전용 별도 모델"),
    ("키워드", "영구", "카드 속성", "전투 0", "중간", "덱 관리 가치는 별도 메타 점수"),
    ("조건", "모든 적 대상", "효과 배율", "×1.30", "중간", "평균 적 수가 아닌 안정적 비교 계수"),
    ("조건", "다음 턴/지연", "효과 배율", "×0.80", "낮음", "즉시성 할인"),
    ("조건", "X/더미/상태 비례", "효과 배율", "시나리오", "낮음", "보수·기준·낙관 3개 입력 필요"),
    ("조건", "파워/반복 발동", "효과 배율", "턴수", "낮음", "기본 기대 발동 횟수를 별도 가정"),
]


# Values not observed directly are priors.  observed_anchor_count makes that
# distinction explicit in the generated table.
BENCHMARK_PRIORS = {
    "character": {
        "Basic": {0: 1.5, 1: 3.0, 2: 6.0, 3: 10.0, 4: 15.0},
        "Common": {0: 2.7, 1: 5.3, 2: 9.0, 3: 13.5, 4: 19.0},
        "Uncommon": {0: 5.0, 1: 6.5, 2: 11.0, 3: 16.0, 4: 26.0},
        "Rare": {0: 6.5, 1: 8.0, 2: 17.0, 3: 22.0, 4: 30.0},
    },
    "colorless": {
        "Common": {0: 3.5, 1: 6.0, 2: 10.0, 3: 15.0, 4: 21.0},
        "Uncommon": {0: 6.5, 1: 6.8, 2: 12.0, 3: 17.0, 4: 25.0},
        "Rare": {0: 7.5, 1: 9.0, 2: 18.0, 3: 24.0, 4: 32.0},
    },
}


def write_csv(path: Path, rows: Iterable[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def markup_to_analysis_text(value: str) -> str:
    # Resource tags serve two roles in the source: they can carry the amount
    # ("Gain [energy:2]") or merely render the unit after an already printed
    # number ("0[energy:1] card", "Gain 9 [star:1]").  Expanding every tag to
    # its numeric argument produced artifacts such as ``0 1 Energy`` and
    # ``9 1 Stars`` and prevented otherwise ordinary clauses from matching.
    # Consume unit-only tags first, then expand amount-bearing tags.
    value = re.sub(r"(\d+)\s*\[energy:\d+\]", r"\1 Energy", value, flags=re.I)
    value = re.sub(r"(\d+\s+(?:less|more|additional|extra))\s+\[energy:\d+\]", r"\1 Energy", value, flags=re.I)
    value = re.sub(r"\[energy:(\d+)\]", r"\1 Energy", value, flags=re.I)
    value = re.sub(r"(\d+)\s*\[star:\d+\]", r"\1 Stars", value, flags=re.I)
    value = re.sub(r"\[star:(\d+)\]", r"\1 Stars", value, flags=re.I)
    value = re.sub(r"\[/?[a-zA-Z]+(?::[^\]]+)?\]", "", value)
    return " ".join(value.split())


def clauses(value: str) -> list[str]:
    text = markup_to_analysis_text(value)
    if not text:
        return []
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]


def normalize_template(value: str) -> str:
    value = re.sub(r"\b\d+(?:\.\d+)?%?\b", "{n}", value)
    return re.sub(r"\s+", " ", value).strip()


def classify_clause(value: str) -> list[str]:
    found = [family for family, pattern in FAMILY_PATTERNS if pattern.search(value)]
    return found or ["bespoke_rule"]


def polarity_for(families: list[str], value: str) -> str:
    lower = value.lower()
    if any(word in lower for word in ("lose hp", "take ", "cannot", "costs an additional", "enemy gains", "lose ")):
        return "negative_or_cost"
    if "bespoke_rule" in families:
        return "contextual"
    return "positive_or_mixed"


def is_scaling_clause(value: str) -> bool:
    return bool(dict(FAMILY_PATTERNS)["scaling"].search(value))


PURE_DAMAGE_RE = re.compile(
    r"^Deal (?P<amount>\d+) damage"
    r"(?P<aoe> to ALL enemies)?"
    r"(?: (?P<hits>\d+) times| (?P<twice>twice))?\.$",
    re.I,
)
PURE_BLOCK_RE = re.compile(r"^Gain (?P<amount>\d+) Block\.$", re.I)


def pure_body_value(card: dict[str, Any]) -> tuple[bool, float, str]:
    total = 0.0
    parts: list[str] = []
    body_clauses = clauses(card["text"]["en"]["description"])
    if not body_clauses:
        return False, 0.0, ""
    for clause in body_clauses:
        damage = PURE_DAMAGE_RE.fullmatch(clause)
        block = PURE_BLOCK_RE.fullmatch(clause)
        if damage:
            hits = int(damage.group("hits") or (2 if damage.group("twice") else 1))
            amount = int(damage.group("amount")) * hits
            multiplier = AOE_MULTIPLIER if damage.group("aoe") else 1.0
            points = amount * DAMAGE_POINT * multiplier
            total += points
            parts.append(f"damage={amount}×{multiplier:.2f}×{DAMAGE_POINT:.2f}")
        elif block:
            amount = int(block.group("amount"))
            points = amount * BLOCK_POINT
            total += points
            parts.append(f"block={amount}×{BLOCK_POINT:.2f}")
        else:
            return False, 0.0, ""
    return True, total, "; ".join(parts)


def cost_signature(card: dict[str, Any]) -> str:
    energy = card["cost"]["energy"]
    star = card["cost"]["star"]
    if card["cost"].get("is_x_cost"):
        energy = "X"
    if card["cost"].get("is_x_star_cost"):
        star = "X"
    bits = []
    if energy is not None:
        bits.append(f"E{energy}")
    if star is not None:
        bits.append(f"S{star}")
    return "+".join(bits) or "none"


def main() -> None:
    cards = json.loads(SOURCE.read_text(encoding="utf-8"))
    OUTPUT.mkdir(parents=True, exist_ok=True)

    instance_rows: list[dict[str, Any]] = []
    template_cards: dict[tuple[str, str], list[str]] = defaultdict(list)
    template_pools: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    family_counts: Counter[str] = Counter()
    card_family_counts: Counter[str] = Counter()

    for card in cards:
        seen_for_card: set[str] = set()
        body_clauses = clauses(card["text"]["en"]["description"])
        ko_body_clauses = clauses(card["text"]["ko"]["description"])
        if not body_clauses:
            body_clauses = [""]
        for index, clause in enumerate(body_clauses, start=1):
            families = ["empty"] if not clause else classify_clause(clause)
            template = "(empty)" if not clause else normalize_template(clause)
            clause_ko = ko_body_clauses[index - 1] if index <= len(ko_body_clauses) else ""
            numeric_values = "|".join(re.findall(r"\b\d+(?:\.\d+)?%?\b", clause))
            for family in families:
                key = (family, template)
                template_cards[key].append(card["id"])
                template_pools[key][card["pool"]["key"]] += 1
                family_counts[family] += 1
                seen_for_card.add(family)
                instance_rows.append(
                    {
                        "card_id": card["id"],
                        "name_ko": card["name"]["ko"],
                        "name_en": card["name"]["en"],
                        "pool": card["pool"]["key"],
                        "rarity": card["rarity"]["key"],
                        "cost": cost_signature(card),
                        "source": "body",
                        "clause_index": index,
                        "family": family,
                        "family_ko": FAMILY_LABELS[family][0],
                        "polarity": polarity_for(families, clause),
                        "scaling_or_conditional": is_scaling_clause(clause),
                        "template_en": template,
                        "clause_en": clause,
                        "clause_ko": clause_ko,
                        "numeric_values": numeric_values,
                        "structured_stats": json.dumps(card["stats"], ensure_ascii=False, separators=(",", ":")),
                    }
                )
        for keyword in card["keywords"]:
            family = "keyword"
            template = f"Keyword: {keyword['name_en']}"
            key = (family, template)
            template_cards[key].append(card["id"])
            template_pools[key][card["pool"]["key"]] += 1
            family_counts[family] += 1
            seen_for_card.add(family)
            instance_rows.append(
                {
                    "card_id": card["id"],
                    "name_ko": card["name"]["ko"],
                    "name_en": card["name"]["en"],
                    "pool": card["pool"]["key"],
                    "rarity": card["rarity"]["key"],
                    "cost": cost_signature(card),
                    "source": "keyword",
                    "clause_index": "",
                    "family": family,
                    "family_ko": FAMILY_LABELS[family][0],
                    "polarity": "contextual",
                    "scaling_or_conditional": False,
                    "template_en": template,
                    "clause_en": template,
                    "clause_ko": f"키워드: {keyword['name_ko']}",
                    "numeric_values": "",
                    "structured_stats": json.dumps(card["stats"], ensure_ascii=False, separators=(",", ":")),
                }
            )
        card_family_counts.update(seen_for_card)

    template_rows = []
    for (family, template), ids in sorted(template_cards.items()):
        template_rows.append(
            {
                "family": family,
                "family_ko": FAMILY_LABELS[family][0],
                "template_en": template,
                "occurrences": len(ids),
                "card_count": len(set(ids)),
                "pools": "|".join(f"{k}:{v}" for k, v in sorted(template_pools[(family, template)].items())),
                "example_card_ids": "|".join(sorted(set(ids))[:12]),
            }
        )

    catalog_rows = []
    for family, (label, definition) in FAMILY_LABELS.items():
        templates = {template for f, template in template_cards if f == family}
        catalog_rows.append(
            {
                "family": family,
                "family_ko": label,
                "definition": definition,
                "card_count": card_family_counts[family],
                "effect_instance_count": family_counts[family],
                "unique_template_count": len(templates),
            }
        )

    anchor_rows = []
    strict_anchor_values: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for card in cards:
        pure, points, formula = pure_body_value(card)
        if not pure:
            continue
        scope = "character" if card["pool"]["is_playable_character"] else card["pool"]["key"]
        anchor_kind = "strict" if not card["keyword_ids"] else "keyword_contaminated"
        eligible = scope in {"character", "colorless"} and card["rarity"]["key"] in PLAYABLE_RARITIES
        if eligible and anchor_kind == "strict":
            strict_anchor_values[(scope, card["rarity"]["key"], cost_signature(card))].append(points)
        anchor_rows.append(
            {
                "card_id": card["id"],
                "name_ko": card["name"]["ko"],
                "name_en": card["name"]["en"],
                "scope": scope,
                "rarity": card["rarity"]["key"],
                "cost": cost_signature(card),
                "keywords": "|".join(card["keyword_ids"]),
                "anchor_kind": anchor_kind,
                "effect_points": round(points, 3),
                "formula": formula,
                "description_en": markup_to_analysis_text(card["text"]["en"]["description"]),
            }
        )

    observed_rows = []
    for (scope, rarity, cost), values in sorted(strict_anchor_values.items()):
        observed_rows.append(
            {
                "scope": scope,
                "rarity": rarity,
                "cost": cost,
                "strict_anchor_count": len(values),
                "median_points": round(statistics.median(values), 3),
                "min_points": round(min(values), 3),
                "max_points": round(max(values), 3),
                "evidence": "observed",
            }
        )

    benchmark_rows = []
    for scope, rarity_map in BENCHMARK_PRIORS.items():
        for rarity, cost_map in rarity_map.items():
            for energy, prior in cost_map.items():
                observed = strict_anchor_values.get((scope, rarity, f"E{energy}"), [])
                benchmark_rows.append(
                    {
                        "scope": scope,
                        "rarity": rarity,
                        "energy_cost": energy,
                        "benchmark_points_v1": prior,
                        "strict_anchor_count": len(observed),
                        "observed_median_if_any": round(statistics.median(observed), 3) if observed else "",
                        "confidence": "high" if len(observed) >= 3 else ("medium" if observed else "low_prior"),
                    }
                )

    anchor_residual_rows = []
    for row in anchor_rows:
        if row["scope"] not in BENCHMARK_PRIORS or "+S" in row["cost"]:
            continue
        energy_match = re.fullmatch(r"E(\d+)", row["cost"])
        if not energy_match or row["rarity"] not in BENCHMARK_PRIORS[row["scope"]]:
            continue
        energy = int(energy_match.group(1))
        benchmark = BENCHMARK_PRIORS[row["scope"]][row["rarity"]].get(energy)
        if benchmark is None:
            continue
        keywords = row["keywords"].split("|") if row["keywords"] else []
        unsupported = [keyword for keyword in keywords if KEYWORD_POINTS.get(keyword) is None]
        keyword_adjustment = sum(KEYWORD_POINTS[keyword] for keyword in keywords if KEYWORD_POINTS.get(keyword) is not None)
        adjusted = float(row["effect_points"]) + keyword_adjustment
        anchor_residual_rows.append(
            {
                "card_id": row["card_id"],
                "name_ko": row["name_ko"],
                "name_en": row["name_en"],
                "scope": row["scope"],
                "rarity": row["rarity"],
                "cost": row["cost"],
                "body_effect_points": row["effect_points"],
                "keywords": row["keywords"],
                "keyword_adjustment": round(keyword_adjustment, 3),
                "adjusted_effect_points": "" if unsupported else round(adjusted, 3),
                "benchmark_points": benchmark,
                "residual": "" if unsupported else round(adjusted - benchmark, 3),
                "status": f"interaction_required:{'|'.join(unsupported)}" if unsupported else "scored",
            }
        )

    score_rows = [
        {
            "family": row[0],
            "effect": row[1],
            "unit": row[2],
            "points_v1": row[3],
            "confidence": row[4],
            "basis_or_caution": row[5],
        }
        for row in EFFECT_SCORE_ROWS
    ]

    write_csv(
        OUTPUT / "effect_instances.csv",
        instance_rows,
        ["card_id", "name_ko", "name_en", "pool", "rarity", "cost", "source", "clause_index", "family", "family_ko", "polarity", "scaling_or_conditional", "template_en", "clause_en", "clause_ko", "numeric_values", "structured_stats"],
    )
    write_csv(
        OUTPUT / "effect_templates.csv",
        template_rows,
        ["family", "family_ko", "template_en", "occurrences", "card_count", "pools", "example_card_ids"],
    )
    write_csv(
        OUTPUT / "effect_catalog.csv",
        catalog_rows,
        ["family", "family_ko", "definition", "card_count", "effect_instance_count", "unique_template_count"],
    )
    write_csv(
        OUTPUT / "damage_block_anchors.csv",
        anchor_rows,
        ["card_id", "name_ko", "name_en", "scope", "rarity", "cost", "keywords", "anchor_kind", "effect_points", "formula", "description_en"],
    )
    write_csv(
        OUTPUT / "observed_baselines.csv",
        observed_rows,
        ["scope", "rarity", "cost", "strict_anchor_count", "median_points", "min_points", "max_points", "evidence"],
    )
    write_csv(
        OUTPUT / "benchmark_table_v1.csv",
        benchmark_rows,
        ["scope", "rarity", "energy_cost", "benchmark_points_v1", "strict_anchor_count", "observed_median_if_any", "confidence"],
    )
    write_csv(
        OUTPUT / "effect_scores_v1.csv",
        score_rows,
        ["family", "effect", "unit", "points_v1", "confidence", "basis_or_caution"],
    )
    write_csv(
        OUTPUT / "anchor_residuals_v1.csv",
        anchor_residual_rows,
        ["card_id", "name_ko", "name_en", "scope", "rarity", "cost", "body_effect_points", "keywords", "keyword_adjustment", "adjusted_effect_points", "benchmark_points", "residual", "status"],
    )

    pool_counts = Counter(card["pool"]["key"] for card in cards)
    collectible = sum(card["pool"]["is_playable_character"] for card in cards)
    special = sum(card["pool"]["key"] in SPECIAL_POOLS for card in cards)
    strict_count = sum(row["anchor_kind"] == "strict" and row["scope"] in {"character", "colorless"} for row in anchor_rows)
    contaminated_count = sum(row["anchor_kind"] == "keyword_contaminated" and row["scope"] in {"character", "colorless"} for row in anchor_rows)
    bespoke_cards = card_family_counts["bespoke_rule"]
    unique_clause_templates = len({row["template_en"] for row in template_rows})

    summary = {
        "source": str(SOURCE.relative_to(ROOT)),
        "card_count": len(cards),
        "character_card_count": collectible,
        "colorless_card_count": pool_counts["colorless"],
        "special_pool_card_count": special,
        "pool_counts": dict(sorted(pool_counts.items())),
        "effect_instance_count": len(instance_rows),
        "unique_clause_template_count": unique_clause_templates,
        "family_template_pair_count": len(template_rows),
        "effect_family_count": len(FAMILY_LABELS),
        "strict_damage_block_anchor_count": strict_count,
        "keyword_contaminated_damage_block_count": contaminated_count,
        "bespoke_rule_card_count": bespoke_cards,
        "coefficients": {
            "single_target_damage": DAMAGE_POINT,
            "block": BLOCK_POINT,
            "all_enemies_multiplier": AOE_MULTIPLIER,
            "delayed_multiplier": DELAY_MULTIPLIER,
        },
    }
    (OUTPUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    report = f"""# 카드 가치 모델 v1 — 효과 목록과 기준점

## 결론

이 버전의 점수는 **카드 효과 점수**와 **희귀도·코스트 기준점**을 분리한다.

`카드 효과 점수 S = Σ(효과량 × 단위 점수 × 대상/시점 배율) + 키워드 + 상호작용 보정`

`기준 대비 잔차 Δ = S - B(카드 풀, 희귀도, 에너지/별 비용)`

기준점 `B`를 카드 효과 점수에 다시 더하지 않는다. 기준점은 같은 비용·희귀도의 카드가 기대받는 예산이고, `Δ`가 양수면 기준보다 많은 효과, 음수면 적은 효과다.

## 카드 풀 범위

- 전체: **{len(cards)}장**
- 5개 캐릭터: **{collectible}장**
- 무색: **{pool_counts['colorless']}장**
- 이벤트·저주·상태·토큰·퀘스트: **{special}장**
- 본문 효과 인스턴스와 본문 밖 키워드: **{len(instance_rows)}개**
- 고유 본문/키워드 템플릿: **{unique_clause_templates}개**
- 한 템플릿이 여러 효과 계열에 걸친 경우를 펼친 계열-템플릿 조합: **{len(template_rows)}개**
- 상위 효과 계열: **{len(FAMILY_LABELS)}개**

특수 풀은 획득 방식과 덱 오염 목적이 달라 일반 카드의 코스트·희귀도 기준에 넣지 않는다. 무색도 캐릭터 카드와 별도 기준을 쓴다.

## 최초 앵커

- 단일 적 즉시 피해 1 = **{DAMAGE_POINT:.2f}점**
- 즉시 방어도 1 = **{BLOCK_POINT:.2f}점**
- 모든 적 대상 = 단일 대상 값의 **×{AOE_MULTIPLIER:.2f}**
- 다음 턴 등 지연 효과 = 즉시 값의 **×{DELAY_MULTIPLIER:.2f}**

이 계수는 1코스트 일반 카드에서 특히 잘 맞는다. Twin Strike는 `10×0.50=5.0`, Leap은 `9×0.60=5.4`, Iron Wave는 `5×0.50 + 5×0.60=5.5`로 중앙값이 약 5.3점이다. Strike와 Defend는 각각 3점으로 기본 카드 기준도 일치한다.

본문이 피해/방어뿐인 카드 중 키워드도 없는 엄격 앵커는 **{strict_count}장**, 소멸·선천성·보존·교활·휘발성 등이 붙은 오염 표본은 **{contaminated_count}장**이다. 오염 표본은 키워드 값을 추정할 때만 쓰고 기준 중앙값에서는 제외했다.

## 기준점 사용 주의

`benchmark_table_v1.csv`의 `observed_median_if_any`는 엄격 앵커로 직접 관측한 값이고, 빈 칸은 희귀도·코스트 곡선으로 채운 낮은 신뢰도의 사전값이다. 특히 0·3·4코스트와 희귀 등급은 표본이 적다. 별 비용이 붙은 카드는 `observed_baselines.csv`의 `E+S` 서명을 그대로 쓰며, 아직 에너지 비용 표에 합치지 않는다.

교활은 버리면 무료로 사용되므로 인쇄 코스트 기준선과 직접 비교하면 안 된다. X비용, 파워, 조건부 반복, 더미 크기 비례 효과도 단일 상수 대신 보수/기준/낙관 시나리오가 필요하다.

## 파일

- `effect_instances.csv`: 577장별 본문 절과 키워드를 효과 계열로 분리한 전체 기록
- `effect_templates.csv`: 숫자를 일반화한 고유 효과 템플릿과 등장 카드
- `effect_catalog.csv`: 상위 효과 계열별 카드·인스턴스·템플릿 수
- `damage_block_anchors.csv`: 순수 피해/방어 카드, 키워드 오염 여부, 산식
- `observed_baselines.csv`: 엄격 앵커에서 직접 관측한 기준점
- `benchmark_table_v1.csv`: 캐릭터/무색 × 희귀도 × 에너지 비용의 1차 기준표
- `effect_scores_v1.csv`: 수량화 가능한 효과와 키워드의 1차 점수표
- `anchor_residuals_v1.csv`: 피해/방어 앵커에 키워드 점수를 적용한 기준 대비 잔차
- `summary.json`: 범위와 계수 요약

## 다음 보정 순서

1. 직접 관측 표본이 3장 이상인 셀부터 기준점을 고정한다.
2. 피해/방어에 한 효과만 더해진 카드를 이용해 약화·취약·드로우·에너지·중독·파멸 값을 회귀한다.
3. 파워는 기대 전투 잔여 턴, X/비례 카드는 3개 시나리오를 입력한다.
4. 잔차가 큰 카드를 모아 시너지, 대상 선택권, 선천성 손패 혼잡 같은 상호작용 항을 추가한다.
5. 강화 카드는 같은 단위 점수로 `강화 후 - 기본`의 증가분을 별도 평가한다.
"""
    (OUTPUT / "README.md").write_text(report, encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
