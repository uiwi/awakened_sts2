#!/usr/bin/env python3
"""Complete a scenario score for every card and attach evidence confidence.

V4 keeps all V3 rules, adds proxy models for card manipulation, replay, cost
changes, class mechanics, co-op and meta rewards, and uses an explicit generic
fallback only for truly bespoke clauses.  A completed numeric range is not
silently treated as equally reliable: every card receives a confidence grade
and rank-stability interval.
"""

from __future__ import annotations

import csv
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import analyze_card_values as v1
import calibrate_effect_values_v2 as v2
import score_card_scenarios_v3 as v3


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "web/public/data/cards.json"
OUTPUT = ROOT / "analysis/card_value_v4"


@dataclass(frozen=True)
class RuleResult:
    score: v3.RangeScore
    rule: str
    confidence: str
    basis: str


CONFIDENCE_WEIGHT = {"high": 1.0, "medium": 0.72, "low": 0.42, "fallback": 0.18}
STAR_COST_POINT = 2.0
BASIC_REFERENCE_EXCLUDED_IDS = {
    "DEFEND_DEFECT",
    "DEFEND_IRONCLAD",
    "DEFEND_NECROBINDER",
    "DEFEND_REGENT",
    "DEFEND_SILENT",
    "STRIKE_DEFECT",
    "STRIKE_IRONCLAD",
    "STRIKE_NECROBINDER",
    "STRIKE_REGENT",
    "STRIKE_SILENT",
}


def benchmark_rarity(card: dict[str, Any]) -> str:
    """Return the rarity used by the comparison model, not the printed rarity."""
    rarity = card["rarity"]["key"]
    if rarity == "Basic" and card["id"] not in BASIC_REFERENCE_EXCLUDED_IDS:
        return "Common"
    return rarity


def fixed(value: float) -> v3.RangeScore:
    return v3.RangeScore(value, value, value)


def positive_range(low: float, base: float, high: float) -> v3.RangeScore:
    return v3.RangeScore(low, base, high)


def negative_range(mild: float, base: float, severe: float) -> v3.RangeScore:
    """Arguments are positive magnitudes; output is value-low/base/high."""
    return v3.RangeScore(-severe, -base, -mild)


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def v3_confidence(clause: str, rules: list[str], ranged: bool) -> str:
    low_markers = {
        "atomic_random_card", "atomic_random_attack_card", "atomic_random_power_card",
        "atomic_random_colorless_card", "atomic_heal_meta", "equal_to_quantity",
        "orb_diversity_bundle", "atomic_evoke_orb", "atomic_grant_replay",
        "atomic_exhaust_hand", "atomic_copy_self_discard", "atomic_copy_selected_hand",
        "atomic_free_hand", "double_current_block",
    }
    if any(rule in low_markers or rule.startswith(("atomic_lose_", "atomic_random_")) for rule in rules):
        return "low"
    lower = clause.lower()
    # Exact quantities can still use a less certain conversion coefficient.
    # Preserve the evidence strength of the underlying V2 unit instead of
    # labeling every successfully parsed fixed clause as high confidence.
    if " stars" in lower or " star" in lower:
        return "low"
    if any(rule.startswith("atomic_enemy_strength_loss") or rule in {"atomic_upgrade", "atomic_orb_slot", "atomic_grant_replay"} for rule in rules):
        return "medium" if not ranged else "low"
    if re.search(r"\b(draw|energy|weak|vulnerable|forge|plating|lightning|frost|dark|glass|plasma|focus|vigor|thorns|upgrade|transform)\b", lower):
        return "medium" if not ranged else "low"
    if ranged:
        return "medium"
    return "high"


def card_reference_value(card: dict[str, Any]) -> float:
    base = v2.benchmark(card, v3.BENCHMARKS_V3)
    if base is not None:
        return base
    energy = card["cost"].get("energy")
    return 5.0 if not isinstance(energy, int) or energy < 0 else 2.5 + 2.5 * energy


def score_card_move_or_create(clause: str, card: dict[str, Any]) -> RuleResult | None:
    # Retrieve/tutor cards. Selected access is worth more than random access.
    if re.search(r"Put (?:ALL |every )?.+ from your (?:Draw|Discard|Exhaust) Pile into your Hand", clause, re.I):
        if re.search(r"ALL|every", clause, re.I):
            return RuleResult(positive_range(3.0, 7.0, 14.0), "bulk_tutor_to_hand", "low", "selected/bulk card access")
        amount = int((re.search(r"Put (?:up to )?(\d+)", clause, re.I) or [None, "1"])[1])
        return RuleResult(fixed(2.5 * amount), "tutor_to_hand", "medium", "selected card access = 2.5")
    if re.search(r"Put .+ from your (?:Draw|Discard) Pile on top of your Draw Pile", clause, re.I):
        return RuleResult(fixed(1.5), "tutor_to_topdeck", "medium", "delayed selected access")
    if re.search(r"Put .+ from your Draw Pile into your Hand", clause, re.I):
        amount_match = re.search(r"Choose (\d+) of", clause, re.I)
        amount = int(amount_match.group(1)) if amount_match else 1
        return RuleResult(fixed(2.5 * amount), "tutor_draw_to_hand", "medium", "selected draw-pile access")
    if re.search(r"Shuffle ALL your cards into your Draw Pile", clause, re.I):
        return RuleResult(positive_range(0.5, 1.5, 3.0), "shuffle_all_piles", "low", "reshuffle availability")
    if re.search(r"Put this card on top of your Draw Pile", clause, re.I):
        return RuleResult(positive_range(-0.5, 0.5, 2.0), "self_topdeck", "low", "future redraw versus draw clog")
    if re.search(r"Return this card to your Hand", clause, re.I):
        return RuleResult(positive_range(1.0, 2.5, 5.0), "self_return_hand", "low", "replay access")

    # Copies and generated cards.
    if re.search(r"copy of this card into your Discard Pile", clause, re.I):
        zero_cost = bool(re.search(r"\b0\d* Energy copy", clause))
        return RuleResult(
            positive_range(-0.5, 4.0 if zero_cost else 0.5, 9.0 if zero_cost else 2.5),
            "copy_self_discard_zero" if zero_cost else "copy_self_discard",
            "low",
            "future self-copy; deck clog included in lower bound",
        )
    if re.search(r"copy of (?:that|the third) .+ into your Hand", clause, re.I):
        recurring = "each turn" in clause.lower()
        score = positive_range(2.0, 5.0, 10.0)
        if recurring:
            score = score.scale(v3.SCENARIOS["recurring_turns"])
        return RuleResult(score, "copy_selected_to_hand", "low", "average copied card value")
    if re.search(r"Add \d+ random Attacks into your Draw Pile", clause, re.I):
        amount = int(re.search(r"Add (\d+)", clause).group(1))
        free = "free" in clause.lower()
        per = 3.5 if free else 1.5
        return RuleResult(fixed(amount * per), "random_attacks_to_draw", "low", "generated attack discounted by destination")
    if re.search(r"Add \d+ random \d+ Energy cards into your Hand", clause, re.I):
        amount = int(re.search(r"Add (\d+)", clause).group(1))
        return RuleResult(fixed(amount * 3.0), "random_fixed_cost_cards", "low", "random hand generation")
    if re.search(r"Add \d+ Inky Shivs into your Hand", clause, re.I):
        amount = int(re.search(r"Add (\d+)", clause).group(1))
        return RuleResult(fixed(amount * 2.5), "inky_shiv_generation", "medium", "Shiv value plus Inky premium")
    if re.search(r"Add a Soul into your Draw Pile, Hand, and Discard Pile", clause, re.I):
        return RuleResult(fixed(3.0), "three_pile_souls", "medium", "three Soul placements")
    if re.search(r"ALL players add (\d+) Souls into their Draw Pile", clause, re.I):
        amount = int(re.search(r"add (\d+)", clause, re.I).group(1))
        return RuleResult(positive_range(amount, amount * 1.5, amount * 2.5), "coop_soul_generation", "low", "party-size range")
    if re.search(r"Fill your Hand with", clause, re.I):
        return RuleResult(positive_range(-3.0, -1.0, 1.0), "fill_hand_status", "low", "status hand pollution")

    # Transform and upgrade.
    if re.search(r"Transform", clause, re.I):
        if "ALL" in clause or "all " in clause:
            return RuleResult(positive_range(2.0, 6.0, 12.0), "bulk_transform", "low", "multiple card replacement")
        if re.search(r"into (?:Soul|Fuel|Giant Rock|Minion)", clause, re.I):
            return RuleResult(positive_range(1.5, 3.5, 7.0), "specific_transform", "medium", "known-output transform")
        return RuleResult(positive_range(-1.0, 1.5, 4.0), "random_transform", "low", "quality replacement uncertainty")
    if re.search(r"Upgrade ALL your cards", clause, re.I):
        return RuleResult(positive_range(12.0, 25.0, 45.0), "upgrade_all_cards", "low", "deck-wide upgrade count")
    if re.search(r"Upgrade", clause, re.I):
        amount_match = re.search(r"Upgrade (\d+)", clause, re.I)
        amount = int(amount_match.group(1)) if amount_match else 1
        recurring = "At the start" in clause
        score = fixed(amount * 2.0)
        if recurring:
            score = score.scale(v3.SCENARIOS["recurring_turns"])
        return RuleResult(score, "upgrade_cards", "medium", "upgrade delta proxy = 2")
    return None


