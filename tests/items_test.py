import asyncio, json, sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from bot.playerok.client import PlayerokClient
from bot.playerok import PlayerokError

tries = []
def make(ok_filter):
    async def t(body, token):
        f = body["variables"]["filter"]
        tries.append(f)
        if f == ok_filter:
            return type("R",(),{"status":200,"text":json.dumps({"data":{"items":{"edges":[{"node":{"id":"i1","name":"X","status":"SOLD"}}]}}}),"token":None})()
        return type("R",(),{"status":200,"text":json.dumps({"errors":[{"message":"Access denied"}]}),"token":None})()
    return t

async def main():
    PlayerokClient._items_filter = None
    tries.clear()
    cl = PlayerokClient("T", transport=make({"sellerId":"u1"}))
    items = await cl.my_items("u1")
    assert len(items)==1 and items[0].id=="i1"
    assert PlayerokClient._items_filter == {"sellerId":"u1"}, PlayerokClient._items_filter
    # второй вызов использует запомненный фильтр сразу
    tries.clear()
    await cl.my_items("u1")
    assert tries == [{"sellerId":"u1"}], tries

    # все варианты не подходят → PlayerokError
    PlayerokClient._items_filter = None
    cl2 = PlayerokClient("T", transport=make({"nope":1}))
    try:
        await cl2.my_items("u1"); raise AssertionError("ожидали ошибку")
    except PlayerokError as e:
        assert "Access denied" in str(e)
    print("OK")
asyncio.run(main())
