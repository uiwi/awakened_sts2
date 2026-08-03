#!/usr/bin/env python3
"""Score fixed and scenario-dependent card effects with a three-point range.

V3 adds a nonlinear draw curve and explicit conservative/baseline/optimistic
assumptions for conditions, repeated triggers, X costs and scaling clauses.
Unparsed clauses are retained and partial scores are never presented as full
card scores.
"""

from __future__ import annotations

import csv
import copy
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


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "web/public/data/cards.json"
OUTPUT = ROOT / "analysis/card_value_v3"

BENCHMARKS_V3 = copy.deepcopy(v2.BENCHMARKS_V2)
BENCHMARKS_V3["character"]["Uncommon"][0] = 4.30

DRAW_FIRST = 1.25
DRAW_ADDITIONAL = 2.15
DISCARD_SELECTED_V3 = 0.25

SCENARIOS = {
    "condition_probability": (0.00, 0.70, 1.00),
    "recurring_turns": (1.5, 3.70, 6.0),
    "recurring_resource_turns": (2.0, 6.80, 10.0),
    "event_triggers": (1.0, 3.50, 18.0),
    "threshold_triggers": (0.0, 1.80, 4.0),
    "scaling_count": (1.0, 2.20, 10.0),
    "x_value": (1.0, 2.0, 4.0),
    "equal_to_quantity": (3.0, 8.0, 15.0),
}

LOW_CONFIDENCE_VALUES = {
    "damage_osty": 0.45,
    "enemy_hp_loss": 0.55,
    # Combat healing persists between fights and is not symmetric with HP loss.
    # It is represented as a range in atomic_action rather than this scalar.
    "heal": 1.50,
    "orb_slot": 2.50,
    "intangible": 12.00,
    "artifact": 3.00,
    "selected_exhaust": 1.00,
    "selected_upgrade": 2.00,
    "random_card_hand": 3.00,
    "card_retrieve": 2.50,
}


@dataclass(frozen=True)
class RangeScore:
    low: float
    base: float
    high: float

    def __add__(self, other: "RangeScore") -> "RangeScore":
        return RangeScore(self.low + other.low, self.base + other.base, self.high + other.high)

    def scale(self, factors: tuple[float, float, float] | float) -> "RangeScore":
        if isinstance(factors, tuple):
            # The assumption tuple is quantity-low/base/high, while RangeScore
            # is card-value-low/base/high. For negative effects a larger
            # quantity makes card value lower, so endpoint-wise multiplication
            # would reverse the interval. Use interval products for bounds and
            # retain the paired baseline assumptions for the center.
            endpoints = (
                self.low * factors[0],
                self.low * factors[2],
                self.high * factors[0],
                self.high * factors[2],
            )
            return RangeScore(min(endpoints), self.base * factors[1], max(endpoints))
        return RangeScore(self.low * factors, self.base * factors, self.high * factors)


ZERO = RangeScore(0.0, 0.0, 0.0)


def fixed(value: float) -> RangeScore:
    return RangeScore(value, value, value)


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def draw_value(quantity: float) -> float:
    if quantity <= 0:
        return 0.0
    # Card text uses integer draw counts; interpolation keeps scenario math sane.
    return DRAW_FIRST + max(0.0, quantity - 1.0) * DRAW_ADDITIONAL


def feature_score(features: dict[str, float]) -> float:
    total = 0.0
    for name, quantity in features.items():
        if name == "draw":
            total += draw_value(quantity)
        elif name == "discard_selected":
            # Chosen discard is filtering and an enabler, not equivalent to a
            # random hand loss. Acrobatics/Prepared/Tools support a small net
            # positive value before deck-specific Sly synergies.
            total += DISCARD_SELECTED_V3 * quantity
        else:
            total += v2.VALUES_V2[name] * quantity
    return total


def fake_card(text: str) -> dict[str, Any]:
    return {"text": {"en": {"description": text}}}


def split_compound_actions(text: str) -> str:
    protected = text.replace("Discard Pile", "Discard_Pile").replace("Draw Pile", "Draw_Pile")
    value = re.sub(
        r"\s+and\s+(?=(?:gain|draw|discard|apply|deal|channel|forge|summon|lose|add|exhaust|heal)\b)",
        ". ",
        protected,
        flags=re.I,
    )
    return value.replace("Discard_Pile", "Discard Pile").replace("Draw_Pile", "Draw Pile")


