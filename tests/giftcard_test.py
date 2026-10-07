"""Плагин Gift Card (FazerCards) на поддельных Playerok и FazerCards, без сети."""
import asyncio, io, json, logging, os, sys
from datetime import datetime, timedelta
from types import SimpleNamespace
os.environ["SECRET_KEY"] = __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode()
os.environ["FAZER_API_KEY"] = "fc_SECRET_TEST_KEY"
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from sqlalchemy import select
from bot.crypto import TokenCipher
from bot.db import init_db, DealState, DeliveryItem, GiftcardMap, GiftcardOrder
from tests.fakes.tariffs import check_publish, tariff_response
from bot.playerok.client import PlayerokClient, RawResponse
from bot.plugins.access import OWNER_ID
from bot.plugins.giftcard import service as gc
from bot.services import features as ft
from bot.services.poller import sync_seller
from bot.services.sellers import get_or_create_seller

LOG = io.StringIO()
logging.getLogger().addHandler(logging.StreamHandler(LOG))
logging.getLogger().setLevel(logging.INFO)

NOW = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
DEALS = {}       # deal_id -> [status, lot name]
CHAT = []
CONFIRMED = []

async def pk(body, token):
    op, v = body["operationName"], body["variables"]
    _t = tariff_response(op, v)
    if _t: return _t
    if op == "publishItem": check_publish(v)
    if op == "deals":
        edges = [{"node": {"id": d, "status": st, "createdAt": NOW, "item": {"id": "lot-" + d, "slug": "slug-" + d, "name": name, "price": 500},
                           "user": {"id": "b", "username": "buyer"}, "chat": {"id": "c-" + d}}}
                 for d, (st, name) in DEALS.items()]
        return RawResponse(200, json.dumps({"data": {"deals": {"pageInfo": {"hasNextPage": False}, "edges": edges}}}), None)
    if op == "chats":
        return RawResponse(200, json.dumps({"data": {"chats": {"edges": []}}}), None)
    if op == "createChatMessage":
        CHAT.append((v["input"]["chatId"], v["input"]["text"]))
        return RawResponse(200, json.dumps({"data": {"createChatMessage": {"id": "m"}}}), None)
    if op == "updateDeal":
        CONFIRMED.append(v["input"]["id"])
        DEALS[v["input"]["id"]][0] = "SENT"
        return RawResponse(200, json.dumps({"data": {"updateDeal": {"id": v["input"]["id"]}}}), None)
    if op in ("items",):
        return RawResponse(200, json.dumps({"errors": [{"message": "Access denied"}]}), None)
    raise AssertionError(op)
PlayerokClient._curl_post = lambda self, body, token: pk(body, token)

