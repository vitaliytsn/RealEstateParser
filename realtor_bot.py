import os
import sqlite3
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ----------------- Logging -----------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ----------------- Config -----------------
DB_NAME = os.getenv("DB_PATH", "otodom_ads.db")
REALTOR_CHAT_ID = int(os.getenv("REALTOR_CHAT_ID", "0"))
REALTOR_POLL_INTERVAL = int(os.getenv("REALTOR_POLL_INTERVAL", "20"))  # seconds

if REALTOR_CHAT_ID == 0:
    logger.warning("REALTOR_CHAT_ID is not set or equals 0. Bot will not notify to any group.")

# Statuses
STATUS_NEW = "NEW"
STATUS_IN_PROGRESS = "IN_PROGRESS"
STATUS_CONTACTED = "CONTACTED"
STATUS_VIEWING_SET = "VIEWING_SET"
STATUS_NOT_ACTUAL = "NOT_ACTUAL"
STATUS_CLIENT_REFUSED = "CLIENT_REFUSED"
STATUS_DONE = "DONE"

STATUS_LABELS = {
    STATUS_NEW: "🆕 NEW",
    STATUS_IN_PROGRESS: "🧲 В работе",
    STATUS_CONTACTED: "✅ Связался",
    STATUS_VIEWING_SET: "📅 Просмотр назначен",
    STATUS_NOT_ACTUAL: "🚫 Неактуально",
    STATUS_CLIENT_REFUSED: "🙅 Отказ клиента",
    STATUS_DONE: "🏁 Закрыто",
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


def _table_has_column(conn: sqlite3.Connection, table: str, col: str) -> bool:
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    cols = [r[1] for r in cur.fetchall()]
    return col in cols


def ensure_realtor_schema() -> None:
    """Add extra columns we need into leads (non-destructive)."""
    conn = get_conn()
    cur = conn.cursor()

    # leads must exist (created in client bot), but we protect anyway
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

    # Add missing columns (safe)
    alters: List[str] = []
    if not _table_has_column(conn, "leads", "notified"):
        alters.append("ALTER TABLE leads ADD COLUMN notified INTEGER DEFAULT 0")
    if not _table_has_column(conn, "leads", "notified_at"):
        alters.append("ALTER TABLE leads ADD COLUMN notified_at DATETIME")
    if not _table_has_column(conn, "leads", "realtor_chat_id"):
        alters.append("ALTER TABLE leads ADD COLUMN realtor_chat_id INTEGER")
    if not _table_has_column(conn, "leads", "realtor_msg_id"):
        alters.append("ALTER TABLE leads ADD COLUMN realtor_msg_id INTEGER")
    if not _table_has_column(conn, "leads", "claimed_by_id"):
        alters.append("ALTER TABLE leads ADD COLUMN claimed_by_id INTEGER")
    if not _table_has_column(conn, "leads", "claimed_by_username"):
        alters.append("ALTER TABLE leads ADD COLUMN claimed_by_username TEXT")
    if not _table_has_column(conn, "leads", "updated_at"):
        alters.append("ALTER TABLE leads ADD COLUMN updated_at DATETIME")

    for a in alters:
        try:
            cur.execute(a)
        except Exception as e:
            logger.error(f"Schema alter failed: {a} | {e}")

    conn.commit()
    conn.close()


def fetch_new_unnotified_leads(limit: int = 20) -> List[sqlite3.Row]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT *
        FROM leads
        WHERE COALESCE(notified, 0) = 0
        ORDER BY datetime(created_at) ASC
        LIMIT ?
        """,
        (limit,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def mark_lead_notified(lead_id: int, chat_id: int, msg_id: int) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE leads
        SET notified = 1,
            notified_at = CURRENT_TIMESTAMP,
            realtor_chat_id = ?,
            realtor_msg_id = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE lead_id = ?
        """,
        (chat_id, msg_id, lead_id),
    )
    conn.commit()
    conn.close()


def get_lead(lead_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM leads WHERE lead_id = ?", (lead_id,))
    row = cur.fetchone()
    conn.close()
    return row


def update_lead_status(lead_id: int, status: str) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE leads
        SET status = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE lead_id = ?
        """,
        (status, lead_id),
    )
    conn.commit()
    conn.close()


def claim_lead(lead_id: int, user_id: int, username: Optional[str]) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE leads
        SET claimed_by_id = ?,
            claimed_by_username = ?,
            status = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE lead_id = ?
        """,
        (user_id, username, STATUS_IN_PROGRESS, lead_id),
    )
    conn.commit()
    conn.close()


