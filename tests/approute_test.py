"""Плагин Gift Card с поставщиком AppRoute на поддельных Playerok и AppRoute, без сети."""
import asyncio, io, json, logging, os, sys
from datetime import datetime
from types import SimpleNamespace
os.environ["SECRET_KEY"] = __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode()
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from sqlalchemy import select
from bot.crypto import TokenCipher
from bot.db import init_db, DealState, GiftcardMap, GiftcardOrder
from tests.fakes.tariffs import check_publish, tariff_response
from bot.playerok.client import PlayerokClient, RawResponse
from bot.plugins.access import OWNER_ID
from bot.plugins.giftcard import service as gc
from bot.plugins.giftcard.approute import AppRouteClient, reference_for
from bot.services import features as ft
from bot.services.poller import sync_seller
from bot.services.sellers import get_or_create_seller

LOG = io.StringIO()
logging.getLogger().addHandler(logging.StreamHandler(LOG))
logging.getLogger().setLevel(logging.INFO)

NOW = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
DEALS, CHAT, CONFIRMED = {}, [], []

async def pk(body, token):
    op, v = body["operationName"], body["variables"]
    _t = tariff_response(op, v)
    if _t: return _t
    if op == "publishItem": check_publish(v)
    if op == "deals":
        edges = [{"node": {"id": d, "status": st, "createdAt": NOW, "item": {"id": "lot-" + d, "slug": "slug-" + d, "name": name, "price": 900},
                           "user": {"id": "b", "username": "buyer"}, "chat": {"id": "c-" + d}}}
                 for d, (st, name) in DEALS.items()]
        return RawResponse(200, json.dumps({"data": {"deals": {"pageInfo": {"hasNextPage": False}, "edges": edges}}}), None)
    if op == "chats":
        return RawResponse(200, json.dumps({"data": {"chats": {"edges": []}}}), None)
    if op == "createChatMessage":
        CHAT.append((v["input"]["chatId"], v["input"]["text"]))
        return RawResponse(200, json.dumps({"data": {"createChatMessage": {"id": "m"}}}), None)
    if op == "updateDeal":
        CONFIRMED.append(v["input"]["id"]); DEALS[v["input"]["id"]][0] = "SENT"
        return RawResponse(200, json.dumps({"data": {"updateDeal": {"id": v["input"]["id"]}}}), None)
    if op == "items":
        return RawResponse(200, json.dumps({"errors": [{"message": "Access denied"}]}), None)
    raise AssertionError(op)
PlayerokClient._curl_post = lambda self, body, token: pk(body, token)

# ---- поддельный AppRoute (конверт как в SDK; часть ответов — с числовым statusCode, как в гайде) ----
A = {"mode": "ok", "orders": {}, "charges": 0, "calls": []}
def env(data, code="OK", status="SUCCESS", http=200):
    return http, json.dumps({"status": status, "code": code, "message": "", "traceId": "t-1", "data": data})
def err(code_num, msg, http):
    return http, json.dumps({"status": "CANCELLED", "statusCode": code_num, "statusMessage": msg, "traceId": "t-2", "data": None})

