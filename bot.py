import asyncio
import aiohttp
import sqlite3
import re
import os
import json
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiohttp import web

# ========== КОНФИГ ==========
BOT_TOKEN = os.environ.get("BOT_TOKEN")
RENDER_URL = os.environ.get("RENDER_URL", "https://wbcheck.onrender.com")

# База данных
conn = sqlite3.connect("prices.db", check_same_thread=False)
cursor = conn.cursor()

# Таблицы
cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    join_date TEXT
)
""") 

cursor.execute("""
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    url TEXT,
    name TEXT,
    last_price REAL,
    min_price REAL,
    max_price REAL,
    target_price REAL,
    category TEXT,
    in_stock INTEGER DEFAULT 1,
    added_date TEXT,
    FOREIGN KEY (user_id) REFERENCES users (user_id)
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER,
    price REAL,
    in_stock INTEGER,
    check_date TEXT,
    FOREIGN KEY (product_id) REFERENCES products (id)
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    name TEXT,
    UNIQUE(user_id, name)
)
""")
conn.commit()

# Состояния FSM
class AddProductState(StatesGroup):
    waiting_for_url = State()
    waiting_for_category = State()
    waiting_for_target_price = State()
    waiting_for_category_name = State()

class SetTargetPriceState(StatesGroup):
    waiting_for_price = State()

# Инициализация бота
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ========== ФУНКЦИИ ПАРСИНГА ==========
async def parse_wildberries(url_or_article):
    try:
        # Извлекаем артикул
        match = re.search(r'/(\d+)/', url_or_article)
        if not match:
            if url_or_article.isdigit():
                article = url_or_article
            else:
                return None, None, None
        else:
            article = match.group(1)
        
        # Используем официальное API Wildberries
        api_url = f"https://card.wb.ru/cards/v2/detail?appType=1&curr=rub&dest=-1257786&spp=30&nm={article}"
        
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15) as resp:
                if resp.status != 200:
                    print(f"API вернул статус {resp.status}")
                    return None, None, None
                
                data = await resp.json()
                
                # Проверяем структуру ответа
                products = data.get("data", {}).get("products", [])
                if not products:
                    print(f"Товар {article} не найден")
                    return None, None, None
                
                product = products[0]
                
                # Цена в копейках, делим на 100
                price = product.get("salePriceU", 0) / 100
                if price == 0:
                    price = product.get("priceU", 0) / 100
                
                name = product.get("name", "Неизвестный товар")
                
                # Проверка наличия (totalQuantity может отсутствовать)
                in_stock = product.get("totalQuantity", 0) > 0
                
                return price, name, in_stock
                
    except Exception as e:
        print(f"Ошибка парсинга WB: {e}")
        return None, None, None

async def parse_ozon(url_or_article):
    try:
        # Извлекаем артикул
        match = re.search(r'/product/(\d+)', url_or_article)
        if not match:
            if url_or_article.isdigit():
                article = url_or_article
            else:
                return None, None, None
        else:
            article = match.group(1)
        
        ozon_url = f"https://www.ozon.ru/product/{article}/"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        
        async with aiohttp.ClientSession() as session:
            async with session.get(ozon_url, headers=headers, timeout=15) as resp:
                html = await resp.text()
                
                # Ищем цену в JSON-подобных данных
                price = None
                name = None
                
                # Пробуем найти в script тегах
                match = re.search(r'"price":"([\d\s]+)"', html)
                if match:
                    price = float(match.group(1).replace(' ', ''))
                
                if not price:
                    match = re.search(r'"current_price":([\d.]+)', html)
                    if match:
                        price = float(match.group(1))
                
                # Ищем название
                match_name = re.search(r'"name":"([^"]+)"', html)
                if match_name:
                    name = match_name.group(1)
                
                if not name:
                    name = "Неизвестный товар"
                
                in_stock = "Нет в наличии" not in html and "Товар закончился" not in html
                
                return price, name, in_stock
                
    except Exception as e:
        print(f"Ошибка парсинга Ozon: {e}")
        return None, None, None

async def parse_price(url_or_article):
    """Парсит цену по артикулу или ссылке"""
    # Если артикул (только цифры)
    if url_or_article.isdigit():
        # Пробуем Wildberries
        price, name, in_stock = await parse_wildberries(url_or_article)
        if price:
            return price, name, in_stock
        # Пробуем Ozon
        price, name, in_stock = await parse_ozon(url_or_article)
        return price, name, in_stock
    
    # Если ссылка
    if "wildberries" in url_or_article.lower():
        return await parse_wildberries(url_or_article)
    elif "ozon" in url_or_article.lower():
        return await parse_ozon(url_or_article)
    else:
        return None, None, None

# ========== ФУНКЦИИ БАЗЫ ДАННЫХ ==========
def register_user(user_id):
    cursor.execute("INSERT OR IGNORE INTO users (user_id, join_date) VALUES (?, ?)", 
                   (user_id, datetime.now().isoformat()))
    conn.commit()

def get_user_categories(user_id):
    cursor.execute("SELECT name FROM categories WHERE user_id = ?", (user_id,))
    return [row[0] for row in cursor.fetchall()]

def add_category(user_id, name):
    try:
        cursor.execute("INSERT INTO categories (user_id, name) VALUES (?, ?)", (user_id, name))
        conn.commit()
        return True
    except:
        return False

def get_user_products(user_id):
    cursor.execute("SELECT id, name, last_price, url, category, in_stock FROM products WHERE user_id = ?", (user_id,))
    return cursor.fetchall()

def get_product_history(product_id):
    cursor.execute("SELECT price, check_date FROM price_history WHERE product_id = ? ORDER BY check_date DESC LIMIT 20", (product_id,))
    return cursor.fetchall()

# ========== КЛАВИАТУРЫ ==========
def main_menu():
    buttons = [
        [InlineKeyboardButton(text="➕ Добавить товар", callback_data="add_product")],
        [InlineKeyboardButton(text="📋 Мои товары", callback_data="my_products")],
        [InlineKeyboardButton(text="🛒 Корзины", callback_data="categories")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton(text="🔄 Проверить цены", callback_data="check_prices")],
        [InlineKeyboardButton(text="❓ Помощь", callback_data="help")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def products_keyboard(products):
    buttons = []
    for prod in products:
        stock_icon = "✅" if prod[5] else "❌"
        buttons.append([InlineKeyboardButton(text=f"{stock_icon} {prod[1][:30]} - {prod[2]}₽", callback_data=f"prod_{prod[0]}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def product_detail_keyboard(product_id):
    buttons = [
        [InlineKeyboardButton(text="🎯 Целевая цена", callback_data=f"target_{product_id}")],
        [InlineKeyboardButton(text="📈 История цен", callback_data=f"history_{product_id}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete_{product_id}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="my_products")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ========== ОБРАБОТЧИКИ ==========
@dp.message(Command("start"))
async def start_command(message: Message):
    register_user(message.from_user.id)
    await message.answer(
        "🤖 *Продвинутый парсер цен*\n\n"
        "Я отслеживаю цены на Wildberries и Ozon!\n\n"
        "📌 *Что умею:*\n"
        "• 📉 Снижение цены\n"
        "• 📈 Повышение цены\n"
        "• ⚠️ Пропажа товара\n"
        "• 🛒 Корзины для группировки\n"
        "• 🎯 Целевая цена\n"
        "• 📊 График цен\n\n"
        "📌 *Как добавить товар:*\n"
        "• По артикулу: просто введи цифры (12345678)\n"
        "• По ссылке: отправь ссылку с WB или Ozon\n\n"
        "👇 Выбери действие:",
        reply_markup=main_menu(),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "add_product")
async def add_product_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddProductState.waiting_for_url)
    await callback.message.edit_text(
        "🔗 Отправь *артикул* или *ссылку* на товар:\n\n"
        "✅ *Примеры:*\n"
        "• Артикул WB: `12345678`\n"
        "• Артикул Ozon: `123456789`\n"
        "• Ссылка WB: https://www.wildberries.ru/catalog/12345678/detail.aspx\n"
        "• Ссылка Ozon: https://www.ozon.ru/product/123456789/\n\n"
        "❌ Нажми «Отмена» чтобы вернуться",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data="back")]]),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.message(AddProductState.waiting_for_url)
async def process_url(message: Message, state: FSMContext):
    url_or_article = message.text.strip()
    await message.answer("🔄 Парсю товар...")
    
    price, name, in_stock = await parse_price(url_or_article)
    
    if price is None:
        await message.answer("❌ Не удалось распознать артикул или ссылку.\n\n"
                           "✅ *Правильные форматы:*\n"
                           "• Артикул WB: `12345678`\n"
                           "• Артикул Ozon: `123456789`\n"
                           "• Ссылка WB: https://www.wildberries.ru/catalog/12345678/detail.aspx\n"
                           "• Ссылка Ozon: https://www.ozon.ru/product/123456789/",
                           parse_mode="Markdown")
        return
    
    await state.update_data(url=url_or_article, name=name, price=price, in_stock=in_stock)
    
    categories = get_user_categories(message.from_user.id)
    buttons = []
    for cat in categories:
        buttons.append([InlineKeyboardButton(text=cat, callback_data=f"cat_{cat}")])
    buttons.append([InlineKeyboardButton(text="➕ Новая корзина", callback_data="new_cat")])
    buttons.append([InlineKeyboardButton(text="📁 Без корзины", callback_data="cat_None")])
    
    await state.set_state(AddProductState.waiting_for_category)
    await message.answer(
        f"📦 *{name}*\n💰 Цена: {price} ₽\n{'✅ В наличии' if in_stock else '❌ Нет в наличии'}\n\n"
        f"Выбери корзину:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown"
    )

@dp.callback_query(AddProductState.waiting_for_category, F.data.startswith("cat_"))
async def process_category(callback: CallbackQuery, state: FSMContext):
    category = callback.data.replace("cat_", "")
    if category == "None":
        category = None
    elif category == "new_cat":
        await callback.message.edit_text("Введи название новой корзины:")
        await state.set_state(AddProductState.waiting_for_category_name)
        await callback.answer()
        return
    
    await state.update_data(category=category)
    await state.set_state(AddProductState.waiting_for_target_price)
    await callback.message.edit_text(
        "🎯 Установи целевую цену (при достижении пришлю уведомление)\n\n"
        "Введи число или нажми «Пропустить»:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⏭ Пропустить", callback_data="skip_target")]])
    )
    await callback.answer()

@dp.callback_query(AddProductState.waiting_for_target_price, F.data == "skip_target")
async def skip_target(callback: CallbackQuery, state: FSMContext):
    await state.update_data(target_price=None)
    
    data = await state.get_data()
    cursor.execute("""
        INSERT INTO products (user_id, url, name, last_price, min_price, max_price, target_price, category, in_stock, added_date)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (callback.from_user.id, data['url'], data['name'], data['price'], data['price'], data['price'], 
          data['target_price'], data['category'], 1 if data['in_stock'] else 0, datetime.now().isoformat()))
    product_id = cursor.lastrowid
    
    cursor.execute("INSERT INTO price_history (product_id, price, in_stock, check_date) VALUES (?, ?, ?, ?)",
                   (product_id, data['price'], 1 if data['in_stock'] else 0, datetime.now().isoformat()))
    conn.commit()
    
    await callback.message.edit_text(
        f"✅ *Товар добавлен!*\n\n"
        f"📦 {data['name']}\n"
        f"💰 {data['price']} ₽\n"
        f"📁 Корзина: {data['category'] or 'Без корзины'}\n\n"
        f"🔔 Буду следить за ценой каждые 30 минут!",
        parse_mode="Markdown",
        reply_markup=main_menu()
    )
    await state.clear()
    await callback.answer()