def score_cost_or_play(clause: str, card: dict[str, Any]) -> RuleResult | None:
    # Energy cost modification.
    match = re.search(r"Costs? (\d+)(?: Energy)? less(?: Energy)? for each", clause, re.I)
    if match:
        reduction = int(match.group(1))
        printed_cost = card["cost"].get("energy")
        cap = printed_cost if isinstance(printed_cost, int) and printed_cost >= 0 else reduction * 10
        quantities = v3.SCENARIOS["scaling_count"]
        values = tuple(min(cap, reduction * quantity) * v2.VALUES_V2["energy"] for quantity in quantities)
        return RuleResult(v3.RangeScore(*values), "cost_reduction_scaling_capped", "medium", "energy saved × reference count, capped by printed cost")
    match = re.search(r"reduce (?:this card's|its) cost by (\d+)", clause, re.I)
    if match:
        per = int(match.group(1)) * v2.VALUES_V2["energy"]
        triggers = v3.SCENARIOS["event_triggers"] if "whenever" in clause.lower() else (1.0, 1.0, 1.0)
        return RuleResult(fixed(per).scale(triggers), "cost_reduction", "medium", "energy saved")
    if re.search(r"Reduce this card's cost to 0", clause, re.I):
        energy = max(1, int(card["cost"].get("energy") or 1))
        return RuleResult(fixed(energy * v2.VALUES_V2["energy"]), "cost_to_zero", "medium", "printed energy saved")
    if re.search(r"costs 0.* if", clause, re.I):
        energy = max(1, int(card["cost"].get("energy") or 1))
        return RuleResult(fixed(energy * v2.VALUES_V2["energy"]).scale(v3.SCENARIOS["condition_probability"]), "conditional_cost_zero", "medium", "conditional printed energy saved")
    if re.search(r"Increase (?:the cost|this card's cost)(?: .*?)? by (\d+)", clause, re.I):
        amount = int(re.search(r"by (\d+)", clause).group(1))
        return RuleResult(fixed(-amount * v2.VALUES_V2["energy"]), "cost_increase", "medium", "energy penalty")
    match = re.search(r"(?:It|This card) costs? an? extra (\d+) Energy", clause, re.I)
    if match:
        return RuleResult(fixed(-int(match.group(1)) * v2.VALUES_V2["energy"]), "cost_increase", "medium", "energy penalty")
    if re.search(r"Reduce the cost of ALL cards in your Hand to 1 this turn", clause, re.I):
        return RuleResult(positive_range(0.0, 5.0, 12.5), "hand_cost_floor_one", "low", "energy saved by cards above 1 cost")
    if re.search(r"Cards cost an additional", clause, re.I):
        return RuleResult(negative_range(2.5, 5.0, 10.0), "hand_cost_increase", "low", "affected cards this turn")
    if re.search(r"Skills cost 0", clause, re.I):
        return RuleResult(positive_range(2.5, 7.5, 15.0), "skills_cost_zero", "low", "skills played after power")
    if re.search(r"next (?:Skill|Attack|Power|Ethereal card).+ costs? 0", clause, re.I):
        return RuleResult(fixed(2.5), "next_card_free", "medium", "one energy saved by default")
    if re.search(r"free to play this (?:turn|combat)", clause, re.I):
        return RuleResult(positive_range(2.0, 4.0, 7.0), "generated_cards_free", "low", "future generated-card energy saved")

    # Extra/automatic card plays.
    match = re.search(r"next (Skill|Attack|Power).+played an? (?:extra|additional) time", clause, re.I)
    if match:
        return RuleResult(positive_range(2.0, 5.0, 10.0), "next_card_replay", "low", "average repeated card effect")
    if re.search(r"first card you play each turn is played an extra time", clause, re.I):
        return RuleResult(positive_range(5.0, 18.5, 36.0), "first_card_each_turn_replay", "low", "average card × recurring turns")
    match = re.search(r"play it (\d+) times", clause, re.I)
    if match:
        repeats = int(match.group(1)) - 1
        return RuleResult(positive_range(2.0 * repeats, 5.0 * repeats, 10.0 * repeats), "selected_card_multi_play", "low", "average selected card repeats")
    if re.search(r"Play (?:the top|a random|\d+ random|every) .+ (?:Draw|Discard|Exhaust) Pile", clause, re.I):
        amount_match = re.search(r"Play (\d+)", clause, re.I)
        amount = int(amount_match.group(1)) if amount_match else 1
        return RuleResult(positive_range(2.0 * amount, 4.0 * amount, 7.0 * amount), "auto_play_from_pile", "low", "average auto-played card")
    if re.search(r"play it", clause, re.I) and ("Exhaust Pile" in clause or "top of your Draw Pile" in clause):
        ref = card_reference_value(card)
        return RuleResult(positive_range(0.25 * ref, 0.70 * ref, ref), "self_auto_replay", "low", "fraction of card benchmark replayed")
    if re.search(r"Replay", clause):
        amount_match = re.search(r"Replay (\d+)", clause)
        amount = int(amount_match.group(1)) if amount_match else 1
        return RuleResult(positive_range(2.0 * amount, 5.0 * amount, 9.0 * amount), "grant_replay", "low", "average repeated effect")
    return None


def score_combat_modifier(clause: str, card: dict[str, Any]) -> RuleResult | None:
    # Additional or scaling damage.
    match = re.search(r"Deals? (\d+) additional damage for (?:each|ALL)", clause, re.I)
    if match:
        per = int(match.group(1)) * v2.VALUES_V2["damage_single"]
        return RuleResult(fixed(per).scale(v3.SCENARIOS["scaling_count"]), "additional_damage_scaling", "medium", "damage unit × reference count")
    match = re.search(r"Deals? (\d+) less damage for each", clause, re.I)
    if match:
        per = int(match.group(1)) * v2.VALUES_V2["damage_single"]
        return RuleResult(negative_range(per, per * 2.2, per * 10), "damage_reduction_scaling", "medium", "hand-size damage penalty")
    match = re.search(r"Increase the damage of ALL .+ cards by (\d+)", clause, re.I)
    if match:
        per_use = int(match.group(1)) * v2.VALUES_V2["damage_single"]
        return RuleResult(fixed(per_use).scale(v3.SCENARIOS["event_triggers"]), "archetype_damage_growth", "low", "future matching-card hits")
    match = re.search(r"Shivs? deal (\d+) additional damage", clause, re.I)
    if match:
        per_use = int(match.group(1)) * v2.VALUES_V2["damage_single"]
        return RuleResult(fixed(per_use).scale((1.0, 4.0, 10.0)), "shiv_damage_aura", "low", "future Shiv count")
    if re.search(r"additional damage equal to", clause, re.I):
        return RuleResult(positive_range(2.0, 6.0, 15.0), "additional_damage_equal", "low", "referenced HP/deck quantity")
    if re.search(r"double damage", clause, re.I):
        return RuleResult(positive_range(2.5, 6.0, 15.0), "double_damage_window", "low", "additional attack damage")
    match = re.search(r"additional (\d+)% damage", clause, re.I)
    if match:
        percent = int(match.group(1)) / 100
        return RuleResult(positive_range(5 * percent, 12 * percent, 25 * percent), "percent_damage_bonus", "low", "expected affected attack damage")
    if re.search(r"Damage ALL other enemies equal to the damage dealt", clause, re.I):
        damage = float(card["stats"].get("damage") or 8)
        return RuleResult(fixed(damage * v2.VALUES_V2["damage_single"] * 0.8), "splash_damage_copy", "medium", "copied base damage")

    # Defensive and debuff manipulation.
    if re.search(r"Block is not removed", clause, re.I):
        if "next turn" in clause.lower():
            return RuleResult(positive_range(2.0, 5.0, 10.0), "retain_block_once", "low", "carried Block")
        return RuleResult(positive_range(6.0, 15.0, 30.0), "retain_block_power", "low", "multi-turn carried Block")
    if re.search(r"receive \d+% less damage", clause, re.I) or re.search(r"take half damage", clause, re.I):
        return RuleResult(positive_range(2.0, 5.0, 12.0), "percent_damage_reduction", "low", "prevented incoming damage")
    if re.search(r"Prevent the next time you would lose HP", clause, re.I):
        return RuleResult(positive_range(3.0, 8.0, 18.0), "buffer_prevention", "low", "prevented HP-loss event")
    if re.search(r"Remove all Artifact and Block from the enemy", clause, re.I):
        return RuleResult(positive_range(1.0, 4.0, 10.0), "strip_enemy_defense", "low", "enemy state dependent")
    if re.search(r"Enemy gains (\d+) Strength", clause, re.I):
        amount = int(re.search(r"(\d+)", clause).group(1))
        return RuleResult(fixed(-3.25 * amount), "enemy_strength_gain", "medium", "persistent enemy Strength")
    if re.search(r"Double the enemy's Vulnerable", clause, re.I):
        return RuleResult(positive_range(1.0, 4.0, 8.0), "double_vulnerable", "low", "existing Vulnerable dependent")
    if re.search(r"Vulnerable and Weak are twice as effective", clause, re.I):
        return RuleResult(positive_range(2.0, 6.0, 12.0), "double_debuff_effect", "low", "two-turn debuff amplification")
    if re.search(r"Apply any debuffs .+ ALL other enemies", clause, re.I):
        return RuleResult(positive_range(2.0, 7.0, 16.0), "spread_debuffs", "low", "existing debuff stack")
    if re.search(r"Kill enemies with at least as much Doom as HP", clause, re.I):
        return RuleResult(positive_range(0.0, 4.0, 12.0), "doom_execute", "low", "execute threshold frequency")
    if re.search(r"Poison is triggered (\d+) additional", clause, re.I):
        amount = int(re.search(r"(\d+)", clause).group(1))
        return RuleResult(positive_range(2.0 * amount, 6.0 * amount, 15.0 * amount), "extra_poison_trigger", "low", "existing Poison stack")
    return None