# ---- поддельный FazerCards ----
F = {"mode": "ok", "purchases": {}, "charges": 0, "headers": [], "pending_polls": 0}
async def fazer(method, url, headers, params, body):
    F["headers"].append(dict(headers))
    assert headers.get("X-API-Key") in ("fc_SECRET_TEST_KEY", "fc_FROM_BOT_KEY_123")
    path = url.split("api.fzr.cards", 1)[1]
    if path == "/api/v2/balance":
        return 200, json.dumps({"ok": True, "balance": "12.3400", "currency": "USD"})
    if path == "/api/v2/giftcards/order":
        assert method == "POST" and body["quantity"] >= 1
        key = headers["Idempotency-Key"]
        mode = F["mode"]
        if mode == "timeout":
            F["mode"] = "ok"
            # покупка на стороне поставщика прошла, но ответ потерялся
            F["purchases"].setdefault(key, {"id": f"ord-{len(F['purchases']) + 1}", "status": "completed", "cards": ["CODE-" + key[-6:]]})
            F["charges"] += 1
            raise asyncio.TimeoutError()
        if mode == "nofunds":
            return 400, json.dumps({"ok": False, "error": "Insufficient balance", "code": "insufficient_balance"})
        if mode == "garbage":
            return 502, "<html>Bad gateway</html>"
        if key in F["purchases"]:
            return 200, json.dumps({"ok": True, "order": F["purchases"][key]})  # идемпотентность
        F["charges"] += 1
        if mode == "pending":
            order = {"id": f"ord-{len(F['purchases']) + 1}", "status": "processing"}
            F["purchases"][key] = order
            return 200, json.dumps({"ok": True, "order": order})
        if mode == "weird":
            order = {"id": f"ord-{len(F['purchases']) + 1}", "status": "completed", "cards": [{"zzz": "?"}]}
            F["purchases"][key] = order
            return 200, json.dumps({"ok": True, "order": order})
        order = {"id": f"ord-{len(F['purchases']) + 1}", "status": "completed",
                 "cards": [{"code": f"GIFT-{len(F['purchases']) + 1}-XYZ", "pin": "1234"}]}
        F["purchases"][key] = order
        return 200, json.dumps({"ok": True, "order": order})
    if path.startswith("/api/v2/orders/"):
        oid = path.rsplit("/", 1)[1]
        order = next(o for o in F["purchases"].values() if o["id"] == oid)
        F["pending_polls"] += 1
        if F["pending_polls"] >= 2:
            order.update(status="completed", cards=["LATE-CODE-777"])
        return 200, json.dumps({"ok": True, "order": order})
    if path == "/api/v2/giftcards/cards":
        return 200, json.dumps({"ok": True, "kind": "gift_card", "category_id": params["category_id"], "name": "Steam",
                                "offers": [{"card_id": "steam-5", "name": "$5", "price_usd": "5.10", "stock": 3,
                                            "min_order_quantity": 1, "max_order_quantity": 10}]})
    raise AssertionError(path)
gc.TRANSPORT = fazer

class Bot:
    def __init__(self): self.sent = []
    async def send_message(self, chat_id, text, **kw): self.sent.append(text)

def order_codes(o):
    return TokenCipher(os.environ["SECRET_KEY"]).decrypt(o.codes_enc)