@dp.message(AddProductState.waiting_for_target_price)
async def process_target_price(message: Message, state: FSMContext):
    try:
        target_price = float(message.text.replace(",", "."))
    except:
        await message.answer("❌ Введи число или нажми «Пропустить»")
        return
    
    await state.update_data(target_price=target_price)
    
    data = await state.get_data()
    cursor.execute("""
        INSERT INTO products (user_id, url, name, last_price, min_price, max_price, target_price, category, in_stock, added_date)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (message.from_user.id, data['url'], data['name'], data['price'], data['price'], data['price'], 
          data['target_price'], data['category'], 1 if data['in_stock'] else 0, datetime.now().isoformat()))
    product_id = cursor.lastrowid
    
    cursor.execute("INSERT INTO price_history (product_id, price, in_stock, check_date) VALUES (?, ?, ?, ?)",
                   (product_id, data['price'], 1 if data['in_stock'] else 0, datetime.now().isoformat()))
    conn.commit()
    
    await message.answer(
        f"✅ *Товар добавлен!*\n\n"
        f"📦 {data['name']}\n"
        f"💰 {data['price']} ₽\n"
        f"🎯 Цель: {target_price} ₽\n"
        f"📁 Корзина: {data['category'] or 'Без корзины'}\n\n"
        f"🔔 Буду следить за ценой каждые 30 минут!",
        parse_mode="Markdown",
        reply_markup=main_menu()
    )
    await state.clear()

@dp.message(AddProductState.waiting_for_category_name)
async def new_category_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if add_category(message.from_user.id, name):
        await message.answer(f"✅ Корзина «{name}» создана!\n\nТеперь добавь товар заново.")
    else:
        await message.answer(f"❌ Корзина «{name}» уже существует")
    await state.clear()
    await start_command(message)

@dp.callback_query(F.data == "my_products")
async def show_products(callback: CallbackQuery):
    products = get_user_products(callback.from_user.id)
    if not products:
        await callback.message.edit_text("📭 У тебя пока нет товаров.", reply_markup=main_menu())
        await callback.answer()
        return
    await callback.message.edit_text("📋 *Твои товары:*\n\n✅ — в наличии, ❌ — нет", reply_markup=products_keyboard(products), parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data.startswith("prod_"))
async def show_product_detail(callback: CallbackQuery):
    product_id = int(callback.data.split("_")[1])
    cursor.execute("SELECT id, name, last_price, url, target_price, category, in_stock FROM products WHERE id = ?", (product_id,))
    product = cursor.fetchone()
    if not product:
        await callback.answer("Товар не найден", show_alert=True)
        return
    stock_text = "✅ В наличии" if product[6] else "❌ Нет в наличии"
    target_text = f"🎯 Цель: {product[4]} ₽" if product[4] else "🎯 Цель не установлена"
    text = f"📦 *{product[1]}*\n\n💰 Цена: {product[2]} ₽\n{stock_text}\n{target_text}\n📁 Корзина: {product[5] or 'Без корзины'}\n🔗 [Ссылка]({product[3]})"
    await callback.message.edit_text(text, reply_markup=product_detail_keyboard(product_id), parse_mode="Markdown", disable_web_page_preview=True)
    await callback.answer()

@dp.callback_query(F.data.startswith("history_"))
async def show_price_history(callback: CallbackQuery):
    product_id = int(callback.data.split("_")[1])
    history = get_product_history(product_id)
    if not history:
        await callback.answer("Нет истории цен", show_alert=True)
        return
    text = "📈 *История цен:*\n\n"
    for i, (price, date) in enumerate(history[:10]):
        date_formatted = date.split("T")[1][:5] if "T" in date else date[:16]
        text += f"{i+1}. {price} ₽ — {date_formatted}\n"
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data=f"prod_{product_id}")]]))
    await callback.answer()

@dp.callback_query(F.data.startswith("target_"))
async def set_target_price_start(callback: CallbackQuery, state: FSMContext):
    product_id = int(callback.data.split("_")[1])
    await state.update_data(product_id=product_id)
    await state.set_state(SetTargetPriceState.waiting_for_price)
    await callback.message.edit_text("🎯 Введи целевую цену:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data=f"prod_{product_id}")]]))
    await callback.answer()

@dp.message(SetTargetPriceState.waiting_for_price)
async def set_target_price(message: Message, state: FSMContext):
    try:
        target_price = float(message.text.replace(",", "."))
    except:
        await message.answer("❌ Введи число")
        return
    data = await state.get_data()
    cursor.execute("UPDATE products SET target_price = ? WHERE id = ?", (target_price, data['product_id']))
    conn.commit()
    await message.answer(f"✅ Целевая цена установлена: {target_price} ₽")
    await state.clear()

@dp.callback_query(F.data.startswith("delete_"))
async def delete_product(callback: CallbackQuery):
    product_id = int(callback.data.split("_")[1])
    cursor.execute("DELETE FROM products WHERE id = ?", (product_id,))
    cursor.execute("DELETE FROM price_history WHERE product_id = ?", (product_id,))
    conn.commit()
    await callback.answer("🗑 Товар удалён!", show_alert=True)
    await show_products(callback)

@dp.callback_query(F.data == "categories")
async def show_categories(callback: CallbackQuery):
    categories = get_user_categories(callback.from_user.id)
    buttons = [[InlineKeyboardButton(text=f"🛒 {cat}", callback_data=f"category_{cat}")] for cat in categories]
    buttons.append([InlineKeyboardButton(text="➕ Новая корзина", callback_data="new_category")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back")])
    await callback.message.edit_text("🛒 *Твои корзины:*", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "new_category")
async def new_category_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddProductState.waiting_for_category_name)
    await callback.message.edit_text("Введи название новой корзины:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data="categories")]]))
    await callback.answer()

@dp.callback_query(F.data.startswith("category_"))
async def show_category_products(callback: CallbackQuery):
    category = callback.data.replace("category_", "")
    cursor.execute("SELECT id, name, last_price, in_stock FROM products WHERE user_id = ? AND category = ?", (callback.from_user.id, category))
    products = cursor.fetchall()
    if not products:
        await callback.answer("В этой корзине нет товаров", show_alert=True)
        return
    text = f"🛒 *Корзина: {category}*\n\n"
    for prod in products:
        stock_icon = "✅" if prod[3] else "❌"
        text += f"{stock_icon} {prod[1][:40]}\n💰 {prod[2]} ₽\n\n"
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="categories")]]))
    await callback.answer()

@dp.callback_query(F.data == "check_prices")
async def check_prices_manual(callback: CallbackQuery):
    await callback.message.edit_text("🔄 Проверяю цены...")
    await check_all_prices(callback.from_user.id, callback.message)
    await callback.answer()

@dp.callback_query(F.data == "stats")
async def show_stats(callback: CallbackQuery):
    cursor.execute("SELECT COUNT(*) FROM products WHERE user_id = ?", (callback.from_user.id,))
    total_products = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM products WHERE user_id = ? AND in_stock = 1", (callback.from_user.id,))
    in_stock = cursor.fetchone()[0]
    cursor.execute("SELECT AVG(last_price) FROM products WHERE user_id = ?", (callback.from_user.id,))
    avg_price = cursor.fetchone()[0] or 0
    text = f"📊 *Твоя статистика*\n\n📦 Всего товаров: {total_products}\n✅ В наличии: {in_stock}\n❌ Нет: {total_products - in_stock}\n💰 Средняя цена: {avg_price:.0f} ₽"
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=main_menu())
    await callback.answer()

@dp.callback_query(F.data == "back")
async def back_to_main(callback: CallbackQuery):
    await callback.message.edit_text("🤖 Главное меню:", reply_markup=main_menu())
    await callback.answer()

@dp.callback_query(F.data == "help")
async def show_help(callback: CallbackQuery):
    text = (
        "ℹ️ *Помощь*\n\n"
        "📌 *Как добавить товар:*\n"
        "1. Нажми «➕ Добавить товар»\n"
        "2. Отправь *артикул* (просто цифры) или *ссылку*\n"
        "3. Выбери корзину\n"
        "4. Установи целевую цену (опционально)\n\n"
        "📌 *Примеры:*\n"
        "• Артикул WB: `12345678`\n"
        "• Артикул Ozon: `123456789`\n\n"
        "📌 *Уведомления:*\n"
        "• 📉 При снижении цены\n"
        "• 📈 При повышении цены\n"
        "• ⚠️ Когда товар пропадает\n"
        "• 🎯 При достижении цели\n\n"
        "🔔 Проверка цен каждые 30 минут!"
    )
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=main_menu())
    await callback.answer()

# ========== ФОНОВЫЕ ЗАДАЧИ ==========
async def check_all_prices(user_id, message=None):
    cursor.execute("SELECT id, url, last_price, target_price, name FROM products WHERE user_id = ?", (user_id,))
    products = cursor.fetchall()
    changes = []
    for prod in products:
        price, name, in_stock = await parse_price(prod[1])
        if price is None:
            continue
        old_price = prod[2]
        target = prod[3]
        cursor.execute("INSERT INTO price_history (product_id, price, in_stock, check_date) VALUES (?, ?, ?, ?)",
                       (prod[0], price, 1 if in_stock else 0, datetime.now().isoformat()))
        cursor.execute("UPDATE products SET last_price = ?, min_price = MIN(min_price, ?), max_price = MAX(max_price, ?), in_stock = ? WHERE id = ?",
                       (price, price, price, 1 if in_stock else 0, prod[0]))
        if in_stock == False and prod[2] > 0:
            changes.append(("stock", prod[4], None, None))
        elif price < old_price:
            changes.append(("down", prod[4], old_price, price))
        elif price > old_price:
            changes.append(("up", prod[4], old_price, price))
        if target and price <= target:
            changes.append(("target", prod[4], target, price))
        conn.commit()
    if changes and message:
        for change in changes:
            if change[0] == "down":
                await bot.send_message(user_id, f"📉 *Цена снизилась!*\n\n📦 {change[1]}\n💰 {change[2]} ₽ → {change[3]} ₽", parse_mode="Markdown")
            elif change[0] == "up":
                await bot.send_message(user_id, f"📈 *Цена повысилась!*\n\n📦 {change[1]}\n💰 {change[2]} ₽ → {change[3]} ₽", parse_mode="Markdown")
            elif change[0] == "stock":
                await bot.send_message(user_id, f"⚠️ *Товар пропал из наличия!*\n\n📦 {change[1]}", parse_mode="Markdown")
            elif change[0] == "target":
                await bot.send_message(user_id, f"🎯 *Достигнута целевая цена!*\n\n📦 {change[1]}\n💰 Цена: {change[3]} ₽\n🎯 Цель: {change[2]} ₽", parse_mode="Markdown")
        if message:
            await message.edit_text("✅ Цены проверены!", reply_markup=main_menu())
    elif message:
        await message.edit_text("✅ Цены не изменились.", reply_markup=main_menu())

async def scheduled_check():
    while True:
        await asyncio.sleep(1800)  # 30 минут
        cursor.execute("SELECT DISTINCT user_id FROM products")
        users = cursor.fetchall()
        for user in users:
            await check_all_prices(user[0])

# ========== ВЕБ-СЕРВЕР И САМОПИНГ ==========
async def health_check(request):
    return web.Response(text="✅ Бот работает")

async def self_ping():
    while True:
        await asyncio.sleep(600)  # 10 минут
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(RENDER_URL, timeout=10) as resp:
                    print(f"[SELF-PING] {resp.status} - {datetime.now().strftime('%H:%M:%S')}")
        except Exception as e:
            print(f"[SELF-PING] Ошибка: {e}")

async def start_web():
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    port = int(os.environ.get('PORT', 10000))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"✅ Веб-сервер на порту {port}")

# ========== ЗАПУСК ==========
async def main():
    print("✅ Бот-парсер цен запущен!")
    print(f"📍 Адрес: {RENDER_URL}")
    await start_web()
    asyncio.create_task(self_ping())
    asyncio.create_task(scheduled_check())
    print("🔄 Самопинг (каждые 10 минут) и проверка цен (каждые 30 минут) запущены")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
