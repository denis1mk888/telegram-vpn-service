# bot.py — Telegram VPN Bot (aiogram + Platega + USDT Polygon + Referrals + Promo)
#
# Stack: aiogram 3.x, aiohttp, Marzban VPN panel
# Payments: Platega (RUB via SBP/card/crypto), USDT Polygon (direct, no fees)
# Features: subscription plans, promo codes, referral system, admin stats
#
# Configuration: set variables in the CONFIG section below

import asyncio
import logging
import aiohttp
import random
import string
import json
import os
from datetime import datetime, timedelta
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

# ===== CONFIG =====
BOT_TOKEN = "YOUR_BOT_TOKEN"                        # @BotFather
MARZBAN_URL = "https://yourdomain.com:8000"         # Marzban panel URL
MARZBAN_USER = "admin"
MARZBAN_PASS = "YOUR_MARZBAN_PASSWORD"
SUPPORT_USERNAME = "your_support_username"          # Telegram username without @
ADMIN_ID = 000000000                                # Your Telegram user ID
USDT_POLYGON_ADDRESS = "0xYOUR_POLYGON_WALLET"      # USDT (Polygon) wallet address
USDT_RATE = 73                                      # 1 USDT = N RUB

PLATEGA_MERCHANT_ID = "YOUR_PLATEGA_MERCHANT_ID"
PLATEGA_API_KEY = "YOUR_PLATEGA_API_KEY"
PLATEGA_WEBHOOK_HOST = "0.0.0.0"
PLATEGA_WEBHOOK_PORT = 8088
PLATEGA_CALLBACK_URL = "https://yourdomain.com/platega/webhook"
PLATEGA_SUCCESS_URL = "https://t.me/your_bot"

ETHERSCAN_API_KEY = "YOUR_ETHERSCAN_API_KEY"        # Free at etherscan.io

PROMO_DISCOUNT = 15  # discount percent

# ===== PLANS =====
PLANS = {
    "1m":  {"name": "1 месяц",    "days": 30,  "rub": 299,  "usdt": 3.5},
    "2m":  {"name": "2 месяца",   "days": 60,  "rub": 499,  "usdt": 5.5},
    "3m":  {"name": "3 месяца",   "days": 90,  "rub": 699,  "usdt": 8.0},
    "6m":  {"name": "6 месяцев",  "days": 180, "rub": 1199, "usdt": 13.5},
    "12m": {"name": "12 месяцев", "days": 365, "rub": 1999, "usdt": 22.0},
    "24m": {"name": "24 месяца",  "days": 730, "rub": 3499, "usdt": 39.0},
}

platega_orders = {}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ===== FSM =====
class PromoState(StatesGroup):
    waiting_for_code = State()

# ===== FILES =====
REF_FILE = "/var/lib/marzban/referrals.json"
PROMO_FILE = "/var/lib/marzban/promos.json"
STATS_FILE = "/var/lib/marzban/stats.json"