async def main():
    sessions = await init_db("sqlite+aiosqlite:///:memory:")
    cipher = TokenCipher(os.environ["SECRET_KEY"])
    tg = OWNER_ID
    async with sessions() as s:
        seller = await get_or_create_seller(s, SimpleNamespace(id=tg, username="owner"))
        seller.playerok_id = "me"; seller.playerok_username = "eskton"; seller.token_enc = cipher.encrypt("T")
        await s.commit()
        await ft.set_setting(s, tg, "autodelivery_enabled", "1")
        s.add(GiftcardMap(seller_tg_id=tg, lot_key="Steam Gift Card 5$", category_id="steam", card_id="steam-5", quantity=1))
        # запас автовыдачи с тем же названием — не должен расходоваться на заказы Gift Card
        s.add(DeliveryItem(seller_tg_id=tg, item_name="Steam Gift Card 5$", item_key="steam gift card 5", content="STOCK-CODE"))
        await s.commit()
    bot = Bot()
    poll = lambda: sync_seller(bot, sessions, cipher, seller, notify=True)

    # 0. плагин выключен → не трогаем
    DEALS["d0"] = ["PAID", "🎁 Steam Gift Card 5$ | моментально"]
    await poll()
    async with sessions() as s:
        assert await s.scalar(select(GiftcardOrder)) is None
        await ft.set_setting(s, tg, gc.ENABLED_KEY, "1")
        await ft.set_setting(s, tg, gc.ENABLED_AT_KEY, datetime.utcnow().isoformat())
    await poll()
    async with sessions() as s:
        assert await s.scalar(select(GiftcardOrder)) is None, "заказ до включения не должен покупаться"
    print("0. выключен / заказ до включения — не покупается")
    del DEALS["d0"]

    # 1. новый заказ → покупка → код в чат → заказ отмечен выполненным
    DEALS["d1"] = ["PAID", "🎁 Steam Gift Card 5$ | моментально"]
    for _ in range(3):
        await poll()
    async with sessions() as s:
        o = await s.scalar(select(GiftcardOrder).where(GiftcardOrder.deal_id == "d1"))
        st = await s.scalar(select(DealState).where(DealState.deal_id == "d1"))
        stock = await s.scalar(select(DeliveryItem))
    assert o.status == "DELIVERED" and o.provider_order_id == "ord-1", (o.status, o.error)
    assert F["charges"] == 1, F["charges"]
    sent = [t for c, t in CHAT if c == "c-d1"]
    assert sum("GIFT-1-XYZ" in t for t in sent) == 1 and any("PIN: 1234" in t for t in sent), sent
    # запас ушёл только заказу d0 (плагин тогда был выключен — работала обычная автовыдача)
    assert [c for c, t in CHAT if "STOCK-CODE" in t] == ["c-d0"], CHAT
    assert stock.used_deal_id == "d0"
    assert CONFIRMED == ["d0", "d1"] and st.delivered, CONFIRMED
    assert F["headers"][-1]["Idempotency-Key"] == f"playerok-{tg}-d1"
    print("1. заказ → покупка → код покупателю → заказ отмечен выполненным; повторных покупок нет")

    # 2. перезапуск в PROCESSING: строка есть, покупка повторяется с тем же ключом → второй карты нет
    async with sessions() as s:
        s.add(GiftcardOrder(seller_tg_id=tg, deal_id="d2", chat_id="c-d2", item_name="x", category_id="steam",
                            card_id="steam-5", quantity=1, idem_key=f"playerok-{tg}-d2", status="PROCESSING"))
        await s.commit()
    F["purchases"][f"playerok-{tg}-d2"] = {"id": "ord-50", "status": "completed", "cards": ["ALREADY-BOUGHT"]}
    charges = F["charges"]
    DEALS["d2"] = ["PAID", "Steam Gift Card 5$"]
    await poll(); await poll()
    async with sessions() as s:
        o = await s.scalar(select(GiftcardOrder).where(GiftcardOrder.deal_id == "d2"))
    assert o.status == "DELIVERED" and F["charges"] == charges, (o.status, F["charges"])
    assert any("ALREADY-BOUGHT" in t for c, t in CHAT if c == "c-d2")
    print("2. перезапуск во время покупки → тот же Idempotency-Key, повторного списания нет")

    # 3. тайм-аут после покупки → UNKNOWN → повтор с тем же ключом → исходный заказ
    F["mode"] = "timeout"
    charges = F["charges"]
    DEALS["d3"] = ["PAID", "Steam Gift Card 5$"]
    await poll()
    async with sessions() as s:
        o = await s.scalar(select(GiftcardOrder).where(GiftcardOrder.deal_id == "d3"))
    assert o.status == "UNKNOWN" and "d3" not in CONFIRMED, (o.status, o.error)
    await poll(); await poll()
    async with sessions() as s:
        o = await s.scalar(select(GiftcardOrder).where(GiftcardOrder.deal_id == "d3"))
    assert o.status == "DELIVERED" and F["charges"] == charges + 1, (o.status, F["charges"])
    print("3. тайм-аут → повтор с тем же ключом → одна покупка")

    # 4. мало баланса → FAILED, админ уведомлён, покупатель — один раз, заказ НЕ подтверждён
    F["mode"] = "nofunds"
    DEALS["d4"] = ["PAID", "Steam Gift Card 5$"]
    for _ in range(3):
        await poll()
    async with sessions() as s:
        o = await s.scalar(select(GiftcardOrder).where(GiftcardOrder.deal_id == "d4"))
    assert o.status == "FAILED" and "Insufficient balance" in o.error, (o.status, o.error)
    assert "d4" not in CONFIRMED
    assert sum(1 for c, t in CHAT if c == "c-d4" and "заминка" in t) == 1
    assert sum("Insufficient balance" in t for t in bot.sent) == 1
    # ручной повтор после пополнения — новый ключ, покупка проходит
    F["mode"] = "ok"
    async with sessions() as s:
        o = await s.scalar(select(GiftcardOrder).where(GiftcardOrder.deal_id == "d4"))
        await gc.retry(s, o)
    await poll(); await poll()
    async with sessions() as s:
        o = await s.scalar(select(GiftcardOrder).where(GiftcardOrder.deal_id == "d4"))
    assert o.status == "DELIVERED" and o.idem_key.endswith("#r2"), (o.status, o.idem_key)
    print("4. нет баланса → ошибка, админ уведомлён, заказ не подтверждён; «Повторить» после пополнения")

    # 5. 502/мусор → UNKNOWN, повтор тем же ключом; после MAX_ATTEMPTS — стоп и уведомление
    F["mode"] = "garbage"
    DEALS["d5"] = ["PAID", "Steam Gift Card 5$"]
    for _ in range(gc.MAX_ATTEMPTS + 3):
        await poll()
    async with sessions() as s:
        o = await s.scalar(select(GiftcardOrder).where(GiftcardOrder.deal_id == "d5"))
    assert o.status == "UNKNOWN" and o.attempts == gc.MAX_ATTEMPTS, (o.status, o.attempts)
    assert len({h.get("Idempotency-Key") for h in F["headers"] if h.get("Idempotency-Key", "").endswith("-d5")}) == 1
    assert sum("итог покупки неизвестен" in t for t in bot.sent) == 1
    assert "d5" not in CONFIRMED
    print("5. сбой поставщика → только тот же ключ, после 5 попыток — стоп и уведомление админу")

    # 6. коды не сразу: заказ создан, коды приходят при следующей проверке
    F["mode"] = "pending"
    DEALS["d6"] = ["PAID", "Steam Gift Card 5$"]
    for _ in range(4):
        await poll()
    async with sessions() as s:
        o = await s.scalar(select(GiftcardOrder).where(GiftcardOrder.deal_id == "d6"))
    assert o.status == "DELIVERED" and any("LATE-CODE-777" in t for c, t in CHAT if c == "c-d6"), (o.status, o.error)
    print("6. код готов не сразу → бот опрашивает заказ поставщика и выдаёт")

    # 7. неизвестный формат кода → не выдаём наугад, зовём админа
    F["mode"] = "weird"
    DEALS["d7"] = ["PAID", "Steam Gift Card 5$"]
    await poll(); await poll()
    async with sessions() as s:
        o = await s.scalar(select(GiftcardOrder).where(GiftcardOrder.deal_id == "d7"))
    assert o.status == "NEEDS_CHECK" and not any(c == "c-d7" and "{" in t for c, t in CHAT)
    assert any("незнакомом формате" in t for t in bot.sent)
    print("7. непонятный ответ с кодом → покупателю ничего, админу уведомление")

    # 8. сделку отменили до выдачи → не покупаем
    F["mode"] = "ok"
    charges = F["charges"]
    DEALS["d8"] = ["CANCELED", "Steam Gift Card 5$"]
    await poll()
    async with sessions() as s:
        assert await s.scalar(select(GiftcardOrder).where(GiftcardOrder.deal_id == "d8")) is None
    assert F["charges"] == charges
    print("8. отменённая сделка → покупки нет")

    # 9. продавец не админ → плагин не работает
    async with sessions() as s:
        other = await get_or_create_seller(s, SimpleNamespace(id=555, username="other"))
        other.playerok_id = "me2"; other.playerok_username = "x"; other.token_enc = cipher.encrypt("T")
        await s.commit()
        await ft.set_setting(s, 555, gc.ENABLED_KEY, "1")
        s.add(GiftcardMap(seller_tg_id=555, lot_key="Steam Gift Card 5$", category_id="steam", card_id="steam-5", quantity=1))
        await s.commit()
    DEALS.clear(); DEALS["o1"] = ["PAID", "Steam Gift Card 5$"]
    charges = F["charges"]
    await sync_seller(bot, sessions, cipher, other, notify=True)
    await sync_seller(bot, sessions, cipher, other, notify=True)
    assert F["charges"] == charges
    print("9. не-админ → плагин не срабатывает")

    # 10. секреты: ключа и кодов нет в логах
    text = LOG.getvalue()
    assert "fc_SECRET_TEST_KEY" not in text
    for code in ("GIFT-1-XYZ", "ALREADY-BOUGHT", "LATE-CODE-777", "1234"):
        assert code not in text, code
    for ev in ("ORDER_RECEIVED", "PROCESSING", "API_REQUEST", "API_SUCCESS", "DELIVERY_SUCCESS", "API_ERROR"):
        assert ev in text, ev
    print("10. API-ключа и кодов в логах нет, события пишутся")
    from bot.plugins.giftcard import handlers as gh
    assert gh._admin_msg(SimpleNamespace(from_user=SimpleNamespace(id=OWNER_ID)))
    assert not gh._admin_msg(SimpleNamespace(from_user=SimpleNamespace(id=555)))
    assert not gh._admin_cb(SimpleNamespace(from_user=SimpleNamespace(id=555)))
    text, _ = await gh.render(sessions, tg)
    assert "fc_SECRET" not in text and "✅ задан" in text and "Выдано:</b> 5" in text, text
    print("11. /giftcard — только админу, ключ на экране не показывается")

    # 12. ключ, введённый через бота: шифруется в базе и используется вместо переменной
    from bot.db import SellerSetting
    async with sessions() as s:
        await gc.set_api_key(s, tg, "fc_FROM_BOT_KEY_123")
        raw = await s.scalar(select(SellerSetting.value).where(SellerSetting.key == gc.KEY_SETTING))
        assert raw and "fc_FROM_BOT_KEY_123" not in raw
        assert await gc.get_api_key(s, tg) == "fc_FROM_BOT_KEY_123"
        async with gc.make_client(await gc.get_api_key(s, tg)) as api:
            await api.balance()
    assert F["headers"][-1]["X-API-Key"] == "fc_FROM_BOT_KEY_123"
    os.environ.pop("FAZER_API_KEY")
    DEALS.clear(); DEALS["k1"] = ["PAID", "Steam Gift Card 5$"]
    await poll(); await poll()
    async with sessions() as s:
        o = await s.scalar(select(GiftcardOrder).where(GiftcardOrder.deal_id == "k1"))
        assert o.status == "DELIVERED", (o.status, o.error)
        await gc.set_api_key(s, tg, "")
        assert await gc.get_api_key(s, tg) == ""
    assert "fc_FROM_BOT_KEY_123" not in LOG.getvalue()
    print("12. ключ из бота: зашифрован в базе, работает без переменной Railway, удаляется")

    # 13. заказ пришёл, пока плагин был выключен → кнопка «Выдать по оплаченным заказам»
    async with sessions() as s:
        await gc.set_api_key(s, tg, "fc_FROM_BOT_KEY_123")
        await ft.set_setting(s, tg, gc.ENABLED_KEY, "0")
        await s.execute(DeliveryItem.__table__.delete())  # без запаса автовыдачи
        await s.commit()
    DEALS.clear(); DEALS["m1"] = ["PAID", "🎁 Steam Gift Card 5$ | моментально"]
    charges = F["charges"]
    await poll()
    async with sessions() as s:
        st = await s.scalar(select(DealState).where(DealState.deal_id == "m1"))
        st.first_seen_at = datetime.utcnow() - timedelta(minutes=1)  # заказ был раньше включения
        await s.commit()
        await ft.set_setting(s, tg, gc.ENABLED_KEY, "1")
        await ft.set_setting(s, tg, gc.ENABLED_AT_KEY, datetime.utcnow().isoformat())
    await poll()
    async with sessions() as s:
        assert await s.scalar(select(GiftcardOrder).where(GiftcardOrder.deal_id == "m1")) is None
        pending = await gc.pending_paid_deals(s, tg)
        assert [st.deal_id for st, _ in pending] == ["m1"], pending
        assert await gc.start_manual(s, tg, "m1") == "ok"
        assert await gc.start_manual(s, tg, "m1") != "ok"  # второй раз — нельзя
        assert await gc.pending_paid_deals(s, tg) == []
    await poll(); await poll()
    async with sessions() as s:
        o = await s.scalar(select(GiftcardOrder).where(GiftcardOrder.deal_id == "m1"))
    assert o.status == "DELIVERED" and F["charges"] == charges + 1, (o.status, F["charges"])
    assert any(c == "c-m1" and "Ваш код" in t for c, t in CHAT)
    assert "m1" in CONFIRMED
    print("13. заказ до включения → «Выдать по оплаченным» → куплено один раз, выдано, подтверждено")
    print("OK")

asyncio.run(main())
