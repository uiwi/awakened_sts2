#!/usr/bin/env python3
"""Build the compact browser dataset used by the card-search UI."""

from __future__ import annotations

import json
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    source = root / "data/processed/cards.json"
    destination = root / "web/public/data/cards.json"
    cards = json.loads(source.read_text(encoding="utf-8"))

    compact = []
    for card in cards:
        image = card["image"]
        compact.append(
            {
                "id": card["id"],
                "name": card["name"],
                "text": card["text"],
                "character": card["character"],
                "pool": card["pool"],
                "type": card["type"],
                "rarity": card["rarity"],
                "cost": card["cost"],
                "stats": card["stats"],
                "keywords": card["keywords"],
                "keyword_ids": card["keyword_ids"],
                "flags": card["flags"],
                "image": {
                    "full_ko_url": image["full_ko"]["url"] if image["full_ko"] else None,
                    "full_ko_upgraded_url": (
                        image["full_ko_upgraded"]["url"]
                        if image["full_ko_upgraded"]
                        else None
                    ),
                    "art_url": image["art"]["url"] if image["art"] else None,
                    "variant_art": image["variant_art"],
                },
            }
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(compact, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"Wrote {len(compact)} cards to {destination} ({destination.stat().st_size} bytes)")


if __name__ == "__main__":
    main()