def score_class_mechanic(clause: str, card: dict[str, Any]) -> RuleResult | None:
    # Osty/Soul.
    if re.search(r"Osty's attacks deal (\d+) additional damage", clause, re.I):
        amount = int(re.search(r"(\d+)", clause).group(1)) * 0.45
        return RuleResult(fixed(amount).scale(v3.SCENARIOS["event_triggers"]), "osty_damage_aura", "low", "future Osty hits")
    if re.search(r"Osty heals (\d+) HP", clause, re.I):
        amount = int(re.search(r"(\d+)", clause).group(1))
        return RuleResult(fixed(amount * 0.4), "osty_heal", "medium", "companion HP")
    if re.search(r"Osty dies", clause, re.I):
        return RuleResult(negative_range(3.0, 10.0, 25.0), "osty_death", "low", "lost companion HP/attacks")
    if re.search(r"Osty is alive.*deals (\d+) damage.*gain (\d+) Block", clause, re.I):
        nums = [int(x) for x in re.findall(r"\d+", clause)]
        one = nums[0] * 0.45 * (v1.AOE_MULTIPLIER if "ALL enemies" in clause else 1) + nums[1] * 0.6
        return RuleResult(fixed(one).scale(v3.SCENARIOS["condition_probability"]), "osty_alive_combo", "medium", "conditional damage plus Block")
    if re.search(r"Osty deals (\d+) damage and applies (\d+) Vulnerable", clause, re.I):
        nums = [int(x) for x in re.findall(r"\d+", clause)]
        mult = v1.AOE_MULTIPLIER if "ALL enemies" in clause else 1
        value = (nums[0] * 0.45 + nums[1] * v2.VALUES_V2["vulnerable"]) * mult
        return RuleResult(fixed(value), "osty_damage_debuff", "medium", "Osty damage and debuff")
    if re.search(r"equal to Osty's (?:Max |current )?HP", clause, re.I):
        return RuleResult(positive_range(2.5, 8.0, 18.0), "osty_hp_scaling", "low", "Osty HP scenarios")
    if re.search(r"Osty loses HP.*enemies lose that much HP", clause, re.I):
        return RuleResult(positive_range(2.0, 7.0, 18.0), "osty_hp_mirror_damage", "low", "future Osty HP loss")

    # Orbs.
    if re.search(r"Channel (\d+) random Orb", clause, re.I):
        amount = int(re.search(r"Channel (\d+)", clause, re.I).group(1))
        return RuleResult(fixed(amount * 3.2), "random_orb", "medium", "mean channel value")
    if re.search(r"Trigger the passive ability of all Dark Orbs", clause, re.I):
        return RuleResult(positive_range(2.0, 8.0, 20.0), "trigger_all_dark", "low", "Dark count and charge")
    if re.search(r"Evoke all of your Orbs twice", clause, re.I):
        return RuleResult(positive_range(0.0, 8.0, 36.0), "evoke_all_twice", "low", "orb count/type, including an empty queue")
    match = re.search(r"Evoke your rightmost Orb (\d+) times", clause, re.I)
    if match:
        amount = int(match.group(1))
        return RuleResult(positive_range(0.75 * amount, 2.5 * amount, 6.0 * amount), "multi_evoke", "low", "orb type dependent")
    if re.search(r"Evoke your rightmost Orb X times", clause, re.I):
        return RuleResult(positive_range(0.75, 5.0, 24.0), "x_evoke", "low", "X and orb type")
    if re.search(r"Trigger all Lightning", clause, re.I):
        return RuleResult(positive_range(2.0, 8.0, 20.0), "trigger_all_lightning", "low", "Lightning count")
    if re.search(r"Channel Lightning equal to", clause, re.I):
        return RuleResult(positive_range(2.2, 8.8, 22.0), "lightning_history_scaling", "low", "prior Lightning count")

    # Forge/Sovereign Blade.
    if re.search(r"Sovereign Blade deals double damage", clause, re.I):
        return RuleResult(fixed(5.0), "blade_double_damage", "medium", "base Sovereign Blade damage")
    if re.search(r"Sovereign Blade now gains (\d+) Block", clause, re.I):
        amount = int(re.search(r"(\d+)", clause).group(1))
        return RuleResult(fixed(amount * 0.6), "blade_gain_block", "medium", "Block unit")
    if re.search(r"Sovereign Blade now deals damage to ALL enemies", clause, re.I):
        return RuleResult(positive_range(1.5, 3.0, 6.0), "blade_gain_aoe", "low", "single-to-AOE premium")
    if re.search(r"Sovereign Blade gains Replay (\d+)", clause, re.I):
        amount = int(re.search(r"Replay (\d+)", clause).group(1))
        return RuleResult(positive_range(amount * 5.0, amount * 10.0, amount * 20.0), "blade_replay", "low", "1/2/4 future Blade plays")
    if re.search(r"all allies Forge", clause, re.I):
        return RuleResult(positive_range(2.0, 8.0, 25.0), "coop_forge", "low", "party size, Forge amount and trigger frequency")
    return None


def score_coop_meta_restriction(clause: str, card: dict[str, Any]) -> RuleResult | None:
    # Co-op transfers.
    match = re.search(r"(?:Give|Another player gains) another player (\d+) Strength this turn", clause, re.I)
    if not match:
        match = re.search(r"Give another player (\d+) Strength this turn", clause, re.I)
    if match:
        return RuleResult(fixed(int(match.group(1)) * 0.8 * 0.75), "coop_temporary_strength", "medium", "temporary stat × ally factor")
    match = re.search(r"(?:Give another player|Another player gains) (\d+) Block", clause, re.I)
    if match:
        return RuleResult(fixed(int(match.group(1)) * 0.6 * 0.75), "coop_block", "medium", "Block × ally factor")
    match = re.search(r"Another player gains (\d+) Energy", clause, re.I)
    if match:
        return RuleResult(fixed(int(match.group(1)) * 2.5 * 0.75), "coop_energy", "medium", "Energy × ally factor")
    if re.search(r"Another player Channels Plasma", clause, re.I):
        return RuleResult(fixed(v2.VALUES_V2["plasma"] * 0.75), "coop_plasma", "medium", "Plasma × ally factor")
    if re.search(r"Give another player Block equal to your Block", clause, re.I):
        return RuleResult(positive_range(2.25, 6.75, 13.5), "coop_block_equal", "low", "own Block scenarios × ally factor")
    if re.search(r"Redirect all incoming attacks", clause, re.I):
        return RuleResult(v3.RangeScore(-6.0, 1.5, 10.0), "intercept_attacks", "low", "ally protection minus redirected personal damage")

    # Persistent/meta rewards. Combat and meta values remain separately tagged.
    if re.search(r"Procure a random potion", clause, re.I):
        return RuleResult(positive_range(3.0, 7.0, 12.0), "meta_random_potion", "low", "potion slot and quality")
    match = re.search(r"gain (\d+) Gold", clause, re.I)
    if match:
        gold = int(match.group(1))
        conditional = "If " in clause
        score = fixed(gold * 0.10)
        if conditional:
            score = score.scale(v3.SCENARIOS["condition_probability"])
        return RuleResult(score, "meta_gold", "medium", "1 point per 10 Gold")
    if re.search(r"remove a card from your Deck", clause, re.I):
        return RuleResult(positive_range(3.0, 6.0, 10.0), "meta_deck_removal", "low", "permanent deck quality")
    if re.search(r"additional card reward", clause, re.I):
        return RuleResult(positive_range(1.0, 3.0, 6.0).scale(v3.SCENARIOS["condition_probability"]), "meta_card_reward", "low", "reward quality and Fatal chance")
    if re.search(r"Lose (\d+) Max HP", clause, re.I):
        amount = int(re.search(r"(\d+)", clause).group(1))
        return RuleResult(fixed(-1.5 * amount), "meta_max_hp_loss", "medium", "persistent Max HP penalty")
    if re.search(r"Rest Site|next Act|special event|Marks a site|Can be hatched", clause, re.I):
        if re.search(r"(\d+) extra Gold", clause):
            gold = int(re.search(r"(\d+) extra Gold", clause).group(1))
            return RuleResult(positive_range(gold * 0.01, gold * 0.02, gold * 0.04), "meta_future_gold_site", "low", "delayed/event access discount")
        return RuleResult(positive_range(0.0, 4.0, 12.0), "meta_event_unlock", "low", "future event value; intentionally excluded from combat calibration")

    # Restrictions and dangerous clauses.
    ref = card_reference_value(card)
    if re.search(r"Can only be played if|cannot play|must be played before|cannot play more than", clause, re.I):
        return RuleResult(negative_range(0.0, ref * 0.3, ref * 0.8), "play_restriction", "low", "availability loss as fraction of card budget")
    if re.search(r"End your turn", clause, re.I):
        return RuleResult(negative_range(2.0, 6.0, 12.0), "end_turn_penalty", "low", "foregone cards/energy")
    if re.search(r"die", clause, re.I):
        return RuleResult(negative_range(3.0, 12.0, 30.0), "death_risk", "low", "combat-loss risk")
    if re.search(r"Take double damage", clause, re.I):
        return RuleResult(negative_range(3.0, 10.0, 25.0), "double_damage_taken", "low", "additional incoming damage")
    if re.search(r"cannot gain additional (?:\d+ )?Energy", clause, re.I):
        return RuleResult(negative_range(0.0, 2.5, 7.5), "energy_gain_lock", "low", "foregone energy effects")
    return None


