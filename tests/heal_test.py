import asyncio, json, sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from bot.playerok import client as c
from bot.playerok.client import PlayerokClient, RawResponse, PATCHED_QUERIES, REMOVED_FIELDS
from bot.playerok import PlayerokError

sent = []
async def t(body, token):
    q = body["query"]; sent.append(q)
    for f in ("review", "slug"):
        if f in q:
            return RawResponse(200, json.dumps({"errors": [{"message": f'Cannot query field "{f}" on type "ItemDeal".'}]}), None)
    return RawResponse(200, json.dumps({"data": {"deals": {"pageInfo": {"hasNextPage": False}, "edges": [{"node": {"id": "d1", "status": "PAID", "item": {"name": "x", "price": 5}, "user": {"username": "b"}}}]}}}), None)

async def main():
    PATCHED_QUERIES.clear(); REMOVED_FIELDS.clear()
    cl = PlayerokClient("T", transport=t)
    from bot.playerok import queries as q
    q.DEALS = q.DEALS.replace("        chat {", "        review { id rating }\n        chat {")
    deals = await cl.my_sales("u1")
    assert len(deals) == 1 and deals[0].item_name == "x"
    assert len(sent) == 3, len(sent)
    assert REMOVED_FIELDS["deals"] == ["review", "slug"], REMOVED_FIELDS
    sent.clear()
    await cl.my_sales("u1")
    assert len(sent) == 1 and "review" not in sent[0]  # исправленный запрос закэширован
    # неизвестная ошибка другого рода — пробрасывается как раньше
    async def bad(body, token):
        return RawResponse(200, json.dumps({"errors": [{"message": "Internal error"}]}), None)
    try:
        await PlayerokClient("T", transport=bad).viewer(); raise AssertionError
    except PlayerokError as e:
        assert "Internal" in str(e)
    print("OK")
asyncio.run(main())