def atomic_action(text: str) -> tuple[RangeScore | None, str]:
    text = split_compound_actions(text.strip())
    text = re.sub(r"\b(Draw \d+) additional (cards?)\b", r"\1 \2", text, flags=re.I)
    if text and text[-1] not in ".!?":
        text += "."
    features, status = v2.parse_simple_card(fake_card(text))
    if features is not None:
        return fixed(feature_score(features)), "atomic_fixed"

    match = re.fullmatch(r"Osty deals (\d+) damage( to ALL enemies)?\.", text, re.I)
    if match:
        multiplier = v1.AOE_MULTIPLIER if match.group(2) else 1.0
        return fixed(int(match.group(1)) * LOW_CONFIDENCE_VALUES["damage_osty"] * multiplier), "atomic_osty_damage"
    match = re.fullmatch(r"(?:The )?[Ee]nemy loses (\d+) HP\.", text)
    if match:
        return fixed(int(match.group(1)) * LOW_CONFIDENCE_VALUES["enemy_hp_loss"]), "atomic_hp_bypass"
    match = re.fullmatch(r"Heal (\d+) HP\.", text, re.I)
    if match:
        amount = int(match.group(1))
        return RangeScore(amount * 0.75, amount * 2.0, amount * 3.25), "atomic_heal_meta"
    match = re.fullmatch(r"(?:Take|Lose) (\d+) damage\.", text, re.I)
    if match:
        return fixed(int(match.group(1)) * v2.VALUES_V2["hp_loss"]), "atomic_self_damage"
    match = re.fullmatch(r"Gain (\d+) Orb Slots?\.", text, re.I)
    if match:
        return fixed(int(match.group(1)) * LOW_CONFIDENCE_VALUES["orb_slot"]), "atomic_orb_slot"
    match = re.fullmatch(r"Lose (\d+) Orb Slots?\.", text, re.I)
    if match:
        return fixed(-int(match.group(1)) * LOW_CONFIDENCE_VALUES["orb_slot"]), "atomic_orb_slot_loss"
    match = re.fullmatch(r"Gain (\d+) (Intangible|Artifact)\.", text, re.I)
    if match:
        key = match.group(2).lower()
        return fixed(int(match.group(1)) * LOW_CONFIDENCE_VALUES[key]), f"atomic_{key}"
    match = re.fullmatch(r"Exhaust (?:up to )?(\d+) cards?(?: from your (?:Hand|Draw Pile))?\.", text, re.I)
    if match:
        return fixed(int(match.group(1)) * LOW_CONFIDENCE_VALUES["selected_exhaust"]), "atomic_selected_exhaust"
    if re.fullmatch(r"Exhaust a card\.", text, re.I):
        return fixed(LOW_CONFIDENCE_VALUES["selected_exhaust"]), "atomic_selected_exhaust"
    match = re.fullmatch(r"Upgrade (\d+|a) (?:random )?cards?(?: in your (?:Hand|Discard Pile))?\.", text, re.I)
    if match:
        quantity = 1 if match.group(1).lower() == "a" else int(match.group(1))
        return fixed(quantity * LOW_CONFIDENCE_VALUES["selected_upgrade"]), "atomic_upgrade"
    match = re.fullmatch(r"Add (\d+|a|an) random (?:(Colorless|Common) )?(card|Attack|Skill|Power)s? into your Hand\.", text, re.I)
    if match:
        quantity = 1 if match.group(1).lower() in {"a", "an"} else int(match.group(1))
        category = (match.group(2) or match.group(3)).lower()
        value = 5.0 if category == "power" else (4.0 if category == "colorless" else LOW_CONFIDENCE_VALUES["random_card_hand"])
        return fixed(quantity * value), f"atomic_random_{category}_card"
    if re.fullmatch(r"It's free to play this turn\.", text, re.I):
        return fixed(v2.VALUES_V2["energy"]), "atomic_free_generated_card"
    match = re.fullmatch(r"(?:ALL |All )?(?:[Ee]nemy|[Ee]nemies) loses? (\d+) Strength this turn\.", text)
    if match:
        multiplier = v1.AOE_MULTIPLIER if re.match(r"(?:ALL|All) enemies", text) else 1.0
        return fixed(int(match.group(1)) * 0.90 * multiplier), "atomic_enemy_strength_loss_turn"
    match = re.fullmatch(r"Lose (\d+) (Strength|Dexterity|Focus)\.", text, re.I)
    if match:
        key = match.group(2).lower()
        amount = int(match.group(1))
        maximum = amount * v2.VALUES_V2[key]
        typical = min(amount, 1) * v2.VALUES_V2[key]
        return RangeScore(-maximum, -typical, 0.0), f"atomic_lose_{key}_conditional_stock"
    match = re.fullmatch(r"Gain (\d+) (Strength|Dexterity|Focus) this turn\.", text, re.I)
    if match:
        return fixed(int(match.group(1)) * 0.80), "atomic_temporary_stat"
    if re.fullmatch(r"Retain your Hand this turn\.", text, re.I):
        return fixed(2.0), "atomic_retain_hand"
    if re.fullmatch(r"Put a card from your Discard Pile into your Hand\.", text, re.I):
        return fixed(LOW_CONFIDENCE_VALUES["card_retrieve"]), "atomic_retrieve_hand"
    if re.fullmatch(r"Put a card from your Discard Pile on top of your Draw Pile\.", text, re.I):
        return fixed(1.50), "atomic_retrieve_topdeck"
    match = re.fullmatch(r"Put (\d+) cards? from your Hand on top of your Draw Pile\.", text, re.I)
    if match:
        return fixed(-0.50 * int(match.group(1))), "atomic_hand_to_topdeck"
    if re.fullmatch(r"Exhaust your Hand\.", text, re.I):
        return RangeScore(-2.0, 1.0, 5.0), "atomic_exhaust_hand"
    if re.fullmatch(r"Discard your Hand\.", text, re.I):
        return RangeScore(-3.0, -2.0, -1.0), "atomic_discard_hand"
    match = re.fullmatch(r"Exhaust (\d+) card at random\.", text, re.I)
    if match:
        return fixed(0.50 * int(match.group(1))), "atomic_random_exhaust"
    if re.fullmatch(r"Add a copy of this card into your Discard Pile\.", text, re.I):
        return RangeScore(-0.5, 0.5, 2.5), "atomic_copy_self_discard"
    if re.fullmatch(r"Add a copy of that card into your Hand\.", text, re.I):
        return RangeScore(1.5, 3.0, 5.0), "atomic_copy_selected_hand"
    if re.fullmatch(r"At the start of your next turn, return this to your Hand\.", text, re.I):
        return fixed(2.40), "atomic_delayed_self_return"
    match = re.fullmatch(r"Retain up to (\d+) cards?\.", text, re.I)
    if match:
        return fixed(int(match.group(1)) * 1.10), "atomic_retain_up_to"
    if re.fullmatch(r"Evoke your (?:leftmost|rightmost) Orb(?: twice)?\.", text, re.I):
        times = 2 if "twice" in text.lower() else 1
        return RangeScore(0.75 * times, 1.50 * times, 4.0 * times), "atomic_evoke_orb"
    if re.fullmatch(r"Trigger the passive ability of your rightmost Orb\.", text, re.I):
        return RangeScore(1.5, 3.0, 5.0), "atomic_trigger_orb"
    if re.fullmatch(r"Add Ethereal to a card in your Hand\.", text, re.I):
        return fixed(float(v1.KEYWORD_POINTS["ETHEREAL"])), "atomic_grant_ethereal"
    if re.fullmatch(r"Add Retain to a card in your Hand\.", text, re.I):
        return fixed(v1.KEYWORD_POINTS["RETAIN"]), "atomic_grant_retain"
    if re.fullmatch(r"Add Replay to a card in your Hand\.", text, re.I):
        return RangeScore(2.0, 4.0, 7.0), "atomic_grant_replay"
    match = re.fullmatch(r"Deal (\d+) damage to a random enemy (\d+) times\.", text, re.I)
    if match:
        amount = int(match.group(1)) * int(match.group(2))
        return fixed(amount * 0.45), "atomic_random_target_damage"
    match = re.fullmatch(r"Apply (\d+) Poison to a random enemy (\d+) times\.", text, re.I)
    if match:
        amount = int(match.group(1)) * int(match.group(2))
        return fixed(amount * 0.90), "atomic_random_target_poison"
    match = re.fullmatch(r"Apply (\d+) Doom to a random enemy\.", text, re.I)
    if match:
        return fixed(int(match.group(1)) * v2.VALUES_V2["doom"] * 0.90), "atomic_random_target_doom"
    match = re.fullmatch(r"ALL enemies lose (\d+) Strength\.", text, re.I)
    if match:
        return fixed(int(match.group(1)) * 2.0 * v1.AOE_MULTIPLIER), "atomic_enemy_strength_loss"
    match = re.fullmatch(r"Gain (\d+) (Weak|Frail)\.", text, re.I)
    if match:
        return fixed(-int(match.group(1)) * 1.50), "atomic_self_debuff"
    if re.fullmatch(r"You cannot draw additional cards this turn\.", text, re.I):
        return RangeScore(-5.55, -3.40, 0.0), "atomic_no_more_draw"
    if re.fullmatch(r"ALL cards in your Hand are free to play this turn\.", text, re.I):
        return RangeScore(2.50, 12.50, 25.00), "atomic_free_hand"
    match = re.fullmatch(r"A random card without Replay in your Draw Pile gains Replay (\d+)\.", text, re.I)
    if match:
        amount = int(match.group(1))
        return RangeScore(1.5 * amount, 3.5 * amount, 6.0 * amount), "atomic_random_replay"
    if re.fullmatch(r"Add Sly to a Skill in your Hand this turn\.", text, re.I):
        return RangeScore(0.5, 2.0, 4.0), "atomic_grant_sly"
    if re.fullmatch(r"Add a Soul into your Draw Pile, Hand, and Discard Pile\.", text, re.I):
        return fixed(3 * v2.VALUES_V2["soul"]), "atomic_three_souls"
    match = re.fullmatch(r"Add (\d+) Inky Shivs into your Hand\.", text, re.I)
    if match:
        return fixed(int(match.group(1)) * 2.50), "atomic_inky_shiv"
    return None, status


