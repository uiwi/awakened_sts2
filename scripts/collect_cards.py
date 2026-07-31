#!/usr/bin/env python3
"""Collect the current stable Slay the Spire 2 card catalog.

The collector uses the public Spire Codex API, whose structured data is
extracted from the game's code and localization files. It stores raw source
responses, a bilingual normalized dataset, CSV exports, keyword/glossary
indexes, Korean full-card renders, upgraded renders, and card art.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

API_BASE = "https://spire-codex.com/api"
SITE_BASE = "https://spire-codex.com"
USER_AGENT = "awakened-sts2-card-collector/1.0"
SOURCE_NAME = "Spire Codex"
SOURCE_REPOSITORY = "https://github.com/ptrlrd/spire-codex"

POOL_LABELS: dict[str, tuple[str, str, bool]] = {
    "ironclad": ("아이언클래드", "Ironclad", True),
    "silent": ("사일런트", "Silent", True),
    "defect": ("디펙트", "Defect", True),
    "necrobinder": ("네크로바인더", "Necrobinder", True),
    "regent": ("리전트", "Regent", True),
    "colorless": ("무색", "Colorless", False),
    "curse": ("저주", "Curse", False),
    "status": ("상태이상", "Status", False),
    "event": ("이벤트", "Event", False),
    "token": ("토큰", "Token", False),
    "quest": ("퀘스트", "Quest", False),
}

BB_CODE_RE = re.compile(r"\[/?[a-zA-Z]+(?::[^\]]+)?\]")
SPACE_RE = re.compile(r"[ \t]+")


@dataclass(frozen=True)
class DownloadTask:
    url: str
    local_path: Path
    kind: str
    card_id: str


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def api_url(endpoint: str, lang: str | None = None) -> str:
    url = f"{API_BASE}/{endpoint.lstrip('/')}"
    if lang:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode({"lang": lang})
    return url


def request_bytes(url: str, retries: int = 4, timeout: int = 90) -> bytes:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = error
            if attempt + 1 < retries:
                time.sleep(1.5 * (2**attempt))
    assert last_error is not None
    raise last_error


def fetch_json(url: str) -> Any:
    return json.loads(request_bytes(url).decode("utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def plain_text(value: str | None) -> str:
    if not value:
        return ""
    text = BB_CODE_RE.sub("", html.unescape(value))
    return "\n".join(SPACE_RE.sub(" ", line).strip() for line in text.splitlines()).strip()


def localized_full_card_url(url: str | None, lang: str = "kor") -> str | None:
    if not url:
        return None
    marker = "/cards-full/stable/"
    if marker not in url:
        return url
    return url.replace(marker, f"{marker}{lang}/", 1)


def source_image_url(relative_or_absolute: str | None) -> str | None:
    if not relative_or_absolute:
        return None
    return urllib.parse.urljoin(SITE_BASE, relative_or_absolute)


def filename_from_url(url: str) -> str:
    return Path(urllib.parse.urlsplit(url).path).name


def unique_tasks(tasks: list[DownloadTask]) -> list[DownloadTask]:
    by_path: dict[Path, DownloadTask] = {}
    for task in tasks:
        existing = by_path.get(task.local_path)
        if existing and existing.url != task.url:
            raise ValueError(
                f"Two URLs map to the same path: {existing.url!r} and {task.url!r}"
            )
        by_path[task.local_path] = task
    return sorted(by_path.values(), key=lambda item: str(item.local_path))


def download_one(task: DownloadTask, root: Path, refresh: bool) -> dict[str, Any]:
    destination = root / task.local_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    status = "cached"
    error: str | None = None
    if refresh or not destination.exists() or destination.stat().st_size == 0:
        temporary = destination.with_suffix(destination.suffix + ".part")
        try:
            payload = request_bytes(task.url)
            if len(payload) < 100:
                raise ValueError(f"response is unexpectedly small ({len(payload)} bytes)")
            temporary.write_bytes(payload)
            os.replace(temporary, destination)
            status = "downloaded"
        except Exception as exc:  # Keep collecting and report every failed asset.
            temporary.unlink(missing_ok=True)
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"

    size = destination.stat().st_size if destination.exists() else 0
    digest = ""
    if size:
        hasher = hashlib.sha256()
        with destination.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
        digest = hasher.hexdigest()
    return {
        "card_id": task.card_id,
        "kind": task.kind,
        "url": task.url,
        "local_path": task.local_path.as_posix(),
        "status": status,
        "size_bytes": size,
        "sha256": digest,
        "error": error,
    }


def keyword_entries(
    card: dict[str, Any],
    keyword_ko: dict[str, dict[str, Any]],
    keyword_en: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for raw_key in card.get("keywords_key") or []:
        key = str(raw_key).upper()
        ko = keyword_ko.get(key, {})
        en = keyword_en.get(key, {})
        values.append(
            {
                "id": key,
                "name_ko": ko.get("name"),
                "name_en": en.get("name"),
            }
        )
    return values


def variant_images(card: dict[str, Any]) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    for variant_id, variant in (card.get("type_variants") or {}).items():
        url = source_image_url(variant.get("image_url"))
        if url:
            found.append({"variant": variant_id, "url": url})
    return found


def build_card_record(
    ko: dict[str, Any],
    en: dict[str, Any],
    keyword_ko: dict[str, dict[str, Any]],
    keyword_en: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], list[DownloadTask]]:
    card_id = en["id"]
    pool_key = en.get("color") or "unknown"
    pool_ko, pool_en, is_character = POOL_LABELS.get(
        pool_key, (pool_key, pool_key, False)
    )
    full_en = en.get("image_url_card")
    full_en_upgraded = en.get("image_url_card_upg")
    full_ko = localized_full_card_url(full_en, "kor")
    full_ko_upgraded = localized_full_card_url(full_en_upgraded, "kor")
    art_url = source_image_url(en.get("image_url"))

    tasks: list[DownloadTask] = []
    image: dict[str, Any] = {
        "primary_local_path": None,
        "art": None,
        "full_ko": None,
        "full_ko_upgraded": None,
        "full_en_url": full_en,
        "full_en_upgraded_url": full_en_upgraded,
        "variant_art": [],
    }
    if art_url:
        path = Path("images/art") / filename_from_url(art_url)
        image["art"] = {"url": art_url, "local_path": path.as_posix()}
        tasks.append(DownloadTask(art_url, path, "art", card_id))
    if full_ko:
        path = Path("images/cards_ko") / filename_from_url(full_ko)
        image["full_ko"] = {"url": full_ko, "local_path": path.as_posix()}
        image["primary_local_path"] = path.as_posix()
        tasks.append(DownloadTask(full_ko, path, "full_ko", card_id))
    elif image["art"]:
        image["primary_local_path"] = image["art"]["local_path"]
    if full_ko_upgraded:
        path = Path("images/cards_ko_upgraded") / filename_from_url(full_ko_upgraded)
        image["full_ko_upgraded"] = {
            "url": full_ko_upgraded,
            "local_path": path.as_posix(),
        }
        tasks.append(DownloadTask(full_ko_upgraded, path, "full_ko_upgraded", card_id))

    for variant in variant_images(en):
        path = Path("images/art_variants") / filename_from_url(variant["url"])
        entry = {
            "variant": variant["variant"],
            "url": variant["url"],
            "local_path": path.as_posix(),
        }
        image["variant_art"].append(entry)
        tasks.append(DownloadTask(variant["url"], path, "art_variant", card_id))

    localized_keywords = keyword_entries(en, keyword_ko, keyword_en)
    record = {
        "id": card_id,
        "name": {"ko": ko.get("name"), "en": en.get("name")},
        "text": {
            "ko": {
                "description": ko.get("description"),
                "description_plain": plain_text(ko.get("description")),
                "description_raw": ko.get("description_raw"),
                "upgrade_description": ko.get("upgrade_description"),
                "upgrade_description_plain": plain_text(ko.get("upgrade_description")),
            },
            "en": {
                "description": en.get("description"),
                "description_plain": plain_text(en.get("description")),
                "description_raw": en.get("description_raw"),
                "upgrade_description": en.get("upgrade_description"),
                "upgrade_description_plain": plain_text(en.get("upgrade_description")),
            },
        },
        "character": (
            {"key": pool_key, "name_ko": pool_ko, "name_en": pool_en}
            if is_character
            else None
        ),
        "pool": {
            "key": pool_key,
            "name_ko": pool_ko,
            "name_en": pool_en,
            "is_playable_character": is_character,
        },
        "type": {
            "key": en.get("type_key"),
            "name_ko": ko.get("type"),
            "name_en": en.get("type"),
        },
        "rarity": {
            "key": en.get("rarity_key"),
            "name_ko": ko.get("rarity"),
            "name_en": en.get("rarity"),
        },
        "cost": {
            "energy": en.get("cost"),
            "is_x_cost": en.get("is_x_cost"),
            "star": en.get("star_cost"),
            "is_x_star_cost": en.get("is_x_star_cost"),
        },
        "target": en.get("target"),
        "stats": {
            "damage": en.get("damage"),
            "block": en.get("block"),
            "hit_count": en.get("hit_count"),
            "cards_draw": en.get("cards_draw"),
            "energy_gain": en.get("energy_gain"),
            "hp_loss": en.get("hp_loss"),
            "vars": en.get("vars"),
            "upgrade": en.get("upgrade"),
        },
        "keywords": localized_keywords,
        "keyword_ids": [item["id"] for item in localized_keywords],
        "powers_applied": en.get("powers_applied"),
        "tags": en.get("tags"),
        "spawns_cards": en.get("spawns_cards"),
        "type_variants": {"ko": ko.get("type_variants"), "en": en.get("type_variants")},
        "flags": {
            "can_be_generated_in_combat": en.get("can_be_generated_in_combat"),
            "multiplayer_only": en.get("multiplayer_only"),
        },
        "compendium_order": en.get("compendium_order"),
        "sources": {"ko": ko.get("sources"), "en": en.get("sources")},
        "trivia": {"ko": ko.get("trivia"), "en": en.get("trivia")},
        "image": image,
    }
    return record, tasks


def build_keyword_index(
    cards: list[dict[str, Any]],
    ko_keywords: list[dict[str, Any]],
    en_keywords: list[dict[str, Any]],
    ko_glossary: list[dict[str, Any]],
    en_glossary: list[dict[str, Any]],
) -> dict[str, Any]:
    en_keyword_by_id = {item["id"].upper(): item for item in en_keywords}
    cards_by_keyword: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for card in cards:
        for keyword_id in card["keyword_ids"]:
            cards_by_keyword[keyword_id].append(
                {
                    "id": card["id"],
                    "name_ko": card["name"]["ko"],
                    "name_en": card["name"]["en"],
                    "pool": card["pool"]["key"],
                }
            )

    card_keywords: list[dict[str, Any]] = []
    for ko_item in sorted(ko_keywords, key=lambda item: item["id"]):
        key = ko_item["id"].upper()
        en_item = en_keyword_by_id.get(key, {})
        linked_cards = sorted(cards_by_keyword.get(key, []), key=lambda item: item["id"])
        card_keywords.append(
            {
                "id": key,
                "name_ko": ko_item.get("name"),
                "name_en": en_item.get("name"),
                "description_ko": ko_item.get("description"),
                "description_en": en_item.get("description"),
                "card_count": len(linked_cards),
                "cards": linked_cards,
            }
        )

    en_glossary_by_id = {item["id"].upper(): item for item in en_glossary}
    glossary_terms: list[dict[str, Any]] = []
    for ko_item in sorted(ko_glossary, key=lambda item: item["id"]):
        key = ko_item["id"].upper()
        en_item = en_glossary_by_id.get(key, {})
        ko_name = plain_text(ko_item.get("name")).casefold()
        en_name = plain_text(en_item.get("name")).casefold()
        mentions: list[dict[str, Any]] = []
        for card in cards:
            ko_haystack = " ".join(
                (
                    card["text"]["ko"]["description_plain"],
                    card["text"]["ko"]["upgrade_description_plain"],
                )
            ).casefold()
            en_haystack = " ".join(
                (
                    card["text"]["en"]["description_plain"],
                    card["text"]["en"]["upgrade_description_plain"],
                )
            ).casefold()
            if (ko_name and ko_name in ko_haystack) or (en_name and en_name in en_haystack):
                mentions.append(
                    {
                        "id": card["id"],
                        "name_ko": card["name"]["ko"],
                        "name_en": card["name"]["en"],
                        "pool": card["pool"]["key"],
                    }
                )
        glossary_terms.append(
            {
                "id": key,
                "category": ko_item.get("category") or en_item.get("category"),
                "name_ko": ko_item.get("name"),
                "name_en": en_item.get("name"),
                "description_ko": ko_item.get("description"),
                "description_en": en_item.get("description"),
                "mentioned_card_count": len(mentions),
                "mentioned_cards": sorted(mentions, key=lambda item: item["id"]),
                "match_method": "localized term-name substring in normal/upgraded card text",
            }
        )
    return {
        "generated_at": utc_now(),
        "card_keywords": card_keywords,
        "glossary_terms": glossary_terms,
    }


def card_csv_rows(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for card in cards:
        image = card["image"]
        rows.append(
            {
                "id": card["id"],
                "name_ko": card["name"]["ko"],
                "name_en": card["name"]["en"],
                "class_key": card["character"]["key"] if card["character"] else "",
                "class_ko": card["character"]["name_ko"] if card["character"] else "",
                "class_en": card["character"]["name_en"] if card["character"] else "",
                "pool_key": card["pool"]["key"],
                "pool_ko": card["pool"]["name_ko"],
                "pool_en": card["pool"]["name_en"],
                "type_key": card["type"]["key"],
                "type_ko": card["type"]["name_ko"],
                "type_en": card["type"]["name_en"],
                "rarity_key": card["rarity"]["key"],
                "rarity_ko": card["rarity"]["name_ko"],
                "rarity_en": card["rarity"]["name_en"],
                "cost": card["cost"]["energy"],
                "star_cost": card["cost"]["star"],
                "description_ko": card["text"]["ko"]["description_plain"],
                "upgrade_description_ko": card["text"]["ko"][
                    "upgrade_description_plain"
                ],
                "description_en": card["text"]["en"]["description_plain"],
                "upgrade_description_en": card["text"]["en"][
                    "upgrade_description_plain"
                ],
                "keyword_ids": "|".join(card["keyword_ids"]),
                "keyword_names_ko": "|".join(
                    item["name_ko"] or "" for item in card["keywords"]
                ),
                "keyword_names_en": "|".join(
                    item["name_en"] or "" for item in card["keywords"]
                ),
                "multiplayer_only": bool(card["flags"]["multiplayer_only"]),
                "primary_image_path": image["primary_local_path"] or "",
                "art_path": image["art"]["local_path"] if image["art"] else "",
                "full_card_ko_path": (
                    image["full_ko"]["local_path"] if image["full_ko"] else ""
                ),
                "full_card_ko_upgraded_path": (
                    image["full_ko_upgraded"]["local_path"]
                    if image["full_ko_upgraded"]
                    else ""
                ),
            }
        )
    return rows


def keyword_csv_rows(index: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in index["card_keywords"]:
        rows.append(
            {
                "index_type": "card_keyword",
                "id": item["id"],
                "category": "",
                "name_ko": item["name_ko"],
                "name_en": item["name_en"],
                "description_ko": plain_text(item["description_ko"]),
                "description_en": plain_text(item["description_en"]),
                "card_count": item["card_count"],
                "card_ids": "|".join(card["id"] for card in item["cards"]),
            }
        )
    for item in index["glossary_terms"]:
        rows.append(
            {
                "index_type": "glossary_term",
                "id": item["id"],
                "category": item["category"],
                "name_ko": item["name_ko"],
                "name_en": item["name_en"],
                "description_ko": plain_text(item["description_ko"]),
                "description_en": plain_text(item["description_en"]),
                "card_count": item["mentioned_card_count"],
                "card_ids": "|".join(card["id"] for card in item["mentioned_cards"]),
            }
        )
    return rows


def write_summary(
    path: Path,
    cards: list[dict[str, Any]],
    image_manifest: list[dict[str, Any]],
    source_snapshot: dict[str, Any],
) -> None:
    pool_counts = Counter(card["pool"]["key"] for card in cards)
    rarity_counts = Counter(card["rarity"]["key"] for card in cards)
    failed = [item for item in image_manifest if item["status"] == "failed"]
    blocking_failed = [item for item in failed if item["kind"] != "full_ko_upgraded"]
    lines = [
        "# Slay the Spire 2 카드 데이터셋",
        "",
        f"- 수집 시각(UTC): `{source_snapshot['retrieved_at']}`",
        f"- 카드 수: **{len(cards)}**",
        f"- 이미지 파일: **{sum(item['status'] != 'failed' for item in image_manifest)}**",
        f"- 필수 이미지 실패: **{len(blocking_failed)}**",
        f"- 선택적 강화 이미지 실패: **{len(failed) - len(blocking_failed)}**",
        f"- 출처: [{SOURCE_NAME}]({SITE_BASE})",
        "",
        "## 카드 풀",
        "",
        "| 풀 | 카드 수 |",
        "|---|---:|",
    ]
    for key, count in sorted(pool_counts.items()):
        ko, en, _ = POOL_LABELS.get(key, (key, key, False))
        lines.append(f"| {ko} (`{en}`) | {count} |")
    lines.extend(
        [
            "",
            "## 희귀도",
            "",
            "| 희귀도 키 | 카드 수 |",
            "|---|---:|",
        ]
    )
    for key, count in sorted(rarity_counts.items()):
        lines.append(f"| `{key}` | {count} |")
    lines.extend(
        [
            "",
            "## 주요 파일",
            "",
            "- `cards.json`: 모든 필드를 포함한 한·영 병합 데이터",
            "- `cards.csv`: 스프레드시트용 평면 데이터(UTF-8 BOM)",
            "- `keyword_index.json`: 카드 키워드와 게임 용어별 카드 역색인",
            "- `keyword_index.csv`: 키워드/용어 역색인의 평면 버전",
            "- `image_manifest.json`: 이미지 URL, 로컬 경로, 크기, SHA-256, 상태",
            "- `validation_report.json`: 누락·중복·다운로드 실패 검증",
            "",
            "게임 데이터와 이미지는 Mega Crit Games의 저작물입니다. 이 데이터셋은",
            "개인적 조사·참조 목적으로 사용하고 게임 재배포 용도로 사용하지 마세요.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Project output directory",
    )
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument(
        "--refresh-images",
        action="store_true",
        help="Download images again even if a local file already exists",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    raw_dir = root / "data/raw"
    processed_dir = root / "data/processed"
    raw_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)

    endpoints: dict[str, tuple[str, str | None]] = {
        "cards_ko": ("cards", "kor"),
        "cards_en": ("cards", "eng"),
        "keywords_ko": ("keywords", "kor"),
        "keywords_en": ("keywords", "eng"),
        "glossary_ko": ("glossary", "kor"),
        "glossary_en": ("glossary", "eng"),
        "characters_ko": ("characters", "kor"),
        "characters_en": ("characters", "eng"),
        "stats_ko": ("stats", "kor"),
        "changelogs": ("changelogs", None),
    }
    raw: dict[str, Any] = {}
    source_urls: dict[str, str] = {}
    for name, (endpoint, lang) in endpoints.items():
        url = api_url(endpoint, lang)
        print(f"Fetching {url}", flush=True)
        value = fetch_json(url)
        raw[name] = value
        source_urls[name] = url
        write_json(raw_dir / f"{name}.json", value)

    cards_ko = {item["id"]: item for item in raw["cards_ko"]}
    cards_en = {item["id"]: item for item in raw["cards_en"]}
    if cards_ko.keys() != cards_en.keys():
        missing_ko = sorted(cards_en.keys() - cards_ko.keys())
        missing_en = sorted(cards_ko.keys() - cards_en.keys())
        raise RuntimeError(f"Language card IDs differ: missing_ko={missing_ko}, missing_en={missing_en}")

    keyword_ko = {item["id"].upper(): item for item in raw["keywords_ko"]}
    keyword_en = {item["id"].upper(): item for item in raw["keywords_en"]}
    cards: list[dict[str, Any]] = []
    tasks: list[DownloadTask] = []
    for card_id in sorted(cards_en):
        record, card_tasks = build_card_record(
            cards_ko[card_id],
            cards_en[card_id],
            keyword_ko,
            keyword_en,
        )
        cards.append(record)
        tasks.extend(card_tasks)

    tasks = unique_tasks(tasks)
    print(f"Downloading/verifying {len(tasks)} unique images with {args.workers} workers")
    image_manifest: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(download_one, task, root, args.refresh_images): task
            for task in tasks
        }
        for index, future in enumerate(as_completed(futures), start=1):
            image_manifest.append(future.result())
            if index % 100 == 0 or index == len(tasks):
                print(f"  images {index}/{len(tasks)}", flush=True)
    image_manifest.sort(key=lambda item: (item["kind"], item["card_id"], item["local_path"]))
    image_status = {item["local_path"]: item["status"] for item in image_manifest}
    for card in cards:
        primary = card["image"]["primary_local_path"]
        if primary and image_status.get(primary) == "failed":
            art = card["image"]["art"]
            card["image"]["primary_local_path"] = (
                art["local_path"]
                if art and image_status.get(art["local_path"]) != "failed"
                else None
            )

    keyword_index = build_keyword_index(
        cards,
        raw["keywords_ko"],
        raw["keywords_en"],
        raw["glossary_ko"],
        raw["glossary_en"],
    )
    card_rows = card_csv_rows(cards)
    keyword_rows = keyword_csv_rows(keyword_index)

    write_json(processed_dir / "cards.json", cards)
    write_csv(
        processed_dir / "cards.csv",
        card_rows,
        [
            "id",
            "name_ko",
            "name_en",
            "class_key",
            "class_ko",
            "class_en",
            "pool_key",
            "pool_ko",
            "pool_en",
            "type_key",
            "type_ko",
            "type_en",
            "rarity_key",
            "rarity_ko",
            "rarity_en",
            "cost",
            "star_cost",
            "description_ko",
            "upgrade_description_ko",
            "description_en",
            "upgrade_description_en",
            "keyword_ids",
            "keyword_names_ko",
            "keyword_names_en",
            "multiplayer_only",
            "primary_image_path",
            "art_path",
            "full_card_ko_path",
            "full_card_ko_upgraded_path",
        ],
    )
    write_json(processed_dir / "keyword_index.json", keyword_index)
    write_csv(
        processed_dir / "keyword_index.csv",
        keyword_rows,
        [
            "index_type",
            "id",
            "category",
            "name_ko",
            "name_en",
            "description_ko",
            "description_en",
            "card_count",
            "card_ids",
        ],
    )
    write_json(processed_dir / "image_manifest.json", image_manifest)

    retrieved_at = utc_now()
    source_snapshot = {
        "retrieved_at": retrieved_at,
        "source_name": SOURCE_NAME,
        "source_site": SITE_BASE,
        "source_repository": SOURCE_REPOSITORY,
        "api_endpoints": source_urls,
        "latest_changelog": raw["changelogs"][0] if raw["changelogs"] else None,
        "reported_card_count": raw["stats_ko"].get("cards"),
    }
    write_json(processed_dir / "source_snapshot.json", source_snapshot)

    duplicate_ids = sorted(
        card_id for card_id, count in Counter(card["id"] for card in cards).items() if count > 1
    )
    missing_required = [
        card["id"]
        for card in cards
        if not (
            card["name"]["ko"]
            and card["name"]["en"]
            and card["text"]["ko"]["description"] is not None
            and card["rarity"]["key"]
            and card["pool"]["key"]
            and card["image"]["primary_local_path"]
        )
    ]
    missing_full_renders = [
        card["id"] for card in cards if not card["image"]["full_ko"]
    ]
    failed_images = [item for item in image_manifest if item["status"] == "failed"]
    blocking_failed_images = [
        item for item in failed_images if item["kind"] != "full_ko_upgraded"
    ]
    optional_failed_images = [
        item for item in failed_images if item["kind"] == "full_ko_upgraded"
    ]
    validation = {
        "generated_at": retrieved_at,
        "ok": not duplicate_ids
        and not missing_required
        and not blocking_failed_images
        and len(cards) == raw["stats_ko"].get("cards"),
        "card_count": len(cards),
        "api_reported_card_count": raw["stats_ko"].get("cards"),
        "duplicate_card_ids": duplicate_ids,
        "missing_required_fields": missing_required,
        "cards_without_full_render": missing_full_renders,
        "cards_without_full_render_note": (
            "MAD_SCIENCE is a runtime-composed multi-variant card; its attack, skill, "
            "and power artwork is stored under images/art_variants."
        ),
        "image_task_count": len(image_manifest),
        "image_success_count": sum(item["status"] != "failed" for item in image_manifest),
        "image_failure_count": len(failed_images),
        "blocking_image_failure_count": len(blocking_failed_images),
        "optional_image_failure_count": len(optional_failed_images),
        "blocking_failed_images": blocking_failed_images,
        "optional_failed_images": optional_failed_images,
        "failed_images": failed_images,
        "pool_counts": dict(sorted(Counter(card["pool"]["key"] for card in cards).items())),
        "rarity_counts": dict(
            sorted(Counter(card["rarity"]["key"] for card in cards).items())
        ),
        "keyword_counts": dict(
            sorted(
                Counter(
                    keyword_id for card in cards for keyword_id in card["keyword_ids"]
                ).items()
            )
        ),
    }
    write_json(processed_dir / "validation_report.json", validation)
    write_summary(processed_dir / "README.md", cards, image_manifest, source_snapshot)

    print(json.dumps(validation, ensure_ascii=False, indent=2))
    return 0 if validation["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
