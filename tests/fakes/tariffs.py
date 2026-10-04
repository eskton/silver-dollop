"""Поддельные ответы Playerok для тарифов размещения (нужны publishItem)."""
import json

from bot.playerok.client import RawResponse

FREE_ID = "st-default"


def tariff_response(op, v):
    """Ответ на item / itemPriorityStatuses или None, если запрос не про тарифы."""
    if op.startswith("rest:"):
        # REST «выставить снова» в этих тестах отказывает → проверяется запасной publishItem
        return RawResponse(400, json.dumps({"message": "republish unavailable"}), None)
    if op == "item":
        return RawResponse(200, json.dumps({"data": {"item": {"id": v["id"], "price": 90}}}), None)
    if op == "itemPriorityStatuses":
        assert v["price"] > 0, v
        statuses = [
            {"id": FREE_ID, "name": "Обычный", "type": "DEFAULT", "price": 0},
            {"id": "st-premium", "name": "Премиум", "type": "PREMIUM", "price": 49},
        ]
        return RawResponse(200, json.dumps({"data": {"itemPriorityStatuses": statuses}}), None)
    return None


def check_publish(v):
    """Как настоящий Playerok: без тарифа и способа оплаты publishItem отклоняется."""
    inp = v["input"]
    assert inp.get("priorityStatuses") == [FREE_ID], inp
    assert inp.get("transactionProviderId") == "LOCAL", inp