def score_bespoke_mechanic(clause: str, card: dict[str, Any]) -> RuleResult | None:
    """Quantify the remaining named mechanics using the established units.

    These rules are deliberately kept separate from the generic fallback.  A
    wide interval still means that deck state or encounter state dominates,
    but the interval endpoints now have an explicit damage/Block/resource
    interpretation instead of being a fraction of the card's benchmark.
    """
    cid = card["id"]

    # Whole-card availability gates. The payoff itself is scored by its own
    # clause; this adjustment converts that payoff to an expected value.
    if cid == "GRAND_FINALE" and re.match(r"Can only be played if", clause, re.I):
        payoff = float(card["stats"].get("damage") or 0) * v2.VALUES_V2["damage_all"]
        return RuleResult(v3.RangeScore(-payoff, -0.70 * payoff, 0.0), "draw_pile_empty_gate", "low", "0%/30%/100% availability × printed payoff")
    if cid == "CLASH" and re.match(r"Can only be played if", clause, re.I):
        payoff = float(card["stats"].get("damage") or 0) * v2.VALUES_V2["damage_single"]
        return RuleResult(v3.RangeScore(-payoff, -0.30 * payoff, 0.0), "all_attack_hand_gate", "low", "0%/70%/100% availability × printed payoff")
    if cid == "THE_GAMBIT" and re.fullmatch(r"If you take unblocked attack damage this combat, die\.", clause, re.I):
        return RuleResult(negative_range(3.0, 25.0, 35.0), "gambit_death_risk", "low", "combat-loss risk after the initial Block expires")
    if cid == "SACRIFICE" and re.fullmatch(r"If Osty is alive, he dies and you gain Block equal to double his Max HP\.", clause, re.I):
        return RuleResult(v3.RangeScore(-5.0, 7.0, 18.0), "sacrifice_osty_for_block", "low", "double-Max-HP Block minus lost companion value")

    # Structural selection clauses are recorded, but their value belongs to
    # the following copy/tutor effect and must not be counted twice.
    if re.fullmatch(r"Choose (?:a|an) (?:card|Attack or Power card|Colorless card in your Hand)\.", clause, re.I):
        return RuleResult(fixed(0.0), "selection_scope", "high", "selection scope; value counted by linked effect")

    # Card flow, hand replacement and generated-card modifiers.
    if re.fullmatch(r"Discard your Hand, then draw that many cards\.", clause, re.I):
        return RuleResult(positive_range(1.0, 4.0, 8.0), "hand_redraw_equal", "low", "hand replacement and discard synergy; hand-size scenarios")
    if re.fullmatch(r"Draw cards until you have 6 in your Hand\.", clause, re.I):
        return RuleResult(positive_range(0.0, v3.draw_value(3), v3.draw_value(6)), "draw_to_six", "low", "0/3/6 cards drawn")
    if re.fullmatch(r"Draw cards until your Hand is full\.", clause, re.I):
        return RuleResult(positive_range(v3.draw_value(1), v3.draw_value(4), v3.draw_value(9)), "draw_to_full", "low", "1/4/9 cards drawn")
    if re.fullmatch(r"Draw cards until you draw a non-Attack card\.", clause, re.I):
        return RuleResult(positive_range(v3.draw_value(1), v3.draw_value(2), v3.draw_value(5)), "draw_until_non_attack", "low", "deck Attack ratio scenarios")
    if re.fullmatch(r"Discard all cards drawn this way that do not cost 0 Energy\.", clause, re.I):
        return RuleResult(v3.RangeScore(-v3.draw_value(4), -(v3.draw_value(4) - v3.draw_value(1.5)), -(v3.draw_value(4) - v3.draw_value(3))), "discard_scrape_misses", "low", "removes the draw value of 4/2.5/1 misses")
    if re.fullmatch(r"Exhaust up to 3 cards in your Hand\.", clause, re.I):
        return RuleResult(fixed(3.0), "selected_exhaust_three", "medium", "selected exhaust = 1 point each")
    if re.fullmatch(r"Exhaust ALL your Status cards\.", clause, re.I):
        return RuleResult(positive_range(0.0, 2.5, 7.5), "exhaust_status_cleanup", "low", "0/2/6 harmful cards removed")
    if re.fullmatch(r"Exhaust all non-Attack cards in your Hand\.", clause, re.I):
        return RuleResult(v3.RangeScore(-3.0, -0.5, 3.0), "exhaust_non_attacks", "low", "lost hand utility versus exhaust synergy")
    if re.fullmatch(r"It gains Ethereal\.", clause, re.I):
        return RuleResult(negative_range(0.25, 0.75, 1.25), "generated_card_ethereal", "medium", "reduced generated-card availability")
    if re.fullmatch(r"Whenever you play a Skill, Exhaust it\.", clause, re.I):
        return RuleResult(v3.RangeScore(-2.0, 1.0, 5.0), "skill_exhaust_aura", "low", "lost reuse versus exhaust/deck-thinning synergies")
    if re.fullmatch(r"Shivs gain Retain\.", clause, re.I):
        return RuleResult(positive_range(0.5, 2.0, 4.0), "shiv_retain_aura", "low", "1/4/8 Shivs retained")
    if re.fullmatch(r"Transform any number of cards in your Hand into Minion Sacrifice\.", clause, re.I):
        return RuleResult(positive_range(3.8, 11.4, 22.8), "transform_hand_to_minion_sacrifice", "low", "1/3/6 zero-cost Minion Sacrifice outputs")
    if re.fullmatch(r"Choose 2 cards in your Draw Pile to Transform into Minion Dive Bombs\.", clause, re.I):
        return RuleResult(positive_range(5.0, 8.0, 11.0), "transform_two_to_dive_bombs", "low", "two zero-cost 13-damage outputs minus replaced cards")
    if re.fullmatch(r"Choose a card in your Hand to Transform into Minion Strike\.", clause, re.I):
        return RuleResult(fixed(3.25), "transform_to_minion_strike", "medium", "Minion Strike damage/draw/Exhaust net value")
    if re.fullmatch(r"Transform a card in your Draw Pile into Soul\.", clause, re.I):
        return RuleResult(fixed(2.4), "transform_to_soul", "medium", "Soul draw/Exhaust net value")

    # Choice among three is weaker than a full tutor but stronger than a
    # blind random card.  Destination/free-play value is scored separately.
    if re.fullmatch(r"Choose 1 of 3 random cards to add into your Hand\.", clause, re.I):
        return RuleResult(positive_range(2.5, 3.5, 5.0), "discover_card", "low", "best-of-three generated card")
    if re.fullmatch(r"Choose 1 of 3 random (?:Colorless cards|Attacks from another character) to add into your Hand\.", clause, re.I):
        return RuleResult(positive_range(3.0, 4.0, 5.5), "discover_restricted_card", "low", "best-of-three restricted generation")
    if re.fullmatch(r"Choose 1 of 3 cards in your Draw Pile to add into your Hand\.", clause, re.I):
        return RuleResult(fixed(2.5), "partial_tutor_draw_to_hand", "medium", "selected draw-pile access")
    if re.fullmatch(r"Another player adds 1 random Colorless card to their Hand\.", clause, re.I):
        return RuleResult(positive_range(2.25, 3.0, 4.0), "coop_random_colorless", "low", "random Colorless value × ally factor")
    if re.fullmatch(r"Put Sovereign Blade into your Hand from anywhere\.", clause, re.I):
        return RuleResult(fixed(2.5), "retrieve_sovereign_blade", "medium", "specific 10-damage card access")
    if re.fullmatch(r"Next turn, add 3 copies of that card into your Hand\.", clause, re.I):
        return RuleResult(positive_range(2.4, 7.2, 19.2), "delayed_three_selected_copies", "low", "three selected copies × delay; combo dependent")
    if re.fullmatch(r"At the start of your turn, put a random Attack from your Discard Pile into your Hand and Upgrade it\.", clause, re.I):
        return RuleResult(positive_range(3.0, 12.0, 24.0), "recurring_upgraded_attack_retrieval", "low", "random retrieval plus upgrade across active turns")
    if re.fullmatch(r"At the start of your turn, Transform 1 card in your Hand\.", clause, re.I):
        return RuleResult(v3.RangeScore(-3.0, 5.55, 24.0), "recurring_random_transform", "low", "random transform quality × active turns")

    # Direct and reactive damage.
    match = re.fullmatch(r"Deal (\d+) damage to a random enemy twice\.", clause, re.I)
    if match:
        return RuleResult(fixed(int(match.group(1)) * 2 * 0.45), "random_damage_twice", "medium", "random-target damage unit × 2")
    match = re.fullmatch(r"Deal (\d+) damage to a random enemy for each card Exhausted\.", clause, re.I)
    if match:
        per = int(match.group(1)) * 0.45
        return RuleResult(positive_range(0.0, per * 2.2, per * 6.0), "random_damage_per_exhausted", "low", "0/2.2/6 Status cards exhausted")
    match = re.fullmatch(r"Whenever you are attacked(?: this turn)?, deal (\d+) damage back\.", clause, re.I)
    if match:
        per = int(match.group(1)) * v2.VALUES_V2["damage_single"]
        counts = (1.0, 3.0, 8.0) if "this turn" not in clause.lower() else (1.0, 3.0, 6.0)
        return RuleResult(fixed(per).scale(counts), "retaliation_damage", "low", "damage unit × incoming-hit scenarios")
    match = re.fullmatch(r"Whenever you gain Block, deal (\d+) damage to a random enemy\.", clause, re.I)
    if match:
        per = int(match.group(1)) * 0.45
        return RuleResult(fixed(per).scale((1.0, 3.5, 10.0)), "block_trigger_damage", "low", "random damage × Block-gain events")
    match = re.fullmatch(r"Whenever you apply a debuff to an enemy, they take (\d+) damage\.", clause, re.I)
    if match:
        per = int(match.group(1)) * v2.VALUES_V2["damage_single"]
        return RuleResult(fixed(per).scale((1.0, 3.5, 10.0)), "debuff_trigger_damage", "low", "damage × debuff events")
    match = re.fullmatch(r"Whenever you Evoke Lightning, deal (\d+) damage to each enemy hit\.", clause, re.I)
    if match:
        per = int(match.group(1)) * 0.45
        return RuleResult(fixed(per).scale((1.0, 3.5, 10.0)), "lightning_followup_damage", "low", "random damage × Lightning evokes")
    match = re.fullmatch(r"Whenever you play a card, deal (\d+) damage to a random enemy\.", clause, re.I)
    if match:
        per = int(match.group(1)) * 0.45
        return RuleResult(fixed(per).scale((1.0, 7.0, 20.0)), "card_play_trigger_damage", "low", "random damage × cards played after power")
    match = re.fullmatch(r"Whenever you play a Soul, a random enemy loses (\d+) HP\.", clause, re.I)
    if match:
        per = int(match.group(1)) * v3.LOW_CONFIDENCE_VALUES["enemy_hp_loss"]
        return RuleResult(positive_range(0.0, per * 3.0, per * 10.0), "soul_trigger_hp_loss", "low", "HP loss × Soul plays")
    if re.fullmatch(r"Whenever Attacks deal damage, apply that much Doom\.", clause, re.I):
        return RuleResult(positive_range(2.7, 9.45, 21.6), "attack_damage_to_doom", "low", "10/35/80 Attack damage × Doom unit")
    match = re.fullmatch(r"The first Shiv you play each turn deals (\d+) additional damage\.", clause, re.I)
    if match:
        per = int(match.group(1)) * v2.VALUES_V2["damage_single"]
        return RuleResult(positive_range(0.0, per * 2.0, per * 5.0), "first_shiv_bonus_each_turn", "low", "0/2/5 turns with a Shiv")
    match = re.fullmatch(r"Whenever you draw a card containing .+Strike.+, it is played against a random enemy\.", clause, re.I)
    if match:
        return RuleResult(positive_range(3.0, 10.5, 30.0), "strike_draw_autoplay", "low", "average free Strike × draw events")
    if re.fullmatch(r"At the end of your turn, 1 random Attack in your Hand is played against a random enemy\.", clause, re.I):
        return RuleResult(positive_range(5.25, 12.95, 21.0), "recurring_random_attack_autoplay", "low", "average free Attack × active turns")
    if re.fullmatch(r"At the start of your turn, play the top card of your Draw Pile\.", clause, re.I):
        return RuleResult(positive_range(3.0, 14.8, 42.0), "recurring_topdeck_autoplay", "low", "average auto-played card × active turns")
    if re.fullmatch(r"Play the top X cards of your Draw Pile\.", clause, re.I):
        return RuleResult(positive_range(2.0, 8.0, 28.0), "x_topdeck_autoplay", "low", "average auto-play value × X=1/2/4")
    if re.fullmatch(r"Play every Shiv in your Exhaust Pile on the enemy\.", clause, re.I):
        return RuleResult(positive_range(0.0, 8.0, 20.0), "play_exhausted_shivs", "low", "0/4/10 exhausted Shivs replayed")
    if re.fullmatch(r"At the start of your turn, deal 5 damage to ALL enemies and increase this damage by 5\.", clause, re.I):
        per_step = 5 * v2.VALUES_V2["damage_all"]
        # Arithmetic growth: 5, 10, 15, ... damage across 1/3.7/6 turns.
        totals = tuple(per_step * turns * (turns + 1) / 2 for turns in v3.SCENARIOS["recurring_turns"])
        return RuleResult(v3.RangeScore(*totals), "recurring_growing_aoe", "low", "arithmetic damage growth × active turns")

    # Damage and Block amplification.
    if re.fullmatch(r"Shivs now hit ALL enemies\.", clause, re.I):
        return RuleResult(positive_range(0.0, 2.28, 4.0), "shiv_aoe_conversion", "low", "AoE premium on four generated Shivs")
    if re.fullmatch(r"The enemy takes double attack damage from other players this turn\.", clause, re.I):
        return RuleResult(positive_range(3.75, 9.375, 18.75), "coop_double_attack_damage", "low", "10/25/50 allied Attack damage × ally factor")
    if re.fullmatch(r"The first Attack each turn deals 50% additional damage\.", clause, re.I):
        return RuleResult(positive_range(3.75, 9.25, 15.0), "first_attack_percent_bonus", "low", "50% of a 10-damage Attack × active turns")
    if re.fullmatch(r"Double your Block gain this turn\.", clause, re.I):
        return RuleResult(positive_range(1.8, 6.0, 15.0), "double_block_gain_turn", "low", "additional 3/10/25 Block")
    if re.fullmatch(r"Blocked attack damage is reflected to your attacker this turn\.", clause, re.I):
        return RuleResult(positive_range(2.5, 6.0, 7.5), "reflect_blocked_damage", "low", "5/12/15 reflected damage")
    if re.fullmatch(r"The first time you gain Block from a card each turn, double the amount gained\.", clause, re.I):
        return RuleResult(positive_range(3.0, 12.0, 30.0), "first_block_doubled_each_turn", "low", "additional first-card Block across active turns")
    if re.fullmatch(r"Whenever you gain Block on your turn, other players gain half that much Block\.", clause, re.I):
        return RuleResult(positive_range(0.9, 4.5, 18.0), "coop_share_half_block", "low", "shared Block × half × ally factor")
    match = re.fullmatch(r"Gain an additional (\d+) Block from Defend cards\.", clause, re.I)
    if match:
        per = int(match.group(1)) * v2.VALUES_V2["block"]
        return RuleResult(fixed(per).scale((1.0, 3.0, 7.0)), "defend_block_aura", "low", "Block unit × Defends played")
    match = re.fullmatch(r"Gain another (\d+) Block if you have Exhausted a card this turn\.", clause, re.I)
    if match:
        per = int(match.group(1)) * v2.VALUES_V2["block"]
        return RuleResult(fixed(per).scale(v3.SCENARIOS["condition_probability"]), "conditional_extra_block", "medium", "conditional Block")
    match = re.fullmatch(r"If you applied Doom this turn, gain Block (\d+) additional times\.", clause, re.I)
    if match:
        base_block = float(card["stats"].get("block") or 0) * v2.VALUES_V2["block"]
        extra = int(match.group(1)) * base_block
        return RuleResult(fixed(extra).scale(v3.SCENARIOS["condition_probability"]), "conditional_repeat_block", "medium", "printed Block × extra repeats × condition")
    if re.fullmatch(r"You cannot gain Block from cards for 2 turns\.", clause, re.I):
        return RuleResult(negative_range(3.0, 8.0, 15.0), "two_turn_block_lock", "low", "foregone card Block over two turns")

    # Scaling attacks and persistent self-growth.
    match = re.fullmatch(r"Increase this card's damage by (\d+) this combat\.", clause, re.I)
    if match:
        per = int(match.group(1)) * v2.VALUES_V2["damage_single"]
        return RuleResult(positive_range(0.0, per, per * 3.0), "self_damage_growth_combat", "low", "0/1/3 future plays")
    match = re.fullmatch(r"Whenever you draw this card, increase its damage by (\d+) this combat\.", clause, re.I)
    if match:
        per = int(match.group(1)) * v2.VALUES_V2["damage_single"]
        return RuleResult(fixed(per).scale((1.0, 2.0, 3.0)), "self_damage_growth_on_draw", "low", "1/2/3 draws this combat")
    match = re.fullmatch(r"Permanently increase this card's damage by (\d+)\.", clause, re.I)
    if match:
        per = int(match.group(1)) * v2.VALUES_V2["damage_single"]
        return RuleResult(fixed(per).scale((1.0, 3.0, 7.0)), "permanent_damage_growth", "low", "future-combat uses")
    match = re.fullmatch(r"Permanently increase this card's Block by (\d+)\.", clause, re.I)
    if match:
        per = int(match.group(1)) * v2.VALUES_V2["block"]
        return RuleResult(fixed(per).scale((1.0, 3.0, 7.0)), "permanent_block_growth", "low", "future-combat uses")
    if re.fullmatch(r"Repeat this effect for each enemy killed\.", clause, re.I):
        damage = float(card["stats"].get("damage") or 0) * v2.VALUES_V2["damage_all"]
        return RuleResult(positive_range(0.0, damage, damage * 3.0), "repeat_aoe_per_kill", "low", "0/1/3 chained kills")
    if re.fullmatch(r"Double the damage ALL Hang cards deal to this enemy\.", clause, re.I):
        return RuleResult(positive_range(0.0, 5.0, 15.0), "hang_damage_amplifier", "low", "0/1/3 future 10-damage Hang hits")
    if re.fullmatch(r"Double X if it's 4 or more\.", clause, re.I):
        damage = float(card["stats"].get("damage") or 0) * v2.VALUES_V2["damage_single"]
        return RuleResult(positive_range(0.0, 0.0, damage * 4.0), "x_threshold_double", "low", "bonus is inactive at X=1/2 and adds four hits at X=4")
    if re.fullmatch(r"Hits an additional time for each time you lost HP this combat\.", clause, re.I):
        per = float(card["stats"].get("damage") or 0) * v2.VALUES_V2["damage_single"]
        return RuleResult(positive_range(0.0, per * 3.0, per * 8.0), "extra_hits_per_hp_loss", "low", "0/3/8 prior HP-loss events")
    if re.fullmatch(r"Hits an additional time for each other time he has attacked this turn\.", clause, re.I):
        per = float(card["stats"].get("damage") or 0) * v3.LOW_CONFIDENCE_VALUES["damage_osty"]
        return RuleResult(positive_range(0.0, per, per * 4.0), "osty_extra_hits", "low", "0/1/4 earlier Osty attacks")
    if re.fullmatch(r"Forges an additional 5 for every other time you've hit the enemy this turn\.", clause, re.I):
        per = 5 * v2.VALUES_V2["forge"]
        return RuleResult(positive_range(0.0, per * 2.2, per * 10.0), "forge_per_prior_hit", "low", "Forge unit × prior-hit scenarios")
    if re.fullmatch(r"Exhaust a random Attack in your Hand and add its damage to this card\.", clause, re.I):
        return RuleResult(positive_range(1.0, 3.0, 8.0), "absorb_random_attack_damage", "low", "absorbed 5/10/20 damage minus lost-card value")

    # Energy, Stars, costs and recurring card access.
    if re.fullmatch(r"Double your Energy\.", clause, re.I):
        return RuleResult(fixed(v2.VALUES_V2["energy"]).scale((1.0, 2.0, 4.0)), "double_current_energy", "low", "1/2/4 current Energy")
    if re.fullmatch(r"Next turn, gain 1 Energy and 1 Stars\.", clause, re.I):
        return RuleResult(fixed((v2.VALUES_V2["energy"] + v2.VALUES_V2["stars"]) * v1.DELAY_MULTIPLIER), "delayed_energy_and_stars", "medium", "resource units × next-turn delay")
    if re.fullmatch(r"When this card is Exhausted, gain 2 Energy\.", clause, re.I):
        return RuleResult(fixed(2 * v2.VALUES_V2["energy"]), "self_exhaust_energy", "medium", "self-Exhaust reliably triggers 2 Energy")
    if re.fullmatch(r"The first 2 cards you play each turn are free to play\.", clause, re.I):
        return RuleResult(positive_range(7.5, 18.5, 30.0), "two_free_cards_each_turn", "low", "two average 1-cost cards × active turns")
    if re.fullmatch(r"The first time you play a 0 Energy Attack each turn, return it to your Hand\.", clause, re.I):
        return RuleResult(positive_range(3.0, 7.4, 14.0), "zero_cost_attack_return", "low", "average 0-cost Attack replay × active turns")
    if re.fullmatch(r"The first Attack or Skill you play each turn is placed on top of your Draw Pile\.", clause, re.I):
        return RuleResult(positive_range(1.5, 5.5, 12.0), "first_card_recurring_topdeck", "low", "delayed selected reuse across turns")
    if re.fullmatch(r"Put the next card you play this turn on top of your Draw Pile\.", clause, re.I):
        return RuleResult(fixed(1.5), "next_card_topdeck", "medium", "delayed selected access")
    if re.fullmatch(r"Every 3 Skills you play in a turn, put this into your Hand\.", clause, re.I):
        return RuleResult(positive_range(0.0, 3.0, 6.0), "self_return_skill_threshold", "low", "0/1/2 returns of a 6-damage zero-cost card")
    if re.fullmatch(r"Whenever you play a card that costs 2 Energy or more, return this to your Hand from the Discard Pile\.", clause, re.I):
        return RuleResult(positive_range(0.0, 1.8, 5.4), "self_return_on_expensive_card", "low", "0/1/3 extra Osty attacks")
    if re.fullmatch(r"Whenever you shuffle your Draw Pile, choose a card from it to put into your Hand\.", clause, re.I):
        return RuleResult(positive_range(0.0, 2.5, 7.5), "shuffle_tutor", "low", "0/1/3 shuffles × selected access")
    if re.fullmatch(r"When you play a Skill, it gains Sly\.", clause, re.I):
        return RuleResult(positive_range(2.0, 7.0, 16.0), "skill_gains_sly", "low", "Sly enablement × future Skill/discard events")
    if re.fullmatch(r"Whenever you create a Status, Channel 1 random Orb\.", clause, re.I):
        return RuleResult(positive_range(3.2, 11.2, 32.0), "status_trigger_random_orb", "low", "mean Orb value × Status creation events")

    # Character-specific status, debuff and summon rules.
    if re.fullmatch(r"Summon 3 X times\.", clause, re.I):
        per_x = 3 * v2.VALUES_V2["summon"]
        return RuleResult(fixed(per_x).scale(v3.SCENARIOS["x_value"]), "x_summon", "medium", "Summon unit × 3 × X")
    if re.fullmatch(r"Osty deals (\d+) damage to a random enemy\.", clause, re.I):
        amount = int(re.search(r"\d+", clause).group())
        return RuleResult(fixed(amount * v3.LOW_CONFIDENCE_VALUES["damage_osty"]), "osty_random_damage", "medium", "Osty random-target damage unit")
    if re.fullmatch(r"At the start of your turn, add 1 Sweeping Gaze into your Hand\.", clause, re.I):
        return RuleResult(positive_range(3.5, 16.65, 27.0), "recurring_sweeping_gaze", "low", "generated zero-cost Osty attack × active turns")
    if re.fullmatch(r"Enemy loses X Strength\.", clause, re.I):
        return RuleResult(fixed(2.0).scale(v3.SCENARIOS["x_value"]), "x_enemy_strength_loss", "medium", "persistent enemy Strength-loss unit × X")
    match = re.fullmatch(r"Enemy loses (\d+) Strength\.", clause, re.I)
    if match:
        return RuleResult(fixed(int(match.group(1)) * 2.0), "enemy_strength_loss", "medium", "persistent enemy Strength-loss unit")
    if re.fullmatch(r"Whenever you attack an enemy, it loses 1 Strength this turn\.", clause, re.I):
        return RuleResult(fixed(0.9).scale((1.0, 3.5, 18.0)), "attack_trigger_temp_strength_loss", "low", "temporary Strength loss × Attack hits")
    if re.fullmatch(r"At the start of your turn, apply 3 Doom to yourself\.", clause, re.I):
        return RuleResult(negative_range(1.5, 5.5, 12.0), "recurring_self_doom", "low", "self-Doom lethality across active turns")
    if re.fullmatch(r"Apply 10 Doom, plus an additional 5 Doom for every 10 Doom already on this enemy\.", clause, re.I):
        base = 10 * v2.VALUES_V2["doom"]
        bonus = 5 * v2.VALUES_V2["doom"]
        return RuleResult(positive_range(base, base + bonus * 2, base + bonus * 5), "doom_threshold_scaling", "low", "base Doom plus 0/2/5 existing ten-Doom groups")
    if re.fullmatch(r"Whenever you play a card this turn, apply 3 Doom to the enemy\.", clause, re.I):
        per = 3 * v2.VALUES_V2["doom"]
        return RuleResult(positive_range(0.0, per * 3.0, per * 7.0), "card_play_doom_turn", "low", "0/3/7 subsequent cards")

    # Meta progression, restrictions, status and encounter-specific state.
    if re.fullmatch(r"If Fatal, raise your Max HP by 3\.", clause, re.I):
        return RuleResult(positive_range(0.0, 3.15, 4.5), "fatal_max_hp_gain", "low", "Max HP unit × Fatal probability")
    if re.fullmatch(r"At the end of your turn, if this is in your Hand, lose 10 Gold\.", clause, re.I):
        return RuleResult(negative_range(1.0, 2.0, 5.0), "recurring_gold_loss_curse", "low", "1/2/5 end-turn Gold losses")
    if re.fullmatch(r"Removed from your Deck after 5 combats\.", clause, re.I):
        return RuleResult(positive_range(1.0, 2.0, 4.0), "self_removing_curse", "low", "bounded curse lifetime versus permanent clog")
    if re.search(r"Rest Site|special event in the next Act", clause, re.I):
        return RuleResult(positive_range(0.0, 4.0, 12.0), "meta_event_unlock", "low", "future event value kept separate from combat evidence")
    if re.fullmatch(r"Draw 1 fewer card each turn\.", clause, re.I):
        per = v3.draw_value(1)
        return RuleResult(negative_range(per * 2.0, per * 6.8, per * 10.0), "recurring_draw_reduction", "low", "one fewer draw × combat turns")
    if re.fullmatch(r"Gain 1 less Energy per turn\.", clause, re.I):
        per = v2.VALUES_V2["energy"]
        return RuleResult(negative_range(per * 2.0, per * 6.8, per * 10.0), "recurring_energy_reduction", "low", "one less Energy × combat turns")
    if re.fullmatch(r"Whenever you draw this card, lose 1 Energy\.", clause, re.I):
        return RuleResult(fixed(-v2.VALUES_V2["energy"]), "draw_trigger_energy_loss", "medium", "one Energy lost on draw")
    if re.fullmatch(r"Stun the enemy\.", clause, re.I):
        return RuleResult(positive_range(3.0, 7.2, 15.0), "enemy_stun", "low", "prevented enemy action as 5/12/25 Block")
    if re.fullmatch(r"At the start of your turn, draw 1 card and Exhaust 1 card from your Hand\.", clause, re.I):
        per_turn = v3.RangeScore(-1.0, 1.0, 2.25)
        return RuleResult(per_turn.scale(v3.SCENARIOS["recurring_turns"]), "recurring_draw_and_exhaust", "low", "forced exhaust quality × active turns")
    if re.fullmatch(r"If you play 5 or more cards in a turn, draw 1 card at the start of your next turn\.", clause, re.I):
        return RuleResult(positive_range(0.0, 2.5, 6.25), "threshold_delayed_draw_recurring", "low", "0/2/5 successful turn thresholds")
    match = re.fullmatch(r"Gain (\d+) Block at the start of the next (\d+) turns\.", clause, re.I)
    if match:
        amount, turns = map(int, match.groups())
        multiplier = sum(v1.DELAY_MULTIPLIER**index for index in range(1, turns + 1))
        return RuleResult(fixed(amount * v2.VALUES_V2["block"] * multiplier), "fixed_future_turn_block", "medium", "fixed delayed Block with geometric delay")
    if re.fullmatch(r"Whenever you draw a Status each turn, draw 2 cards\.", clause, re.I) or re.fullmatch(r"The first time you draw a Status each turn, draw 2 cards\.", clause, re.I):
        return RuleResult(positive_range(0.0, 5.1, 13.6), "status_draw_compensation", "low", "0/1.5/4 Status draws")
    if re.fullmatch(r"Get farther away\.", clause, re.I):
        return RuleResult(positive_range(0.0, 2.0, 6.0), "encounter_escape_progress", "low", "encounter-specific progress")
    if re.fullmatch(r"Increase Sandpit by 1\.", clause, re.I):
        return RuleResult(positive_range(0.0, 1.5, 5.0), "encounter_sandpit_progress", "low", "encounter-specific progress")
    return None


