#!/usr/bin/env python3
"""Add recent auction sales to the best craft flip candidates.

Lowest BIN only shows asking prices, so this looks up what items actually
sold for over the past week (SkyCofl price history) and recalculates profit.
"""
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

DATA = Path("data/craft_flips.json")
HISTORY = "https://sky.coflnet.com/api/item/price/{item}/history/week"
AUCTION_TAX = 0.035
LIMIT = 60


def get_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": "sbs-craft-flips"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def summarize(points):
    points = [p for p in points if isinstance(p, dict)]
    volume = sum(p.get("volume", 0) or 0 for p in points)
    if not volume:
        return {"week_sales": 0}
    weighted = sum((p.get("avg", 0) or 0) * (p.get("volume", 0) or 0) for p in points)
    return {
        "week_sales": volume,
        "week_avg_sold": round(weighted / volume),
        "week_min_sold": round(min(p.get("min", 0) or 0 for p in points if p.get("volume"))),
    }


def main():
    data = json.loads(DATA.read_text(encoding="utf-8"))
    pool = {r["item"]: r for r in data.get("collection_recipes", [])}
    for r in data.get("recipes", []):
        pool.setdefault(r["item"], r)
    candidates = sorted(
        (r for r in pool.values() if r["sell_where"] == "auction"),
        key=lambda r: (r["requirement"] is None, -r["profit_patient"]),
    )[:LIMIT]

    checked = []
    for recipe in candidates:
        result = dict(recipe)
        try:
            result.update(summarize(get_json(HISTORY.format(item=recipe["item"]))))
        except (urllib.error.URLError, ValueError, TimeoutError) as error:
            result["sales_error"] = str(error)
        if result.get("week_avg_sold"):
            sold = result["week_avg_sold"] * recipe["output_count"] * (1 - AUCTION_TAX)
            result["profit_by_sales"] = round(sold - recipe["cost_instant_buy"])
        checked.append(result)
        time.sleep(1)

    checked.sort(key=lambda r: r.get("profit_by_sales", float("-inf")), reverse=True)
    data["sales_checked"] = checked

    extra = [
        ("npc_flips_checked", [
            r for r in data.get("npc_flips", []) if r["sell_where"] == "auction"
        ][:40], "npc_price"),
        ("forge_checked", [
            r for r in data.get("forge", [])
            if r["sell_where"] == "auction" and r["requirement_met"]
        ][:20], "cost_instant_buy"),
    ]
    for key, rows, cost_field in extra:
        results = []
        for recipe in rows:
            result = dict(recipe)
            try:
                result.update(summarize(get_json(HISTORY.format(item=recipe["item"]))))
            except (urllib.error.URLError, ValueError, TimeoutError) as error:
                result["sales_error"] = str(error)
            if result.get("week_avg_sold"):
                sold = result["week_avg_sold"] * (1 - AUCTION_TAX)
                result["profit_by_sales"] = round(sold - recipe[cost_field])
            results.append(result)
            time.sleep(1)
        results.sort(key=lambda r: r.get("profit_by_sales", float("-inf")), reverse=True)
        data[key] = results
    DATA.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"Checked sales for {len(checked)} recipes")


if __name__ == "__main__":
    main()
