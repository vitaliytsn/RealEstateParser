import sqlite3
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
)

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Database
DB_NAME = "otodom_ads.db"

# Conversation states
SELECTING_DISTRICTS, SELECTING_BUDGET, SELECTING_ROOMS = range(3)

# Warsaw districts
DISTRICTS = {
    "Śródmieście": "Средместье / Śródmieście",
    "Mokotów": "Мокотув / Mokotów",
    "Wola": "Воля / Wola",
    "Wilanów": "Вилянув / Wilanów",
    "Żoliborz": "Жолибож / Żoliborz",
    "Ochota": "Охота / Ochota",
    "Bemowo": "Бемово / Bemowo",
    "Bielany": "Беляны / Bielany",
    "Ursynów": "Урсынов / Ursynów",
    "Praga-Południe": "Прага-Полудне / Praga-Południe",
    "Praga-Północ": "Прага-Пулноц / Praga-Północ",
    "Targówek": "Таргувек / Targówek",
    "Białołęka": "Бялоленка / Białołęka",
    "Wawer": "Вавер / Wawer",
    "Wesoła": "Весола / Wesoła",
    "Rembertów": "Рембертув / Rembertów",
    "Ursus": "Урсус / Ursus",
    "Włochy": "Влохи / Włochy",
}

# Budget ranges
BUDGETS = {
    "0-2000": "До 2000 PLN",
    "2000-3000": "2000-3000 PLN",
    "3000-4000": "3000-4000 PLN",
    "4000-5000": "4000-5000 PLN",
    "5000+": "Более 5000 PLN",
}