def score_priority_override(clause: str, card: dict[str, Any]) -> RuleResult | None:
    """Card-specific reference counts that supersede generic V3 scaling."""
    cid = card["id"]
    if cid == "BUNDLE_OF_JOY" and re.fullmatch(r"Add 3 random Colorless cards into your Hand\.", clause, re.I):
        return RuleResult(positive_range(5.0, 7.5, 10.0), "three_random_colorless_diminishing", "low", "three random cards with hand-clog and diminishing-choice discount")
    if cid == "TRANSFIGURE" and re.fullmatch(r"Add Replay to a card in your Hand\.", clause, re.I):
        return RuleResult(positive_range(3.0, 8.0, 20.0), "selected_replay_with_combo_range", "low", "selected repeated-card value before added Energy cost")
    if cid == "TRACKING" and re.fullmatch(r"Weak enemies take double damage from Attacks\.", clause, re.I):
        return RuleResult(positive_range(2.5, 12.5, 30.0), "persistent_weak_double_damage", "low", "additional Attack damage across Weak windows")
    if cid == "MURDER" and re.fullmatch(r"Deals 1 additional damage for each card drawn this combat\.", clause, re.I):
        return RuleResult(fixed(v2.VALUES_V2["damage_single"]).scale((5.0, 25.0, 50.0)), "damage_per_combat_draw", "low", "5/25/50 cards drawn this combat")
    if cid == "PERFECTED_STRIKE" and re.search(r"for ALL your cards containing", clause, re.I):
        per = 2 * v2.VALUES_V2["damage_single"]
        return RuleResult(fixed(per).scale((2.0, 5.0, 10.0)), "damage_per_strike_card", "low", "2/5/10 Strike cards")
    if cid == "REND" and re.search(r"for each unique debuff", clause, re.I):
        per = 5 * v2.VALUES_V2["damage_single"]
        return RuleResult(fixed(per).scale((1.0, 4.0, 7.0)), "damage_per_unique_debuff", "low", "1/4/7 unique debuffs")
    if cid == "SQUEEZE" and re.search(r"for ALL your other Osty Attacks", clause, re.I):
        per = 5 * v2.VALUES_V2["damage_single"]
        return RuleResult(fixed(per).scale((1.0, 3.5, 8.0)), "damage_per_osty_attack", "low", "1/3.5/8 other Osty Attacks")
    if cid == "FIEND_FIRE" and re.fullmatch(r"Deal 7 damage for each card Exhausted\.", clause, re.I):
        per = 7 * v2.VALUES_V2["damage_single"]
        return RuleResult(fixed(per).scale((1.0, 4.0, 8.0)), "damage_per_exhausted_hand_card", "low", "1/4/8 cards in Hand")
    return None