def unclaim_lead(lead_id: int) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE leads
        SET claimed_by_id = NULL,
            claimed_by_username = NULL,
            status = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE lead_id = ?
        """,
        (STATUS_NEW, lead_id),
    )
    conn.commit()
    conn.close()


def get_ad_summary(ad_id: Optional[int]) -> Dict[str, Any]:
    """Fetch a short ad info from ads table, if exists."""
    if not ad_id:
        return {}
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT ad_id, title, price, area, rooms, district, street, details_url, first_seen
            FROM ads
            WHERE ad_id = ?
            """,
            (ad_id,),
        )
        row = cur.fetchone()
        conn.close()
        return dict(row) if row else {}
    except Exception:
        conn.close()
        return {}


# ----------------- Message formatting -----------------
def lead_text(lead: sqlite3.Row) -> str:
    lead_id = lead["lead_id"]
    ad_id = lead["ad_id"]
    created_at = lead["created_at"]
    status = lead.get("status") if isinstance(lead, dict) else lead["status"]
    status_label = STATUS_LABELS.get(status, status)

    tg_username = lead["telegram_username"] or ""
    tg_user_id = lead["telegram_user_id"] or ""
    full_name = lead["full_name"] or ""

    contact_method = lead["contact_method"] or "unknown"
    phone = lead["phone"] or ""

    claimed = ""
    if lead["claimed_by_id"]:
        who = lead["claimed_by_username"] or str(lead["claimed_by_id"])
        claimed = f"\n👤 Взял: @{who}"

    ad = get_ad_summary(ad_id)
    ad_block = ""
    if ad:
        rooms = ad.get("rooms", "N/A")
        district = ad.get("district", "N/A")
        street = ad.get("street", "")
        title = ad.get("title", "N/A")
        price = ad.get("price", "N/A")
        area = ad.get("area", "N/A")
        details_url = ad.get("details_url", "")

        ad_block = (
            f"\n\n🏠 Объект: {title}"
            f"\n💰 Цена: {price}"
            f"\n📐 Площадь: {area} м²"
            f"\n🛏️ Комнат: {rooms}"
            f"\n📍 Район: {district}"
        )
        if street and street != "N/A":
            ad_block += f"\n🗺️ Улица: {street}"
        if details_url:
            ad_block += f"\n🔗 {details_url}"

    lead_block = (
        f"📩 Новый лид #{lead_id}\n"
        f"🕒 {created_at}\n"
        f"📌 Статус: {status_label}{claimed}\n\n"
        f"👤 Клиент: {full_name}".strip()
    )

    if tg_username:
        lead_block += f"\n💬 Telegram: @{tg_username}"
    if tg_user_id:
        lead_block += f"\n🆔 TG ID: {tg_user_id}"
    lead_block += f"\n☎️ Контакт: {contact_method}"
    if phone:
        lead_block += f"\n📞 Телефон: {phone}"

    return lead_block + ad_block


def lead_keyboard(lead: sqlite3.Row) -> InlineKeyboardMarkup:
    lead_id = int(lead["lead_id"])
    status = lead["status"] or STATUS_NEW
    claimed_by_id = lead["claimed_by_id"]

    buttons = []

    # Claim / Unclaim
    if not claimed_by_id:
        buttons.append([InlineKeyboardButton("🧲 Забрать", callback_data=f"claim:{lead_id}")])
    else:
        buttons.append([InlineKeyboardButton("↩️ Освободить", callback_data=f"unclaim:{lead_id}")])

    # Status buttons (only if claimed OR allow everyone? обычно только если забрал)
    # Тут сделаем: менять статусы может только тот, кто забрал.
    buttons.append([
        InlineKeyboardButton("✅ Связался", callback_data=f"status:{lead_id}:{STATUS_CONTACTED}"),
        InlineKeyboardButton("📅 Просмотр", callback_data=f"status:{lead_id}:{STATUS_VIEWING_SET}"),
    ])
    buttons.append([
        InlineKeyboardButton("🚫 Неактуально", callback_data=f"status:{lead_id}:{STATUS_NOT_ACTUAL}"),
        InlineKeyboardButton("🙅 Отказ", callback_data=f"status:{lead_id}:{STATUS_CLIENT_REFUSED}"),
    ])
    buttons.append([
        InlineKeyboardButton("🏁 Закрыто", callback_data=f"status:{lead_id}:{STATUS_DONE}"),
    ])

    return InlineKeyboardMarkup(buttons)