def load_refs():
    if os.path.exists(REF_FILE):
        with open(REF_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_refs(data):
    with open(REF_FILE, 'w') as f:
        json.dump(data, f)

def load_promos():
    if os.path.exists(PROMO_FILE):
        with open(PROMO_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_promos(data):
    with open(PROMO_FILE, 'w') as f:
        json.dump(data, f)

def load_stats():
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE, 'r') as f:
            return json.load(f)
    return {"users": [], "payments": []}

def save_stats(data):
    with open(STATS_FILE, 'w') as f:
        json.dump(data, f)

# ===== STATS =====
def track_user(user_id: int, source: str = "direct"):
    data = load_stats()
    uid = str(user_id)
    existing_ids = [u["id"] if isinstance(u, dict) else u for u in data["users"]]
    if uid not in existing_ids:
        data["users"].append({
            "id": uid,
            "joined": datetime.now().strftime("%d.%m.%Y %H:%M"),
            "source": source
        })
        save_stats(data)

def track_payment(user_id: int, plan_name: str, amount: int, method: str, promo: str = None):
    data = load_stats()
    data["payments"].append({
        "user_id": str(user_id),
        "plan": plan_name,
        "amount": amount,
        "method": method,
        "promo": promo,
        "date": datetime.now().strftime("%d.%m.%Y %H:%M")
    })
    save_stats(data)

def apply_discount(price: int, discount: int) -> int:
    return int(price * (100 - discount) / 100)

# ===== PROMO CODES =====
def create_promo(code: str) -> bool:
    promos = load_promos()
    code = code.upper()
    if code in promos:
        return False
    expire_dt = datetime.now() + timedelta(days=7)
    promos[code] = {
        "discount": PROMO_DISCOUNT,
        "expires_at": expire_dt.isoformat(),
        "used_by": [],
    }
    save_promos(promos)
    return True

def check_promo(code: str, user_id: int):
    promos = load_promos()
    code = code.upper()
    if code not in promos:
        return None, "❌ Промокод не найден"
    promo = promos[code]
    expires_at = datetime.fromisoformat(promo["expires_at"])
    if datetime.now() > expires_at:
        return None, "❌ Промокод истёк"
    if str(user_id) in promo["used_by"]:
        return None, "❌ Вы уже использовали этот промокод"
    return promo["discount"], "✅ Промокод применён"

def use_promo(code: str, user_id: int):
    promos = load_promos()
    code = code.upper()
    if code in promos:
        if str(user_id) not in promos[code]["used_by"]:
            promos[code]["used_by"].append(str(user_id))
            save_promos(promos)

# ===== REFERRALS =====
def add_referral(referrer_id: int, new_user_id: int):
    data = load_refs()
    str_new = str(new_user_id)
    str_ref = str(referrer_id)
    if str_new not in data.get("referred_by", {}):
        if "referred_by" not in data:
            data["referred_by"] = {}
        data["referred_by"][str_new] = str_ref
        if "referrals" not in data:
            data["referrals"] = {}
        if str_ref not in data["referrals"]:
            data["referrals"][str_ref] = []
        if str_new not in data["referrals"][str_ref]:
            data["referrals"][str_ref].append(str_new)
        save_refs(data)

def get_referrer(user_id: int):
    data = load_refs()
    return data.get("referred_by", {}).get(str(user_id))

def get_ref_count(user_id: int):
    data = load_refs()
    return len(data.get("referrals", {}).get(str(user_id), []))

def mark_ref_rewarded(referrer_id: int, new_user_id: int):
    data = load_refs()
    if "rewarded" not in data:
        data["rewarded"] = []
    key = f"{referrer_id}_{new_user_id}"
    if key not in data["rewarded"]:
        data["rewarded"].append(key)
        save_refs(data)
        return True
    return False

def get_used_hashes():
    data = load_refs()
    return data.get("used_tx_hashes", [])

def mark_hash_used(tx_hash: str):
    data = load_refs()
    if "used_tx_hashes" not in data:
        data["used_tx_hashes"] = []
    if tx_hash not in data["used_tx_hashes"]:
        data["used_tx_hashes"].append(tx_hash)
        save_refs(data)
        return True
    return False

# ===== MARZBAN =====
async def marzban_get_token():
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{MARZBAN_URL}/api/admin/token",
            data={"username": MARZBAN_USER, "password": MARZBAN_PASS},
            ssl=False
        ) as resp:
            data = await resp.json()
            return data.get("access_token")

async def marzban_create_user(username: str, days: int):
    token = await marzban_get_token()
    expire_ts = int((datetime.now() + timedelta(days=days)).timestamp())
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "username": username,
        "proxies": {"vless": {"flow": ""}},
        "inbounds": {"vless": ["VLESS WS"]},
        "data_limit": 0,
        "ip_limit": 3,
        "expire": expire_ts,
        "status": "active"
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{MARZBAN_URL}/api/user",
            json=payload, headers=headers, ssl=False
        ) as resp:
            return await resp.json()

async def marzban_get_user_by_tgid(tg_id: int):
    token = await marzban_get_token()
    headers = {"Authorization": f"Bearer {token}"}
    prefix = f"u{tg_id}_"
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{MARZBAN_URL}/api/users?limit=100",
            headers=headers, ssl=False
        ) as resp:
            data = await resp.json()
            users = data.get("users", [])
            matched = [u for u in users if u.get("username", "").startswith(prefix)]
            if not matched:
                return None
            matched.sort(key=lambda u: u.get("created_at", ""), reverse=True)
            return matched[0]