# Rooms options
ROOMS_OPTIONS = {
    "1": "1 комната",
    "2": "2 комнаты",
    "3": "3 комнаты",
    "4": "4 комнаты",
    "5+": "5+ комнат",
}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the conversation and ask for districts."""
    context.user_data.clear()
    context.user_data['selected_districts'] = []
    
    keyboard = []
    for district, description in DISTRICTS.items():
        keyboard.append([InlineKeyboardButton(description, callback_data=f"district_{district}")])
    
    keyboard.append([InlineKeyboardButton("✅ Готово, продолжить", callback_data="districts_done")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "🏠 Добро пожаловать в поиск квартир в Варшаве!\n\n"
        "Выберите интересующие вас районы (можно несколько):",
        reply_markup=reply_markup
    )
    
    return SELECTING_DISTRICTS


async def district_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle district selection."""
    query = update.callback_query
    await query.answer()
    
    if query.data == "districts_done":
        if not context.user_data.get('selected_districts'):
            await query.edit_message_text("❌ Выберите хотя бы один район!")
            return SELECTING_DISTRICTS
        
        # Move to budget selection
        keyboard = []
        for budget_key, budget_label in BUDGETS.items():
            keyboard.append([InlineKeyboardButton(budget_label, callback_data=f"budget_{budget_key}")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        selected = ", ".join(context.user_data['selected_districts'])
        await query.edit_message_text(
            f"✅ Выбранные районы: {selected}\n\n"
            "💰 Теперь выберите бюджет:",
            reply_markup=reply_markup
        )
        
        return SELECTING_BUDGET
    
    # Toggle district selection
    district = query.data.replace("district_", "")
    selected_districts = context.user_data.get('selected_districts', [])
    
    if district in selected_districts:
        selected_districts.remove(district)
        status = "❌"
    else:
        selected_districts.append(district)
        status = "✅"
    
    context.user_data['selected_districts'] = selected_districts
    
    # Update keyboard with checkmarks
    keyboard = []
    for dist, description in DISTRICTS.items():
        prefix = "✅ " if dist in selected_districts else ""
        keyboard.append([InlineKeyboardButton(f"{prefix}{description}", callback_data=f"district_{dist}")])
    
    keyboard.append([InlineKeyboardButton("✅ Готово, продолжить", callback_data="districts_done")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_reply_markup(reply_markup=reply_markup)
    
    return SELECTING_DISTRICTS


async def budget_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle budget selection."""
    query = update.callback_query
    await query.answer()
    
    budget = query.data.replace("budget_", "")
    context.user_data['budget'] = budget
    
    # Move to rooms selection
    keyboard = []
    for rooms_key, rooms_label in ROOMS_OPTIONS.items():
        keyboard.append([InlineKeyboardButton(rooms_label, callback_data=f"rooms_{rooms_key}")])
    
    keyboard.append([InlineKeyboardButton("Любое количество комнат", callback_data="rooms_any")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"✅ Бюджет: {BUDGETS[budget]}\n\n"
        "🛏️ Выберите количество комнат:",
        reply_markup=reply_markup
    )
    
    return SELECTING_ROOMS


async def rooms_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle rooms selection and show results."""
    query = update.callback_query
    await query.answer()
    
    if query.data == "rooms_any":
        context.user_data['rooms'] = None
    else:
        rooms = query.data.replace("rooms_", "")
        context.user_data['rooms'] = rooms
    
    # Search apartments
    await query.edit_message_text("🔍 Ищу подходящие квартиры...")
    
    apartments = search_apartments(
        context.user_data['selected_districts'],
        context.user_data['budget'],
        context.user_data['rooms']
    )
    
    if not apartments:
        await query.message.reply_text(
            "😔 К сожалению, не найдено квартир по вашим параметрам.\n\n"
            "Попробуйте изменить критерии поиска.\n\n"
            "Используйте /start для нового поиска."
        )
    else:
        await query.message.reply_text(
            f"✅ Найдено квартир: {len(apartments)}\n\n"
            "Отправляю результаты..."
        )
        
        for apt in apartments[:20]:  # Limit to 20 results
            message = format_apartment(apt)
            
            keyboard = [[InlineKeyboardButton("🔗 Открыть объявление", url=apt['details_url'])]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            # Send all available photos in one media group
            photos = apt.get('photos', [])
            if photos and len(photos) > 1:
                # Multiple photos - send as media group
                try:
                    media_group = []
                    for i, photo_url in enumerate(photos[:10]):  # Telegram limit is 10 photos per media group
                        if i == 0:
                            media_group.append(InputMediaPhoto(media=photo_url, caption=message))
                        else:
                            media_group.append(InputMediaPhoto(media=photo_url))
                    
                    await query.message.reply_media_group(media_group)
                    # Send link button separately (without duplicate text)
                    await query.message.reply_text("🔗 Ссылка на объявление:", reply_markup=reply_markup)
                except Exception as e:
                    logger.error(f"Error sending media group: {e}")
                    # Fallback to text message
                    await query.message.reply_text(message, reply_markup=reply_markup)
            elif photos and len(photos) == 1:
                # Single photo
                try:
                    await query.message.reply_photo(
                        photo=photos[0],
                        caption=message,
                        reply_markup=reply_markup
                    )
                except:
                    await query.message.reply_text(message, reply_markup=reply_markup)
            else:
                # No photos
                await query.message.reply_text(message, reply_markup=reply_markup)
        
        if len(apartments) > 20:
            await query.message.reply_text(
                f"ℹ️ Показаны первые 20 из {len(apartments)} квартир.\n\n"
                "Используйте /start для нового поиска."
            )
        else:
            await query.message.reply_text(
                "Используйте /start для нового поиска."
            )
    
    return ConversationHandler.END


def search_apartments(districts, budget, rooms):
    """Search apartments in database based on criteria."""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Build query
    query = "SELECT * FROM ads WHERE 1=1"
    params = []
    
    # Filter by districts
    if districts:
        placeholders = ','.join(['?' for _ in districts])
        query += f" AND district IN ({placeholders})"
        params.extend(districts)
    
    # Filter by budget
    if budget != "5000+":
        min_price, max_price = map(int, budget.split('-'))
        query += " AND CAST(REPLACE(REPLACE(price, ' PLN', ''), ' ', '') AS INTEGER) BETWEEN ? AND ?"
        params.extend([min_price, max_price])
    else:
        query += " AND CAST(REPLACE(REPLACE(price, ' PLN', ''), ' ', '') AS INTEGER) >= ?"
        params.append(5000)
    
    # Filter by rooms
    if rooms:
        # Map numbers to text format as stored in database
        num_to_text = {
            '1': 'ONE', '2': 'TWO', '3': 'THREE', '4': 'FOUR',
            '5': 'FIVE', '6': 'SIX', '7': 'SEVEN', '8': 'EIGHT',
            '9': 'NINE', '10': 'TEN'
        }
        
        if rooms == "5+":
            # For 5+ rooms, check for FIVE, SIX, SEVEN, etc.
            query += " AND rooms IN ('FIVE', 'SIX', 'SEVEN', 'EIGHT', 'NINE', 'TEN')"
        else:
            # Convert number to text format
            room_text = num_to_text.get(rooms, rooms)
            query += " AND rooms = ?"
            params.append(room_text)
    
    cursor.execute(query, params)
    results = cursor.fetchall()
    conn.close()
    
    # Convert to list of dicts
    apartments = []
    for row in results:
        apt = dict(row)
        # Parse photos JSON
        import json
        try:
            apt['photos'] = json.loads(apt['photos']) if apt['photos'] else []
        except:
            apt['photos'] = []
        apartments.append(apt)
    
    return apartments


def format_apartment(apt):
    """Format apartment data for display."""
    message = f"🏠 {apt['title']}\n\n"
    message += f"💰 Цена: {apt['price']}\n"
    message += f"📐 Площадь: {apt['area']} м²\n"
    
    # Convert room number to numeric format
    rooms_display = apt['rooms']
    
    # Map text numbers to digits
    text_to_num = {
        'ONE': '1', 'TWO': '2', 'THREE': '3', 'FOUR': '4', 
        'FIVE': '5', 'SIX': '6', 'SEVEN': '7', 'EIGHT': '8',
        'NINE': '9', 'TEN': '10'
    }
    
    rooms_str = str(apt['rooms']).strip().upper()
    if rooms_str in text_to_num:
        rooms_display = text_to_num[rooms_str]
    else:
        try:
            # Try to convert to integer if it's already a number
            rooms_num = int(str(apt['rooms']).replace(' ', ''))
            rooms_display = str(rooms_num)
        except:
            # Keep original if conversion fails
            pass
    
    message += f"🛏️ Комнат: {rooms_display}\n"
    message += f"📍 Район: {apt['district']}\n"
    
    if apt['street'] and apt['street'] != 'N/A':
        message += f"🗺️ Улица: {apt['street']}\n"
    
    message += f"\n🕐 Добавлено: {apt['first_seen']}"
    
    return message


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the conversation."""
    await update.message.reply_text(
        "❌ Поиск отменен. Используйте /start для нового поиска."
    )
    return ConversationHandler.END


def main():
    """Start the bot."""
    # Replace with your bot token
    TOKEN = "8598905735:AAFGmnrIfjDHp4DG2lFSXRkKYskQg0NIEHY"
    
    application = Application.builder().token(TOKEN).build()
    
    # Conversation handler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SELECTING_DISTRICTS: [CallbackQueryHandler(district_selection)],
            SELECTING_BUDGET: [CallbackQueryHandler(budget_selection)],
            SELECTING_ROOMS: [CallbackQueryHandler(rooms_selection)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    
    application.add_handler(conv_handler)
    
    # Start the bot
    logger.info("Bot started!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
