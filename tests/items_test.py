"""Список своих лотов — сохранённый запрос `items` (как PlayerokAPI.get_my_items)."""
import asyncio, json, sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from bot.playerok.client import PERSISTED_QUERIES, PlayerokClient, RawResponse

calls = []
async def t(body, token):
    calls.append(body)
    assert body["operationName"] == "items" and body["persisted"] == PERSISTED_QUERIES["items"]
    assert "query" not in body  # текст запроса не шлём — только хеш
    v = body["variables"]
    page = 0 if v["pagination"]["after"] is None else int(v["pagination"]["after"])
    edges = [{"node": {"id": f"i{page * 24 + k}", "name": f"L{k}", "status": "SOLD", "rawPrice": 50, "priority": "DEFAULT"}}
             for k in range(24 if page == 0 else 5)]
    info = {"hasNextPage": page == 0, "endCursor": str(page + 1)}
    return RawResponse(200, json.dumps({"data": {"items": {"edges": edges, "pageInfo": info}}}), None)

async def main():
    cl = PlayerokClient("T", transport=t)
    items = await cl.my_items("u1", statuses=["SOLD"])
    assert len(items) == 29 and len(calls) == 2, (len(items), len(calls))
    v = calls[0]["variables"]
    assert v["filter"] == {"userId": "u1", "status": ["SOLD"]} and v["pagination"]["first"] == 24
    assert items[0].raw_price == 50 and items[0].priority == "DEFAULT"
    print("OK")
asyncio.run(main())
