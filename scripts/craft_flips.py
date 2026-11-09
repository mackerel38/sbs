#!/usr/bin/env python3
"""List crafting recipes this profile can use and estimate their profit.

Recipes come from the NotEnoughUpdates item repository. Unlocked collections,
slayer levels and prices come from the Hypixel API. The result is written to
data/craft_flips.json.
"""
import base64
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from fetch_stats import Reader, read_tag

API = "https://api.hypixel.net/v2"
OUTPUT = Path("data/craft_flips.json")
BAZAAR_TAX = 0.0125  # Assumed default Bazaar tax.
AUCTION_TAX = 0.035  # Assumed listing fee plus claim tax, rounded up.
ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}
REQUIREMENT = re.compile(r"Requires:?\s*(.*?)\s+([IVXLC]+|\d+)$")
SLAYERS = {
    "Zombie Slayer": "zombie",
    "Spider Slayer": "spider",
    "Wolf Slayer": "wolf",
    "Enderman Slayer": "enderman",
    "Blaze Slayer": "blaze",
    "Vampire Slayer": "vampire",
}


REQUEST_INTERVAL = 1.0  # Seconds between API requests.
_last_request = 0.0


def get_json(url, key=None):
    global _last_request
    wait = _last_request + REQUEST_INTERVAL - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()
    headers = {"User-Agent": "sbs-craft-flips"}
    if key:
        headers["API-Key"] = key
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def roman_to_int(text):
    if text.isdigit():
        return int(text)
    total = 0
    for i, char in enumerate(text):
        value = ROMAN[char]
        if i + 1 < len(text) and ROMAN[text[i + 1]] > value:
            total -= value
        else:
            total += value
    return total


def unlocked_collections(member, resources):
    """Return {collection display name: highest unlocked tier}."""
    counts = member.get("collection", {})
    unlocked = {}
    for category in resources.get("collections", {}).values():
        for item_id, info in category.get("items", {}).items():
            amount = counts.get(item_id, 0)
            tier = 0
            for step in info.get("tiers", []):
                if amount >= step.get("amountRequired", 0):
                    tier = max(tier, step.get("tier", 0))
            unlocked[info.get("name", item_id)] = tier
    return unlocked


def slayer_levels(member):
    bosses = member.get("slayer", {}).get("slayer_bosses", {})
    levels = {}
    for boss, info in bosses.items():
        claimed = [
            int(key.split("_")[1])
            for key in info.get("claimed_levels", {}) if key.startswith("level_")
            and key.split("_")[1].isdigit()
        ]
        levels[boss] = max(claimed, default=0)
    return levels


def requirement_status(text, collections, slayers):
    """Return (True, None) when met, or (False, reason)."""
    if not text:
        return True, None
    match = REQUIREMENT.match(text)
    if not match:
        return False, text
    name, level = match.group(1), roman_to_int(match.group(2))
    if name in SLAYERS:
        if slayers.get(SLAYERS[name], 0) >= level:
            return True, None
        return False, text
    if name in collections:
        if collections[name] >= level:
            return True, None
        return False, text
    return False, f"{text} (not checked)"


def load_items(repo):
    items = {}
    for path in (repo / "items").glob("*.json"):
        try:
            items[path.stem] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return items


def crafting_recipes(item):
    """Yield (ingredients {id: count}, output count) for crafting-table recipes."""
    grids = []
    if isinstance(item.get("recipe"), dict):
        grids.append((item["recipe"], item["recipe"].get("count", 1)))
    for recipe in item.get("recipes", []):
        if recipe.get("type") == "crafting":
            grids.append((recipe, recipe.get("count", 1)))
    for grid, count in grids:
        ingredients = {}
        for slot in ("A1", "A2", "A3", "B1", "B2", "B3", "C1", "C2", "C3"):
            value = grid.get(slot) or ""
            if not value:
                continue
            item_id, _, amount = value.partition(":")
            ingredients[item_id] = ingredients.get(item_id, 0) + float(amount or 1)
        if ingredients:
            yield ingredients, int(count or 1)


def npc_prices(items):
    """Return {item id: cheapest coins-only NPC price per unit}."""
    prices = {}
    for item in items.values():
        for recipe in item.get("recipes", []):
            if recipe.get("type") != "npc_shop":
                continue
            cost = recipe.get("cost", [])
            if len(cost) != 1 or not cost[0].startswith("SKYBLOCK_COIN:"):
                continue
            result_id, _, amount = (recipe.get("result") or "").partition(":")
            if not result_id:
                continue
            per_unit = float(cost[0].split(":")[1]) / float(amount or 1)
            prices[result_id] = min(per_unit, prices.get(result_id, per_unit))
    return prices


def is_mutation(item):
    return any("MUTATION" in line for line in item.get("lore", [])[-3:])


def auction_item_id(item_bytes):
    reader = Reader(gzip.decompress(base64.b64decode(item_bytes)))
    root_tag = reader.unpack("b")
    reader.string()
    root = read_tag(reader, root_tag)
    items = root.get("i", [])
    if not items:
        return None
    extra = items[0].get("tag", {}).get("ExtraAttributes", {})
    item_id = extra.get("id")
    if item_id == "PET":
        try:
            pet = json.loads(extra.get("petInfo", "{}"))
            return f"{pet.get('type')};{pet.get('tier')}"
        except ValueError:
            return None
    return item_id


