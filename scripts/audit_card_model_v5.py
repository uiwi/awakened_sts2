#!/usr/bin/env python3
"""Audit V4 coverage, calibration and ranking sensitivity.

V5 does not add another opaque scoring layer.  It freezes the V4 effect model,
checks that every published total is reconstructible from evidence, perturbs
the calibrated unit values, and reports which rankings survive those changes.
"""

from __future__ import annotations

import csv
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import analyze_card_values as v1
import calibrate_effect_values_v2 as v2
import score_card_scenarios_v3 as v3
import score_all_cards_v4 as v4


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "web/public/data/cards.json"
V2_OUTPUT = ROOT / "analysis/card_value_v2"
V4_OUTPUT = ROOT / "analysis/card_value_v4"
OUTPUT = ROOT / "analysis/card_value_v5"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def score_snapshot(cards: list[dict[str, Any]]) -> dict[str, float]:
    scores: dict[str, float] = {}
    for card in cards:
        total = v3.ZERO
        for clause in v1.clauses(card["text"]["en"]["description"]):
            total += v4.v4_clause_score(clause, card).score
        for result in v4.keyword_score(card):
            total += result.score
        for result in v4.printed_cost_score(card):
            total += result.score
        features, _ = v2.parse_simple_card(card)
        interaction, _ = v3.interaction_score(card, features)
        total += interaction
        if total == v3.ZERO and not v1.clauses(card["text"]["en"]["description"]):
            total += v4.negative_range(0.5, 1.25, 2.0)
        scores[card["id"]] = total.base
    return scores


def perturbed_snapshot(
    cards: list[dict[str, Any]],
    value_factors: dict[str, float] | None = None,
    draw_factor: float = 1.0,
    osty_damage_factor: float = 1.0,
    star_cost_factor: float = 1.0,
) -> dict[str, float]:
    saved_values = dict(v2.VALUES_V2)
    saved_draw = (v3.DRAW_FIRST, v3.DRAW_ADDITIONAL)
    saved_osty = v3.LOW_CONFIDENCE_VALUES["damage_osty"]
    saved_star_cost = v4.STAR_COST_POINT
    try:
        for key, factor in (value_factors or {}).items():
            v2.VALUES_V2[key] = saved_values[key] * factor
        v3.DRAW_FIRST = saved_draw[0] * draw_factor
        v3.DRAW_ADDITIONAL = saved_draw[1] * draw_factor
        v3.LOW_CONFIDENCE_VALUES["damage_osty"] = saved_osty * osty_damage_factor
        v4.STAR_COST_POINT = saved_star_cost * star_cost_factor
        return score_snapshot(cards)
    finally:
        v2.VALUES_V2.clear()
        v2.VALUES_V2.update(saved_values)
        v3.DRAW_FIRST, v3.DRAW_ADDITIONAL = saved_draw
        v3.LOW_CONFIDENCE_VALUES["damage_osty"] = saved_osty
        v4.STAR_COST_POINT = saved_star_cost


def rank_map(cards: list[dict[str, Any]], scores: dict[str, float], groups: dict[str, str]) -> dict[str, int]:
    by_pool: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for card in cards:
        by_pool[groups[card["id"]]].append(card)
    ranks: dict[str, int] = {}
    for pool_cards in by_pool.values():
        ordered = sorted(pool_cards, key=lambda card: (-scores[card["id"]], card["id"]))
        ranks.update({card["id"]: index for index, card in enumerate(ordered, start=1)})
    return ranks


def balance_band(value: float) -> str:
    if value > 4.0:
        return "very_above_budget"
    if value > 2.0:
        return "above_budget"
    if value >= -2.0:
        return "on_budget"
    if value >= -4.0:
        return "below_budget"
    return "very_below_budget"