async def approute(method, url, headers, params, body):
    A["calls"].append((method, url, dict(headers), params, body))
    assert headers["X-API-Key"] == "ar_SECRET_KEY"
    assert url.startswith("https://approute.io/api/v1/"), url
    path = url.split("/api/v1", 1)[1]
    if path == "/accounts":
        return env({"items": [{"currency": "USDT", "balance": 50.5, "available": 48.25, "overdraftLimit": 0, "recentActivity": []}]})
    if path == "/services":
        return env({"items": [
            {"id": "psn-tr", "name": "PlayStation Turkey", "type": "voucher", "countryCode": "TR", "imageUrl": None, "items": []},
            {"id": "apple-us", "name": "Apple USA", "type": "voucher", "countryCode": "US", "imageUrl": None, "items": []},
            {"id": "dtu-1", "name": "Mobile top-up", "type": "direct_topup", "imageUrl": None, "items": []},
        ], "hasNext": False})
    if path == "/services/psn-tr":
        return env({"id": "psn-tr", "name": "PlayStation Turkey", "type": "voucher", "imageUrl": None,
                    "items": [{"id": "psn-tr-250", "name": "250 TRY", "nominal": 250, "price": 6.4, "currency": "USDT", "available": True, "stock": 5}]})
    if path == "/orders" and method == "POST":
        assert headers["Content-Type"] == "application/json"
        assert body["ordersType"] == "shop" and body["productId"] == "psn-tr" and body["itemId"] == "psn-tr-250"
        assert body["quantity"] == 1 and len(body["referenceId"]) == 36, body
        ref = body["referenceId"]
        mode = A["mode"]
        if mode == "timeout":  # покупка прошла, ответ потерялся
            A["mode"] = "ok"
            A["orders"][ref] = {"orderId": "o-" + ref[:4], "status": "completed", "price": 6.4, "currency": "USDT",
                                "result": {"vouchers": [{"pin": "PSN-TIMEOUT-CODE"}]}}
            A["charges"] += 1
            raise asyncio.TimeoutError()
        if mode == "nostock":
            return err(9, "Out of stock", 422)
        if mode == "badkey":
            return err("UNAUTHORIZED", "Invalid API key", 401)
        if ref in A["orders"]:  # идемпотентность: тот же referenceId — первый результат
            return env(A["orders"][ref], code="IDEMPOTENCY_REPLAY")
        A["charges"] += 1
        if mode == "pending":
            A["orders"][ref] = {"orderId": "o-p", "status": "in_progress", "price": 6.4, "currency": "USDT", "result": {"vouchers": []}}
            return env(A["orders"][ref], code="ACCEPTED", status="IN_PROGRESS", http=202)
        # коды в ответе на покупку скрыты — полные только через GET /orders?…&unhide=true
        A["orders"][ref] = {"orderId": "o-1", "status": "completed", "price": 6.4, "currency": "USDT",
                            "result": {"vouchers": [{"pin": "****R2DJ", "serialNumber": "SN-1"}]},
                            "_full": [{"pin": "89M9-5E9J-R2DJ", "serialNumber": "SN-1"}]}
        return env({k: v for k, v in A["orders"][ref].items() if k != "_full"})
    if path == "/orders" and method == "GET":
        assert params["unhide"] == "true" and params["referenceId"]
        o = A["orders"].get(params["referenceId"])
        if o is None:
            return env({"page": {"items": [], "hasNext": False}})
        if o["status"] == "in_progress":  # готово со второго опроса
            o["status"] = "completed"; o["_full"] = [{"pin": "LATE-PSN-CODE"}]
            return env({"page": {"items": [{"status": "in_progress", "quantity": 1, "currency": "USDT", "vouchers": []}], "hasNext": False}})
        return env({"page": {"items": [{"status": o["status"], "quantity": 1, "amount": 6.4, "currency": "USDT",
                                        "vouchers": o.get("_full") or o["result"]["vouchers"]}], "hasNext": False}})
    raise AssertionError(path)
gc.AR_TRANSPORT = approute

class Bot:
    def __init__(self): self.sent = []
    async def send_message(self, chat_id, text, **kw): self.sent.append(text)

