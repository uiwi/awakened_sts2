#!/usr/bin/env python3
"""Second-pass calibration from simple multi-effect cards.

This pass keeps damage, Block, target and timing anchors fixed, then measures
the implied unit value of one additional effect at a time.  It also scores the
subset of cards whose text can be decomposed without scenario assumptions.
"""

from __future__ import annotations

import csv
import copy
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import analyze_card_values as v1


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "web/public/data/cards.json"
OUTPUT = ROOT / "analysis/card_value_v2"

BENCHMARKS_V2 = copy.deepcopy(v1.BENCHMARK_PRIORS)
# The v1 colorless E0 priors were anchored almost entirely on Dramatic
# Entrance. Three independent cantrip/resource cards cluster near 4.0–4.25.
BENCHMARKS_V2["colorless"]["Uncommon"][0] = 4.25
BENCHMARKS_V2["colorless"]["Rare"][0] = 5.00
# v1 had no direct anchors for these cells. Simple fixed-effect cards provide
# usable (still low-sample) replacements.
BENCHMARKS_V2["character"]["Rare"][1] = 6.50
BENCHMARKS_V2["character"]["Rare"][3] = 20.00
BENCHMARKS_V2["colorless"]["Rare"][3] = 14.50


VALUES_V2: dict[str, float] = {
    "damage_single": 0.50,
    "damage_all": 0.65,
    "block": 0.60,
    "draw": 1.75,
    "discard_selected": -0.50,
    "energy": 2.50,
    "stars": 1.50,
    "hp_loss": -0.55,
    "weak": 2.10,
    "vulnerable": 2.00,
    "poison": 1.00,
    "doom": 0.27,
    "forge": 0.33,
    "summon": 1.10,
    "shiv": 1.90,
    "soul": 1.00,
    "lightning": 2.20,
    "frost": 3.20,
    "dark": 4.40,
    "glass": 3.50,
    "plasma": 7.00,
    "strength": 3.25,
    "dexterity": 3.25,
    "focus": 6.50,
    "vigor": 0.50,
    "plating": 1.60,
    "thorns": 0.50,
    "wound": -0.65,
    "dazed": -0.90,
    "slimed": -0.70,
    "void": -2.30,
    "burn": -1.50,
    "debris": -2.80,
}


CONFIDENCE = {
    "damage_single": "high",
    "damage_all": "high",
    "block": "high",
    "draw": "medium",
    "energy": "medium",
    "stars": "low_context",
    "hp_loss": "high",
    "weak": "medium",
    "vulnerable": "medium",
    "poison": "high",
    "doom": "high",
    "forge": "medium",
    "summon": "high",
    "shiv": "high",
    "strength": "high",
    "dexterity": "high",
    "plating": "medium",
}