async def marzban_extend_user(username: str, extra_days: int):
    token = await marzban_get_token()
    headers = {"Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{MARZBAN_URL}/api/user/{username}",
            headers=headers, ssl=False
        ) as resp:
            user = await resp.json()
    current_expire = user.get("expire") or 0
    now_ts = int(datetime.now().timestamp())
    base_ts = max(current_expire, now_ts)
    new_expire = base_ts + extra_days * 86400
    async with aiohttp.ClientSession() as session:
        await session.put(
            f"{MARZBAN_URL}/api/user/{username}",
            json={"expire": new_expire}, headers=headers, ssl=False
        )

def gen_username(tg_id: int) -> str:
    suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=5))
    return f"u{tg_id}_{suffix}"

async def reward_referrer(buyer_id: int):
    referrer_id = get_referrer(buyer_id)
    if not referrer_id:
        return
    if not mark_ref_rewarded(int(referrer_id), buyer_id):
        return
    ref_user = await marzban_get_user_by_tgid(int(referrer_id))
    if ref_user:
        await marzban_extend_user(ref_user["username"], 7)
    try:
        await bot.send_message(
            int(referrer_id),
            "🎁 <b>+7 дней к подписке!</b>\n\nВаш друг оплатил подписку — бонус начислен автоматически.",
            parse_mode="HTML"
        )
    except Exception:
        pass

def success_text(plan: dict, sub_url: str) -> str:
    return (
        f"✅ <b>Оплата прошла!</b>\n\n"
        f"🔑 Подписка на <b>{plan['name']}</b> активирована.\n\n"
        f"📎 Ссылка подписки:\n<code>{sub_url}</code>\n\n"
        f"📱 <b>Инструкция по подключению:</b>\n\n"
        f"<b>Android / iOS / Mac:</b>\n"
        f"1. Скачайте <b>Happ</b> (рекомендуем)\n"
        f"2. Вставьте ссылку подписки — и всё готово ✅\n"
        f"Также работает: Hiddify, V2rayNG\n\n"
        f"<b>Windows:</b>\n"
        f"1. Скачайте <b>Karing</b>\n"
        f"2. Вставьте ссылку подписки\n"
        f"3. Включите режим <b>TUN</b>\n\n"
        f"📱 Подписка работает до <b>3 устройств</b> одновременно"
    )

# ===== USDT POLYGON =====
async def check_polygon_payment(expected_amount_usdt: float, since_ts: int):
    # Checks incoming USDT (Polygon) transactions via Etherscan API
    # Each tx_hash can only be used once to prevent double-spending
    usdt_contract = "0xc2132d05d31c914a87c6611c10748aeb04b58e8f"
    url = "https://api.etherscan.io/v2/api"
    params = {
        "chainid": "137",
        "module": "account",
        "action": "tokentx",
        "contractaddress": usdt_contract,
        "address": USDT_POLYGON_ADDRESS,
        "sort": "desc",
        "apikey": ETHERSCAN_API_KEY,
    }
    used = get_used_hashes()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as resp:
                data = await resp.json()
                txs = data.get("result", [])
                if not isinstance(txs, list):
                    return None
                for tx in txs:
                    ts = int(tx.get("timeStamp", 0))
                    if ts < since_ts - 3600:
                        continue
                    if tx.get("to", "").lower() != USDT_POLYGON_ADDRESS.lower():
                        continue
                    tx_hash = tx.get("hash", "")
                    if tx_hash in used:
                        continue
                    amount = float(tx.get("value", 0)) / 1_000_000
                    if amount >= expected_amount_usdt * 0.98:
                        return tx_hash
    except Exception as e:
        logger.error(f"Polygon check error: {e}")
    return None

