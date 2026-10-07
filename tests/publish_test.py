"""Перевыставление по схеме Playerok Universal: SOLD-лот по названию → item → тарифы → publishItem."""
import asyncio, json, sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from bot.playerok.client import PaidPlacementRefused, PlayerokClient, PlayerokError, RawResponse

M = {"cost": 0, "published": [], "bodies": []}
ITEMS = {
    "listing-1": {"id": "listing-1", "name": "💰 50 РОБУКСОВ | ПРОМОКОД", "rawPrice": 45, "price": 50,
                  "status": "SOLD", "priority": "DEFAULT", "mayBePublished": True},
    "prem-1": {"id": "prem-1", "name": "Премиум лот", "rawPrice": 90, "price": 100,
               "status": "SOLD", "priority": "PREMIUM", "mayBePublished": True},
    "acc-1": {"id": "acc-1", "name": "Аккаунт", "rawPrice": 300, "price": 330,
              "status": "SOLD", "priority": "DEFAULT", "mayBePublished": False},
}
async def pk(body, token):
    M["bodies"].append(body)
    op, v = body["operationName"], body["variables"]
    if op == "items":
        assert body.get("persisted") and v["filter"]["status"] == ["SOLD"]
        edges = [{"node": i} for i in ITEMS.values()]
        return RawResponse(200, json.dumps({"data": {"items": {"edges": edges, "pageInfo": {"hasNextPage": False}}}}), None)
    if op == "item":
        assert body.get("persisted")
        it = ITEMS.get(v["id"])
        if it is None:
            return RawResponse(200, json.dumps({"errors": [{"message": "Something gone wrong"}]}), None)
        return RawResponse(200, json.dumps({"data": {"item": it}}), None)
    if op == "itemPriorityStatuses":
        assert body.get("persisted") and isinstance(v["price"], int)
        M["price"] = v["price"]
        return RawResponse(200, json.dumps({"data": {"itemPriorityStatuses": [
            {"id": "prem", "type": "PREMIUM", "price": 30},
            {"id": "def", "type": "DEFAULT", "price": M["cost"]}]}}), None)
    if op == "publishItem":
        assert "query" in body and not body.get("persisted")
        M["published"].append(v["input"])
        return RawResponse(200, json.dumps({"data": {"publishItem": {"id": v["input"]["itemId"]}}}), None)
    if op == "increaseItemPriorityStatus":
        M["bumped"] = v["input"]
        return RawResponse(200, json.dumps({"data": {"increaseItemPriorityStatus": {"id": v["input"]["itemId"]}}}), None)
    raise AssertionError(op)

async def main():
    c = PlayerokClient("T", transport=pk)
    # 1. после продажи: ID из сделки не тот — настоящий лот находим среди SOLD по названию
    got = await c.publish_item("deal-item-id", price=50, sold_name="💰 50 РОБУКСОВ | ПРОМОКОД", user_id="me")
    assert got == "listing-1" and M["price"] == 45, (got, M.get("price"))
    assert M["published"][-1] == {"transactionProviderId": "LOCAL", "priorityStatuses": ["def"], "itemId": "listing-1"}
    print("1. лот найден среди проданных по названию, выставлен бесплатно (DEFAULT, LOCAL, по rawPrice)")
    # 2. премиум-лот выставляется только премиум-статусом и только если платное разрешено
    try:
        await c.publish_item("prem-1"); raise AssertionError("должен отказать")
    except PaidPlacementRefused as e:
        assert "30" in str(e)
    await c.publish_item("prem-1", allow_paid=True)
    assert M["published"][-1]["priorityStatuses"] == ["prem"]
    print("2. премиум-лот — только с разрешения платного восстановления")
    # 3. mayBePublished=false → понятная ошибка
    try:
        await c.publish_item("acc-1"); raise AssertionError("должен отказать")
    except PlayerokError as e:
        assert "лот:" in str(e) and "повторно" in str(e), e
    # 4. ошибки подписаны шагом
    try:
        await c.publish_item("nope"); raise AssertionError("должен отказать")
    except PlayerokError as e:
        assert str(e).startswith("лот: Something gone wrong"), e
    print("3-4. «нельзя выставить повторно» и шаг ошибки видны в тексте")
    # 5. поднятие — платный PREMIUM, с лимитом
    try:
        await c.bump_item("listing-1", max_cost=10); raise AssertionError("должен отказать")
    except PaidPlacementRefused:
        pass
    assert await c.bump_item("listing-1", max_cost=100) == 30
    assert M["bumped"]["priorityStatuses"] == ["prem"] and M["bumped"]["transactionProviderId"] == "LOCAL"
    print("5. поднятие: премиум-статус, не дороже лимита")
    print("OK")
asyncio.run(main())