def metric_row(label: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    residuals = [float(row["residual_baseline"]) for row in rows if row["benchmark_comparable"] == "True"]
    if not residuals:
        return {
            "group": label, "count": 0, "median_residual": "", "median_absolute_residual": "",
            "rmse": "", "within_1": 0, "within_2": 0, "interval_contains_benchmark": 0,
            "interval_coverage": "",
        }
    comparable = [row for row in rows if row["benchmark_comparable"] == "True"]
    inside = sum(
        float(row["score_low"]) <= float(row["benchmark_v4"]) <= float(row["score_high"])
        for row in comparable
    )
    return {
        "group": label,
        "count": len(residuals),
        "median_residual": round(statistics.median(residuals), 3),
        "median_absolute_residual": round(statistics.median(map(abs, residuals)), 3),
        "rmse": round(math.sqrt(sum(value * value for value in residuals) / len(residuals)), 3),
        "within_1": sum(abs(value) <= 1 for value in residuals),
        "within_2": sum(abs(value) <= 2 for value in residuals),
        "interval_contains_benchmark": inside,
        "interval_coverage": round(inside / len(residuals), 3),
    }


def build_effect_table(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for source in read_csv(V2_OUTPUT / "effect_scores_v2.csv"):
        chosen = float(source["unit_value_v2"])
        observed = [float(value) for value in (source["implied_q1"], source["implied_q3"]) if value]
        low = min([chosen, *observed])
        high = max([chosen, *observed])
        rows.append(
            {
                "category": "atomic_unit", "effect": source["effect"], "low": low,
                "baseline": chosen, "high": high, "confidence": source["confidence"],
                "evidence_count": source["evidence_count"], "basis": "V2 anchor/card-pair calibration",
            }
        )
    rows.extend(
        [
            {"category": "draw_curve", "effect": "first_draw", "low": 1.062, "baseline": v3.DRAW_FIRST, "high": 2.042, "confidence": "medium", "evidence_count": 8, "basis": "nonlinear V3 draw curve"},
            {"category": "draw_curve", "effect": "each_additional_draw", "low": 1.50, "baseline": v3.DRAW_ADDITIONAL, "high": 2.50, "confidence": "medium", "evidence_count": 8, "basis": "multi-draw residual correction"},
            {"category": "target_modifier", "effect": "all_enemies_multiplier", "low": 1.20, "baseline": v1.AOE_MULTIPLIER, "high": 1.40, "confidence": "medium", "evidence_count": 27, "basis": "strict damage/Block anchors"},
            {"category": "timing_modifier", "effect": "next_turn_multiplier", "low": 0.70, "baseline": v1.DELAY_MULTIPLIER, "high": 0.90, "confidence": "low", "evidence_count": "", "basis": "delay prior"},
            {"category": "printed_cost_effect", "effect": "spend_1_star", "low": -2.50, "baseline": -v4.STAR_COST_POINT, "high": -1.50, "confidence": "low", "evidence_count": sum(isinstance(card["cost"].get("star"), int) and card["cost"].get("star") > 0 for card in cards), "basis": "printed Star payment is subtracted from the card effect score"},
            {"category": "printed_cost_effect", "effect": "spend_x_stars", "low": -8.00, "baseline": -4.00, "high": -2.00, "confidence": "low", "evidence_count": sum(bool(card["cost"].get("is_x_star_cost")) for card in cards), "basis": "-2 points per Star across X=1/2/4 scenarios"},
        ]
    )
    for keyword, value in v1.KEYWORD_POINTS.items():
        rows.append(
            {
                "category": "keyword", "effect": keyword.lower(), "low": value if value is not None else "scenario",
                "baseline": value if value is not None else "scenario", "high": value if value is not None else "scenario",
                "confidence": "medium" if keyword == "ETERNAL" else "low", "evidence_count": "", "basis": "keyword opportunity/cleanup model",
            }
        )
    for name, values in v3.SCENARIOS.items():
        rows.append(
            {
                "category": "scenario_count", "effect": name, "low": values[0], "baseline": values[1],
                "high": values[2], "confidence": "low", "evidence_count": "", "basis": "explicit conservative/baseline/optimistic assumption",
            }
        )
    return rows


def main() -> None:
    # Rebuild V4 first so the audit cannot accidentally read stale output.
    v4.main()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    cards = json.loads(SOURCE.read_text(encoding="utf-8"))
    card_by_id = {card["id"]: card for card in cards}
    fixed_star_cost_cards = sum(
        isinstance(card["cost"].get("star"), int) and card["cost"].get("star") > 0
        for card in cards
    )
    x_star_cost_cards = sum(bool(card["cost"].get("is_x_star_cost")) for card in cards)
    score_rows = read_csv(V4_OUTPUT / "card_scores_v4.csv")
    evidence_rows = read_csv(V4_OUTPUT / "effect_evidence_v4.csv")
    family_catalog = read_csv(ROOT / "analysis/card_value_v1/effect_catalog.csv")
    family_catalog.append(
        {
            "family": "printed_cost",
            "family_ko": "인쇄 추가 비용",
            "definition": "카드 사용 시 지불하는 별 비용을 음수 효과로 분리",
            "card_count": fixed_star_cost_cards + x_star_cost_cards,
            "effect_instance_count": fixed_star_cost_cards + x_star_cost_cards,
            "unique_template_count": 2,
        }
    )
    v1_benchmark_confidence = {
        (row["scope"], row["rarity"], int(row["energy_cost"])): row["confidence"].replace("low_prior", "low")
        for row in read_csv(ROOT / "analysis/card_value_v1/benchmark_table_v1.csv")
    }
    benchmark_confidence_overrides = {
        ("character", "Uncommon", 0): "medium",
        ("colorless", "Uncommon", 0): "medium",
        ("colorless", "Rare", 0): "low",
        ("character", "Rare", 1): "medium",
        ("character", "Rare", 3): "medium",
        ("colorless", "Rare", 3): "low",
    }

    # Arithmetic and coverage invariants.
    evidence_totals: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
    for row in evidence_rows:
        for index, field in enumerate(("score_low", "score_baseline", "score_high")):
            evidence_totals[row["card_id"]][index] += float(row[field])
    sum_mismatches = []
    for row in score_rows:
        published = [float(row[field]) for field in ("score_low", "score_baseline", "score_high")]
        if any(abs(published[index] - evidence_totals[row["card_id"]][index]) > 0.002 for index in range(3)):
            sum_mismatches.append(row["card_id"])
    ordered = all(float(row["score_low"]) <= float(row["score_baseline"]) <= float(row["score_high"]) for row in score_rows)
    finite = all(
        math.isfinite(float(row[field]))
        for row in score_rows
        for field in ("score_low", "score_baseline", "score_high", "confidence_score", "rank_stability")
    )
    fallback_effects = sum(row["confidence"] == "fallback" for row in evidence_rows)
    source_clause_count = sum(len(v1.clauses(card["text"]["en"]["description"])) for card in cards)
    evidence_clause_count = sum(row["source"] == "clause" for row in evidence_rows)

    # Unit-value perturbations.  These are one-factor-at-a-time snapshots, not
    # claims that all uncertainties are statistically independent.
    base_snapshot = score_snapshot(cards)
    variants: list[tuple[str, dict[str, float], float, float, float]] = []
    groups = {
        "damage": ["damage_single", "damage_all"],
        "block": ["block"],
        "energy": ["energy"],
        "debuff": ["weak", "vulnerable", "poison", "doom"],
        "class": ["stars", "forge", "summon", "shiv", "soul", "lightning", "frost", "dark", "glass", "plasma", "focus"],
        "penalty": ["hp_loss", "wound", "dazed", "slimed", "void", "burn", "debris"],
    }
    magnitudes = {"damage": 0.10, "block": 0.10, "energy": 0.15, "debuff": 0.20, "class": 0.20, "penalty": 0.20}
    for group, names in groups.items():
        delta = magnitudes[group]
        for direction, factor in (("low", 1.0 - delta), ("high", 1.0 + delta)):
            variants.append(
                (
                    f"{group}_{direction}",
                    {name: factor for name in names},
                    1.0,
                    factor if group == "class" else 1.0,
                    factor if group == "class" else 1.0,
                )
            )
    variants.extend([("draw_low", {}, 0.85, 1.0, 1.0), ("draw_high", {}, 1.15, 1.0, 1.0)])
    snapshots = {"base": base_snapshot}
    for name, factors, draw_factor, osty_factor, star_cost_factor in variants:
        snapshots[name] = perturbed_snapshot(cards, factors, draw_factor, osty_factor, star_cost_factor)

    recalculation_mismatches = [
        row["card_id"] for row in score_rows
        if abs(base_snapshot[row["card_id"]] - float(row["score_baseline"])) > 0.002
    ]
    score_by_id = {row["card_id"]: row for row in score_rows}
    ranking_groups: dict[str, str] = {}
    benchmark_by_id: dict[str, float] = {}
    benchmark_confidence_by_id: dict[str, str] = {}
    for card in cards:
        row = score_by_id[card["id"]]
        comparable = row["benchmark_comparable"] == "True"
        ranking_groups[card["id"]] = f"{card['pool']['key']}:{'budget_adjusted' if comparable else 'raw_noncomparable'}"
        benchmark_by_id[card["id"]] = float(row["benchmark_v4"]) if comparable else 0.0
        if not comparable:
            benchmark_confidence_by_id[card["id"]] = "not_comparable"
        elif card["id"] == "METEOR_STRIKE":
            benchmark_confidence_by_id[card["id"]] = "high"
        elif card["cost"].get("is_x_cost"):
            benchmark_confidence_by_id[card["id"]] = "low"
        else:
            scope = "character" if card["pool"]["is_playable_character"] else "colorless"
            key = (scope, v4.benchmark_rarity(card), int(card["cost"]["energy"]))
            benchmark_confidence_by_id[card["id"]] = benchmark_confidence_overrides.get(key, v1_benchmark_confidence.get(key, "low"))
    adjusted_snapshots = {
        name: {cid: value - benchmark_by_id[cid] for cid, value in scores.items()}
        for name, scores in snapshots.items()
    }
    parameter_ranks = {name: rank_map(cards, scores, ranking_groups) for name, scores in adjusted_snapshots.items()}
    scenario_snapshots = {
        field: {
            row["card_id"]: float(row[field]) - benchmark_by_id[row["card_id"]]
            for row in score_rows
        }
        for field in ("score_low", "score_baseline", "score_high")
    }
    scenario_ranks = {name: rank_map(cards, scores, ranking_groups) for name, scores in scenario_snapshots.items()}
    group_sizes = Counter(ranking_groups.values())

    stability_rows: list[dict[str, Any]] = []
    for card in cards:
        cid = card["id"]
        row = dict(score_by_id[cid])
        ranks = [rank[cid] for rank in parameter_ranks.values()]
        scenario_card_ranks = [rank[cid] for rank in scenario_ranks.values()]
        combined = ranks + scenario_card_ranks
        size = group_sizes[ranking_groups[cid]]
        denominator = max(1, size - 1)
        top_cut = max(1, math.ceil(size * 0.25))
        parameter_stability = 1.0 - (max(ranks) - min(ranks)) / denominator
        combined_stability = 1.0 - (max(combined) - min(combined)) / denominator
        value_low = float(row["score_low"]) - benchmark_by_id[cid]
        value_base = float(row["score_baseline"]) - benchmark_by_id[cid]
        value_high = float(row["score_high"]) - benchmark_by_id[cid]
        comparable = row["benchmark_comparable"] == "True"
        benchmark_confidence = benchmark_confidence_by_id[cid]
        if comparable:
            effect_level = {"A": 3, "B": 2, "C": 1}[row["confidence_grade"]]
            benchmark_level = {"high": 3, "medium": 2, "low": 1}[benchmark_confidence]
            evaluation_confidence = {3: "A", 2: "B", 1: "C"}[min(effect_level, benchmark_level)]
        else:
            evaluation_confidence = "raw_only"
        if comparable:
            base_band = balance_band(value_base)
            parameter_bands = [balance_band(scores[cid]) for scores in adjusted_snapshots.values()]
            combined_bands = parameter_bands + [balance_band(scores[cid]) for scores in scenario_snapshots.values()]
            parameter_band_stability: float | str = round(sum(band == base_band for band in parameter_bands) / len(parameter_bands), 3)
            combined_band_stability: float | str = round(sum(band == base_band for band in combined_bands) / len(combined_bands), 3)
            if value_low > 2.0:
                interval_class = "robustly_above_budget"
            elif value_high < -2.0:
                interval_class = "robustly_below_budget"
            elif value_low >= -2.0 and value_high <= 2.0:
                interval_class = "contained_on_budget"
            else:
                interval_class = "scenario_overlaps_budget"
        else:
            base_band = "not_comparable"
            parameter_band_stability = ""
            combined_band_stability = ""
            interval_class = "not_comparable"
        row.update(
            {
                "ranking_group": ranking_groups[cid],
                "benchmark_confidence": benchmark_confidence, "evaluation_confidence": evaluation_confidence,
                "value_index_low": round(value_low, 3), "value_index_baseline": round(value_base, 3), "value_index_high": round(value_high, 3),
                "balance_band_baseline": base_band, "balance_interval_class": interval_class,
                "parameter_band_stability": parameter_band_stability, "combined_band_stability": combined_band_stability,
                "parameter_rank_min": min(ranks), "parameter_rank_max": max(ranks),
                "parameter_rank_span": max(ranks) - min(ranks), "parameter_rank_stability": round(parameter_stability, 3),
                "combined_rank_min": min(combined), "combined_rank_max": max(combined),
                "combined_rank_span": max(combined) - min(combined), "combined_rank_stability": round(combined_stability, 3),
                "parameter_top_quartile_frequency": round(sum(rank <= top_cut for rank in ranks) / len(ranks), 3),
                "rank_stability_label": "robust" if combined_stability >= 0.85 else ("moderate" if combined_stability >= 0.65 else "sensitive"),
            }
        )
        stability_rows.append(row)

    # Residual groups make scope/rarity/cost biases visible.
    residual_groups: list[dict[str, Any]] = [metric_row("all", score_rows)]
    for field in ("confidence_grade", "pool", "rarity", "cost"):
        for value in sorted({row[field] for row in score_rows}):
            residual_groups.append(metric_row(f"{field}:{value}", [row for row in score_rows if row[field] == value]))

    # Sensitivity of Star payment as a negative effect. The energy benchmark is
    # held fixed; only the published effect total is adjusted.
    star_cost_sensitivity: list[dict[str, Any]] = []

    def baseline_star_payment(card: dict[str, Any]) -> float:
        star = card["cost"].get("star")
        if isinstance(star, int) and star > 0:
            return float(star)
        if card["cost"].get("is_x_star_cost"):
            return float(v3.SCENARIOS["x_value"][1])
        return 0.0

    star_cards = [card for card in cards if baseline_star_payment(card) > 0]
    for star_unit in (1.50, 1.75, 2.00, 2.25, 2.50):
        for label, subset in (("all", cards), ("star_cards", star_cards)):
            residuals = []
            for card in subset:
                benchmark, _ = v4.benchmark_v4(card)
                row = score_by_id[card["id"]]
                comparable = benchmark is not None and "SLY" not in card["keyword_ids"] and "UNPLAYABLE" not in card["keyword_ids"]
                if comparable:
                    quantity = baseline_star_payment(card)
                    adjusted_score = float(row["score_baseline"]) + v4.STAR_COST_POINT * quantity - star_unit * quantity
                    residuals.append(adjusted_score - float(benchmark))
            star_cost_sensitivity.append(
                {
                    "subset": label, "star_cost_effect_point": -star_unit, "count": len(residuals),
                    "median_residual": round(statistics.median(residuals), 3),
                    "median_absolute_residual": round(statistics.median(map(abs, residuals)), 3),
                    "rmse": round(math.sqrt(sum(value * value for value in residuals) / len(residuals)), 3),
                }
            )

    # Rule-level score summaries retain examples and basis text for audit.
    rule_groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in evidence_rows:
        rule_groups[(row["rule"], row["confidence"])].append(row)
    rule_summary = []
    for (rule, confidence), rows in rule_groups.items():
        rule_summary.append(
            {
                "rule": rule, "confidence": confidence, "effect_count": len(rows),
                "card_count": len({row["card_id"] for row in rows}),
                "median_score_low": round(statistics.median(float(row["score_low"]) for row in rows), 3),
                "median_score_baseline": round(statistics.median(float(row["score_baseline"]) for row in rows), 3),
                "median_score_high": round(statistics.median(float(row["score_high"]) for row in rows), 3),
                "example_card": rows[0]["name_en"], "example_text": rows[0]["text_en"], "basis": rows[0]["basis"],
            }
        )
    rule_summary.sort(key=lambda row: (-int(row["effect_count"]), row["rule"]))

    # Final atomic/scenario table and benchmark table.
    effect_table = build_effect_table(cards)
    benchmark_table = []
    for scope, rarities in v3.BENCHMARKS_V3.items():
        for rarity, costs in rarities.items():
            if rarity == "Basic":
                continue
            for energy, value in costs.items():
                confidence = benchmark_confidence_overrides.get((scope, rarity, energy), v1_benchmark_confidence.get((scope, rarity, energy), "low"))
                benchmark_table.append({"scope": scope, "rarity": rarity, "cost_component": f"E{energy}", "points": value, "confidence": confidence, "basis": "V1 anchors with V2/V3 residual-cell corrections"})
    benchmark_table.append(
        {"scope": "character", "rarity": "Rare", "cost_component": "E5", "points": 33.0, "confidence": "high", "basis": "Meteor Strike direct effect anchor"}
    )

    comparable = [row for row in score_rows if row["benchmark_comparable"] == "True"]
    a_rows = [row for row in score_rows if row["confidence_grade"] == "A" and row["benchmark_comparable"] == "True"]
    c_rows = [row for row in score_rows if row["confidence_grade"] == "C" and row["benchmark_comparable"] == "True"]
    a_metric = metric_row("A", a_rows)
    all_metric = metric_row("all", comparable)
    c_metric = metric_row("C", c_rows)
    stability_counts = Counter(row["rank_stability_label"] for row in stability_rows)
    balance_band_counts = Counter(row["balance_band_baseline"] for row in stability_rows if row["benchmark_comparable"] == "True")
    interval_class_counts = Counter(row["balance_interval_class"] for row in stability_rows if row["benchmark_comparable"] == "True")
    evaluation_confidence_counts = Counter(row["evaluation_confidence"] for row in stability_rows)
    stable_balance_cards = sum(
        row["benchmark_comparable"] == "True" and float(row["combined_band_stability"]) >= 0.80
        for row in stability_rows
    )
    stable_high_confidence_balance_cards = sum(
        row["evaluation_confidence"] in {"A", "B"} and float(row["combined_band_stability"]) >= 0.80
        for row in stability_rows
    )
    evaluation_a_metric = metric_row("evaluation_A", [row for row in stability_rows if row["evaluation_confidence"] == "A"])
    evaluation_b_metric = metric_row("evaluation_B", [row for row in stability_rows if row["evaluation_confidence"] == "B"])
    confidence_counts = Counter(row["confidence_grade"] for row in score_rows)
    summary = {
        "card_count": len(cards),
        "evaluation_table_card_count": len(cards) - len(v4.BASIC_REFERENCE_EXCLUDED_IDS),
        "basic_strike_defend_excluded_cards": len(v4.BASIC_REFERENCE_EXCLUDED_IDS),
        "basic_cards_compared_as_common": sum(
            card["rarity"]["key"] == "Basic" and card["id"] not in v4.BASIC_REFERENCE_EXCLUDED_IDS
            for card in cards
        ),
        "source_clause_count": source_clause_count,
        "evidence_clause_count": evidence_clause_count,
        "effect_evidence_row_count": len(evidence_rows),
        "effect_family_count": len(family_catalog),
        "unique_rule_count": len(rule_groups),
        "star_cost_treatment": "negative_card_effect",
        "star_cost_effect_point": -v4.STAR_COST_POINT,
        "fixed_star_cost_cards": fixed_star_cost_cards,
        "x_star_cost_cards": x_star_cost_cards,
        "generic_fallback_effects": fallback_effects,
        "numeric_ranges_complete": len(score_rows) == len(cards),
        "ranges_ordered": ordered,
        "scores_finite": finite,
        "evidence_sum_mismatch_count": len(sum_mismatches),
        "recalculation_mismatch_count": len(recalculation_mismatches),
        "confidence_grades": dict(sorted(confidence_counts.items())),
        "evaluation_confidence": dict(sorted(evaluation_confidence_counts.items())),
        "evaluation_a_median_absolute_residual": evaluation_a_metric["median_absolute_residual"],
        "evaluation_a_rmse": evaluation_a_metric["rmse"],
        "evaluation_b_median_absolute_residual": evaluation_b_metric["median_absolute_residual"],
        "evaluation_b_rmse": evaluation_b_metric["rmse"],
        "benchmark_comparable_cards": len(comparable),
        "all_median_absolute_residual": all_metric["median_absolute_residual"],
        "all_rmse": all_metric["rmse"],
        "grade_a_median_absolute_residual": a_metric["median_absolute_residual"],
        "grade_a_rmse": a_metric["rmse"],
        "grade_c_interval_coverage": c_metric["interval_coverage"],
        "parameter_snapshot_count": len(snapshots),
        "stability_labels": dict(sorted(stability_counts.items())),
        "balance_bands": dict(sorted(balance_band_counts.items())),
        "balance_interval_classes": dict(sorted(interval_class_counts.items())),
        "combined_balance_band_stability_ge_0_80": stable_balance_cards,
        "high_confidence_balance_band_stability_ge_0_80": stable_high_confidence_balance_cards,
        "median_parameter_rank_stability": round(statistics.median(float(row["parameter_rank_stability"]) for row in stability_rows), 3),
        "median_combined_rank_stability": round(statistics.median(float(row["combined_rank_stability"]) for row in stability_rows), 3),
        "hard_validation_passed": ordered and finite and not fallback_effects and not sum_mismatches and not recalculation_mismatches and source_clause_count == evidence_clause_count,
    }

    card_fields = list(stability_rows[0].keys())
    write_csv(OUTPUT / "card_stability_v5.csv", stability_rows, card_fields)
    write_csv(OUTPUT / "effect_evidence_v5.csv", evidence_rows, list(evidence_rows[0].keys()))
    write_csv(OUTPUT / "effect_family_catalog_v5.csv", family_catalog, list(family_catalog[0].keys()))
    write_csv(
        OUTPUT / "residual_groups_v5.csv", residual_groups,
        ["group", "count", "median_residual", "median_absolute_residual", "rmse", "within_1", "within_2", "interval_contains_benchmark", "interval_coverage"],
    )
    write_csv(
        OUTPUT / "star_cost_effect_sensitivity_v5.csv", star_cost_sensitivity,
        ["subset", "star_cost_effect_point", "count", "median_residual", "median_absolute_residual", "rmse"],
    )
    legacy_sensitivity = OUTPUT / "benchmark_sensitivity_v5.csv"
    if legacy_sensitivity.exists():
        legacy_sensitivity.unlink()
    write_csv(
        OUTPUT / "rule_score_summary_v5.csv", rule_summary,
        ["rule", "confidence", "effect_count", "card_count", "median_score_low", "median_score_baseline", "median_score_high", "example_card", "example_text", "basis"],
    )
    write_csv(
        OUTPUT / "effect_score_table_v5.csv", effect_table,
        ["category", "effect", "low", "baseline", "high", "confidence", "evidence_count", "basis"],
    )
    write_csv(
        OUTPUT / "benchmark_table_v5.csv", benchmark_table,
        ["scope", "rarity", "cost_component", "points", "confidence", "basis"],
    )
    outliers = sorted(comparable, key=lambda row: abs(float(row["residual_baseline"])), reverse=True)
    write_csv(OUTPUT / "calibration_outliers_v5.csv", outliers, list(outliers[0].keys()))
    (OUTPUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    report = f"""# 카드 가치 모델 v5 — 안정성 감사 및 기준표 동결본

## 결론

- 전체 **{len(cards)}장**, 원문 효과 문장 **{source_clause_count}개**가 모두 근거 행과 연결됨
- 평가표 대상 **{len(cards) - len(v4.BASIC_REFERENCE_EXCLUDED_IDS)}장**: 기본 타격·수비 {len(v4.BASIC_REFERENCE_EXCLUDED_IDS)}장은 제외하고, 나머지 기본 카드는 일반 등급으로 비교
- 범용 fallback **{fallback_effects}개**, 합계 불일치 **{len(sum_mismatches)}개**, 재계산 불일치 **{len(recalculation_mismatches)}개**
- 신뢰도: {', '.join(f'{grade} {count}장' for grade, count in sorted(confidence_counts.items()))}
- 효과와 기준점을 함께 반영한 최종 평가 신뢰도: {', '.join(f'{grade} {count}장' for grade, count in sorted(evaluation_confidence_counts.items()))}
- 최종 평가 A/B 오차: A 중앙 절대편차 **{evaluation_a_metric['median_absolute_residual']}**·RMSE **{evaluation_a_metric['rmse']}**, B 중앙 절대편차 **{evaluation_b_metric['median_absolute_residual']}**·RMSE **{evaluation_b_metric['rmse']}**
- A등급 기준점 비교: 중앙 절대편차 **{a_metric['median_absolute_residual']}**, RMSE **{a_metric['rmse']}**
- 전체 기준점 비교: 중앙 절대편차 **{all_metric['median_absolute_residual']}**, RMSE **{all_metric['rmse']}**
- C등급 시나리오 범위의 기준점 포함률: **{float(c_metric['interval_coverage']) * 100:.1f}%**
- 15개 단위값 민감도 스냅샷과 보수·기준·낙관 범위를 합친 **예산 보정 순위** 안정성: robust {stability_counts['robust']}장, moderate {stability_counts['moderate']}장, sensitive {stability_counts['sensitive']}장
- 기준점 비교 카드 중 2점 단위 효율 밴드가 80% 이상 유지되는 카드: **{stable_balance_cards}/{len(comparable)}장**
- 그중 최종 평가 신뢰도 A/B인 안정 카드: **{stable_high_confidence_balance_cards}장**

`hard_validation_passed={str(summary['hard_validation_passed']).lower()}`는 모든 카드의 유한·정렬된 점수 범위, 원문 문장 커버리지, 근거 합계 재현, fallback 0을 동시에 검사한 결과다.

## 해석 원칙

- A/B의 기준점 잔차는 단위값·기준표 보정 품질을 판단하는 데 사용한다.
- `confidence_grade`는 효과 합계의 신뢰도, `benchmark_confidence`는 희귀도/코스트 기준점의 신뢰도, `evaluation_confidence`는 둘 중 낮은 쪽이다.
- C는 덱 구성·전투 길이·대상 수에 본질적으로 의존하므로 단일 잔차보다 점수 범위와 `combined_band_stability`를 우선한다.
- 순위는 원점수가 아니라 `value_index = 효과 점수 - 희귀도/코스트 기준점`으로 계산한다. 2점 이내 차이는 `on_budget` 동급 밴드로 취급한다.
- `benchmark_v4`는 카드의 기대 예산이고 `score_baseline`은 효과 합계다. 잔차는 카드가 반드시 잘못 설계되었다는 뜻이 아니라 상호작용, 조건, 메타 가치 또는 기준표 표본 부족을 찾는 신호다.
- 별 비용은 기준점에서 완전히 분리해 카드 효과에 별당 **-{v4.STAR_COST_POINT}점**으로 기록한다. 고정 별 비용 {fixed_star_cost_cards}장과 X별 비용 {x_star_cost_cards}장이 이에 해당하며, X별 비용은 X=1/2/4 범위로 계산한다. E9 가변 할인 카드는 근거 없는 고비용 외삽에서 제외했다.
- 기본 등급의 타격·수비는 비교 대상과 공개 평가표에서 제외한다. 그 외 기본 카드는 일반 등급 기준점을 적용한다.

## 파일

- `card_stability_v5.csv`: 전체 점수·신뢰도·잔차와 단위/시나리오 순위 안정성
- `effect_evidence_v5.csv`: 860개 원문 문장과 키워드·상호작용의 개별 점수 근거
- `effect_family_catalog_v5.csv`: 식별된 {len(family_catalog)}개 효과 대분류와 카드/문장/템플릿 수
- `effect_score_table_v5.csv`: 최종 원자 효과·키워드·시나리오 점수표
- `benchmark_table_v5.csv`: 희귀도/에너지 기준점
- `rule_score_summary_v5.csv`: 모든 규칙의 사용량·대표 점수·근거
- `residual_groups_v5.csv`: 신뢰도·풀·희귀도·코스트별 오차
- `star_cost_effect_sensitivity_v5.csv`: 별 비용 음수 효과 계수의 민감도
- `calibration_outliers_v5.csv`: 잔차 감사 우선순위
- `summary.json`: 기계 검증 결과
"""
    (OUTPUT / "README.md").write_text(report, encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