# ===== PLATEGA PAYMENTS =====
async def create_platega_invoice(amount_rub: int, order_id: str, plan_name: str) -> dict:
    url = "https://app.platega.io/v2/transaction/process"
    headers = {
        "Content-Type": "application/json",
        "X-MerchantId": PLATEGA_MERCHANT_ID,
        "X-Secret": PLATEGA_API_KEY,
    }
    data = {
        "paymentDetails": {
            "amount": amount_rub,
            "currency": "RUB",
        },
        "description": f"VPN — {plan_name}",
        "return": PLATEGA_SUCCESS_URL,
        "failedUrl": PLATEGA_SUCCESS_URL,
        "payload": order_id,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=data, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                text = await resp.text()
                logger.info(f"Platega response {resp.status}: {text}")
                return await resp.json(content_type=None)
    except Exception as e:
        logger.error(f"Platega error: {e}")
        return {"error": str(e)}

async def platega_webhook_handler(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        logger.info(f"Platega webhook: {data}")
    except Exception:
        body = await request.text()
        logger.info(f"Platega webhook (raw): {body}")
        return web.Response(text="ok")
    order_id = data.get("order_id") or data.get("orderId") or data.get("payload")
    status = str(data.get("status") or "").lower()
    if order_id and status in ("success", "paid", "completed", "1", "paid_over"):
        order = platega_orders.get(order_id)
        if order and order.get("status") == "pending":
            order["status"] = "processing"
            asyncio.create_task(deliver_platega_key(order["user_id"], order_id))
    return web.Response(text="ok")

async def deliver_platega_key(user_id: int, order_id: str):
    order = platega_orders.get(order_id)
    if not order:
        return
    plan = PLANS[order["plan_key"]]
    try:
        username = gen_username(user_id)
        user_data = await marzban_create_user(username, plan["days"])
        sub_url = user_data.get("subscription_url") or f"{MARZBAN_URL}/sub/{username}"
        order["status"] = "delivered"
        track_payment(user_id, plan["name"], plan["rub"], "Platega", promo=order.get("promo_code"))
        if order.get("promo_code"):
            use_promo(order["promo_code"], user_id)
        text = success_text(plan, sub_url)
        await reward_referrer(user_id)
    except Exception as e:
        logger.error(f"Key delivery error: {e}")
        order["status"] = "error"
        text = (
            f"✅ Оплата получена!\n\n"
            f"⚠️ Техническая ошибка при выдаче ключа.\n"
            f"Обратись в поддержку: @{SUPPORT_USERNAME}\n"
            f"Номер заказа: <code>{order_id}</code>"
        )
    try:
        await bot.send_message(ADMIN_ID, f"💰 <b>Новая оплата!</b>\n\nТариф: {plan['name']}\nСумма: {plan['rub']} ₽\nПользователь: {user_id}", parse_mode="HTML")
    except Exception:
        pass
    try:
        await bot.send_message(user_id, text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Failed to send key to {user_id}: {e}")

async def run_webhook_server():
    app = web.Application()
    app.router.add_post("/platega/webhook", platega_webhook_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, PLATEGA_WEBHOOK_HOST, PLATEGA_WEBHOOK_PORT)
    await site.start()
    logger.info(f"Webhook server started on port {PLATEGA_WEBHOOK_PORT}")

# ===== KEYBOARDS =====
def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Купить подписку", callback_data="buy")],
        [InlineKeyboardButton(text="📋 Моя подписка", callback_data="my_sub")],
        [InlineKeyboardButton(text="💬 Поддержка", callback_data="support")],
        [InlineKeyboardButton(text="📱 Как подключиться", callback_data="howto")],
        [InlineKeyboardButton(text="👥 Реферальная программа", callback_data="referral")],
        [InlineKeyboardButton(text="ℹ️ Информация", callback_data="info")],
    ])

def plans_keyboard():
    rows = []
    for key, plan in PLANS.items():
        rows.append([InlineKeyboardButton(
            text=f"{plan['name']} — {plan['rub']} руб.",
            callback_data=f"plan_{key}"
        )])
    rows.append([InlineKeyboardButton(text="🎟 Ввести промокод", callback_data="enter_promo")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def payment_keyboard(plan_key: str, discounted_rub: int = None):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Рубли (СБП / карта / крипта)", callback_data=f"pay_platega_{plan_key}")],
        [InlineKeyboardButton(text="🟣 USDT Polygon (без комиссии)", callback_data=f"pay_polygon_{plan_key}")],
        [InlineKeyboardButton(text="🎟 Ввести промокод", callback_data=f"promo_for_{plan_key}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="buy")],
    ])

# ===== START =====
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    args = message.text.split()
    source = args[1] if len(args) > 1 else "direct"
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            referrer_id = int(args[1].replace("ref_", ""))
            if referrer_id != message.from_user.id:
                add_referral(referrer_id, message.from_user.id)
        except Exception:
            pass
    track_user(message.from_user.id, source)
    await message.answer(
        "🚀 <b>VPN</b>\n\n"
        "🔐 Военное шифрование — ваши данные под защитой\n"
        "⚡ Максимальная скорость — без throttling\n"
        "👻 Никаких логов — полная анонимность\n\n"
        "📱 Android • iOS • Mac • Windows\n"
        "🇪🇺 Серверы в Европе\n\n"
        "💰 <b>От 146 ₽ в месяц</b>",
        parse_mode="HTML",
        reply_markup=main_menu()
    )

# ===== PLANS =====
@dp.callback_query(F.data == "buy")
async def show_plans(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "📦 <b>Выберите тариф:</b>\n\n💰 <b>От 146 ₽ в месяц</b>",
        parse_mode="HTML",
        reply_markup=plans_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("plan_"))
async def show_payment(callback: types.CallbackQuery, state: FSMContext):
    plan_key = callback.data.split("_", 1)[1]
    plan = PLANS[plan_key]
    amount_usdt = round(plan["rub"] / USDT_RATE, 2)

    data = await state.get_data()
    promo_code = data.get("promo_code")
    promo_plan = data.get("promo_plan")
    discount = data.get("discount", 0)

    if promo_code and promo_plan == plan_key and discount:
        discounted = apply_discount(plan["rub"], discount)
        price_text = f"💳 Рубли: <s>{plan['rub']} ₽</s> → <b>{discounted} ₽</b> (-{discount}%)\n"
        promo_text = f"🎟 Промокод <b>{promo_code}</b> применён!\n\n"
    else:
        discounted = None
        price_text = f"💳 Рубли (СБП/карта/крипта): <b>{plan['rub']} ₽</b>\n"
        promo_text = ""

    await callback.message.edit_text(
        f"✅ <b>{plan['name']} — {plan['rub']} руб.</b>\n\n"
        f"{promo_text}"
        f"Способы оплаты:\n"
        f"{price_text}"
        f"🟣 USDT Polygon (без комиссии): <b>{amount_usdt} USDT</b>\n\n"
        f"Выберите удобный способ:",
        parse_mode="HTML",
        reply_markup=payment_keyboard(plan_key, discounted)
    )
    await callback.answer()

# ===== PROMO CODE =====
@dp.callback_query(F.data == "enter_promo")
async def enter_promo_from_plans(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(PromoState.waiting_for_code)
    await state.update_data(promo_plan=None)
    await callback.message.edit_text(
        "🎟 <b>Введите промокод:</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="buy")]
        ])
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("promo_for_"))
async def enter_promo_for_plan(callback: types.CallbackQuery, state: FSMContext):
    plan_key = callback.data.replace("promo_for_", "")
    await state.set_state(PromoState.waiting_for_code)
    await state.update_data(promo_plan=plan_key)
    await callback.message.edit_text(
        "🎟 <b>Введите промокод:</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data=f"plan_{plan_key}")]
        ])
    )
    await callback.answer()