def generic_fallback(clause: str, card: dict[str, Any]) -> RuleResult:
    """Last-resort transparent proxy; never silently claims direct evidence."""
    lower = clause.lower()
    ref = card_reference_value(card)
    if any(word in lower for word in ("gain", "add", "put", "play", "trigger", "double", "increase", "return")):
        return RuleResult(positive_range(0.0, min(3.0, ref * 0.35), min(10.0, ref)), "generic_positive_bespoke", "fallback", clause)
    if any(word in lower for word in ("lose", "cannot", "discard", "exhaust", "cost", "damage")):
        return RuleResult(negative_range(0.0, min(3.0, ref * 0.35), min(10.0, ref)), "generic_negative_bespoke", "fallback", clause)
    return RuleResult(v3.RangeScore(-2.0, 0.0, 4.0), "generic_context_bespoke", "fallback", clause)


def v4_clause_score(clause: str, card: dict[str, Any]) -> RuleResult:
    override = score_priority_override(clause, card)
    if override is not None:
        return override
    score, rules = v3.score_clause(clause, card)
    if score is not None:
        ranged = abs(score.high - score.low) > 1e-9
        confidence = v3_confidence(clause, rules, ranged)
        return RuleResult(score, "+".join(sorted(set(rules))), confidence, "v3 quantified/scenario rule")
    for scorer in (
        score_bespoke_mechanic,
        score_card_move_or_create,
        score_cost_or_play,
        score_combat_modifier,
        score_class_mechanic,
        score_coop_meta_restriction,
    ):
        result = scorer(clause, card)
        if result is not None:
            return result
    return generic_fallback(clause, card)