def ensure_sentence(text: str) -> str:
    text = text.strip()
    if text and text[-1] not in ".!?":
        text += "."
    return text[0].upper() + text[1:] if text else text


def score_clause(clause: str, card: dict[str, Any], depth: int = 0) -> tuple[RangeScore | None, list[str]]:
    if depth > 5:
        return None, ["recursion_limit"]
    direct, rule = atomic_action(clause)
    if direct is not None:
        return direct, [rule]

    # Fixed-delay effects.
    match = re.fullmatch(r"Next turn, (.+)", clause, re.I)
    if match:
        nested, rules = score_clause(ensure_sentence(match.group(1)), card, depth + 1)
        return (nested.scale(v1.DELAY_MULTIPLIER), ["delay_next_turn", *rules]) if nested else (None, rules)
    match = re.fullmatch(r"At the start of your next turn, (.+)", clause, re.I)
    if match:
        nested, rules = score_clause(ensure_sentence(match.group(1)), card, depth + 1)
        return (nested.scale(v1.DELAY_MULTIPLIER), ["delay_next_turn", *rules]) if nested else (None, rules)
    if re.fullmatch(r"At the start of your next turn, return this to your Hand\.", clause, re.I):
        return fixed(2.40), ["delayed_self_return"]
    match = re.fullmatch(r"At the start of the next (\d+) turns, (.+)", clause, re.I)
    if match:
        turns = int(match.group(1))
        nested, rules = score_clause(ensure_sentence(match.group(2)), card, depth + 1)
        multiplier = sum(v1.DELAY_MULTIPLIER**index for index in range(1, turns + 1))
        return (nested.scale(multiplier), ["fixed_delayed_turns", *rules]) if nested else (None, rules)
    match = re.fullmatch(r"At the end of (\d+) turns, (.+)", clause, re.I)
    if match:
        turns = int(match.group(1))
        nested, rules = score_clause(ensure_sentence(match.group(2)), card, depth + 1)
        return (nested.scale(v1.DELAY_MULTIPLIER**turns), ["fixed_delay_n", *rules]) if nested else (None, rules)

    # Recurring powers and event-driven triggers.
    match = re.fullmatch(r"At the (?:start|end) of your turn, (.+)", clause, re.I)
    if match:
        nested, rules = score_clause(ensure_sentence(match.group(1)), card, depth + 1)
        return (nested.scale(SCENARIOS["recurring_turns"]), ["recurring_turns", *rules]) if nested else (None, rules)
    match = re.fullmatch(r"Gain (\d+) Energy at the start of each turn\.", clause, re.I)
    if match:
        nested = fixed(int(match.group(1)) * v2.VALUES_V2["energy"])
        return nested.scale(SCENARIOS["recurring_resource_turns"]), ["recurring_resource_turns", "atomic_fixed"]
    match = re.fullmatch(r"Whenever .+?, (.+)", clause, re.I)
    if match:
        nested, rules = score_clause(ensure_sentence(match.group(1)), card, depth + 1)
        return (nested.scale(SCENARIOS["event_triggers"]), ["event_triggers", *rules]) if nested else (None, rules)
    match = re.fullmatch(r"Every .+?, (.+)", clause, re.I)
    if match:
        nested, rules = score_clause(ensure_sentence(match.group(1)), card, depth + 1)
        return (nested.scale(SCENARIOS["threshold_triggers"]), ["threshold_triggers", *rules]) if nested else (None, rules)

    # Conditions. "hits twice/N times" refers back to the card's base damage.
    match = re.fullmatch(r"If .+?, hits (twice|\d+ times)\.", clause, re.I)
    if match and card["stats"].get("damage"):
        hits = 2 if match.group(1).lower() == "twice" else int(match.group(1).split()[0])
        extra_damage = card["stats"]["damage"] * max(0, hits - 1)
        nested = fixed(extra_damage * v2.VALUES_V2["damage_single"])
        return nested.scale(SCENARIOS["condition_probability"]), ["conditional_extra_hits"]
    match = re.fullmatch(r"If .+?, (.+)", clause, re.I)
    if match:
        nested, rules = score_clause(ensure_sentence(match.group(1)), card, depth + 1)
        return (nested.scale(SCENARIOS["condition_probability"]), ["condition_probability", *rules]) if nested else (None, rules)

    # Numeric effect scaled by a deck/combat quantity.
    match = re.fullmatch(r"(.+?) for each .+\.", clause, re.I)
    if match:
        base_action = ensure_sentence(match.group(1).replace("Deals ", "Deal ", 1))
        nested, rules = score_clause(base_action, card, depth + 1)
        return (nested.scale(SCENARIOS["scaling_count"]), ["scaling_count", *rules]) if nested else (None, rules)
    if re.fullmatch(r"Apply Doom equal to damage dealt\.", clause, re.I) and card["stats"].get("damage"):
        doom_score = card["stats"]["damage"] * v2.VALUES_V2["doom"]
        return fixed(doom_score), ["doom_equal_damage"]
    match = re.fullmatch(r"(.+?) equal to .+\.", clause, re.I)
    if match:
        prefix = match.group(1)
        lower = clause.lower()
        if re.match(r"Deal damage", prefix, re.I):
            unit = v2.VALUES_V2["damage_single"]
        elif re.match(r"Gain Block", prefix, re.I):
            unit = v2.VALUES_V2["block"]
        else:
            return None, ["unmodeled_equal_to"]
        if "enemy's doom" in lower:
            factors = (5.0, 20.0, 40.0)
        elif "your block" in lower or "block on another player" in lower:
            factors = (5.0, 15.0, 30.0)
        elif "cards played this combat" in lower:
            factors = (3.0, 10.0, 20.0)
        elif "draw pile" in lower or "discard pile" in lower:
            factors = (5.0, 15.0, 25.0)
        elif "poison" in lower:
            factors = (5.0, 15.0, 30.0)
        else:
            factors = SCENARIOS["equal_to_quantity"]
        return RangeScore(*(unit * value for value in factors)), ["equal_to_quantity"]

    # X is represented explicitly instead of selecting one hidden expectation.
    if re.search(r"\bX\b", clause):
        match = re.fullmatch(r"Deal (\d+) damage to a random enemy X times\.", clause, re.I)
        if match:
            per_x = int(match.group(1)) * 0.45
            return RangeScore(*(per_x * value for value in SCENARIOS["x_value"])), ["x_value", "random_target"]
        unit_clause = ensure_sentence(re.sub(r"\bX\b", "1", clause))
        nested, rules = score_clause(unit_clause, card, depth + 1)
        return (nested.scale(SCENARIOS["x_value"]), ["x_value", *rules]) if nested else (None, rules)

    # A few relational combat effects can be represented by the same quantity
    # range used for equal-to clauses.
    if re.fullmatch(r"Double your Block\.", clause, re.I):
        values = SCENARIOS["equal_to_quantity"]
        return RangeScore(*(v2.VALUES_V2["block"] * value for value in values)), ["double_current_block"]
    match = re.fullmatch(r"ALL players (.+)", clause, re.I)
    if match:
        nested, rules = score_clause(ensure_sentence(match.group(1)), card, depth + 1)
        return (nested.scale((1.0, 1.5, 2.5)), ["all_players", *rules]) if nested else (None, rules)
    match = re.fullmatch(r"Another player (.+)", clause, re.I)
    if match:
        nested, rules = score_clause(ensure_sentence(match.group(1)), card, depth + 1)
        return (nested.scale(0.75), ["another_player", *rules]) if nested else (None, rules)
    return None, ["unmodeled"]