# Each tuple is (effect, card id, target quantity, other scored effects, note).
# Quantities for ALL-enemy debuffs already include the 1.30 target multiplier.
EVIDENCE_SPECS: list[tuple[str, str, float, dict[str, float], str]] = [
    ("draw", "BACKFLIP", 2, {"block": 5}, "방어+드로우"),
    ("draw", "POMMEL_STRIKE", 1, {"damage_single": 9}, "피해+드로우"),
    ("draw", "SHRUG_IT_OFF", 1, {"block": 8}, "방어+드로우"),
    ("draw", "SWEEPING_BEAM", 1, {"damage_all": 6}, "광역 피해+드로우"),
    ("draw", "SKIM", 3, {}, "순수 다중 드로우"),
    ("draw", "PARSE", 3, {}, "휘발성 다중 드로우"),
    ("draw", "PROPHESIZE", 6, {}, "2코스트 다중 드로우"),
    ("draw", "MASTER_OF_STRATEGY", 3, {}, "무색 0코스트 소멸"),
    ("energy", "WISP", 1, {}, "0코스트 소멸"),
    ("energy", "PRODUCTION", 2, {}, "무색 0코스트 소멸"),
    ("energy", "SUPERCRITICAL", 4, {}, "희귀 0코스트 소멸"),
    ("energy", "BLOODLETTING", 2, {"hp_loss": 3}, "체력 손실+에너지"),
    ("energy", "ADRENALINE", 1, {"draw": 2}, "드로우+에너지+소멸"),
    ("energy", "OFFERING", 2, {"draw": 3, "hp_loss": 6}, "체력 손실+드로우+에너지"),
    ("stars", "VENERATE", 2, {}, "기본 카드 순수 별"),
    ("stars", "GATHER_LIGHT", 1, {"block": 8}, "방어+별"),
    ("stars", "SOLAR_STRIKE", 1, {"damage_single": 9}, "피해+별"),
    ("weak", "SUCKER_PUNCH", 1, {"damage_single": 8}, "피해+약화"),
    ("weak", "DEFY", 1, {"block": 6}, "방어+약화+휘발성"),
    ("weak", "LEG_SWEEP", 2, {"block": 11}, "방어+약화"),
    ("weak", "UPPERCUT", 1, {"damage_single": 13, "vulnerable": 1}, "피해+약화+취약"),
    ("vulnerable", "BEAM_CELL", 1, {"damage_single": 3}, "0코스트 피해+취약"),
    ("vulnerable", "BASH", 2, {"damage_single": 8}, "기본 카드 피해+취약"),
    ("vulnerable", "FEAR", 1, {"damage_single": 7}, "피해+취약+휘발성"),
    ("vulnerable", "TAUNT", 1, {"block": 7}, "방어+취약"),
    ("vulnerable", "TREMBLE", 3, {}, "소멸+취약"),
    ("vulnerable", "ASSASSINATE", 1, {"damage_single": 10}, "피해+선천성+소멸"),
    ("poison", "DEADLY_POISON", 5, {}, "순수 중독"),
    ("poison", "POISONED_STAB", 3, {"damage_single": 6}, "피해+중독"),
    ("poison", "SNAKEBITE", 7, {}, "보존+중독"),
    ("doom", "SCOURGE", 13, {"draw": 1}, "파멸+드로우"),
    ("doom", "NEGATIVE_PULSE", 7 * 1.30, {"block": 5}, "방어+광역 파멸"),
    ("doom", "DEATHBRINGER", 21 * 1.30, {"weak": 1 * 1.30}, "광역 파멸+약화"),
    ("forge", "WROUGHT_IN_WAR", 7, {"damage_single": 7}, "피해+제련"),
    ("forge", "BULWARK", 10, {"block": 12}, "방어+제련"),
    ("forge", "SPOILS_OF_BATTLE", 5, {"draw": 2}, "제련+드로우"),
    ("summon", "AFTERLIFE", 6, {}, "소멸+소환"),
    ("summon", "BODYGUARD", 5, {}, "기본 카드 순수 소환"),
    ("summon", "PULL_AGGRO", 4, {"block": 7}, "소환+방어"),
    ("summon", "REANIMATE", 20, {}, "고코스트 소멸+소환"),
    ("hp_loss", "BLOODLETTING", 3, {"energy": 2}, "체력 손실+에너지"),
    ("hp_loss", "BREAKTHROUGH", 1, {"damage_all": 9}, "체력 손실+광역 피해"),
    ("hp_loss", "BLOOD_WALL", 2, {"block": 16}, "체력 손실+방어"),
    ("hp_loss", "HEMOKINESIS", 2, {"damage_single": 15}, "체력 손실+피해"),
    ("shiv", "CLOAK_AND_DAGGER", 1, {"block": 6}, "방어+단도"),
    ("shiv", "LEADING_STRIKE", 2, {"damage_single": 3}, "피해+단도"),
    ("shiv", "BLADE_DANCE", 3, {}, "소멸+단도"),
    ("lightning", "BALL_LIGHTNING", 1, {"damage_single": 7}, "피해+번개"),
    ("lightning", "ZAP", 1, {}, "기본 카드 순수 번개"),
    ("frost", "COLD_SNAP", 1, {"damage_single": 6}, "피해+냉기"),
    ("frost", "COOLHEADED", 1, {"draw": 1}, "냉기+드로우"),
    ("frost", "GLACIER", 2, {"block": 6}, "방어+냉기"),
    ("dark", "SHADOW_SHIELD", 1, {"block": 11}, "방어+암흑"),
    ("glass", "GLASSWORK", 1, {"block": 5}, "방어+유리"),
    ("glass", "REFRACT", 2, {"damage_single": 18}, "피해+유리"),
    ("strength", "INFLAME", 2, {}, "순수 힘"),
    ("dexterity", "FOOTWORK", 2, {}, "순수 민첩"),
    ("plating", "STONE_ARMOR", 4, {}, "순수 도금"),
]


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def benchmark(card: dict[str, Any], table: dict[str, dict[str, dict[int, float]]] = BENCHMARKS_V2) -> float | None:
    scope = "character" if card["pool"]["is_playable_character"] else card["pool"]["key"]
    if scope not in table or card["cost"]["star"] is not None:
        return None
    energy = card["cost"]["energy"]
    if not isinstance(energy, int) or energy < 0:
        return None
    return table.get(scope, {}).get(card["rarity"]["key"], {}).get(energy)


