import asyncio, json, os, sys
from datetime import datetime
from types import SimpleNamespace
os.environ["SECRET_KEY"] = __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode()
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from bot.crypto import TokenCipher
from bot.db import init_db, DealState, RelistRule
from tests.fakes.tariffs import check_publish, tariff_response
from bot.playerok.client import PlayerokClient, RawResponse
from bot.services import features as ft
from bot.services.poller import sync_seller
from bot.services.sellers import get_or_create_seller
from bot.keyboards import deal_kb

M = {"deals": [], "published": [], "fail": False}
NOW = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
async def pk(body, token):
    op, v = body["operationName"], body["variables"]
    _t = tariff_response(op, v)
    if _t: return _t
    if op == "publishItem": check_publish(v)
    if op == "deals":
        edges = [{"node": {"id": d, "status": st, "createdAt": NOW,
                           "item": {"id": iid, "slug": iid, "name": name, "price": 5},
                           "user": {"id": "b", "username": "buyer"}, "chat": {"id": "c" + d}}}
                 for d, st, iid, name in M["deals"]]
        return RawResponse(200, json.dumps({"data": {"deals": {"pageInfo": {"hasNextPage": False}, "edges": edges}}}), None)
    if op == "chats":
        return RawResponse(200, json.dumps({"data": {"chats": {"edges": []}}}), None)
    if op == "createChatMessage":
        return RawResponse(200, json.dumps({"data": {"createChatMessage": {"id": "m"}}}), None)
    if op == "publishItem":
        if M["fail"]:
            return RawResponse(200, json.dumps({"errors": [{"message": "Item cannot be published"}]}), None)
        M["published"].append(v["input"]["itemId"])
        return RawResponse(200, json.dumps({"data": {"publishItem": {"id": v["input"]["itemId"]}}}), None)
    if op == "items":
        return RawResponse(200, json.dumps({"errors": [{"message": "Access denied"}]}), None)
    raise AssertionError(op)
PlayerokClient._curl_post = lambda self, body, token: pk(body, token)

class Bot:
    def __init__(self): self.sent = []
    async def send_message(self, chat_id, text, **kw): self.sent.append(text)

async def main():
    sessions = await init_db("sqlite+aiosqlite:///:memory:")
    cipher = TokenCipher(os.environ["SECRET_KEY"])
    async with sessions() as s:
        seller = await get_or_create_seller(s, SimpleNamespace(id=1, username="u"))
        seller.playerok_id = "me"; seller.playerok_username = "eskton"; seller.token_enc = cipher.encrypt("T")
        await s.commit()
        await ft.set_setting(s, 1, "relist_enabled", "1")
        # старая запись (NULL) — не трогаем
        s.add(DealState(seller_tg_id=1, deal_id="old", item_name="Старый", status="PAID", created_at=datetime.utcnow()))
        await s.commit()
        # как у строк, созданных до появления колонки (ALTER TABLE ADD COLUMN → NULL)
        from sqlalchemy import text
        await s.execute(text("UPDATE deal_states SET relisted = NULL WHERE deal_id = 'old'"))
        await s.commit()
    bot = Bot()
    M["deals"] = [("old", "PAID", "item-old", "Старый")]
    await sync_seller(bot, sessions, cipher, seller, notify=True)
    assert M["published"] == [], M["published"]
    print("1. старые сделки не перевыставляются")

    # новая продажа → лот выставлен заново по ID из сделки, хотя список лотов недоступен
    M["deals"].append(("d1", "PAID", "item-50", "50 робуксов"))
    for _ in range(3):
        await sync_seller(bot, sessions, cipher, seller, notify=True)
    assert M["published"] == ["item-50"], M["published"]
    assert any("Выставил заново" in t for t in bot.sent)
    print("2. новая продажа → выставлен заново один раз")

    # правила отбора: только «100»
    async with sessions() as s:
        await ft.set_setting(s, 1, "relist_all", "0")
        s.add(RelistRule(seller_tg_id=1, pattern="100")); await s.commit()
    M["deals"].append(("d2", "PAID", "item-x", "Ключ Steam"))
    M["deals"].append(("d3", "PAID", "item-100", "100 робуксов"))
    await sync_seller(bot, sessions, cipher, seller, notify=True)
    assert M["published"] == ["item-50", "item-100"], M["published"]
    print("3. правила отбора соблюдаются")

    # ошибка Playerok → одно сообщение с текстом
    M["fail"] = True
    M["deals"].append(("d4", "PAID", "item-100b", "100 робуксов"))
    for _ in range(3):
        await sync_seller(bot, sessions, cipher, seller, notify=True)
    errs = [t for t in bot.sent if "Не смог выставить" in t]
    assert len(errs) == 1 and "Item cannot be published" in errs[0], errs
    print("4. ошибка → одно уведомление с текстом")

    # экран автовыставления показывает, что будет с лотами после продаж
    from bot.handlers.settings import render_relist
    M["fail"] = False
    async with sessions() as s:
        await ft.set_setting(s, 1, "relist_enabled", "0")
    M["deals"].append(("d5", "PAID", "item-100c", "100 робуксов VIP"))
    await sync_seller(bot, sessions, cipher, seller, notify=True)
    async with sessions() as s:
        await ft.set_setting(s, 1, "relist_enabled", "1")
        seller_row = await get_or_create_seller(s, SimpleNamespace(id=1, username="u"))
    text, kb = await render_relist(sessions, seller_row, ft.FEATURE_BY_KEY["relist"])
    assert "50 робуксов</a> — ✅ выставлен заново" in text, text
    assert "Ключ Steam</a> — ⏸ не будет: лот не подходит под правила отбора" in text, text
    assert "❌ ошибка: Item cannot be published" in text, text
    assert "100 робуксов VIP</a> — ⏳ будет выставлен" in text, text
    assert "Старый</a> — ⏸ не будет: сделка была в базе до" in text, text
    btns = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "rl:pub:item-100b" in btns and "rl:pub:item-x" in btns and "rl:pub:item-50" not in btns, btns
    print("5. экран показывает судьбу лотов после продаж")

    # следующий опрос выставляет ожидающий лот; ручное выставление помечает сделку
    await sync_seller(bot, sessions, cipher, seller, notify=True)
    assert M["published"][-1] == "item-100c", M["published"]
    from bot.handlers.settings import _restore
    async with PlayerokClient("T") as c:
        assert (await _restore(c, sessions, 1, "item-100b", "100 робуксов")).startswith("✅")
    text, kb = await render_relist(sessions, seller_row, ft.FEATURE_BY_KEY["relist"])
    assert "❌" not in text and "⏳" not in text, text
    print("6. после выставления статусы обновляются")

    kb = deal_kb("chat1", "11111111-2222-3333-4444-555555555555")
    assert [b.callback_data for r in kb.inline_keyboard for b in r][-1].startswith("rl:pub:")
    print("OK")
asyncio.run(main())
