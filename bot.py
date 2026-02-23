import os
import sqlite3
import logging
import json
import re
from typing import Any

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ----------------- Logging -----------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ----------------- Config -----------------
DB_NAME = os.getenv("DB_PATH", "otodom_ads.db")

# Conversation states (search)
SELECTING_DISTRICTS, SELECTING_BUDGET, SELECTING_ROOMS = range(3)

# Conversation states (lead)
LEAD_METHOD, LEAD_PHONE = range(2)

# Pagination
PAGE_SIZE = 5

# Invisible safe text for Telegram messages (NOT empty, but looks empty)
INVISIBLE_TEXT = "\u2800"  # Braille blank character

# ----------------- UI dictionaries -----------------
DISTRICTS = [
    "Mokotów",
    "Wola",
    "Śródmieście",
    "Ursynów",
    "Praga-Południe",
    "Ochota",
    "Żoliborz",
    "Bielany",
    "Wilanów",
    "Bemowo",
    "Ursus",
    "Targówek",
    "Włochy",
    "Praga-Północ",
    "Białołęka",
]
DISTRICT_LABELS = {d: d for d in DISTRICTS}

BUDGETS = {
    "0-4000": "До 4000 PLN",
    "4000-5000": "4000-5000 PLN",
    "5000-7000": "5000-7000 PLN",
    "7000-10000": "7000-10000 PLN",
    "10000+": "Более 10000 PLN",
}

ROOMS_OPTIONS = {
    "1": "1 комната",
    "2": "2 комнаты",
    "3": "3 комнаты",
    "4": "4 комнаты",
    "5+": "5+ комнат",
}

# ----------------- DB helpers -----------------
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_NAME, timeout=30)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute("PRAGMA synchronous=NORMAL;")
    cur.execute("PRAGMA busy_timeout=30000;")
    return conn


def ensure_schema() -> None:
    conn = get_conn()
    cur = conn.cursor()

    # ads table (existing)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ads (
            ad_id INTEGER PRIMARY KEY,
            title TEXT,
            price TEXT,
            area REAL,
            rooms TEXT,
            city TEXT,
            province TEXT,
            district TEXT,
            street TEXT,
            details_url TEXT,
            photos TEXT,
            first_seen DATETIME
        )
        """
    )

    # leads table (new)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS leads (
            lead_id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            ad_id INTEGER,
            telegram_user_id INTEGER,
            telegram_username TEXT,
            full_name TEXT,
            contact_method TEXT,
            phone TEXT,
            status TEXT DEFAULT 'NEW'
        )
        """
    )

    conn.commit()
    conn.close()