def keyword_adjustment(card: dict[str, Any]) -> float | None:
    total = 0.0
    for keyword in card["keyword_ids"]:
        value = v1.KEYWORD_POINTS.get(keyword)
        if value is None:
            return None
        total += value
    return total


def score_features(features: dict[str, float]) -> float:
    return sum(VALUES_V2[name] * quantity for name, quantity in features.items())


def add_feature(features: dict[str, float], name: str, amount: float) -> None:
    features[name] = features.get(name, 0.0) + amount


def parse_simple_card(card: dict[str, Any]) -> tuple[dict[str, float] | None, str]:
    """Parse immediate, fixed effects only; reject scenario-dependent clauses."""
    features: dict[str, float] = {}
    for clause in v1.clauses(card["text"]["en"]["description"]):
        if re.search(r"\b(if|whenever|every|for each|equal to|random|next turn|at the start|at the end|until|this turn|\bX\b)\b", clause, re.I):
            return None, f"conditional:{clause}"

        match = re.fullmatch(r"Deal (\d+) damage(?: to ALL enemies)?(?: (\d+) times| (twice))?\.", clause, re.I)
        if match:
            hits = int(match.group(2) or (2 if match.group(3) else 1))
            name = "damage_all" if "ALL enemies" in clause else "damage_single"
            add_feature(features, name, int(match.group(1)) * hits)
            continue
        match = re.fullmatch(r"Gain (\d+) Block\.", clause, re.I)
        if match:
            add_feature(features, "block", int(match.group(1)))
            continue
        match = re.fullmatch(r"Draw (\d+) cards?\.", clause, re.I)
        if match:
            add_feature(features, "draw", int(match.group(1)))
            continue
        match = re.fullmatch(r"Discard (\d+) cards?\.", clause, re.I)
        if match:
            add_feature(features, "discard_selected", int(match.group(1)))
            continue
        match = re.fullmatch(r"Gain (\d+) (Energy|Stars)\.", clause, re.I)
        if match:
            add_feature(features, "energy" if match.group(2).lower() == "energy" else "stars", int(match.group(1)))
            continue
        match = re.fullmatch(r"Lose (\d+) HP\.", clause, re.I)
        if match:
            add_feature(features, "hp_loss", int(match.group(1)))
            continue

        match = re.fullmatch(r"Apply (\d+) (Weak|Vulnerable|Poison|Doom)( to ALL enemies)?\.", clause, re.I)
        if match:
            quantity = int(match.group(1)) * (v1.AOE_MULTIPLIER if match.group(3) else 1.0)
            add_feature(features, match.group(2).lower(), quantity)
            continue
        match = re.fullmatch(r"Apply (\d+) Weak and Vulnerable( to ALL enemies)?\.", clause, re.I)
        if match:
            quantity = int(match.group(1)) * (v1.AOE_MULTIPLIER if match.group(2) else 1.0)
            add_feature(features, "weak", quantity)
            add_feature(features, "vulnerable", quantity)
            continue
        match = re.fullmatch(r"Apply (\d+) Doom and (\d+) Weak to ALL enemies\.", clause, re.I)
        if match:
            add_feature(features, "doom", int(match.group(1)) * v1.AOE_MULTIPLIER)
            add_feature(features, "weak", int(match.group(2)) * v1.AOE_MULTIPLIER)
            continue

        match = re.fullmatch(r"(Forge|Summon) (\d+)\.", clause, re.I)
        if match:
            add_feature(features, match.group(1).lower(), int(match.group(2)))
            continue
        match = re.fullmatch(r"Channel (\d+) (Lightning|Frost|Dark|Glass|Plasma)\.", clause, re.I)
        if match:
            add_feature(features, match.group(2).lower(), int(match.group(1)))
            continue
        match = re.fullmatch(r"Gain (\d+) (Strength|Dexterity|Focus|Vigor|Plating|Thorns)\.", clause, re.I)
        if match:
            add_feature(features, match.group(2).lower(), int(match.group(1)))
            continue

        match = re.fullmatch(r"Add (\d+) Shivs? into your Hand\.", clause, re.I)
        if match:
            add_feature(features, "shiv", int(match.group(1)))
            continue
        match = re.fullmatch(r"Add (?:a|an|1) (Soul|Wound|Dazed|Burn|Void|Slimed|Debris)(?:s)? into your (?:Hand|Draw Pile|Discard Pile)\.", clause, re.I)
        if match:
            add_feature(features, match.group(1).lower(), 1)
            continue
        match = re.fullmatch(r"Add (\d+) (Souls|Wounds) into your (?:Hand|Draw Pile|Discard Pile)\.", clause, re.I)
        if match:
            add_feature(features, "soul" if match.group(2).lower() == "souls" else "wound", int(match.group(1)))
            continue

        return None, f"unparsed:{clause}"
    return (features, "ok") if features else (None, "empty")


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return math.nan
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def main() -> None:
    cards = json.loads(SOURCE.read_text(encoding="utf-8"))
    by_id = {card["id"]: card for card in cards}
    OUTPUT.mkdir(parents=True, exist_ok=True)

    evidence_rows: list[dict[str, Any]] = []
    implied_by_effect: dict[str, list[float]] = defaultdict(list)
    for effect, card_id, quantity, others, note in EVIDENCE_SPECS:
        card = by_id[card_id]
        base = benchmark(card)
        base_v1 = benchmark(card, v1.BENCHMARK_PRIORS)
        keyword = keyword_adjustment(card)
        if base is None or keyword is None:
            continue
        other_score = score_features(others)
        implied = (base - keyword - other_score) / quantity
        implied_by_effect[effect].append(implied)
        evidence_rows.append(
            {
                "effect": effect,
                "card_id": card_id,
                "name_ko": card["name"]["ko"],
                "name_en": card["name"]["en"],
                "pool": card["pool"]["key"],
                "rarity": card["rarity"]["key"],
                "cost": v1.cost_signature(card),
                "benchmark_v1": base_v1,
                "benchmark_v2": base,
                "keyword_adjustment": keyword,
                "target_quantity": round(quantity, 3),
                "other_effect_score_v2": round(other_score, 3),
                "implied_unit_value": round(implied, 3),
                "chosen_unit_value_v2": VALUES_V2[effect],
                "difference_from_chosen": round(implied - VALUES_V2[effect], 3),
                "note": note,
            }
        )

    calibration_rows: list[dict[str, Any]] = []
    for effect, chosen in VALUES_V2.items():
        values = implied_by_effect.get(effect, [])
        calibration_rows.append(
            {
                "effect": effect,
                "unit_value_v1": {
                    "draw": 2.0,
                    "energy": 2.5,
                    "stars": 1.5,
                    "weak": 1.5,
                    "vulnerable": 1.5,
                    "poison": 1.0,
                    "doom": 0.25,
                    "forge": 0.25,
                    "summon": 1.0,
                    "hp_loss": "",
                    "shiv": 2.0,
                }.get(effect, VALUES_V2[effect]),
                "unit_value_v2": chosen,
                "evidence_count": len(values),
                "implied_median": round(statistics.median(values), 3) if values else "",
                "implied_q1": round(percentile(values, 0.25), 3) if values else "",
                "implied_q3": round(percentile(values, 0.75), 3) if values else "",
                "confidence": CONFIDENCE.get(effect, "low_prior"),
            }
        )

    residual_rows: list[dict[str, Any]] = []
    rejection_counts: Counter[str] = Counter()
    for card in cards:
        base = benchmark(card)
        base_v1 = benchmark(card, v1.BENCHMARK_PRIORS)
        keyword = keyword_adjustment(card)
        if base is None or keyword is None:
            continue
        features, status = parse_simple_card(card)
        if features is None:
            rejection_counts[status.split(":", 1)[0]] += 1
            continue
        effect_score = score_features(features) + keyword
        residual_rows.append(
            {
                "card_id": card["id"],
                "name_ko": card["name"]["ko"],
                "name_en": card["name"]["en"],
                "pool": card["pool"]["key"],
                "rarity": card["rarity"]["key"],
                "cost": v1.cost_signature(card),
                "type": card["type"]["key"],
                "keywords": "|".join(card["keyword_ids"]),
                "features": json.dumps(features, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                "body_score_v2": round(score_features(features), 3),
                "keyword_adjustment": round(keyword, 3),
                "effect_score_v2": round(effect_score, 3),
                "benchmark_v1": base_v1,
                "benchmark_v2": base,
                "residual_v2": round(effect_score - base, 3),
                "absolute_residual": round(abs(effect_score - base), 3),
            }
        )

    residuals = [float(row["residual_v2"]) for row in residual_rows]
    abs_residuals = [abs(value) for value in residuals]
    outliers = sorted(residual_rows, key=lambda row: float(row["absolute_residual"]), reverse=True)[:20]
    group_rows: list[dict[str, Any]] = []
    for dimension in ("pool", "rarity", "cost", "type"):
        grouped: dict[str, list[float]] = defaultdict(list)
        for row in residual_rows:
            grouped[str(row[dimension])].append(float(row["residual_v2"]))
        for group, values in sorted(grouped.items()):
            group_rows.append(
                {
                    "dimension": dimension,
                    "group": group,
                    "card_count": len(values),
                    "mean_residual": round(statistics.mean(values), 3),
                    "median_residual": round(statistics.median(values), 3),
                    "median_absolute_residual": round(statistics.median(abs(value) for value in values), 3),
                    "rmse": round(math.sqrt(sum(value * value for value in values) / len(values)), 3),
                }
            )
    summary = {
        "source": str(SOURCE.relative_to(ROOT)),
        "evidence_card_equations": len(evidence_rows),
        "calibrated_effects_with_direct_evidence": sum(bool(values) for values in implied_by_effect.values()),
        "simple_cards_scored": len(residual_rows),
        "simple_card_median_residual": round(statistics.median(residuals), 3),
        "simple_card_median_absolute_residual": round(statistics.median(abs_residuals), 3),
        "simple_card_rmse": round(math.sqrt(sum(value * value for value in residuals) / len(residuals)), 3),
        "within_1_point": sum(value <= 1 for value in abs_residuals),
        "within_2_points": sum(value <= 2 for value in abs_residuals),
        "rejection_counts": dict(rejection_counts),
        "largest_outliers": [
            {"card_id": row["card_id"], "name_ko": row["name_ko"], "residual": row["residual_v2"]}
            for row in outliers
        ],
    }

    write_csv(
        OUTPUT / "calibration_evidence.csv",
        evidence_rows,
        ["effect", "card_id", "name_ko", "name_en", "pool", "rarity", "cost", "benchmark_v1", "benchmark_v2", "keyword_adjustment", "target_quantity", "other_effect_score_v2", "implied_unit_value", "chosen_unit_value_v2", "difference_from_chosen", "note"],
    )
    write_csv(
        OUTPUT / "effect_scores_v2.csv",
        calibration_rows,
        ["effect", "unit_value_v1", "unit_value_v2", "evidence_count", "implied_median", "implied_q1", "implied_q3", "confidence"],
    )
    write_csv(
        OUTPUT / "simple_card_residuals_v2.csv",
        residual_rows,
        ["card_id", "name_ko", "name_en", "pool", "rarity", "cost", "type", "keywords", "features", "body_score_v2", "keyword_adjustment", "effect_score_v2", "benchmark_v1", "benchmark_v2", "residual_v2", "absolute_residual"],
    )
    benchmark_change_specs = [
        ("colorless", "Uncommon", 0, "Finesse/Flash of Steel/Production cluster at 4.0–4.25; Dramatic Entrance is an upper outlier"),
        ("colorless", "Rare", 0, "Master of Strategy supports a substantially lower E0 colorless prior; still low confidence"),
        ("character", "Rare", 1, "Conflagration and Defragment reject the unsupported 8-point prior; low sample"),
        ("character", "Rare", 3, "Reanimate and Ice Lance center near 20 points; replaces unsupported prior"),
        ("colorless", "Rare", 3, "Eternal Armor with Plating value cross-calibrated from Stone Armor; one-card anchor"),
    ]
    benchmark_change_rows = [
        {
            "scope": scope,
            "rarity": rarity,
            "energy_cost": energy,
            "benchmark_v1": v1.BENCHMARK_PRIORS[scope][rarity][energy],
            "benchmark_v2": BENCHMARKS_V2[scope][rarity][energy],
            "reason": reason,
        }
        for scope, rarity, energy, reason in benchmark_change_specs
    ]
    write_csv(
        OUTPUT / "benchmark_changes_v2.csv",
        benchmark_change_rows,
        ["scope", "rarity", "energy_cost", "benchmark_v1", "benchmark_v2", "reason"],
    )
    write_csv(
        OUTPUT / "residual_groups_v2.csv",
        group_rows,
        ["dimension", "group", "card_count", "mean_residual", "median_residual", "median_absolute_residual", "rmse"],
    )
    write_csv(
        OUTPUT / "outliers_v2.csv",
        outliers,
        ["card_id", "name_ko", "name_en", "pool", "rarity", "cost", "type", "keywords", "features", "body_score_v2", "keyword_adjustment", "effect_score_v2", "benchmark_v1", "benchmark_v2", "residual_v2", "absolute_residual"],
    )
    (OUTPUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    report = f"""# 카드 가치 모델 v2 — 단순 복합효과 보정

## 결과

피해·방어 계수를 고정하고 **{len(evidence_rows)}개 카드 방정식**으로 추가 효과의 암시 단위값을 계산했다. 조건·X·반복 파워 없이 분해 가능한 카드 **{len(residual_rows)}장**을 v2로 채점했다.

- 잔차 중앙값: **{statistics.median(residuals):.2f}점**
- 절대 잔차 중앙값: **{statistics.median(abs_residuals):.2f}점**
- RMSE: **{summary['simple_card_rmse']:.2f}점**
- ±1점 이내: **{summary['within_1_point']}/{len(residual_rows)}장**
- ±2점 이내: **{summary['within_2_points']}/{len(residual_rows)}장**

## v1에서 바뀐 핵심 계수

| 효과 | v1 | v2 | 판단 |
|---|---:|---:|---|
| 드로우 1장 | 2.00 | 1.75 | 단일 드로우와 순수 다중 드로우의 편차가 커 중간값 사용 |
| 약화 1 | 1.50 | 2.10 | Sucker Punch, Defy, Leg Sweep에서 상향 |
| 취약 1 | 1.50 | 2.00 | Beam Cell, Fear, Taunt, Tremble에서 상향 |
| 파멸 1 | 0.25 | 0.27 | Scourge, Negative Pulse, Deathbringer 중앙값 |
| 제련 1 | 0.25 | 0.33 | Wrought in War, Bulwark, Spoils of Battle 중앙 영역 |
| 소환 1 | 1.00 | 1.10 | Afterlife, Pull Aggro, Reanimate가 일관됨 |
| 체력 손실 1 | 미정 | -0.55 | 4개 직접 표본의 중앙값과 사분위 범위 |
| 단도 생성 1장 | 2.00 | 1.90 | Cloak and Dagger, Leading Strike, Blade Dance 교차 일치 |

피해 0.50, 방어 0.60, 광역 ×1.30, 에너지 2.50, 별 1.50, 중독 1.00은 유지했다.

무색 고급 0코스트 기준은 **6.50→4.25점**, 무색 희귀 0코스트의 낮은 신뢰도 사전값은 **7.50→5.00점**으로 내렸다. 전자는 Finesse 4.15, Flash of Steel 4.25, Production 4.00의 독립 표본이 근거다. Dramatic Entrance는 같은 셀에서 +2.40점인 상단 이상치로 남는다.

직접 앵커가 없던 캐릭터 희귀 1코스트는 **8.0→6.5점**, 희귀 3코스트는 **22→20점**으로 수정했다. 무색 희귀 3코스트는 Eternal Armor 한 장만을 근거로 **24→14.5점**으로 내렸으므로 신뢰도가 가장 낮다.

## 해석

`calibration_evidence.csv`의 암시값은 각 카드가 기준점과 정확히 같다고 가정했을 때 필요한 값이다. 카드 자체의 의도된 강약과 기존 기준점 오차가 섞이므로, 평균 하나를 정답으로 보지 않고 중앙값·사분위 범위와 개별 근거를 함께 저장했다.

드로우는 특히 **단일 캔트립과 순수 다중 드로우가 같은 선형 단위값을 공유하지 않는다**는 신호가 강하다. 다음 버전에서는 `첫 1장/추가 1장` 또는 `순카드이득` 항으로 분리해야 한다. 별 획득도 Regent 일반 카드에서 기준보다 반복적으로 높은 잔차가 나와 캐릭터별 기준점 검사가 필요하다.

가장 큰 미해결 잔차는 Rainbow **-8.20**, Terraforming **-3.50**, Scare **-3.27**, Overclock **-3.00**, Shockwave **+2.99**다. Rainbow는 서로 다른 세 구체를 한꺼번에 만드는 조합 가치, Terraforming은 활력 단위값, Overclock은 Burn 페널티가 현재 선형값으로 설명되지 않는다. 이 항목들은 계수를 전체적으로 흔들기보다 다음 상호작용 보정 대상으로 격리했다.

## 파일

- `calibration_evidence.csv`: 효과별 카드 방정식, 암시 단위값, 채택값과 차이
- `effect_scores_v2.csv`: v1→v2 계수와 표본 수·중앙값·사분위
- `simple_card_residuals_v2.csv`: 자동 분해 가능한 카드별 효과 점수와 잔차
- `benchmark_changes_v2.csv`: v1에서 수정한 무색 기준점과 근거
- `residual_groups_v2.csv`: 카드 풀·희귀도·코스트·유형별 잔차 집계
- `outliers_v2.csv`: 절대 잔차 상위 20장과 분해식
- `summary.json`: 적합도와 최대 이상치
"""
    (OUTPUT / "README.md").write_text(report, encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