@dp.message(PromoState.waiting_for_code)
async def process_promo_code(message: types.Message, state: FSMContext):
    code = message.text.strip().upper()
    user_id = message.from_user.id
    discount, msg = check_promo(code, user_id)

    data = await state.get_data()
    plan_key = data.get("promo_plan")

    if discount is None:
        await message.answer(
            f"{msg}\n\nПопробуйте другой промокод или выберите тариф без скидки.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📦 К тарифам", callback_data="buy")]
            ])
        )
        await state.clear()
        return

    await state.update_data(promo_code=code, discount=discount)

    if plan_key:
        plan = PLANS[plan_key]
        discounted = apply_discount(plan["rub"], discount)
        await message.answer(
            f"✅ Промокод <b>{code}</b> применён! Скидка {discount}%\n\n"
            f"Тариф: <b>{plan['name']}</b>\n"
            f"Цена: <s>{plan['rub']} ₽</s> → <b>{discounted} ₽</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💳 Оплатить рублями", callback_data=f"pay_platega_{plan_key}")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data=f"plan_{plan_key}")],
            ])
        )
    else:
        await message.answer(
            f"✅ Промокод <b>{code}</b> применён! Скидка {discount}%\n\nТеперь выберите тариф:",
            parse_mode="HTML",
            reply_markup=plans_keyboard()
        )
    await state.set_state(None)

