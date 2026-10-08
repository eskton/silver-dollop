"""Выгода по рынку: настоящий минимум по номиналу — поиск по числу + сортировка по цене,
а не первые страницы каталога (там сверху продвигаемые и новые, не дешёвые)."""
import asyncio, json, sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from bot.playerok.client import PlayerokClient, RawResponse
from bot.services import pricing

# каталог: 200 лотов; самые дешёвые 50/4500 — в самом конце «естественного» порядка
CATALOG = []
for k in range(190):
    n = [50, 100, 800, 4500][k % 4]
    CATALOG.append({"id": f"x{k}", "slug": f"x{k}", "name": f"💰 {n} РОБУКСОВ", "price": {50: 140, 100: 200, 800: 1200, 4500: 5600}[n] + 1 + (k * 37) % 50,
                    "obtainingType": {"id": "code"}, "user": {"id": f"u{k}"}})
CATALOG += [
    {"id": "c50", "slug": "c50", "name": "50 робуксов промокод", "price": 99, "obtainingType": {"id": "code"}, "user": {"id": "a"}},
    {"id": "c4500", "slug": "c4500", "name": "4500 робуксов", "price": 4999, "obtainingType": {"id": "code"}, "user": {"id": "b"}},
    {"id": "mine", "slug": "mine", "name": "50 робуксов", "price": 10, "obtainingType": {"id": "code"}, "user": {"id": "me"}},
    {"id": "nick", "slug": "nick", "name": "50 робуксов по нику", "price": 5, "obtainingType": {"id": "nick"}, "user": {"id": "c"}},
]
M = {"mode": "full", "calls": 0}

def page(lots, after):
    start = int(after or 0)
    chunk = lots[start:start + 24]
    return {"data": {"items": {"edges": [{"node": n} for n in chunk],
                               "pageInfo": {"hasNextPage": start + 24 < len(lots), "endCursor": str(start + 24)}}}}

async def pk(body, token):
    op, v = body["operationName"], body["variables"]
    if op == "item":
        return RawResponse(200, json.dumps({"data": {"item": {"id": "m", "name": "x", "category": {"id": "cat"},
                           "obtainingType": {"id": "code", "name": "Промокод"}}}}), None)
    assert op == "items"
    M["calls"] += 1
    f = v["filter"]
    lots = [l for l in CATALOG if not f.get("obtainingTypeId") or l["obtainingType"]["id"] == f["obtainingTypeId"]]
    q = f.get("searchQuery")
    if q and M["mode"] != "nosearch":
        lots = [l for l in lots if q in l["name"]]
    if v.get("sort") == {"field": "price", "direction": "ASC"} and M["mode"] not in ("nosort", "nosearch"):
        lots = sorted(lots, key=lambda l: l["price"])
    return RawResponse(200, json.dumps(page(lots, v["pagination"]["after"])), None)

async def main():
    targets = [50, 100, 800, 4500]
    for mode in ("full", "nosort", "nosearch"):
        M.update(mode=mode, calls=0)
        PlayerokClient._sort_ok = PlayerokClient._search_ok = True
        c = PlayerokClient("T", transport=pk)
        scan = await pricing.market_scan(c, "https://playerok.com/products/x", own_user_id="me", targets=targets)
        got = {n: scan.best[n].price for n in scan.best}
        assert got[50] == 99 and got[4500] == 4999, (mode, got)      # настоящий минимум, не свой (10), не «по нику» (5)
        exp = {n: min(l["price"] for l in CATALOG if l["name"] == f"💰 {n} РОБУКСОВ") for n in (100, 800)}
        assert got[100] == exp[100] and got[800] == exp[800], (mode, got, exp)
        print(f"{mode}: минимум найден {got}, запросов к лотам: {M['calls']}")
    print("OK")
asyncio.run(main())
