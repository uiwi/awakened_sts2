#!/usr/bin/env python3
"""Build compact JSON payloads for the card evaluation web pages."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ANALYSIS = ROOT / "analysis/card_value_v5"
PUBLIC_DATA = ROOT / "web/public/data"
BASIC_REFERENCE_EXCLUDED_IDS = {
    "DEFEND_DEFECT", "DEFEND_IRONCLAD", "DEFEND_NECROBINDER", "DEFEND_REGENT", "DEFEND_SILENT",
    "STRIKE_DEFECT", "STRIKE_IRONCLAD", "STRIKE_NECROBINDER", "STRIKE_REGENT", "STRIKE_SILENT",
}
TIER_BY_BAND = {
    "very_above_budget": "S",
    "above_budget": "A",
    "on_budget": "B",
    "below_budget": "C",
    "very_below_budget": "D",
}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def number(value: str | int | float | None) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def number_or_text(value: str) -> float | str | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return value


def energy_label(card: dict[str, Any]) -> str:
    cost = card["cost"]
    if cost.get("is_x_cost"):
        label = "X"
    else:
        energy = cost.get("energy")
        label = str(energy) if isinstance(energy, int) and energy >= 0 else "—"

    star = cost.get("star")
    if isinstance(star, int) and star > 0:
        return f"{label} (+{star}별)"
    if cost.get("is_x_star_cost"):
        return f"{label} (+X별)"
    return label


def main() -> None:
    cards = json.loads((PUBLIC_DATA / "cards.json").read_text(encoding="utf-8"))
    cards_by_id = {card["id"]: card for card in cards}
    rarity_by_key = {card["rarity"]["key"]: card["rarity"] for card in cards}
    score_rows = read_csv(ANALYSIS / "card_stability_v5.csv")

    evaluations: list[dict[str, Any]] = []
    for row in score_rows:
        if row["card_id"] in BASIC_REFERENCE_EXCLUDED_IDS:
            continue
        card = cards_by_id[row["card_id"]]
        comparable = row["benchmark_comparable"].lower() == "true"
        evaluations.append(
            {
                "id": row["card_id"],
                "name": card["name"],
                "pool": card["pool"],
                "type": card["type"],
                "rarity": rarity_by_key[row["rarity"]],
                "printed_rarity": card["rarity"],
                "cost": card["cost"],
                "energy_label": energy_label(card),
                "description_ko": card["text"]["ko"]["description_plain"],
                "keywords": card["keywords"],
                "score": {
                    "low": number(row["score_low"]),
                    "baseline": number(row["score_baseline"]),
                    "high": number(row["score_high"]),
                },
                "benchmark": number(row["benchmark_v4"]) if comparable else None,
                "benchmark_method": row["benchmark_method"],
                "benchmark_comparable": comparable,
                "value_index": {
                    "low": number(row["value_index_low"]) if comparable else None,
                    "baseline": number(row["value_index_baseline"]) if comparable else None,
                    "high": number(row["value_index_high"]) if comparable else None,
                },
                "confidence": {
                    "effect": row["confidence_grade"],
                    "benchmark": row["benchmark_confidence"],
                    "evaluation": row["evaluation_confidence"],
                },
                "balance_band": row["balance_band_baseline"],
                "tier": TIER_BY_BAND.get(row["balance_band_baseline"]),
                "interval_class": row["balance_interval_class"],
                "stability": {
                    "label": row["rank_stability_label"],
                    "combined": number(row["combined_rank_stability"]),
                    "band": number(row["combined_band_stability"]),
                },
                "rank": {
                    "baseline": int(row["rank_baseline_scenario"]),
                    "low": int(row["rank_low_scenario"]),
                    "high": int(row["rank_high_scenario"]),
                },
                "rules": row["rules"].split("|") if row["rules"] else [],
            }
        )

    summary = json.loads((ANALYSIS / "summary.json").read_text(encoding="utf-8"))
    summary["evaluation_table_card_count"] = len(evaluations)
    effect_scores = []
    for row in read_csv(ANALYSIS / "effect_score_table_v5.csv"):
        effect_scores.append(
            {
                "category": row["category"],
                "effect": row["effect"],
                "low": number_or_text(row["low"]),
                "baseline": number_or_text(row["baseline"]),
                "high": number_or_text(row["high"]),
                "confidence": row["confidence"],
                "evidence_count": int(row["evidence_count"]) if row["evidence_count"] else None,
                "basis": row["basis"],
            }
        )
    benchmarks = []
    for row in read_csv(ANALYSIS / "benchmark_table_v5.csv"):
        benchmarks.append(
            {
                "scope": row["scope"],
                "rarity": row["rarity"],
                "cost": row["cost_component"],
                "points": number(row["points"]),
                "confidence": row["confidence"],
                "basis": row["basis"],
            }
        )

    model = {
        "version": 5,
        "summary": summary,
        "formula": "value_index = effect_score - rarity_energy_benchmark",
        "balance_thresholds": [
            {"key": "very_above_budget", "tier": "S", "label": "S 티어", "min": 4.0, "max": None},
            {"key": "above_budget", "tier": "A", "label": "A 티어", "min": 2.0, "max": 4.0},
            {"key": "on_budget", "tier": "B", "label": "B 티어", "min": -2.0, "max": 2.0},
            {"key": "below_budget", "tier": "C", "label": "C 티어", "min": -4.0, "max": -2.0},
            {"key": "very_below_budget", "tier": "D", "label": "D 티어", "min": None, "max": -4.0},
        ],
        "effect_scores": effect_scores,
        "benchmarks": benchmarks,
    }

    PUBLIC_DATA.mkdir(parents=True, exist_ok=True)
    outputs = {
        "card-evaluations.json": evaluations,
        "evaluation-model.json": model,
    }
    for filename, payload in outputs.items():
        destination = PUBLIC_DATA / filename
        destination.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        print(f"Wrote {destination.relative_to(ROOT)} ({destination.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