async def refresh_lead_message(app: Application, lead_id: int) -> None:
    lead = get_lead(lead_id)
    if not lead:
        return

    chat_id = lead["realtor_chat_id"]
    msg_id = lead["realtor_msg_id"]
    if not chat_id or not msg_id:
        return

    try:
        await app.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=lead_text(lead),
            reply_markup=lead_keyboard(lead),
            disable_web_page_preview=True,
        )
    except Exception as e:
        # Editing can fail if message is too old or changed. Fallback: send a new message.
        logger.warning(f"edit_message_text failed for lead {lead_id}: {e}")
        try:
            sent = await app.bot.send_message(
                chat_id=chat_id,
                text=lead_text(lead),
                reply_markup=lead_keyboard(lead),
                disable_web_page_preview=True,
            )
            # overwrite stored msg_id
            conn = get_conn()
            cur = conn.cursor()
            cur.execute(
                "UPDATE leads SET realtor_msg_id = ?, updated_at=CURRENT_TIMESTAMP WHERE lead_id=?",
                (sent.message_id, lead_id),
            )
            conn.commit()
            conn.close()
        except Exception as e2:
            logger.error(f"fallback send_message failed for lead {lead_id}: {e2}")


# ----------------- Poll job -----------------
async def poll_new_leads(context: ContextTypes.DEFAULT_TYPE) -> None:
    if REALTOR_CHAT_ID == 0:
        return

    leads = fetch_new_unnotified_leads(limit=20)
    if not leads:
        return

    for lead in leads:
        try:
            text = lead_text(lead)
            markup = lead_keyboard(lead)
            sent = await context.bot.send_message(
                chat_id=REALTOR_CHAT_ID,
                text=text,
                reply_markup=markup,
                disable_web_page_preview=True,
            )
            mark_lead_notified(int(lead["lead_id"]), REALTOR_CHAT_ID, sent.message_id)
        except Exception as e:
            logger.error(f"notify lead failed (lead_id={lead['lead_id']}): {e}")


# ----------------- Handlers -----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Realtor Bot работает.\n\n"
        "Он автоматически присылает новые лиды в этот чат.\n"
        "Кнопки:\n"
        "🧲 Забрать — закрепить лида за собой\n"
        "✅/📅/🚫/🙅/🏁 — статусы\n"
        "↩️ Освободить — вернуть лида в NEW\n"
    )


async def on_claim(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    parts = q.data.split(":")
    lead_id = int(parts[1])

    lead = get_lead(lead_id)
    if not lead:
        await q.answer("Лид не найден", show_alert=True)
        return

    if lead["claimed_by_id"]:
        await q.answer("Уже взят другим риелтором", show_alert=True)
        return

    user = q.from_user
    claim_lead(lead_id, user.id, user.username)
    await refresh_lead_message(context.application, lead_id)
    await q.answer("Забрал ✅", show_alert=False)


async def on_unclaim(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    lead_id = int(q.data.split(":")[1])
    lead = get_lead(lead_id)
    if not lead:
        await q.answer("Лид не найден", show_alert=True)
        return

    user = q.from_user
    if lead["claimed_by_id"] and int(lead["claimed_by_id"]) != user.id:
        await q.answer("Ты не можешь освободить чужого лида", show_alert=True)
        return

    unclaim_lead(lead_id)
    await refresh_lead_message(context.application, lead_id)
    await q.answer("Освободил ↩️", show_alert=False)


async def on_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    _, lead_id_str, status = q.data.split(":")
    lead_id = int(lead_id_str)

    lead = get_lead(lead_id)
    if not lead:
        await q.answer("Лид не найден", show_alert=True)
        return

    user = q.from_user
    if not lead["claimed_by_id"]:
        await q.answer("Сначала забери лида 🧲", show_alert=True)
        return

    if int(lead["claimed_by_id"]) != user.id:
        await q.answer("Статус может менять только тот, кто забрал", show_alert=True)
        return

    if status not in STATUS_LABELS:
        await q.answer("Неизвестный статус", show_alert=True)
        return

    update_lead_status(lead_id, status)
    await refresh_lead_message(context.application, lead_id)
    await q.answer("Обновил статус ✅", show_alert=False)


# ----------------- Main -----------------
def main():
    ensure_realtor_schema()

    token = os.getenv("REALTOR_BOT_TOKEN")
    if not token:
        raise RuntimeError("REALTOR_BOT_TOKEN is not set in .env")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start_cmd))

    app.add_handler(CallbackQueryHandler(on_claim, pattern=r"^claim:\d+$"))
    app.add_handler(CallbackQueryHandler(on_unclaim, pattern=r"^unclaim:\d+$"))
    app.add_handler(CallbackQueryHandler(on_status, pattern=r"^status:\d+:(NEW|IN_PROGRESS|CONTACTED|VIEWING_SET|NOT_ACTUAL|CLIENT_REFUSED|DONE)$"))

    # Polling job for new leads
    app.job_queue.run_repeating(poll_new_leads, interval=REALTOR_POLL_INTERVAL, first=5)

    logger.info("Realtor bot started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()