# ===== PLATEGA PAYMENT =====
@dp.callback_query(F.data.startswith("pay_platega_"))
async def pay_platega(callback: types.CallbackQuery, state: FSMContext):
    import uuid
    plan_key = callback.data.replace("pay_platega_", "")
    plan = PLANS[plan_key]
    user_id = callback.from_user.id

    state_data = await state.get_data()
    promo_code = state_data.get("promo_code")
    discount = state_data.get("discount", 0)

    amount = plan["rub"]
    promo_text = ""
    active_promo = None

    if promo_code and discount:
        disc, check_msg = check_promo(promo_code, user_id)
        if disc:
            amount = apply_discount(plan["rub"], disc)
            promo_text = f"🎟 Промокод <b>{promo_code}</b> (-{disc}%)\n"
            active_promo = promo_code

    order_id = str(uuid.uuid4())
    platega_orders[order_id] = {
        "user_id": user_id,
        "plan_key": plan_key,
        "status": "pending",
        "promo_code": active_promo,
    }

    invoice = await create_platega_invoice(amount, order_id, plan["name"])
    pay_url = invoice.get("redirect") or invoice.get("pay_url") or invoice.get("payment_url") or invoice.get("url")

    if pay_url:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Перейти к оплате", url=pay_url)],
            [InlineKeyboardButton(text="◀️ Назад", callback_data=f"plan_{plan_key}")],
        ])
        await callback.message.edit_text(
            f"💳 <b>Оплата — {plan['name']}</b>\n\n"
            f"{promo_text}"
            f"💰 Сумма: <b>{amount} ₽</b>\n\n"
            f"Нажми кнопку ниже — откроется страница оплаты (СБП, карта или крипта).\n"
            f"После оплаты ключ придёт автоматически в этот чат.",
            parse_mode="HTML",
            reply_markup=keyboard
        )
    else:
        logger.error(f"Platega returned no URL: {invoice}")
        await callback.answer("Ошибка создания счёта. Попробуйте позже.", show_alert=True)
    await callback.answer()

# ===== USDT POLYGON PAYMENT =====
@dp.callback_query(F.data.startswith("pay_polygon_"))
async def pay_polygon(callback: types.CallbackQuery):
    plan_key = callback.data.replace("pay_polygon_", "")
    plan = PLANS[plan_key]
    amount_usdt = round(plan["rub"] / USDT_RATE, 2)
    since_ts = int(datetime.now().timestamp()) - 1800
    await callback.message.edit_text(
        f"🟣 <b>Оплата USDT Polygon (без комиссии)</b>\n\n"
        f"Тариф: <b>{plan['name']}</b>\n"
        f"Сумма: <b>{amount_usdt} USDT</b>\n\n"
        f"Переведи точную сумму на адрес:\n"
        f"<code>{USDT_POLYGON_ADDRESS}</code>\n\n"
        f"⚠️ Только сеть <b>Polygon (MATIC)</b>\n"
        f"После оплаты нажми кнопку ниже.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"check_polygon_{plan_key}_{since_ts}")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data=f"plan_{plan_key}")],
        ])
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("check_polygon_"))
async def check_polygon_handler(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    plan_key = parts[2]
    since_ts = int(parts[3])
    plan = PLANS[plan_key]
    amount_usdt = round(plan["rub"] / USDT_RATE, 2)
    await callback.answer("Проверяем оплату...", show_alert=False)
    tx_hash = await check_polygon_payment(amount_usdt, since_ts)
    if not tx_hash:
        await callback.message.edit_text(
            f"⏳ <b>Оплата не найдена</b>\n\nПодожди 1-2 минуты и нажми снова.\n\nСумма: <b>{amount_usdt} USDT</b>\nАдрес: <code>{USDT_POLYGON_ADDRESS}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Проверить снова", callback_data=f"check_polygon_{plan_key}_{since_ts}")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data=f"plan_{plan_key}")],
            ])
        )
        return
    mark_hash_used(tx_hash)
    track_payment(callback.from_user.id, plan["name"], plan["rub"], "USDT Polygon")
    username = gen_username(callback.from_user.id)
    user_data = await marzban_create_user(username, plan["days"])
    sub_url = user_data.get("subscription_url") or f"{MARZBAN_URL}/sub/{username}"
    await reward_referrer(callback.from_user.id)
    await callback.message.edit_text(success_text(plan, sub_url), parse_mode="HTML", reply_markup=main_menu())