def keyword_score(card: dict[str, Any]) -> list[RuleResult]:
    results: list[RuleResult] = []
    harmful_card = card["pool"]["key"] in {"status", "curse"} or "UNPLAYABLE" in card["keyword_ids"]
    for keyword in card["keyword_ids"]:
        if keyword == "SLY":
            energy = card["cost"].get("energy")
            energy = energy if isinstance(energy, int) and energy > 0 else 1
            trigger_value = energy * v2.VALUES_V2["energy"] + 0.5
            results.append(RuleResult(v3.RangeScore(0.0, trigger_value * 0.55, trigger_value), "keyword_sly", "low", "discard-trigger probability and free cost"))
        elif keyword == "UNPLAYABLE":
            results.append(RuleResult(negative_range(0.75, 1.25, 2.0), "keyword_unplayable", "medium", "draw/hand clog"))
        elif keyword == "ETERNAL":
            value = -1.0 if card["pool"]["key"] == "curse" else 0.0
            results.append(RuleResult(fixed(value), "keyword_eternal_meta", "medium", "deck removal restriction") )
        elif keyword in {"EXHAUST", "ETHEREAL"} and harmful_card:
            results.append(RuleResult(fixed(1.0), f"keyword_{keyword.lower()}_cleanup", "medium", "removes harmful card") )
        else:
            value = v1.KEYWORD_POINTS.get(keyword)
            if value is not None:
                results.append(RuleResult(fixed(float(value)), f"keyword_{keyword.lower()}", "medium", "v1 keyword calibration"))
    return results


def printed_cost_score(card: dict[str, Any]) -> list[RuleResult]:
    """Represent printed Star payments as negative card effects.

    Energy remains the benchmark axis.  Stars are a second resource, so their
    printed payment belongs in the effect sum beside Star generation rather
    than being hidden inside the rarity/energy budget.
    """
    star = card["cost"].get("star")
    if isinstance(star, int) and star > 0:
        return [
            RuleResult(
                fixed(-STAR_COST_POINT * star),
                "cost_star_spend",
                "low",
                f"printed Star payment: {star} × -{STAR_COST_POINT:.1f}",
            )
        ]
    if card["cost"].get("is_x_star_cost"):
        return [
            RuleResult(
                fixed(-STAR_COST_POINT).scale(v3.SCENARIOS["x_value"]),
                "cost_star_x_spend",
                "low",
                f"printed X-Star payment: -{STAR_COST_POINT:.1f} per Star at X=1/2/4",
            )
        ]
    return []


def benchmark_v4(card: dict[str, Any]) -> tuple[float | None, str]:
    if card["id"] in BASIC_REFERENCE_EXCLUDED_IDS:
        return None, "basic_strike_defend_excluded"
    if card["pool"]["key"] in {"event", "curse", "status", "token", "quest"}:
        return None, "special_pool_raw_score"
    scope = "character" if card["pool"]["is_playable_character"] else "colorless"
    rarity = benchmark_rarity(card)
    if rarity not in v3.BENCHMARKS_V3.get(scope, {}):
        return None, "unsupported_rarity_raw_score"
    energy = card["cost"].get("energy")
    star = card["cost"].get("star")
    if card["cost"].get("is_x_cost"):
        energy = 2
    if not isinstance(energy, int) or energy < 0:
        return None, "no_cost_raw_score"
    cost_map = v3.BENCHMARKS_V3[scope][rarity]
    keys = sorted(cost_map)
    if energy > keys[-1]:
        # Meteor Strike is the only directly quantified ordinary high-cost
        # anchor. Banshee's Cry has a built-in variable discount, so treating
        # its printed 9 as a linear budget would be circular and misleading.
        if card["id"] == "METEOR_STRIKE":
            return 33.0, "direct_high_cost_anchor"
        return None, "unsupported_high_energy_cost"
    if energy <= keys[0]:
        value = cost_map[keys[0]]
    else:
        value = cost_map[energy]
    has_star_cost = (isinstance(star, int) and star > 0) or card["cost"].get("is_x_star_cost")
    method = "x_as_2_energy" if card["cost"].get("is_x_cost") else ("energy_table_star_cost_separated" if has_star_cost else "v3_table")
    if card["rarity"]["key"] == "Basic":
        method = f"basic_as_common_{method}"
    return round(value, 3), method


