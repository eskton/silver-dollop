"""publishItem: бесплатный тариф DEFAULT + LOCAL; платный — только если разрешено."""
import asyncio, json, sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from bot.playerok.client import PlayerokClient, PlayerokError, RawResponse

M = {"cost": 0, "published": [], "item_fails": False, "rest_ok": False, "rest": []}
async def pk(body, token):
    op, v = body["operationName"], body["variables"]
    if op.startswith("rest:"):
        M["rest"].append(body["rest"])
        if M["rest_ok"]:
            return RawResponse(200, "", None)
        return RawResponse(500, json.dumps({"message": "Something gone wrong"}), None)
    if op == "item":
        if M["item_fails"]:
            return RawResponse(200, json.dumps({"errors": [{"message": "Access denied"}]}), None)
        return RawResponse(200, json.dumps({"data": {"item": {"id": v["id"], "price": 120}}}), None)
    if op == "itemPriorityStatuses":
        M["price"] = v["price"]
        return RawResponse(200, json.dumps({"data": {"itemPriorityStatuses": [
            {"id": "vip", "type": "VIP", "price": 300},
            {"id": "def", "type": "DEFAULT", "price": M["cost"]}]}}), None)
    if op == "publishItem":
        M["published"].append(v["input"])
        return RawResponse(200, json.dumps({"data": {"publishItem": {"id": v["input"]["itemId"]}}}), None)
    raise AssertionError(op)
PlayerokClient._curl_post = lambda self, body, token: pk(body, token)

async def main():
    async with PlayerokClient("T") as c:
        await c.publish_item("i1", price=5)
        assert M["price"] == 120  # цена лота с Playerok важнее цены из сделки
        assert M["published"][-1] == {"itemId": "i1", "priorityStatuses": ["def"], "transactionProviderId": "LOCAL"}
        print("1. бесплатный тариф DEFAULT, оплата LOCAL")
        M["item_fails"] = True
        await c.publish_item("i2", price=5)
        assert M["price"] == 5
        print("2. цена лота недоступна → берём цену из сделки")
        M["cost"] = 15
        try:
            await c.publish_item("i3", price=5)
            raise AssertionError("должно было отказать")
        except PlayerokError as e:
            assert "платное" in str(e)
        assert len(M["published"]) == 2
        await c.publish_item("i3", price=5, allow_paid=True)
        assert len(M["published"]) == 3
        print("3. платное размещение только при разрешении")
        assert M["rest"][0] == "/rest-api/public/item/i1/republish", M["rest"]
        M["rest_ok"] = True
        n = len(M["published"])
        await c.publish_item("i4", price=5)
        assert len(M["published"]) == n and M["rest"][-1].endswith("/i4/republish")
        print("4. сначала «Выставить снова» (REST), publishItem — запасной путь")

    print("OK")
asyncio.run(main())