# ===== MY SUBSCRIPTION =====
@dp.callback_query(F.data == "my_sub")
async def my_subscription(callback: types.CallbackQuery):
    user = await marzban_get_user_by_tgid(callback.from_user.id)
    if not user:
        text = "📋 <b>Моя подписка</b>\n\n❌ Активная подписка не найдена.\n\nНажмите <b>Купить подписку</b> чтобы подключиться."
    else:
        status = user.get("status", "unknown")
        expire_ts = user.get("expire")
        sub_url = user.get("subscription_url") or f"{MARZBAN_URL}/sub/{user.get('username', '')}"
        if expire_ts:
            expire_dt = datetime.fromtimestamp(expire_ts)
            days_left = (expire_dt - datetime.now()).days
            expire_str = expire_dt.strftime("%d.%m.%Y")
            days_str = f"⏳ Осталось: <b>{days_left} дн.</b> (до {expire_str})" if days_left > 0 else f"❌ Подписка истекла <b>{expire_str}</b>"
        else:
            days_str = "♾ Без ограничений по времени"
        status_str = "✅ Активна" if status == "active" else f"⚠️ {status}"
        text = f"📋 <b>Моя подписка</b>\n\nСтатус: {status_str}\n{days_str}\n\n📎 Ссылка подписки:\n<code>{sub_url}</code>"
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back")]]))
    await callback.answer()

# ===== HOW TO CONNECT =====
@dp.callback_query(F.data == "howto")
async def show_howto(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "📱 <b>Как подключиться к VPN</b>\n\n<b>Android / iOS / Mac:</b>\n1. Скачайте <b>Happ</b> (рекомендуем)\n2. Откройте приложение\n3. Нажмите + и вставьте ссылку подписки\n4. Нажмите Подключить ✅\n\nТакже работает: <b>Hiddify, V2rayNG</b>\n\n<b>Windows:</b>\n1. Скачайте <b>Karing</b>\n2. Вставьте ссылку подписки\n3. Включите режим <b>TUN</b>\n4. Подключитесь ✅\n\n💡 Ссылку подписки вы получите после оплаты",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛒 Купить подписку", callback_data="buy")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back")],
        ])
    )
    await callback.answer()