def save_lead(ad_id: int | None, user, contact_method: str, phone: str | None) -> None:
    try:
        telegram_user_id = user.id if user else None
        telegram_username = user.username if user else None
        full_name = None
        if user:
            full_name = f"{(user.first_name or '').strip()} {(user.last_name or '').strip()}".strip() or None

        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO leads (ad_id, telegram_user_id, telegram_username, full_name, contact_method, phone, status)
            VALUES (?, ?, ?, ?, ?, ?, 'NEW')
            """,
            (ad_id, telegram_user_id, telegram_username, full_name, contact_method, phone),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"save_lead error: {e}")


def parse_photos(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [p for p in value if isinstance(p, str)]
    try:
        arr = json.loads(value)
        if isinstance(arr, list):
            return [p for p in arr if isinstance(p, str)]
    except Exception:
        pass
    return []

# ----------------- Bot flow: search steps -----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    context.user_data["selected_districts"] = []
    context.user_data["budget"] = None
    context.user_data["rooms"] = None
    context.user_data["offset"] = 0
    context.user_data["selected_ad_id"] = None

    keyboard = []
    for district in DISTRICTS:
        keyboard.append([InlineKeyboardButton(DISTRICT_LABELS[district], callback_data=f"district_{district}")])
    keyboard.append([InlineKeyboardButton("✅ Готово, продолжить", callback_data="districts_done")])

    await update.message.reply_text(
        "🏠 Добро пожаловать в поиск квартир в Варшаве!\n\n"
        "Выберите интересующие вас районы (можно несколько):",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return SELECTING_DISTRICTS


async def district_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "districts_done":
        if not context.user_data.get("selected_districts"):
            await query.edit_message_text("❌ Выберите хотя бы один район!")
            return SELECTING_DISTRICTS

        keyboard = []
        for budget_key, budget_label in BUDGETS.items():
            keyboard.append([InlineKeyboardButton(budget_label, callback_data=f"budget_{budget_key}")])

        selected = ", ".join(context.user_data["selected_districts"])
        await query.edit_message_text(
            f"✅ Выбранные районы: {selected}\n\n💰 Теперь выберите бюджет:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return SELECTING_BUDGET

    district = query.data.replace("district_", "")
    selected = context.user_data.get("selected_districts", [])

    if district in selected:
        selected.remove(district)
    else:
        selected.append(district)

    context.user_data["selected_districts"] = selected

    keyboard = []
    for dist in DISTRICTS:
        prefix = "✅ " if dist in selected else ""
        keyboard.append([InlineKeyboardButton(f"{prefix}{DISTRICT_LABELS[dist]}", callback_data=f"district_{dist}")])
    keyboard.append([InlineKeyboardButton("✅ Готово, продолжить", callback_data="districts_done")])

    await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECTING_DISTRICTS


async def budget_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    budget = query.data.replace("budget_", "")
    context.user_data["budget"] = budget

    keyboard = []
    for k, label in ROOMS_OPTIONS.items():
        keyboard.append([InlineKeyboardButton(label, callback_data=f"rooms_{k}")])
    keyboard.append([InlineKeyboardButton("Любое количество комнат", callback_data="rooms_any")])

    await query.edit_message_text(
        f"✅ Бюджет: {BUDGETS[budget]}\n\n🛏️ Выберите количество комнат:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return SELECTING_ROOMS


async def rooms_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "rooms_any":
        context.user_data["rooms"] = None
    else:
        context.user_data["rooms"] = query.data.replace("rooms_", "")

    context.user_data["offset"] = 0
    await query.edit_message_text("🔍 Ищу подходящие квартиры...")

    await send_next_page(query.message, context)
    return ConversationHandler.END

# ----------------- Search + Pagination -----------------
def search_apartments(districts: list[str], budget: str, rooms: str | None, offset: int, limit: int) -> list[dict]:
    conn = get_conn()
    cur = conn.cursor()

    sql = "SELECT * FROM ads WHERE 1=1"
    params: list[Any] = []

    if districts:
        placeholders = ",".join(["?"] * len(districts))
        sql += f" AND district IN ({placeholders})"
        params.extend(districts)

    price_num_expr = (
        "CAST(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(price, 'PLN', ''), 'zł', ''), 'zl', ''), ' ', ''), '\u00a0', '') AS INTEGER)"
    )
    sql += f" AND {price_num_expr} > 0"

    if budget != "10000+":
        min_p, max_p = map(int, budget.split("-"))
        sql += f" AND {price_num_expr} BETWEEN ? AND ?"
        params.extend([min_p, max_p])
    else:
        sql += f" AND {price_num_expr} >= ?"
        params.append(10000)

    if rooms:
        num_to_text = {
            "1": "ONE", "2": "TWO", "3": "THREE", "4": "FOUR",
            "5": "FIVE", "6": "SIX", "7": "SEVEN", "8": "EIGHT",
            "9": "NINE", "10": "TEN",
        }
        if rooms == "5+":
            sql += " AND UPPER(rooms) IN ('FIVE','SIX','SEVEN','EIGHT','NINE','TEN')"
        else:
            rt = str(num_to_text.get(rooms, rooms)).strip().upper()
            sql += " AND UPPER(rooms) = ?"
            params.append(rt)

    sql += " ORDER BY datetime(first_seen) DESC"
    sql += " LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    cur.execute(sql, params)
    rows = cur.fetchall()
    conn.close()

    out = []
    for r in rows:
        d = dict(r)
        d["photos"] = parse_photos(d.get("photos"))
        out.append(d)
    return out


async def send_next_page(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    districts = context.user_data.get("selected_districts", [])
    budget = context.user_data.get("budget")
    rooms = context.user_data.get("rooms")
    offset = int(context.user_data.get("offset", 0))

    if not budget:
        await message.reply_text("❌ Ошибка: не выбран бюджет. Нажмите /start заново.")
        return

    results = search_apartments(districts, budget, rooms, offset=offset, limit=PAGE_SIZE)

    if offset == 0:
        if not results:
            await message.reply_text(
                "😔 К сожалению, не найдено квартир по вашим параметрам.\n\n"
                "Попробуйте изменить критерии поиска.\n\n"
                "Используйте /start для нового поиска."
            )
            return
        await message.reply_text("✅ Нашёл варианты. Отправляю...")

    if not results:
        await message.reply_text("Больше вариантов нет. Используйте /start для нового поиска.")
        return

    sent_any = False
    for apt in results:
        ok = await send_apartment_variant_a(message, apt)
        if ok:
            sent_any = True

    context.user_data["offset"] = offset + len(results)

    if sent_any:
        # CHANGED: use visible text so inline keyboard ALWAYS appears
        await message.reply_text(
            "Если не нешел могу",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Показать ещё", callback_data="more_results")]]),
        )
    else:
        await message.reply_text("Больше вариантов нет. Используйте /start для нового поиска.")


async def more_results(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await send_next_page(query.message, context)

# ----------------- Sending apartment: Variant A -----------------
def format_apartment(apt: dict) -> str:
    title = apt.get("title", "N/A")
    price = apt.get("price", "N/A")
    area = apt.get("area", "N/A")
    district = apt.get("district", "N/A")
    street = apt.get("street")

    rooms_display = apt.get("rooms", "N/A")
    text_to_num = {
        "ONE": "1", "TWO": "2", "THREE": "3", "FOUR": "4",
        "FIVE": "5", "SIX": "6", "SEVEN": "7", "EIGHT": "8",
        "NINE": "9", "TEN": "10",
    }
    rs = str(rooms_display).strip().upper()
    if rs in text_to_num:
        rooms_display = text_to_num[rs]
    else:
        try:
            rooms_display = str(int(re.sub(r"\D+", "", str(rooms_display))))
        except Exception:
            pass

    msg = f"🏠 {title}\n\n"
    msg += f"💰 Цена: {price}\n"
    msg += f"📐 Площадь: {area} м²\n"
    msg += f"🛏️ Комнат: {rooms_display}\n"
    msg += f"📍 Район: {district}\n"
    if street and street != "N/A":
        msg += f"🗺️ Улица: {street}\n"
    msg += f"\n🕐 Добавлено: {apt.get('first_seen', '')}"
    return msg


async def send_apartment_variant_a(message, apt: dict) -> bool:
    """
    Variant A:
    - 10 photos album (media_group), caption in first photo = description
    - second message: ONLY button “✅ Выбрать объявление”
    - If no photos -> skip listing entirely
    """
    photos = apt.get("photos", []) or []
    photos = [p for p in photos if isinstance(p, str) and p.startswith("http")]

    if len(photos) == 0:
        return False  # skip no-photo ads

    text = format_apartment(apt)

    select_markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Выбрать объявление", callback_data=f"select_{apt['ad_id']}")]]
    )

    # 1) Photos + caption
    try:
        if len(photos) >= 2:
            media = []
            for i, url in enumerate(photos[:10]):
                if i == 0:
                    media.append(InputMediaPhoto(media=url, caption=text[:1000]))
                else:
                    media.append(InputMediaPhoto(media=url))
            await message.reply_media_group(media)
        else:
            await message.reply_photo(photo=photos[0], caption=text[:1000])
    except Exception as e:
        logger.error(f"Send media error (ad_id={apt.get('ad_id')}): {e}")
        await message.reply_text(text)
        await message.reply_text("Нравится?", reply_markup=select_markup)
        return True

    # 2) CHANGED: use visible text so inline keyboard ALWAYS appears
    try:
        await message.reply_text("Нравится?", reply_markup=select_markup)
    except Exception as e:
        logger.error(f"Send button error (ad_id={apt.get('ad_id')}): {e}")

    return True

# ----------------- Lead flow -----------------
async def select_apartment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    try:
        ad_id = int(query.data.replace("select_", ""))
    except Exception:
        ad_id = None
    context.user_data["selected_ad_id"] = ad_id

    markup = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Telegram", callback_data="lead_tg")],
            [InlineKeyboardButton("Телефон", callback_data="lead_phone")],
        ]
    )

    # IMPORTANT: edit the SAME message where the Select button was
    await query.edit_message_text(
        "Отлично! Как удобнее с вами связаться?",
        reply_markup=markup,
    )
    return LEAD_METHOD


async def lead_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    ad_id = context.user_data.get("selected_ad_id")
    user = query.from_user

    if query.data == "lead_tg":
        save_lead(ad_id=ad_id, user=user, contact_method="telegram", phone=None)
        await query.edit_message_text("✅ Принято! Наш консультант свяжется с вами в течение 30 минут.")
        return ConversationHandler.END

    kb = ReplyKeyboardMarkup(
        [[KeyboardButton("📞 Отправить номер", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await query.message.reply_text("📞 Нажмите кнопку «Отправить номер» ниже 👇", reply_markup=kb)
    return LEAD_PHONE


async def lead_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    contact = update.message.contact
    if not contact:
        await update.message.reply_text("❌ Пожалуйста, отправьте номер кнопкой.")
        return LEAD_PHONE

    ad_id = context.user_data.get("selected_ad_id")
    user = update.effective_user

    save_lead(ad_id=ad_id, user=user, contact_method="phone", phone=contact.phone_number)

    await update.message.reply_text(
        "✅ Принято! Наш консультант свяжется с вами в течение 30 минут.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END

# ----------------- Cancel -----------------
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ Поиск отменен. Используйте /start для нового поиска.")
    return ConversationHandler.END

# ----------------- Main -----------------
def main():
    ensure_schema()

    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN is not set. Set BOT_TOKEN in environment (.env / docker).")

    app = Application.builder().token(token).build()

    search_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SELECTING_DISTRICTS: [CallbackQueryHandler(district_selection, pattern=r"^district_|^districts_done$")],
            SELECTING_BUDGET: [CallbackQueryHandler(budget_selection, pattern=r"^budget_")],
            SELECTING_ROOMS: [CallbackQueryHandler(rooms_selection, pattern=r"^rooms_|^rooms_any$")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(search_conv)

    app.add_handler(CallbackQueryHandler(more_results, pattern=r"^more_results$"))

    lead_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(select_apartment, pattern=r"^select_")],
        states={
            LEAD_METHOD: [CallbackQueryHandler(lead_method, pattern=r"^lead_tg$|^lead_phone$")],
            LEAD_PHONE: [MessageHandler(filters.CONTACT, lead_phone)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(lead_conv)

    logger.info("Bot started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()