def interaction_score(card: dict[str, Any], features: dict[str, float] | None) -> tuple[RangeScore, list[str]]:
    score = ZERO
    rules: list[str] = []
    hit_count = card["stats"].get("hit_count") or 1
    if hit_count > 1 and card["stats"].get("damage"):
        # Expected Strength/multi-hit payoff, zero in the conservative case.
        extra_hits = hit_count - 1
        score += RangeScore(0.0, 0.25 * extra_hits, 0.75 * extra_hits)
        rules.append("multi_hit_strength_synergy")
    if features:
        unique_orbs = sum(features.get(name, 0) > 0 for name in ("lightning", "frost", "dark", "glass", "plasma"))
        if unique_orbs >= 3:
            # Diversity enables Compile Driver/Coolant/Synchronize and advances
            # the orb queue. Kept as a wide range because deck support varies.
            score += RangeScore(0.0, 2.5 * (unique_orbs - 1), 4.1 * (unique_orbs - 1))
            rules.append("orb_diversity_bundle")
    return score, rules


def main() -> None:
    cards = json.loads(SOURCE.read_text(encoding="utf-8"))
    by_id = {card["id"]: card for card in cards}
    OUTPUT.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    rule_counts: Counter[str] = Counter()
    unmodeled_templates: dict[str, set[str]] = defaultdict(set)

    for card in cards:
        body_clauses = v1.clauses(card["text"]["en"]["description"])
        full_features, simple_status = v2.parse_simple_card(card)
        total = ZERO
        modeled = 0
        rule_names: list[str] = []
        unmodeled: list[str] = []

        if full_features is not None:
            total = fixed(feature_score(full_features))
            modeled = len(body_clauses)
            rule_names.append("simple_fixed_v3")
            interaction, interaction_rules = interaction_score(card, full_features)
            total += interaction
            rule_names.extend(interaction_rules)
        else:
            for clause in body_clauses:
                score, rules = score_clause(clause, card)
                if score is None:
                    unmodeled.append(clause)
                    unmodeled_templates[v1.normalize_template(clause)].add(card["id"])
                else:
                    total += score
                    modeled += 1
                    rule_names.extend(rules)
            interaction, interaction_rules = interaction_score(card, None)
            total += interaction
            rule_names.extend(interaction_rules)

        keyword_total = 0.0
        unsupported_keywords: list[str] = []
        for keyword in card["keyword_ids"]:
            value = v1.KEYWORD_POINTS.get(keyword)
            if value is None:
                unsupported_keywords.append(keyword)
            else:
                keyword_total += value
        total += fixed(keyword_total)

        for rule_name in set(rule_names):
            rule_counts[rule_name] += 1
        scope = "character" if card["pool"]["is_playable_character"] else card["pool"]["key"]
        if card["cost"].get("is_x_cost") or card["cost"].get("is_x_star_cost"):
            base_v2 = None
            base = None
        else:
            base_v2 = v2.benchmark(card)
            base = v2.benchmark(card, BENCHMARKS_V3)
        all_modeled = modeled == len(body_clauses) and not unsupported_keywords
        status = "full_scenario" if all_modeled and total.low != total.high else ("full_fixed" if all_modeled else "partial")
        rows.append(
            {
                "card_id": card["id"],
                "name_ko": card["name"]["ko"],
                "name_en": card["name"]["en"],
                "pool": card["pool"]["key"],
                "scope": scope,
                "rarity": card["rarity"]["key"],
                "cost": v1.cost_signature(card),
                "type": card["type"]["key"],
                "status": status,
                "modeled_clauses": modeled,
                "total_clauses": len(body_clauses),
                "unsupported_keywords": "|".join(unsupported_keywords),
                "score_low": round(total.low, 3),
                "score_baseline": round(total.base, 3),
                "score_high": round(total.high, 3),
                "benchmark_v2": base_v2 if base_v2 is not None else "",
                "benchmark_v3": base if base is not None else "",
                "residual_low": round(total.low - base, 3) if all_modeled and base is not None else "",
                "residual_baseline": round(total.base - base, 3) if all_modeled and base is not None else "",
                "residual_high": round(total.high - base, 3) if all_modeled and base is not None else "",
                "rules": "|".join(sorted(set(rule_names))),
                "unmodeled_clauses": " || ".join(unmodeled),
                "description_en": v1.markup_to_analysis_text(card["text"]["en"]["description"]),
            }
        )

    assumption_rows = [
        {
            "assumption": name,
            "conservative": values[0],
            "baseline": values[1],
            "optimistic": values[2],
            "meaning": {
                "condition_probability": "조건 충족 기대 비율",
                "recurring_turns": "파워 사용 뒤 남은 유효 턴 수",
                "recurring_resource_turns": "매턴 에너지 획득 파워의 유효 발동 횟수",
                "event_triggers": "Whenever 계열 기대 발동 횟수",
                "threshold_triggers": "Every N 계열 기대 발동 횟수",
                "scaling_count": "더미·상태·사용 카드 등 참조 개수",
                "x_value": "X에 투입하는 자원",
                "equal_to_quantity": "현재 방어도·더미 크기 등 참조량",
            }[name],
        }
        for name, values in SCENARIOS.items()
    ]
    assumption_rows.extend(
        [
            {"assumption": "draw_first", "conservative": DRAW_FIRST, "baseline": DRAW_FIRST, "optimistic": DRAW_FIRST, "meaning": "한 번의 효과에서 첫 드로우"},
            {"assumption": "draw_additional", "conservative": DRAW_ADDITIONAL, "baseline": DRAW_ADDITIONAL, "optimistic": DRAW_ADDITIONAL, "meaning": "같은 효과의 두 번째 장부터"},
            {"assumption": "discard_selected", "conservative": DISCARD_SELECTED_V3, "baseline": DISCARD_SELECTED_V3, "optimistic": DISCARD_SELECTED_V3, "meaning": "선택 버리기의 필터링 기본값; 교활 시너지는 별도"},
        ]
    )

    rule_rows = [
        {"rule": rule, "card_count": count}
        for rule, count in rule_counts.most_common()
    ]
    unmodeled_rows = [
        {
            "template_en": template,
            "card_count": len(ids),
            "card_ids": "|".join(sorted(ids)),
        }
        for template, ids in sorted(unmodeled_templates.items(), key=lambda item: (-len(item[1]), item[0]))
    ]

    regular = [row for row in rows if row["benchmark_v3"] != ""]
    full_regular = [row for row in regular if row["status"] != "partial"]
    fixed_regular = [row for row in full_regular if row["status"] == "full_fixed"]
    scenario_regular = [row for row in full_regular if row["status"] == "full_scenario"]
    residuals = [float(row["residual_baseline"]) for row in full_regular]
    abs_residuals = [abs(value) for value in residuals]
    range_contains_benchmark = sum(float(row["residual_low"]) <= 0 <= float(row["residual_high"]) for row in scenario_regular)
    fixed_abs_residuals = [abs(float(row["residual_baseline"])) for row in fixed_regular]
    scenario_abs_residuals = [abs(float(row["residual_baseline"])) for row in scenario_regular]

    group_rows: list[dict[str, Any]] = []
    for dimension in ("pool", "rarity", "cost", "type", "status"):
        grouped: dict[str, list[float]] = defaultdict(list)
        for row in full_regular:
            grouped[str(row[dimension])].append(float(row["residual_baseline"]))
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

    outliers = sorted(full_regular, key=lambda row: abs(float(row["residual_baseline"])), reverse=True)[:30]
    range_misses = sorted(
        [row for row in scenario_regular if not (float(row["residual_low"]) <= 0 <= float(row["residual_high"]))],
        key=lambda row: min(abs(float(row["residual_low"])), abs(float(row["residual_high"]))),
        reverse=True,
    )

    scenario_factor_rules = {
        "condition_probability": SCENARIOS["condition_probability"],
        "recurring_turns": SCENARIOS["recurring_turns"],
        "recurring_resource_turns": SCENARIOS["recurring_resource_turns"],
        "event_triggers": SCENARIOS["event_triggers"],
        "threshold_triggers": SCENARIOS["threshold_triggers"],
        "scaling_count": SCENARIOS["scaling_count"],
        "x_value": SCENARIOS["x_value"],
    }
    other_range_rules = {
        "multi_hit_strength_synergy", "orb_diversity_bundle", "atomic_heal_meta",
        "atomic_exhaust_hand", "atomic_discard_hand", "atomic_copy_self_discard",
        "atomic_copy_selected_hand", "atomic_grant_replay", "atomic_free_hand",
        "atomic_no_more_draw", "atomic_lose_strength_conditional_stock",
        "atomic_lose_dexterity_conditional_stock", "atomic_lose_focus_conditional_stock",
        "equal_to_quantity", "double_current_block", "atomic_evoke_orb",
    }
    required_rows: list[dict[str, Any]] = []
    for row in scenario_regular:
        rules = set(str(row["rules"]).split("|"))
        present = [rule for rule in scenario_factor_rules if rule in rules]
        if len(present) != 1 or rules & other_range_rules:
            continue
        rule = present[0]
        factors = scenario_factor_rules[rule]
        low = float(row["score_low"])
        high = float(row["score_high"])
        if factors[2] == factors[0] or abs(high - low) < 1e-9:
            continue
        per_unit = (high - low) / (factors[2] - factors[0])
        fixed_part = low - per_unit * factors[0]
        required = (float(row["benchmark_v3"]) - fixed_part) / per_unit
        required_rows.append(
            {
                "rule": rule,
                "card_id": row["card_id"],
                "name_ko": row["name_ko"],
                "name_en": row["name_en"],
                "type": row["type"],
                "benchmark_v2": row["benchmark_v2"],
                "benchmark_v3": row["benchmark_v3"],
                "fixed_score": round(fixed_part, 3),
                "score_per_unit": round(per_unit, 3),
                "required_value_to_hit_benchmark": round(required, 3),
                "assumption_low": factors[0],
                "assumption_baseline": factors[1],
                "assumption_high": factors[2],
                "inside_scenario_range": factors[0] <= required <= factors[2],
            }
        )

    required_summary: dict[str, float] = {}
    for rule in scenario_factor_rules:
        values = [float(row["required_value_to_hit_benchmark"]) for row in required_rows if row["rule"] == rule]
        if values:
            required_summary[rule] = round(statistics.median(values), 3)

    draw_fit_rows: list[dict[str, Any]] = []
    for effect, card_id, quantity, others, note in v2.EVIDENCE_SPECS:
        if effect != "draw":
            continue
        card = by_id[card_id]
        base = v2.benchmark(card, BENCHMARKS_V3)
        keyword = v2.keyword_adjustment(card)
        if base is None or keyword is None:
            continue
        target_total = base - keyword - v2.score_features(others)
        linear = quantity * v2.VALUES_V2["draw"]
        nonlinear = draw_value(quantity)
        draw_fit_rows.append(
            {
                "card_id": card_id,
                "name_ko": card["name"]["ko"],
                "name_en": card["name"]["en"],
                "draw_count": quantity,
                "target_draw_score": round(target_total, 3),
                "v2_linear_score": round(linear, 3),
                "v2_absolute_error": round(abs(linear - target_total), 3),
                "v3_nonlinear_score": round(nonlinear, 3),
                "v3_absolute_error": round(abs(nonlinear - target_total), 3),
                "note": note,
            }
        )
    draw_v2_mae = statistics.mean(float(row["v2_absolute_error"]) for row in draw_fit_rows)
    draw_v3_mae = statistics.mean(float(row["v3_absolute_error"]) for row in draw_fit_rows)

    status_specs = [
        ("BOOST_AWAY", "dazed"),
        ("COLLISION_COURSE", "debris"),
        ("GUNK_UP", "slimed"),
        ("FIGHT_THROUGH", "wound"),
        ("OVERCLOCK", "burn"),
        ("TURBO", "void"),
    ]
    status_rows: list[dict[str, Any]] = []
    for card_id, status_name in status_specs:
        card = by_id[card_id]
        features, _ = v2.parse_simple_card(card)
        base = v2.benchmark(card, BENCHMARKS_V3)
        keyword = v2.keyword_adjustment(card)
        assert features is not None and base is not None and keyword is not None
        score = feature_score(features) + keyword
        residual = score - base
        chosen = v2.VALUES_V2[status_name]
        implied = chosen - residual
        status_rows.append(
            {
                "status": status_name,
                "card_id": card_id,
                "name_ko": card["name"]["ko"],
                "name_en": card["name"]["en"],
                "chosen_penalty": chosen,
                "card_score_v3": round(score, 3),
                "benchmark_v3": base,
                "residual": round(residual, 3),
                "implied_penalty_if_exact": round(implied, 3),
                "interpretation": "card_or_baseline_outlier" if implied > 0 else "usable_anchor",
            }
        )

    summary = {
        "card_count": len(rows),
        "full_fixed_card_count": sum(row["status"] == "full_fixed" for row in rows),
        "full_scenario_card_count": sum(row["status"] == "full_scenario" for row in rows),
        "partial_card_count": sum(row["status"] == "partial" for row in rows),
        "regular_cards_with_benchmark": len(regular),
        "full_regular_cards_scored": len(full_regular),
        "fixed_regular_cards_scored": len(fixed_regular),
        "scenario_regular_cards_scored": len(scenario_regular),
        "baseline_median_residual": round(statistics.median(residuals), 3),
        "baseline_median_absolute_residual": round(statistics.median(abs_residuals), 3),
        "baseline_rmse": round(math.sqrt(sum(value * value for value in residuals) / len(residuals)), 3),
        "fixed_median_absolute_residual": round(statistics.median(fixed_abs_residuals), 3),
        "scenario_median_absolute_residual": round(statistics.median(scenario_abs_residuals), 3),
        "within_1_point": sum(value <= 1 for value in abs_residuals),
        "within_2_points": sum(value <= 2 for value in abs_residuals),
        "scenario_ranges_containing_benchmark": range_contains_benchmark,
        "unique_unmodeled_templates": len(unmodeled_rows),
        "scenario_required_value_medians": required_summary,
        "draw_linear_mae_v2": round(draw_v2_mae, 3),
        "draw_nonlinear_mae_v3": round(draw_v3_mae, 3),
    }

    write_csv(
        OUTPUT / "card_scores_v3.csv",
        rows,
        ["card_id", "name_ko", "name_en", "pool", "scope", "rarity", "cost", "type", "status", "modeled_clauses", "total_clauses", "unsupported_keywords", "score_low", "score_baseline", "score_high", "benchmark_v2", "benchmark_v3", "residual_low", "residual_baseline", "residual_high", "rules", "unmodeled_clauses", "description_en"],
    )
    write_csv(
        OUTPUT / "scenario_assumptions_v3.csv",
        assumption_rows,
        ["assumption", "conservative", "baseline", "optimistic", "meaning"],
    )
    write_csv(OUTPUT / "rule_coverage_v3.csv", rule_rows, ["rule", "card_count"])
    write_csv(OUTPUT / "unmodeled_templates_v3.csv", unmodeled_rows, ["template_en", "card_count", "card_ids"])
    write_csv(
        OUTPUT / "residual_groups_v3.csv",
        group_rows,
        ["dimension", "group", "card_count", "mean_residual", "median_residual", "median_absolute_residual", "rmse"],
    )
    card_score_fields = ["card_id", "name_ko", "name_en", "pool", "scope", "rarity", "cost", "type", "status", "modeled_clauses", "total_clauses", "unsupported_keywords", "score_low", "score_baseline", "score_high", "benchmark_v2", "benchmark_v3", "residual_low", "residual_baseline", "residual_high", "rules", "unmodeled_clauses", "description_en"]
    write_csv(OUTPUT / "outliers_v3.csv", outliers, card_score_fields)
    write_csv(OUTPUT / "scenario_range_misses_v3.csv", range_misses, card_score_fields)
    write_csv(
        OUTPUT / "scenario_required_values_v3.csv",
        required_rows,
        ["rule", "card_id", "name_ko", "name_en", "type", "benchmark_v2", "benchmark_v3", "fixed_score", "score_per_unit", "required_value_to_hit_benchmark", "assumption_low", "assumption_baseline", "assumption_high", "inside_scenario_range"],
    )
    write_csv(
        OUTPUT / "draw_fit_v3.csv",
        draw_fit_rows,
        ["card_id", "name_ko", "name_en", "draw_count", "target_draw_score", "v2_linear_score", "v2_absolute_error", "v3_nonlinear_score", "v3_absolute_error", "note"],
    )
    write_csv(
        OUTPUT / "status_penalty_check_v3.csv",
        status_rows,
        ["status", "card_id", "name_ko", "name_en", "chosen_penalty", "card_score_v3", "benchmark_v3", "residual", "implied_penalty_if_exact", "interpretation"],
    )
    write_csv(
        OUTPUT / "benchmark_changes_v3.csv",
        [{
            "scope": "character",
            "rarity": "Uncommon",
            "energy_cost": 0,
            "benchmark_v2": v2.BENCHMARKS_V2["character"]["Uncommon"][0],
            "benchmark_v3": BENCHMARKS_V3["character"]["Uncommon"][0],
            "reason": "Fixed E0 Uncommon cards center near 4.3; Backstab/Boot Sequence should not set the entire cell",
        }],
        ["scope", "rarity", "energy_cost", "benchmark_v2", "benchmark_v3", "reason"],
    )
    (OUTPUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    report = f"""# 카드 가치 모델 v3 — 비선형 드로우와 시나리오 점수

## 변경점

- 드로우: `첫 1장 {DRAW_FIRST:.2f}점 + 추가 장당 {DRAW_ADDITIONAL:.2f}점`
- 선택 버리기: 손실 -0.50 대신 필터링 가치를 반영해 **+{DISCARD_SELECTED_V3:.2f}점**
- 조건부 효과: 조건 충족률 **0% / 70% / 100%**
- 반복 파워: 유효 발동 **1.5 / 3.7 / 6회**
- 매턴 에너지 파워: **2 / 6.8 / 10회**
- Whenever 발동: **1 / 3.5 / 18회** — 낙관값은 전용 덱의 반복 엔진까지 포함
- 스케일링 참조량: **1 / 2.2 / 10개**
- X값: **1 / 2 / 4**

시나리오 값은 숨은 평균이 아니라 `보수 / 기준 / 낙관` 세 열로 그대로 보존한다.

캐릭터 고급 0코스트 기준은 **5.00→4.30점**으로 낮췄다. 완전 고정 표본의 중앙값을 사용했으며 v2 기준도 결과 파일에 함께 남겼다.

8개 드로우 방정식에서 선형 모델의 평균 절대오차는 **{draw_v2_mae:.2f}점**, 비선형 모델은 **{draw_v3_mae:.2f}점**이다.

## 범위

- 전체 {len(rows)}장 중 완전 고정 점수: **{summary['full_fixed_card_count']}장**
- 완전 시나리오 점수: **{summary['full_scenario_card_count']}장**
- 일부 효과만 점수화: **{summary['partial_card_count']}장**
- 일반/무색 기준점과 비교 가능한 완전 점수: **{len(full_regular)}장**

완전 점수 카드의 기준 시나리오 절대 잔차 중앙값은 **{summary['baseline_median_absolute_residual']:.2f}점**, RMSE는 **{summary['baseline_rmse']:.2f}점**이다. 조건부 카드 중 점수 범위가 기준점을 포함한 카드는 **{range_contains_benchmark}/{len(scenario_regular)}장**이다.

고정 카드의 절대 잔차 중앙값은 **{summary['fixed_median_absolute_residual']:.2f}점**, 시나리오 카드의 기준값 절대 잔차 중앙값은 **{summary['scenario_median_absolute_residual']:.2f}점**이다. 후자의 오차가 큰 것은 범위 모델이 필요한 이유이며, 보수–낙관 범위 포함 여부를 기준값 오차보다 우선해서 본다.

현재 범위조차 기준점을 포함하지 못한 카드는 **{len(range_misses)}장**이다: {', '.join(row['name_en'] for row in range_misses)}. 이들은 다음 버전의 전용 상호작용 표본으로 사용한다.

## 해석 원칙

`partial` 카드는 표시된 수치가 **하한 성격의 부분 점수**다. 미분류 문장을 버리지 않고 `unmodeled_clauses`에 남겼으며 기준 대비 잔차를 계산하지 않았다. `Sly`와 `Unplayable`도 인쇄 코스트 기준을 깨므로 완전 점수에서 제외한다.

Rainbow에는 서로 다른 구체 세 종류의 조합 가치를 별도 범위로 추가했다. 다중 타격에는 힘과의 상호작용을 0부터 시작하는 작은 범위로 추가했다. 두 항 모두 고정 효과 단위값과 분리되어 있어 이후 실제 덱 통계가 생기면 교체할 수 있다.

상태 생성 앵커 6개 중 Dazed, Debris, Slimed, Wound, Void는 현재 페널티로 기준점을 설명한다. Overclock만 Burn 값을 양수로 두어야 기준점과 맞는 모순이 생기므로, Burn 계수를 뒤집지 않고 Overclock 또는 0코스트 고급 기준의 이상치로 유지했다.

## 파일

- `card_scores_v3.csv`: 전체 카드의 보수/기준/낙관 점수, 모델링 상태, 잔차, 미분류 문장
- `scenario_assumptions_v3.csv`: 모든 시나리오 입력값
- `rule_coverage_v3.csv`: 적용 규칙별 카드 수
- `unmodeled_templates_v3.csv`: 다음 보정 대상 템플릿
- `scenario_required_values_v3.csv`: 각 카드를 기준점에 맞추는 데 필요한 조건률·발동 횟수·참조량
- `draw_fit_v3.csv`: 선형/비선형 드로우 오차 비교
- `status_penalty_check_v3.csv`: 상태 생성 카드로 본 페널티 교차검증
- `benchmark_changes_v3.csv`: v3에서 추가 수정한 기준점
- `scenario_range_misses_v3.csv`: 보수–낙관 범위조차 기준점을 포함하지 못한 카드
- `residual_groups_v3.csv`, `outliers_v3.csv`: 그룹별 잔차와 기준 시나리오 이상치
- `summary.json`: 적용 범위와 적합도
"""
    (OUTPUT / "README.md").write_text(report, encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