# ===== REFERRAL =====
@dp.callback_query(F.data == "referral")
async def show_referral(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    ref_count = get_ref_count(user_id)
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{user_id}"
    text = (
        f"👥 <b>Реферальная программа</b>\n\nПриглашай друзей и получай <b>+7 дней</b> к подписке за каждого оплатившего!\n\n"
        f"🔗 Твоя реферальная ссылка:\n<code>{ref_link}</code>\n\n"
        f"👤 Приглашено друзей: <b>{ref_count}</b>\n🎁 Бонус начисляется автоматически после оплаты другом"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back")]]))
    await callback.answer()

# ===== SUPPORT =====
@dp.callback_query(F.data == "support")
async def show_support(callback: types.CallbackQuery):
    await callback.message.answer("💬 <b>Поддержка</b>\n\nПо всем вопросам: @your_support_username", parse_mode="HTML")
    await callback.answer()

# ===== INFO =====
@dp.callback_query(F.data == "info")
async def show_info(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "ℹ️ <b>Информация</b>\n\n📄 Политика конфиденциальности\n📋 Пользовательское соглашение",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back")]])
    )
    await callback.answer()

# ===== BACK =====
@dp.callback_query(F.data == "back")
async def go_back(callback: types.CallbackQuery):
    await callback.message.edit_text("🚀 <b>VPN</b>\n\nШифрование. Анонимность. Никаких логов.", parse_mode="HTML", reply_markup=main_menu())
    await callback.answer()

# ===== ADMIN COMMANDS =====
@dp.message(Command("addpromo"))
async def add_promo(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: /addpromo КОД\nПример: /addpromo SALE15")
        return
    code = args[1].upper()
    if create_promo(code):
        expire_dt = datetime.now() + timedelta(days=7)
        await message.answer(
            f"✅ Промокод <b>{code}</b> создан!\n"
            f"Скидка: <b>{PROMO_DISCOUNT}%</b>\n"
            f"Действует до: <b>{expire_dt.strftime('%d.%m.%Y')}</b>\n"
            f"Многоразовый",
            parse_mode="HTML"
        )
    else:
        await message.answer(f"❌ Промокод <b>{code}</b> уже существует.", parse_mode="HTML")

@dp.message(Command("promos"))
async def list_promos(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    promos = load_promos()
    if not promos:
        await message.answer("Промокодов нет")
        return
    text = "🎟 <b>Активные промокоды:</b>\n\n"
    for code, data in promos.items():
        expires_at = datetime.fromisoformat(data["expires_at"])
        status = "✅" if datetime.now() < expires_at else "❌ истёк"
        used = len(data["used_by"])
        text += f"<b>{code}</b> — {data['discount']}% | до {expires_at.strftime('%d.%m.%Y')} {status} | использован {used} раз\n"
    await message.answer(text, parse_mode="HTML")

@dp.message(Command("stats"))
async def admin_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    data = load_refs()
    referrals = data.get("referrals", {})
    rewarded = data.get("rewarded", [])
    text = "📊 <b>Статистика рефералов</b>\n\n"
    if not referrals:
        text += "Пока никого нет"
    else:
        for ref_id, users in referrals.items():
            paid_count = sum(1 for u in users if f"{ref_id}_{u}" in rewarded)
            text += f"👤 ID {ref_id}: привёл {len(users)}, оплатили {paid_count}\n"
    await message.answer(text, parse_mode="HTML")

@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    data = load_stats()
    payments = data["payments"]
    users = data["users"]

    now = datetime.now()
    today = now.strftime("%d.%m.%Y")
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    def parse_date(d):
        try:
            return datetime.strptime(d, "%d.%m.%Y %H:%M")
        except:
            return datetime(2000, 1, 1)

    total_users = len(users)
    users_today = sum(1 for u in users if isinstance(u, dict) and u.get("joined", "").startswith(today))
    users_week = sum(1 for u in users if isinstance(u, dict) and parse_date(u.get("joined", "")) >= week_ago)
    users_month = sum(1 for u in users if isinstance(u, dict) and parse_date(u.get("joined", "")) >= month_ago)
    ref_users = sum(1 for u in users if isinstance(u, dict) and u.get("source", "direct").startswith("ref_"))

    total_payments = len(payments)
    total_rub = sum(p["amount"] for p in payments)
    pay_today = [p for p in payments if p["date"].startswith(today)]
    pay_week = [p for p in payments if parse_date(p["date"]) >= week_ago]
    pay_month = [p for p in payments if parse_date(p["date"]) >= month_ago]

    from collections import Counter
    plan_counts = Counter(p["plan"] for p in payments)
    top_plan = plan_counts.most_common(1)[0] if plan_counts else ("—", 0)
    method_counts = Counter(p["method"] for p in payments)
    promo_used = sum(1 for p in payments if p.get("promo"))
    conversion = round(total_payments / total_users * 100, 1) if total_users else 0

    text = (
        f"📊 <b>Статистика бота</b>\n\n"
        f"👥 <b>Пользователи:</b>\n"
        f"  Всего: <b>{total_users}</b>\n"
        f"  Сегодня: <b>{users_today}</b>\n"
        f"  За 7 дней: <b>{users_week}</b>\n"
        f"  За 30 дней: <b>{users_month}</b>\n"
        f"  По рефералке: <b>{ref_users}</b>\n\n"
        f"💰 <b>Оплаты:</b>\n"
        f"  Всего: <b>{total_payments}</b> на <b>{total_rub} ₽</b>\n"
        f"  Сегодня: <b>{len(pay_today)}</b> на <b>{sum(p['amount'] for p in pay_today)} ₽</b>\n"
        f"  За 7 дней: <b>{len(pay_week)}</b> на <b>{sum(p['amount'] for p in pay_week)} ₽</b>\n"
        f"  За 30 дней: <b>{len(pay_month)}</b> на <b>{sum(p['amount'] for p in pay_month)} ₽</b>\n\n"
        f"📈 <b>Аналитика:</b>\n"
        f"  Конверсия: <b>{conversion}%</b>\n"
        f"  Топ тариф: <b>{top_plan[0]}</b> ({top_plan[1]} раз)\n"
        f"  Промокоды: <b>{promo_used}</b> оплат со скидкой\n"
        f"  Рубли: <b>{method_counts.get('Platega', 0)}</b> | USDT: <b>{method_counts.get('USDT Polygon', 0)}</b>\n\n"
    )
    if payments:
        text += "<b>Последние 10 оплат:</b>\n"
        for p in payments[-10:][::-1]:
            promo_str = f" 🎟{p['promo']}" if p.get("promo") else ""
            text += f"• {p['date']} — {p['plan']} — {p['amount']} ₽ ({p['method']}){promo_str}\n"
    await message.answer(text, parse_mode="HTML")

# ===== MAIN =====
async def main():
    await run_webhook_server()
    logger.info("Bot started!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
