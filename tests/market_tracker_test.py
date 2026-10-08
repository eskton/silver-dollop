"""Трекер выгоды: мин. цена Playerok ÷ курс против цены FazerCards; уведомление, когда стало выгодно."""
import asyncio, json, os, sys
from types import SimpleNamespace
os.environ["SECRET_KEY"] = __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode()
os.environ.pop("FAZER_API_KEY", None)
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from bot.crypto import TokenCipher
from bot.db import init_db
from bot.playerok.client import PlayerokClient, RawResponse
from bot.plugins.access import OWNER_ID
from bot.plugins.giftcard import service as gc
from bot.services import features as ft, pricing
from bot.services.sellers import get_or_create_seller
from bot.handlers import pricing as hp

PRICES = {100: 120, 500: 520}  # мин. цены конкурентов на Playerok, ₽
async def pk(body, token):
    op, v = body["operationName"], body["variables"]
    if op == "item":
        return RawResponse(200, json.dumps({"data": {"item": {"id": "m", "name": "x", "category": {"id": "cat"},
                           "obtainingType": {"id": "code", "name": "Промокод"}}}}), None)
    if op == "items":
        edges = [{"node": {"id": f"l{n}", "slug": f"l{n}", "name": f"💰 {n} РОБУКСОВ | ПРОМОКОД", "price": p,
                           "obtainingType": {"id": "code"}, "user": {"id": "rival"}}} for n, p in PRICES.items()]
        edges.append({"node": {"id": "mine", "slug": "mine", "name": "100 робуксов", "price": 10,
                               "obtainingType": {"id": "code"}, "user": {"id": "me"}}})  # свой — не считаем
        return RawResponse(200, json.dumps({"data": {"items": {"edges": edges, "pageInfo": {}}}}), None)
    raise AssertionError(op)
PlayerokClient._curl_post = lambda self, body, token: pk(body, token)

async def fazer(method, url, headers, params, body):
    assert params["category_id"] == "roblox_global"
    return 200, json.dumps({"ok": True, "kind": "gift_card", "category_id": "roblox_global", "name": "Roblox", "offers": [
        {"card_id": "100_robux", "name": "100 Robux", "price_usd": "1.05", "stock": 7, "min_order_quantity": 1, "max_order_quantity": 5},
        {"card_id": "500_robux", "name": "500 Robux", "price_usd": "5.30", "stock": 0, "min_order_quantity": 1, "max_order_quantity": 5}]})
gc.TRANSPORT = fazer

class Bot:
    def __init__(self): self.sent = []
    async def send_message(self, chat_id, text, **kw): self.sent.append(text)

async def main():
    sessions = await init_db("sqlite+aiosqlite:///:memory:")
    cipher = TokenCipher(os.environ["SECRET_KEY"])
    tg = OWNER_ID
    user = SimpleNamespace(id=tg, username="o", first_name="o")
    async with sessions() as s:
        seller = await get_or_create_seller(s, user)
        seller.playerok_id = "me"; seller.token_enc = cipher.encrypt("T"); await s.commit()
        await gc.set_api_key(s, tg, "fc_TEST_KEY_1234")
        await ft.set_setting(s, tg, pricing.NOMINAL_LINK_KEY, "https://playerok.com/products/x")
        await ft.set_setting(s, tg, pricing.FAZER_CAT_KEY, "roblox_global")
        await ft.set_setting(s, tg, pricing.NOMINAL_COSTS_KEY, "100 0.5")  # FazerCards главнее ручной
        costs, note = await pricing.load_costs(s, tg)
    assert note is None and costs[100].usd == 1.05 and costs[100].source == "fazer" and costs[500].stock == 0, costs
    print("1. закупка подтянута из FazerCards (главнее ручной), номинал из названия «100 Robux»")

    # отчёт: 120 ₽ ÷ 104 = $1.15 − $1.05 = +$0.10 ✅; 500: 520/104 = $5.00 − 5.30 = −$0.30 ❌
    sent = []
    class Msg:
        async def answer(self, text, reply_markup=None, **k):
            sent.append((text, reply_markup)); return SimpleNamespace(edit_text=_noop, delete=_noop)
    async def _noop(*a, **k): pass
    await hp._nominal(Msg(), sessions, cipher, user)
    text, kb = sent[-1]
    assert "FazerCards $1.05 (в наличии 7) → прибыль <b>$+0.10</b>" in text and "✅" in text, text
    assert "FazerCards $5.3 (нет в наличии) → прибыль <b>$-0.30</b>" in text, text
    assert "10 ₽" not in text  # свой дешёвый лот не учитывается
    assert [b.callback_data for r in kb.inline_keyboard for b in r] == ["dp:nomrun", "dp:nom", "pc"]
    # экран настроек открывается сразу, все кнопки на нём
    mtext, mkb = await hp._market_menu(sessions, tg)
    btns = [b.callback_data for r in mkb.inline_keyboard for b in r]
    assert btns[0] == "dp:nomrun" and "dp:fz" in btns and "dp:fzoff" in btns and "dp:trk" in btns and "dp:nomlink" in btns
    assert "FazerCards" in mtext and "÷104" in mtext and "выключен" in mtext, mtext
    print("2. отчёт: Playerok ÷ 104 против FazerCards; настройки — отдельным экраном")

    # трекер: включаем, порог $0.05 → уведомление про 100 (500 в минус и нет в наличии)
    async with sessions() as s:
        await ft.set_setting(s, tg, pricing.TRACK_KEY, "1")
        await ft.set_setting(s, tg, pricing.TRACK_MIN_KEY, "0.05")
    bot = Bot()
    good = await pricing.track_market(bot, sessions, seller, PlayerokClient("T"))
    assert [r.nominal for r in good] == [100] and len(bot.sent) == 1 and "Стало выгодно" in bot.sent[0], bot.sent
    print("3. трекер уведомил:", bot.sent[0].splitlines()[2][:80])
    # повторно сразу — интервал 30 мин; принудительно — не дублирует уведомление
    assert await pricing.track_market(bot, sessions, seller, PlayerokClient("T")) is None
    await pricing.track_market(bot, sessions, seller, PlayerokClient("T"), force=True)
    assert len(bot.sent) == 1
    # цена упала — номинал невыгоден; снова поднялась — новое уведомление
    PRICES[100] = 100
    await pricing.track_market(bot, sessions, seller, PlayerokClient("T"), force=True)
    PRICES[100] = 130
    await pricing.track_market(bot, sessions, seller, PlayerokClient("T"), force=True)
    assert len(bot.sent) == 2, bot.sent
    print("4. без дублей; снова стал выгодным — новое уведомление")

    # не-админ: FazerCards не используется (ключ владельца)
    async with sessions() as s:
        await ft.set_setting(s, 555, pricing.FAZER_CAT_KEY, "roblox_global")
        await ft.set_setting(s, 555, pricing.NOMINAL_COSTS_KEY, "100 0.9")
        costs, _ = await pricing.load_costs(s, 555)
    assert costs[100].source == "manual" and 500 not in costs
    print("5. не-админ: только ручная закупка")
    print("OK")
asyncio.run(main())