def main() -> None:
    cards = json.loads(SOURCE.read_text(encoding="utf-8"))
    OUTPUT.mkdir(parents=True, exist_ok=True)
    scored: list[dict[str, Any]] = []
    evidence_rows: list[dict[str, Any]] = []
    rule_counts: Counter[tuple[str, str]] = Counter()

    for card in cards:
        results: list[RuleResult] = []
        clauses = v1.clauses(card["text"]["en"]["description"])
        for index, clause in enumerate(clauses, start=1):
            result = v4_clause_score(clause, card)
            results.append(result)
            rule_counts[(result.rule, result.confidence)] += 1
            evidence_rows.append(
                {
                    "card_id": card["id"], "name_ko": card["name"]["ko"], "name_en": card["name"]["en"],
                    "source": "clause", "index": index, "text_en": clause, "rule": result.rule,
                    "confidence": result.confidence, "score_low": round(result.score.low, 3),
                    "score_baseline": round(result.score.base, 3), "score_high": round(result.score.high, 3),
                    "basis": result.basis,
                }
            )
        for result in keyword_score(card):
            results.append(result)
            rule_counts[(result.rule, result.confidence)] += 1
            evidence_rows.append(
                {
                    "card_id": card["id"], "name_ko": card["name"]["ko"], "name_en": card["name"]["en"],
                    "source": "keyword", "index": "", "text_en": result.rule, "rule": result.rule,
                    "confidence": result.confidence, "score_low": round(result.score.low, 3),
                    "score_baseline": round(result.score.base, 3), "score_high": round(result.score.high, 3),
                    "basis": result.basis,
                }
            )
        for result in printed_cost_score(card):
            results.append(result)
            rule_counts[(result.rule, result.confidence)] += 1
            star = card["cost"].get("star")
            cost_text = f"Spend {star} Stars to play this card" if result.rule == "cost_star_spend" else "Spend X Stars to play this card"
            evidence_rows.append(
                {
                    "card_id": card["id"], "name_ko": card["name"]["ko"], "name_en": card["name"]["en"],
                    "source": "printed_cost", "index": "", "text_en": cost_text, "rule": result.rule,
                    "confidence": result.confidence, "score_low": round(result.score.low, 3),
                    "score_baseline": round(result.score.base, 3), "score_high": round(result.score.high, 3),
                    "basis": result.basis,
                }
            )
        simple_features, _ = v2.parse_simple_card(card)
        interaction, interaction_rules = v3.interaction_score(card, simple_features)
        if interaction_rules:
            ranged = abs(interaction.high - interaction.low) > 1e-9
            confidence = v3_confidence(card["text"]["en"]["description"], interaction_rules, ranged)
            result = RuleResult(interaction, "+".join(sorted(interaction_rules)), confidence, "V3 cross-effect interaction")
            results.append(result)
            rule_counts[(result.rule, result.confidence)] += 1
            evidence_rows.append(
                {
                    "card_id": card["id"], "name_ko": card["name"]["ko"], "name_en": card["name"]["en"],
                    "source": "interaction", "index": "", "text_en": result.rule, "rule": result.rule,
                    "confidence": result.confidence, "score_low": round(result.score.low, 3),
                    "score_baseline": round(result.score.base, 3), "score_high": round(result.score.high, 3),
                    "basis": result.basis,
                }
            )
        if not results:
            # A genuinely blank card still has draw/hand opportunity cost.
            result = RuleResult(negative_range(0.5, 1.25, 2.0), "blank_card_clog", "low", "draw and hand slot")
            results.append(result)
            rule_counts[(result.rule, result.confidence)] += 1
        total = v3.ZERO
        weighted_confidence = 0.0
        weight_total = 0.0
        for result in results:
            total += result.score
            weight = max(0.5, abs(result.score.base), (result.score.high - result.score.low) * 0.25)
            weighted_confidence += weight * CONFIDENCE_WEIGHT[result.confidence]
            weight_total += weight
        confidence_score = weighted_confidence / weight_total
        fallback_count = sum(result.confidence == "fallback" for result in results)
        low_count = sum(result.confidence == "low" for result in results)
        if fallback_count:
            grade = "D"
        elif confidence_score >= 0.90 and total.high - total.low <= 1:
            grade = "A"
        elif confidence_score >= 0.70 and total.high - total.low <= 5:
            grade = "B"
        elif confidence_score >= 0.40:
            grade = "C"
        else:
            grade = "D"
        benchmark, benchmark_method = benchmark_v4(card)
        comparable = benchmark is not None and "SLY" not in card["keyword_ids"] and "UNPLAYABLE" not in card["keyword_ids"]
        scored.append(
            {
                "card_id": card["id"], "name_ko": card["name"]["ko"], "name_en": card["name"]["en"],
                "pool": card["pool"]["key"], "rarity": benchmark_rarity(card), "cost": v1.cost_signature(card),
                "type": card["type"]["key"], "score_low": round(total.low, 3), "score_baseline": round(total.base, 3),
                "score_high": round(total.high, 3), "score_width": round(total.high - total.low, 3),
                "confidence_score": round(confidence_score, 3), "confidence_grade": grade,
                "low_confidence_effects": low_count, "fallback_effects": fallback_count,
                "benchmark_v4": benchmark if benchmark is not None else "", "benchmark_method": benchmark_method,
                "benchmark_comparable": comparable,
                "residual_low": round(total.low - benchmark, 3) if comparable else "",
                "residual_baseline": round(total.base - benchmark, 3) if comparable else "",
                "residual_high": round(total.high - benchmark, 3) if comparable else "",
                "rules": "|".join(sorted(set(result.rule for result in results))),
                "description_en": v1.markup_to_analysis_text(card["text"]["en"]["description"]),
            }
        )

    # Scenario rank stability within each pool. Rank 1 is highest score.
    by_pool: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in scored:
        by_pool[row["pool"]].append(row)
    for pool_rows in by_pool.values():
        rank_maps: dict[str, dict[str, int]] = {}
        for field in ("score_low", "score_baseline", "score_high"):
            ordered = sorted(pool_rows, key=lambda row: (-float(row[field]), row["card_id"]))
            rank_maps[field] = {row["card_id"]: index for index, row in enumerate(ordered, start=1)}
        size = len(pool_rows)
        for row in pool_rows:
            ranks = [rank_maps[field][row["card_id"]] for field in ("score_low", "score_baseline", "score_high")]
            row["rank_low_scenario"] = ranks[0]
            row["rank_baseline_scenario"] = ranks[1]
            row["rank_high_scenario"] = ranks[2]
            row["rank_span"] = max(ranks) - min(ranks)
            row["rank_stability"] = round(1.0 - (row["rank_span"] / max(1, size - 1)), 3)

    comparable_rows = [row for row in scored if row["benchmark_comparable"]]
    residuals = [float(row["residual_baseline"]) for row in comparable_rows]
    abs_residuals = [abs(value) for value in residuals]
    grades = Counter(row["confidence_grade"] for row in scored)
    fallback_cards = [row for row in scored if int(row["fallback_effects"]) > 0]
    stable_cards = [row for row in scored if float(row["rank_stability"]) >= 0.90]
    summary = {
        "card_count": len(scored),
        "evaluation_table_card_count": len(scored) - len(BASIC_REFERENCE_EXCLUDED_IDS),
        "basic_strike_defend_excluded_cards": len(BASIC_REFERENCE_EXCLUDED_IDS),
        "basic_cards_compared_as_common": sum(
            card["rarity"]["key"] == "Basic" and card["id"] not in BASIC_REFERENCE_EXCLUDED_IDS
            for card in cards
        ),
        "cards_with_complete_numeric_range": len(scored),
        "confidence_grades": dict(sorted(grades.items())),
        "cards_using_generic_fallback": len(fallback_cards),
        "benchmark_comparable_cards": len(comparable_rows),
        "median_absolute_residual": round(statistics.median(abs_residuals), 3),
        "rmse": round(math.sqrt(sum(value * value for value in residuals) / len(residuals)), 3),
        "within_1_point": sum(value <= 1 for value in abs_residuals),
        "within_2_points": sum(value <= 2 for value in abs_residuals),
        "rank_stability_ge_0_90": len(stable_cards),
        "median_rank_stability": round(statistics.median(float(row["rank_stability"]) for row in scored), 3),
        "rule_count": len(rule_counts),
    }

    card_fields = [
        "card_id", "name_ko", "name_en", "pool", "rarity", "cost", "type",
        "score_low", "score_baseline", "score_high", "score_width", "confidence_score", "confidence_grade",
        "low_confidence_effects", "fallback_effects", "benchmark_v4", "benchmark_method", "benchmark_comparable",
        "residual_low", "residual_baseline", "residual_high", "rank_low_scenario", "rank_baseline_scenario",
        "rank_high_scenario", "rank_span", "rank_stability", "rules", "description_en",
    ]
    write_csv(OUTPUT / "card_scores_v4.csv", scored, card_fields)
    write_csv(
        OUTPUT / "effect_evidence_v4.csv", evidence_rows,
        ["card_id", "name_ko", "name_en", "source", "index", "text_en", "rule", "confidence", "score_low", "score_baseline", "score_high", "basis"],
    )
    rule_rows = [
        {"rule": rule, "confidence": confidence, "effect_count": count}
        for (rule, confidence), count in sorted(rule_counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    write_csv(OUTPUT / "rule_coverage_v4.csv", rule_rows, ["rule", "confidence", "effect_count"])
    write_csv(OUTPUT / "fallback_cards_v4.csv", fallback_cards, card_fields)
    write_csv(OUTPUT / "unstable_rank_cards_v4.csv", sorted(scored, key=lambda row: (float(row["rank_stability"]), -float(row["score_width"])))[:100], card_fields)
    write_csv(OUTPUT / "largest_residuals_v4.csv", sorted(comparable_rows, key=lambda row: abs(float(row["residual_baseline"])), reverse=True)[:100], card_fields)
    (OUTPUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    report = f"""# 카드 가치 모델 v4 — 전체 카드 점수와 안정성

## 결과

- 전체 **{len(scored)}장** 모두 보수/기준/낙관 점수 범위를 가짐
- 기준점 비교 가능: **{len(comparable_rows)}장**
- 신뢰도 등급: {', '.join(f'{key} {value}장' for key, value in sorted(grades.items()))}
- 범용 fallback이 필요한 카드: **{len(fallback_cards)}장**
- 풀 내 시나리오 순위 안정도 0.90 이상: **{len(stable_cards)}/{len(scored)}장**

기준점 비교 카드의 절대 잔차 중앙값은 **{summary['median_absolute_residual']:.2f}점**, RMSE는 **{summary['rmse']:.2f}점**이다. v4의 목적은 잔차를 억지로 0으로 만드는 것이 아니라 모든 카드에 범위와 근거 강도를 제공하는 것이다.

## 신뢰도 의미

- A: 직접 수치화되고 시나리오 폭이 매우 좁음
- B: 교차 보정된 효과 또는 제한된 시나리오
- C: 덱/전투 상태 의존도가 큰 대체 변수
- D: 고유 효과를 범용 대체 변수로만 표현; 다음 보정 최우선

기본 카드 중 타격·수비 10장은 비교와 평가표에서 제외한다. 그 외 기본 카드 9장은 일반 등급 기준점으로 비교한다. `benchmark_comparable=false`인 교활·사용불가·특수 풀 카드는 인쇄 코스트 기준과 직접 비교하지 않고 원점수와 범위만 사용한다. 별 비용은 기준점에 넣지 않고 `별 1 = -{STAR_COST_POINT:.1f}점`인 명시적 카드 효과로 차감한다. X별 비용은 X=1/2/4 범위, X에너지 비용은 기준 X=2로 계산한다. 직접 앵커가 없는 E5 초과 비용은 외삽하지 않는다.

## 파일

- `card_scores_v4.csv`: 전체 카드 점수, 신뢰도, 잔차, 시나리오별 풀 내 순위
- `effect_evidence_v4.csv`: 문장/키워드별 점수와 적용 근거
- `rule_coverage_v4.csv`: 규칙별 사용량과 신뢰도
- `fallback_cards_v4.csv`: 범용 대체 변수가 남은 카드
- `unstable_rank_cards_v4.csv`: 시나리오에 따라 순위가 크게 바뀌는 카드
- `largest_residuals_v4.csv`: 기준점과 가장 크게 어긋나는 카드
- `summary.json`: 범위·적합도·안정성 요약
"""
    (OUTPUT / "README.md").write_text(report, encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
