#!/usr/bin/env python3
"""Fetch one SkyBlock profile and save only the fields needed for analysis."""
import base64
import gzip
import json
import os
import re
import struct
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API_URL = "https://api.hypixel.net/v2/skyblock/profiles?uuid={uuid}"
OUTPUT = Path("data/stats.json")


class Reader:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def take(self, size):
        chunk = self.data[self.pos:self.pos + size]
        if len(chunk) != size:
            raise ValueError("unexpected end of NBT data")
        self.pos += size
        return chunk

    def unpack(self, fmt):
        return struct.unpack(">" + fmt, self.take(struct.calcsize(">" + fmt)))[0]

    def string(self):
        return self.take(self.unpack("H")).decode("utf-8", "replace")


def read_tag(reader, tag):
    if tag == 1:
        return reader.unpack("b")
    if tag == 2:
        return reader.unpack("h")
    if tag == 3:
        return reader.unpack("i")
    if tag == 4:
        return reader.unpack("q")
    if tag == 5:
        return reader.unpack("f")
    if tag == 6:
        return reader.unpack("d")
    if tag == 7:
        return reader.take(reader.unpack("i"))
    if tag == 8:
        return reader.string()
    if tag == 9:
        item_tag = reader.unpack("b")
        return [read_tag(reader, item_tag) for _ in range(reader.unpack("i"))]
    if tag == 10:
        result = {}
        while True:
            child = reader.unpack("b")
            if child == 0:
                return result
            name = reader.string()
            result[name] = read_tag(reader, child)
    if tag == 11:
        return [reader.unpack("i") for _ in range(reader.unpack("i"))]
    if tag == 12:
        return [reader.unpack("q") for _ in range(reader.unpack("i"))]
    raise ValueError(f"unknown NBT tag {tag}")


def decode_items(blob):
    """Return readable items from a base64 + gzip NBT inventory blob."""
    if not isinstance(blob, str) or not blob:
        return []
    reader = Reader(gzip.decompress(base64.b64decode(blob)))
    root_tag = reader.unpack("b")
    reader.string()
    root = read_tag(reader, root_tag)
    items = []
    for raw in root.get("i", []):
        tag = raw.get("tag") if isinstance(raw, dict) else None
        if not tag:
            continue
        extra = tag.get("ExtraAttributes", {})
        name = tag.get("display", {}).get("Name", "")
        item = {"name": re.sub("§.", "", name), "id": extra.get("id")}
        for source, target in (
            ("modifier", "reforge"),
            ("enchantments", "enchantments"),
            ("upgrade_level", "stars"),
            ("rarity_upgrades", "recombobulated"),
        ):
            if source in extra:
                item[target] = extra[source]
        items.append(item)
    return items


def nested(data, *path, default=None):
    for key in path:
        if not isinstance(data, dict) or key not in data:
            return default
        data = data[key]
    return data


def items_from(member, *path):
    try:
        return decode_items(nested(member, *path, "data"))
    except Exception as error:  # A format change in one section should not stop the rest.
        return {"error": str(error)}


def summarize(profile, uuid):
    member = profile["members"][uuid]
    experience = nested(member, "player_data", "experience", default={})
    catacombs = nested(member, "dungeons", "dungeon_types", "catacombs", default={})
    classes = nested(member, "dungeons", "player_classes", default={})
    slayers = nested(member, "slayer", "slayer_bosses", default={})
    pets = nested(member, "pets_data", "pets", default=[])
    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "profile": profile.get("cute_name"),
        "game_mode": profile.get("game_mode", "normal"),
        "skyblock_xp": nested(member, "leveling", "experience"),
        "skills_xp": {
            key.removeprefix("SKILL_").lower(): value for key, value in experience.items()
        },
        "dungeons": {
            "catacombs_xp": catacombs.get("experience"),
            "class_xp": {key: value.get("experience") for key, value in classes.items()},
            "floor_completions": catacombs.get("tier_completions", {}),
            "master_completions": nested(
                member, "dungeons", "dungeon_types", "master_catacombs", "tier_completions",
                default={},
            ),
        },
        "slayer_xp": {key: value.get("xp", 0) for key, value in slayers.items()},
        "pets": [
            {
                "type": pet.get("type"),
                "tier": pet.get("tier"),
                "exp": pet.get("exp"),
                "active": pet.get("active"),
                "item": pet.get("heldItem"),
            }
            for pet in pets
        ],
        "gear": {
            "armor": items_from(member, "inventory", "inv_armor"),
            "equipment": items_from(member, "inventory", "equipment_contents"),
            "inventory": items_from(member, "inventory", "inv_contents"),
            "wardrobe": items_from(member, "inventory", "wardrobe_contents"),
            "accessories": items_from(member, "inventory", "bag_contents", "talisman_bag"),
        },
        "accessory_power": nested(member, "accessory_bag_storage", "highest_magical_power"),
        "hotm": nested(member, "mining_core", "experience"),
        "member_keys": sorted(member),
    }


def main():
    key = os.environ.get("HYPIXEL_API_KEY")
    uuid = os.environ.get("MINECRAFT_UUID", "").replace("-", "")
    profile_name = os.environ.get("PROFILE_NAME", "Kiwi")
    if not key or not uuid:
        sys.exit("HYPIXEL_API_KEY and MINECRAFT_UUID are required")

    request = urllib.request.Request(
        API_URL.format(uuid=uuid),
        headers={"API-Key": key, "User-Agent": "skyblock-stats"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        sys.exit(f"Hypixel API returned HTTP {error.code}: {error.reason}")

    profiles = payload.get("profiles") or []
    match = next((p for p in profiles if p.get("cute_name") == profile_name), None)
    if match is None:
        names = [p.get("cute_name") for p in profiles]
        sys.exit(f"Profile {profile_name!r} not found. Available: {names}")

    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_text(
        json.dumps(summarize(match, uuid), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Saved {OUTPUT}")


if __name__ == "__main__":
    main()