def lowest_bins():
    first = get_json(f"{API}/skyblock/auctions?page=0")
    pages = [first]
    for number in range(1, first.get("totalPages", 1)):
        pages.append(get_json(f"{API}/skyblock/auctions?page={number}"))
    prices, listings = {}, {}
    for page in pages:
        for auction in page.get("auctions", []):
            if not auction.get("bin") or auction.get("claimed"):
                continue
            try:
                item_id = auction_item_id(auction["item_bytes"])
            except Exception:
                continue
            if not item_id:
                continue
            price = auction.get("starting_bid", 0)
            listings[item_id] = listings.get(item_id, 0) + 1
            if item_id not in prices or price < prices[item_id]:
                prices[item_id] = price
    return prices, listings


def main():
    key = os.environ.get("HYPIXEL_API_KEY")
    uuid = os.environ.get("MINECRAFT_UUID", "").replace("-", "")
    profile_name = os.environ.get("PROFILE_NAME", "Kiwi")
    repo = Path(os.environ.get("NEU_REPO", "neu-repo"))
    if not key or not uuid:
        sys.exit("HYPIXEL_API_KEY and MINECRAFT_UUID are required")

    try:
        profiles = get_json(f"{API}/skyblock/profiles?uuid={uuid}", key).get("profiles") or []
    except urllib.error.HTTPError as error:
        sys.exit(f"Hypixel API returned HTTP {error.code}: {error.reason}")
    profile = next((p for p in profiles if p.get("cute_name") == profile_name), None)
    if profile is None:
        sys.exit(f"Profile {profile_name!r} not found")
    member = profile["members"][uuid]
    if "collection" not in member:
        sys.exit("Collection data is hidden. Enable the Collections API setting in game.")

    collections = unlocked_collections(member, get_json(f"{API}/resources/skyblock/collections"))
    slayers = slayer_levels(member)
    bazaar = get_json(f"{API}/skyblock/bazaar").get("products", {})
    bins, listings = lowest_bins()
    items = load_items(repo)
    npc = npc_prices(items)

    def market(item_id):
        """Return (instant buy price, buy order price, source) or None."""
        status = bazaar.get(item_id, {}).get("quick_status")
        if status and status.get("buyPrice", 0) > 0:
            order = status.get("sellPrice") or status["buyPrice"]
            return status["buyPrice"], order, "bazaar"
        if item_id in bins:
            return bins[item_id], bins[item_id], "auction"
        if item_id in npc:
            return npc[item_id], npc[item_id], "npc"
        return None

    cache = {}

    def unit_cost(item_id, depth=0):
        """Cheapest (instant, order, note) way to get one unit."""
        if item_id in cache:
            return cache[item_id]
        if is_mutation(items.get(item_id, {})):
            cache[item_id] = None
            return None
        best = market(item_id)
        if best is not None:
            best = (best[0], best[1], best[2])
        if depth < 3:
            for ingredients, count in crafting_recipes(items.get(item_id, {})):
                ok, _ = requirement_status(
                    items.get(item_id, {}).get("crafttext", ""), collections, slayers
                )
                if not ok:
                    continue
                total = recipe_cost(ingredients, depth + 1)
                if total is None:
                    continue
                crafted = (total[0] / count, total[1] / count, "craft")
                if best is None or crafted[0] < best[0]:
                    best = crafted
        cache[item_id] = best
        return best

    def recipe_cost(ingredients, depth):
        instant = order = 0.0
        for item_id, amount in ingredients.items():
            cost = unit_cost(item_id, depth)
            if cost is None:
                return None
            instant += cost[0] * amount
            order += cost[1] * amount
        return instant, order

    results = []
    skipped = {"requirement": 0, "unpriced_input": 0, "unpriced_output": 0}
    for item_id, item in items.items():
        for ingredients, count in crafting_recipes(item):
            ok, reason = requirement_status(item.get("crafttext", ""), collections, slayers)
            if not ok:
                skipped["requirement"] += 1
                continue
            cost = recipe_cost(ingredients, 1)
            if cost is None:
                skipped["unpriced_input"] += 1
                continue
            status = bazaar.get(item_id, {}).get("quick_status")
            if status and status.get("buyPrice", 0) > 0:
                sell_instant = status.get("sellPrice", 0) * count * (1 - BAZAAR_TAX)
                sell_order = status["buyPrice"] * count * (1 - BAZAAR_TAX)
                volume = round(status.get("sellMovingWeek", 0) / 7)
                where = "bazaar"
            elif item_id in bins:
                sell_instant = sell_order = bins[item_id] * count * (1 - AUCTION_TAX)
                volume = None
                where = "auction"
            else:
                skipped["unpriced_output"] += 1
                continue
            results.append({
                "item": item_id,
                "name": re.sub("§.", "", item.get("displayname", item_id)),
                "requirement": item.get("crafttext") or None,
                "output_count": count,
                "ingredients": ingredients,
                "cost_instant_buy": round(cost[0]),
                "cost_buy_orders": round(cost[1]),
                "sell_instant": round(sell_instant),
                "sell_order_or_bin": round(sell_order),
                "profit_safe": round(sell_instant - cost[0]),
                "profit_patient": round(sell_order - cost[1]),
                "sell_where": where,
                "bazaar_daily_sales": volume,
                "auction_listings": listings.get(item_id),
            })

    results.sort(key=lambda r: r["profit_patient"], reverse=True)
    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_text(json.dumps({
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "assumptions": {
            "bazaar_tax": BAZAAR_TAX,
            "auction_tax": AUCTION_TAX,
            "profit_safe": "buy inputs instantly, sell output instantly",
            "profit_patient": "buy inputs with buy orders, sell with sell orders or at lowest BIN",
            "mutations": "excluded, because they need analysis before Bazaar trading",
        },
        "unlocked_collections": collections,
        "slayer_levels": slayers,
        "skipped": skipped,
        "recipes": [r for r in results if r["profit_patient"] > 0][:300],
    }, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"Saved {OUTPUT} with {len(results)} priced recipes")


if __name__ == "__main__":
    main()