async def main():
    sessions = await init_db("sqlite+aiosqlite:///:memory:")
    cipher = TokenCipher(os.environ["SECRET_KEY"])
    tg = OWNER_ID
    async with sessions() as s:
        seller = await get_or_create_seller(s, SimpleNamespace(id=tg, username="owner"))
        seller.playerok_id = "me"; seller.playerok_username = "eskton"; seller.token_enc = cipher.encrypt("T")
        await s.commit()
        s.add(GiftcardMap(seller_tg_id=tg, lot_key="PlayStation 250 TRY", category_id="psn-tr", card_id="psn-tr-250",
                          quantity=1, provider="approute", card_name="250 TRY"))
        await s.commit()
        await gc.set_api_key(s, tg, "ar_SECRET_KEY", "approute")
        await ft.set_setting(s, tg, gc.ENABLED_KEY, "1")
        await ft.set_setting(s, tg, gc.ENABLED_AT_KEY, datetime.utcnow().isoformat())
    bot = Bot()
    poll = lambda: sync_seller(bot, sessions, cipher, seller, notify=True)

    # баланс
    async with sessions() as s:
        async with await gc.make_approute(s, tg) as api:
            assert await api.balance() == ("48.25", "USDT")
    print("0. баланс AppRoute: 48.25 USDT")

    # 1. заказ → покупка (коды скрыты) → полные коды по referenceId → покупателю → подтверждение
    DEALS["d1"] = ["PAID", "🎮 PlayStation 250 TRY | код"]
    for _ in range(3):
        await poll()
    async with sessions() as s:
        o = await s.scalar(select(GiftcardOrder).where(GiftcardOrder.deal_id == "d1"))
        st = await s.scalar(select(DealState).where(DealState.deal_id == "d1"))
    assert o.provider == "approute" and o.status == "DELIVERED", (o.status, o.error)
    assert o.cost_usd == 6.4 and o.cost_exact and o.provider_order_id == "o-1"
    sent = [t for c, t in CHAT if c == "c-d1"]
    assert sum("89M9-5E9J-R2DJ" in t for t in sent) == 1 and any("Серийный номер: SN-1" in t for t in sent), sent
    assert not any("****" in t for t in sent)
    assert A["charges"] == 1 and CONFIRMED == ["d1"] and st.delivered
    post = [c for c in A["calls"] if c[0] == "POST"][-1]
    assert post[4]["referenceId"] == reference_for(f"playerok-{tg}-d1")
    assert "ar_SECRET_KEY" not in LOG.getvalue() and "89M9-5E9J-R2DJ" not in LOG.getvalue()
    print("1. заказ → покупка в AppRoute → полный код покупателю → заказ отмечен выполненным")

    # 2. тайм-аут после покупки → UNKNOWN → повтор с тем же referenceId → тот же заказ, без второго списания
    A["mode"] = "timeout"
    DEALS["d2"] = ["PAID", "PlayStation 250 TRY"]
    for _ in range(3):
        await poll()
    async with sessions() as s:
        o = await s.scalar(select(GiftcardOrder).where(GiftcardOrder.deal_id == "d2"))
    assert o.status == "DELIVERED" and A["charges"] == 2, (o.status, A["charges"])
    assert sum("PSN-TIMEOUT-CODE" in t for c, t in CHAT if c == "c-d2") == 1
    print("2. ответ потерялся → повтор с тем же referenceId → второй карты нет")

    # 3. заказ в обработке (IN_PROGRESS) → коды приходят позже
    A["mode"] = "pending"
    DEALS["d3"] = ["PAID", "PlayStation 250 TRY"]
    for _ in range(4):
        await poll()
    async with sessions() as s:
        o = await s.scalar(select(GiftcardOrder).where(GiftcardOrder.deal_id == "d3"))
    assert o.status == "DELIVERED" and any("LATE-PSN-CODE" in t for c, t in CHAT if c == "c-d3"), (o.status, o.error)
    print("3. IN_PROGRESS → опрос по referenceId → код выдан, когда готов")

    # 4. нет в наличии (statusCode 9) → FAILED, покупателю — «заминка», владельцу — причина; повторов нет
    A["mode"] = "nostock"
    charges, posts = A["charges"], sum(c[0] == "POST" for c in A["calls"])
    DEALS["d4"] = ["PAID", "PlayStation 250 TRY"]
    for _ in range(3):
        await poll()
    async with sessions() as s:
        o = await s.scalar(select(GiftcardOrder).where(GiftcardOrder.deal_id == "d4"))
    assert o.status == "FAILED" and "нет в наличии" in o.error, (o.status, o.error)
    assert sum(c[0] == "POST" for c in A["calls"]) == posts + 1 and A["charges"] == charges
    assert any("заминка" in t for c, t in CHAT if c == "c-d4")
    assert any("нет в наличии" in t for t in bot.sent), bot.sent
    print("4. нет в наличии → заказ не выдан, владелец уведомлён, повторов нет")

    # 5. неверный ключ → понятная ошибка про ключ и белый список IP
    A["mode"] = "badkey"
    DEALS["d5"] = ["PAID", "PlayStation 250 TRY"]
    await poll()
    async with sessions() as s:
        o = await s.scalar(select(GiftcardOrder).where(GiftcardOrder.deal_id == "d5"))
    assert o.status == "FAILED" and "белый список IP" in o.error, o.error
    print("5. неверный ключ → «проверь ключ и белый список IP»")

    # 7. экраны /giftcard: ключ AppRoute (сообщение удаляется), привязка лота кнопками
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage
    from bot.plugins.giftcard import handlers as gh
    OUT = []
    user = SimpleNamespace(id=tg, username="owner", first_name="o")
    class Msg:
        def __init__(self, text=""): self.text = text; self.from_user = user; self.deleted = False
        async def answer(self, text, reply_markup=None, **k): OUT.append((text, reply_markup)); return Msg()
        async def edit_text(self, text, reply_markup=None, **k): OUT.append((text, reply_markup))
        async def delete(self): self.deleted = True
    class Cb:
        def __init__(self, data): self.data = data; self.from_user = user; self.message = Msg()
        async def answer(self, *a, **k): pass
    btns = lambda kb: [b.callback_data for r in kb.inline_keyboard for b in r]
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=tg, user_id=tg))
    async with sessions() as s:
        await gc.set_api_key(s, tg, "", "approute")
    await gh.ar_key_ask(Cb("gc:arkey"), state)
    assert "белый список" in OUT[-1][0]
    msg = Msg("ar_SECRET_KEY")
    await gh.key_save(msg, state, sessions)
    assert msg.deleted and any("Ключ работает, доступно 48.25 USDT" in t for t, _ in OUT), OUT[-3:]
    text, kb = OUT[-1]
    assert "Ключ AppRoute:</b> ✅" in text and "gc:arkeydel" in btns(kb) and "gc:arreg" in btns(kb), text
    await gh.add_start(Cb("gc:add"), state)
    await gh.add_lot(Msg("https://playerok.com/products/apple-10-usa"), state, sessions)  # ключа Fazer нет — сразу AppRoute
    text, kb = OUT[-1]
    assert "Шаг 2/4" in text and len(btns(kb)) == 2, (text, btns(kb))  # пополнения (direct_topup) не предлагаем
    await gh.add_category_search(Msg("turkey"), state)
    assert btns(OUT[-1][1]) == ["gc:pc:0"] and "TR" in OUT[-1][1].inline_keyboard[0][0].text
    await gh.add_category_pick(Cb("gc:pc:0"), state, sessions)
    text, kb = OUT[-1]
    assert "Шаг 3/4" in text and btns(kb) == ["gc:po:0"] and "250 TRY — $6.4 (в наличии 5)" in kb.inline_keyboard[0][0].text
    await gh.add_card_pick(Cb("gc:po:0"), state)
    await gh.add_quantity_pick(Cb("gc:pq:1"), state, sessions)
    async with sessions() as s:
        m = (await s.scalars(select(GiftcardMap).where(GiftcardMap.lot_key == "apple-10-usa"))).one()
    assert (m.provider, m.category_id, m.card_id, m.quantity) == ("approute", "psn-tr", "psn-tr-250", 1), m.__dict__
    assert m.card_name == "PlayStation Turkey · TR — 250 TRY", m.card_name
    assert any("AppRoute: PlayStation Turkey" in t for t, _ in OUT)
    # тест без покупки
    await gh.test(Cb("gc:test"), sessions)
    assert "✅ AppRoute: ключ работает" in OUT[-1][0] and "$6.4 ×1, в наличии 5" in OUT[-1][0], OUT[-1][0]
    print("7. /giftcard: ключ AppRoute, привязка лота кнопками, тест без покупки")

    # 8. «📦 Разделы закупки»: цены AppRoute по номиналам; из двух поставщиков — дешевле и в наличии
    from bot.handlers import market as hm
    from bot.services import market_sections as ms, pricing
    async with sessions() as s:
        sec = await ms.get_or_create(s, tg, "psn", "tr")
        await ms.upsert_costs(s, sec, {250: 9.99, 500: 12})
    await hm.ar_ask(Cb(f"mk:ar:{sec.id}"), state, sessions)
    await hm.ar_search(Msg("turkey"), state, sessions)
    assert btns(OUT[-1][1]) == ["mk:arp:0"], OUT[-1]
    await hm.ar_pick(Cb("mk:arp:0"), state, sessions)
    text, kb = OUT[-1]
    assert "<b>AppRoute:</b> PlayStation Turkey · TR" in text and f"mk:arx:{sec.id}" in btns(kb), text
    async with sessions() as s:
        await ms.set_active(s, tg, await ms.get_section(s, tg, sec.id))
        setup = await pricing.market_setup(s, tg)
    c = setup.costs
    assert c[250].source == "approute" and c[250].usd == 6.4 and c[250].stock == 5, c   # AppRoute главнее ручной
    assert c[500].source == "manual" and c[500].usd == 12, c                           # номинала нет у AppRoute
    assert pricing._cost_label(c[250]) == "AppRoute $6.4 (в наличии 5)"
    C = pricing.Cost
    best = pricing._cheapest({10: C(9.0, "fazer", 3), 20: C(5.0, "fazer", 0)}, {10: C(8.5, "approute", 1), 20: C(6.0, "approute", 2)})
    assert best[10].source == "approute" and best[20].source == "approute", best  # 20: у Fazer нет в наличии
    print("8. разделы закупки: цены AppRoute по номиналам; выбор дешёвого поставщика в наличии")

    # 6. регион .ru и прокси
    os.environ["PLAYEROK_PROXY"] = "http://u:p@1.2.3.4:8000"
    from bot.plugins.giftcard.approute import proxy_from_env
    assert proxy_from_env() == "http://u:p@1.2.3.4:8000"
    os.environ["APPROUTE_PROXY"] = "none"; assert proxy_from_env() is None
    assert AppRouteClient("k", region="ru")._base == "https://approute.ru/api/v1"
    print("6. прокси для белого списка IP; домен approute.ru для России")
    print("OK")
asyncio.run(main())
