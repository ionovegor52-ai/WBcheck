import asyncio
import aiohttp
import sqlite3
import os
import re
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiohttp import web

# ========== КОНФИГ ==========
BOT_TOKEN = os.environ.get("BOT_TOKEN")
RENDER_URL = os.environ.get("RENDER_URL", "https://crm-bot.onrender.com")

# Данные продавца (единственный продавец)
SELLER_LOGIN = "admin"
SELLER_PASSWORD = "12345"

# База данных
conn = sqlite3.connect("crm.db", check_same_thread=False)
cursor = conn.cursor()

# Таблицы
cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    role TEXT DEFAULT 'client',
    blocked INTEGER DEFAULT 0,
    name TEXT,
    phone TEXT,
    joined_date TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    service TEXT,
    comment TEXT,
    contact TEXT,
    status TEXT DEFAULT 'pending',
    created_at TEXT,
    seller_response TEXT,
    seller_responded_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users (user_id)
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS services (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE,
    price TEXT,
    description TEXT
)
""")
conn.commit()

# Добавляем тестовые услуги
cursor.execute("SELECT COUNT(*) FROM services")
if cursor.fetchone()[0] == 0:
    services = [
        ("Разработка сайта", "от 10000 ₽", "Лендинг, интернет-магазин, корпоративный сайт"),
        ("SEO-продвижение", "от 5000 ₽/мес", "Вывод в топ 10 по целевым запросам"),
        ("Telegram-бот", "от 3000 ₽", "Бот для бизнеса под ключ"),
        ("Дизайн", "от 2000 ₽", "Логотип, фирменный стиль, баннеры"),
        ("Консультация", "1000 ₽/час", "Помощь с IT-проектами"),
    ]
    for name, price, desc in services:
        cursor.execute("INSERT INTO services (name, price, description) VALUES (?, ?, ?)", (name, price, desc))
    conn.commit()

# Состояния
class ClientOrderState(StatesGroup):
    waiting_for_service = State()
    waiting_for_comment = State()
    waiting_for_contact = State()

class SellerReplyState(StatesGroup):
    waiting_for_reply = State()

class SellerLoginState(StatesGroup):
    waiting_for_login = State()
    waiting_for_password = State()

# Инициализация
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ========== ФУНКЦИИ ==========
def register_user(user_id, name=None, phone=None):
    cursor.execute("INSERT OR IGNORE INTO users (user_id, role, blocked, joined_date) VALUES (?, 'client', 0, ?)", 
                   (user_id, datetime.now().isoformat()))
    if name or phone:
        cursor.execute("UPDATE users SET name = ?, phone = ? WHERE user_id = ?", (name, phone, user_id))
    conn.commit()

def is_blocked(user_id):
    cursor.execute("SELECT blocked FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    return result and result[0] == 1

def is_seller(user_id):
    cursor.execute("SELECT role FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    return result and result[0] == 'seller'

def add_order(user_id, service, comment, contact):
    cursor.execute("""
        INSERT INTO orders (user_id, service, comment, contact, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (user_id, service, comment, contact, datetime.now().isoformat()))
    conn.commit()
    return cursor.lastrowid

def get_orders(status=None, user_id=None):
    if status and user_id:
        cursor.execute("SELECT id, user_id, service, comment, contact, status, created_at, seller_response FROM orders WHERE status = ? AND user_id = ? ORDER BY created_at DESC", (status, user_id))
    elif status:
        cursor.execute("SELECT id, user_id, service, comment, contact, status, created_at, seller_response FROM orders WHERE status = ? ORDER BY created_at DESC", (status,))
    elif user_id:
        cursor.execute("SELECT id, user_id, service, comment, contact, status, created_at, seller_response FROM orders WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
    else:
        cursor.execute("SELECT id, user_id, service, comment, contact, status, created_at, seller_response FROM orders ORDER BY created_at DESC")
    return cursor.fetchall()

def update_order_status(order_id, status, response=None):
    if response:
        cursor.execute("UPDATE orders SET status = ?, seller_response = ?, seller_responded_at = ? WHERE id = ?", 
                       (status, response, datetime.now().isoformat(), order_id))
    else:
        cursor.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
    conn.commit()

def get_services():
    cursor.execute("SELECT id, name, price, description FROM services")
    return cursor.fetchall()

def get_user_info(user_id):
    cursor.execute("SELECT name, phone FROM users WHERE user_id = ?", (user_id,))
    return cursor.fetchone()

def get_order_count(status=None):
    if status:
        cursor.execute("SELECT COUNT(*) FROM orders WHERE status = ?", (status,))
    else:
        cursor.execute("SELECT COUNT(*) FROM orders")
    return cursor.fetchone()[0]

def block_user(user_id):
    cursor.execute("UPDATE users SET blocked = 1 WHERE user_id = ?", (user_id,))
    conn.commit()

def unblock_user(user_id):
    cursor.execute("UPDATE users SET blocked = 0 WHERE user_id = ?", (user_id,))
    conn.commit()

# ========== КЛАВИАТУРЫ ==========
def main_menu(user_id):
    if is_seller(user_id):
        return seller_menu()
    else:
        return client_menu()

def client_menu():
    buttons = [
        [InlineKeyboardButton(text="🛒 Сделать заказ", callback_data="new_order")],
        [InlineKeyboardButton(text="📋 Мои заказы", callback_data="my_orders")],
        [InlineKeyboardButton(text="ℹ️ О нас", callback_data="about")],
        [InlineKeyboardButton(text="📞 Контакты", callback_data="contacts")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def seller_menu():
    buttons = [
        [InlineKeyboardButton(text="📋 Новые заказы", callback_data="new_orders")],
        [InlineKeyboardButton(text="✅ Принятые", callback_data="accepted_orders")],
        [InlineKeyboardButton(text="❌ Отказанные", callback_data="rejected_orders")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton(text="🚫 Заблокированные", callback_data="blocked_users")],
        [InlineKeyboardButton(text="🚪 Выйти", callback_data="logout")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def service_keyboard():
    services = get_services()
    buttons = []
    for s in services:
        buttons.append([InlineKeyboardButton(text=f"{s[1]} - {s[2]}", callback_data=f"service_{s[0]}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def orders_keyboard(orders, prefix):
    buttons = []
    for order in orders:
        buttons.append([InlineKeyboardButton(text=f"Заказ #{order[0]} - {order[2][:20]}", callback_data=f"{prefix}_{order[0]}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def order_action_keyboard(order_id):
    buttons = [
        [InlineKeyboardButton(text="✅ Принять", callback_data=f"accept_{order_id}")],
        [InlineKeyboardButton(text="❌ Отказать", callback_data=f"reject_{order_id}")],
        [InlineKeyboardButton(text="💬 Ответить", callback_data=f"reply_{order_id}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="new_orders")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def blocked_users_keyboard():
    cursor.execute("SELECT user_id, name, phone FROM users WHERE role = 'client' AND blocked = 1")
    users = cursor.fetchall()
    buttons = []
    for u in users:
        name = u[1] if u[1] else f"ID:{u[0]}"
        buttons.append([InlineKeyboardButton(text=f"🔓 {name}", callback_data=f"unblock_{u[0]}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ========== ОБРАБОТЧИКИ ==========
@dp.message(Command("start"))
async def start_command(message: Message):
    user_id = message.from_user.id
    
    if is_blocked(user_id):
        await message.answer("🚫 *Вы заблокированы!* Обратитесь к администратору.", parse_mode="Markdown")
        return
    
    register_user(user_id)
    
    await message.answer(
        "🤝 *Добро пожаловать!*\n\n"
        "Я помогу вам оформить заказ или получить консультацию.\n\n"
        "👇 Выберите действие:",
        reply_markup=main_menu(user_id),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "back")
async def back_to_menu(callback: CallbackQuery):
    user_id = callback.from_user.id
    await callback.message.edit_text("🤝 Главное меню:", reply_markup=main_menu(user_id))
    await callback.answer()

@dp.callback_query(F.data == "new_order")
async def new_order_start(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    
    if is_blocked(user_id):
        await callback.answer("Вы заблокированы!", show_alert=True)
        return
    
    await state.set_state(ClientOrderState.waiting_for_service)
    await callback.message.edit_text(
        "🛒 *Выберите услугу:*",
        reply_markup=service_keyboard(),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(ClientOrderState.waiting_for_service, F.data.startswith("service_"))
async def select_service(callback: CallbackQuery, state: FSMContext):
    service_id = int(callback.data.split("_")[1])
    cursor.execute("SELECT name, price, description FROM services WHERE id = ?", (service_id,))
    service = cursor.fetchone()
    
    await state.update_data(service=f"{service[0]} ({service[1]})")
    await state.set_state(ClientOrderState.waiting_for_comment)
    
    await callback.message.edit_text(
        f"📝 Вы выбрали: *{service[0]}*\n\n"
        f"💰 {service[1]}\n"
        f"ℹ️ {service[2]}\n\n"
        f"Напишите *комментарий* к заказу (или нажмите «Пропустить»):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⏭ Пропустить", callback_data="skip_comment")]]),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(ClientOrderState.waiting_for_comment, F.data == "skip_comment")
async def skip_comment(callback: CallbackQuery, state: FSMContext):
    await state.update_data(comment="Без комментария")
    await state.set_state(ClientOrderState.waiting_for_contact)
    await callback.message.edit_text(
        "📞 Напишите *контактные данные* для связи (телефон, Telegram, email):\n\n"
        "Пример: `+7 999 123-45-67` или `@username`",
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.message(ClientOrderState.waiting_for_comment)
async def process_comment(message: Message, state: FSMContext):
    await state.update_data(comment=message.text)
    await state.set_state(ClientOrderState.waiting_for_contact)
    await message.answer(
        "📞 Напишите *контактные данные* для связи (телефон, Telegram, email):\n\n"
        "Пример: `+7 999 123-45-67` или `@username`",
        parse_mode="Markdown"
    )

@dp.message(ClientOrderState.waiting_for_contact)
async def process_contact(message: Message, state: FSMContext):
    data = await state.get_data()
    order_id = add_order(message.from_user.id, data['service'], data['comment'], message.text)
    
    # Обновляем контакты пользователя
    cursor.execute("UPDATE users SET name = ?, phone = ? WHERE user_id = ?", 
                   (message.from_user.full_name, message.text, message.from_user.id))
    conn.commit()
    
    await message.answer(
        f"✅ *Заказ оформлен!*\n\n"
        f"📦 Услуга: {data['service']}\n"
        f"📝 Комментарий: {data['comment']}\n"
        f"📞 Контакт: {message.text}\n"
        f"🆔 Номер заказа: {order_id}\n\n"
        f"Скоро с вами свяжется наш менеджер!",
        parse_mode="Markdown",
        reply_markup=main_menu(message.from_user.id)
    )
    
    # Отправляем уведомление продавцу (если есть)
    cursor.execute("SELECT user_id FROM users WHERE role = 'seller'")
    sellers = cursor.fetchall()
    for seller in sellers:
        try:
            await bot.send_message(
                seller[0],
                f"🆕 *Новый заказ!*\n\n"
                f"🆔 #{order_id}\n"
                f"👤 Клиент: {message.from_user.full_name}\n"
                f"📦 Услуга: {data['service']}\n"
                f"📝 Комментарий: {data['comment']}\n"
                f"📞 Контакт: {message.text}\n\n"
                f"Используйте меню продавца для управления заказами.",
                parse_mode="Markdown",
                reply_markup=main_menu(seller[0])
            )
        except:
            pass
    
    await state.clear()

@dp.callback_query(F.data == "my_orders")
async def show_my_orders(callback: CallbackQuery):
    user_id = callback.from_user.id
    orders = get_orders(user_id=user_id)
    
    if not orders:
        await callback.message.edit_text("📭 У вас пока нет заказов.", reply_markup=main_menu(user_id))
        await callback.answer()
        return
    
    text = "📋 *Ваши заказы:*\n\n"
    for order in orders[:10]:
        status_icon = {
            'pending': '⏳',
            'accepted': '✅',
            'rejected': '❌'
        }.get(order[5], '❓')
        text += f"{status_icon} *Заказ #{order[0]}*\n"
        text += f"📦 {order[2]}\n"
        text += f"📅 {order[6][:16]}\n"
        if order[7]:
            text += f"💬 Ответ: {order[7]}\n"
        text += "\n"
    
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=main_menu(user_id))
    await callback.answer()

@dp.callback_query(F.data == "new_orders")
async def show_new_orders(callback: CallbackQuery):
    if not is_seller(callback.from_user.id):
        await callback.answer("Доступ только для продавца!", show_alert=True)
        return
    
    orders = get_orders(status='pending')
    if not orders:
        await callback.message.edit_text("📭 Нет новых заказов.", reply_markup=seller_menu())
        await callback.answer()
        return
    
    await callback.message.edit_text(
        "📋 *Новые заказы:*\n\n"
        f"Всего: {len(orders)}",
        reply_markup=orders_keyboard(orders, "order"),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("order_"))
async def view_order(callback: CallbackQuery):
    order_id = int(callback.data.split("_")[1])
    cursor.execute("""
        SELECT o.id, o.user_id, o.service, o.comment, o.contact, o.status, o.created_at,
               u.name, u.phone
        FROM orders o
        JOIN users u ON o.user_id = u.user_id
        WHERE o.id = ?
    """, (order_id,))
    order = cursor.fetchone()
    
    if not order:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    
    text = f"🆔 *Заказ #{order[0]}*\n\n"
    text += f"👤 Клиент: {order[7] or 'Не указан'}\n"
    text += f"📞 Контакт: {order[4]}\n"
    text += f"📦 Услуга: {order[2]}\n"
    text += f"📝 Комментарий: {order[3]}\n"
    text += f"📅 Создан: {order[6][:16]}\n"
    text += f"📊 Статус: {order[5]}\n"
    
    if order[5] == 'pending':
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=order_action_keyboard(order_id))
    else:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="new_orders")]]))
    
    await callback.answer()

@dp.callback_query(F.data.startswith("accept_"))
async def accept_order(callback: CallbackQuery):
    if not is_seller(callback.from_user.id):
        await callback.answer("Доступ только для продавца!", show_alert=True)
        return
    
    order_id = int(callback.data.split("_")[1])
    update_order_status(order_id, 'accepted')
    
    # Уведомляем клиента
    cursor.execute("SELECT user_id FROM orders WHERE id = ?", (order_id,))
    user_id = cursor.fetchone()[0]
    
    try:
        await bot.send_message(
            user_id,
            f"✅ *Ваш заказ #{order_id} принят!*\n\n"
            f"Скоро с вами свяжется менеджер.",
            parse_mode="Markdown"
        )
    except:
        pass
    
    await callback.answer("✅ Заказ принят!", show_alert=True)
    await show_new_orders(callback)

@dp.callback_query(F.data.startswith("reject_"))
async def reject_order(callback: CallbackQuery):
    if not is_seller(callback.from_user.id):
        await callback.answer("Доступ только для продавца!", show_alert=True)
        return
    
    order_id = int(callback.data.split("_")[1])
    update_order_status(order_id, 'rejected')
    
    # Уведомляем клиента
    cursor.execute("SELECT user_id FROM orders WHERE id = ?", (order_id,))
    user_id = cursor.fetchone()[0]
    
    try:
        await bot.send_message(
            user_id,
            f"❌ *Ваш заказ #{order_id} отклонён.*\n\n"
            f"Вы можете оформить новый заказ или связаться с нами для уточнения деталей.",
            parse_mode="Markdown"
        )
    except:
        pass
    
    await callback.answer("❌ Заказ отклонён!", show_alert=True)
    await show_new_orders(callback)

@dp.callback_query(F.data.startswith("reply_"))
async def reply_to_order(callback: CallbackQuery, state: FSMContext):
    if not is_seller(callback.from_user.id):
        await callback.answer("Доступ только для продавца!", show_alert=True)
        return
    
    order_id = int(callback.data.split("_")[1])
    await state.update_data(reply_order_id=order_id)
    await state.set_state(SellerReplyState.waiting_for_reply)
    
    await callback.message.edit_text(
        "💬 Введите *ответ* клиенту:\n\n"
        "Клиент получит ваше сообщение в Telegram.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data="new_orders")]])
    )
    await callback.answer()

@dp.message(SellerReplyState.waiting_for_reply)
async def process_reply(message: Message, state: FSMContext):
    data = await state.get_data()
    order_id = data['reply_order_id']
    reply_text = message.text
    
    update_order_status(order_id, 'accepted', reply_text)
    
    # Отправляем ответ клиенту
    cursor.execute("SELECT user_id FROM orders WHERE id = ?", (order_id,))
    user_id = cursor.fetchone()[0]
    
    try:
        await bot.send_message(
            user_id,
            f"💬 *Ответ от менеджера по заказу #{order_id}:*\n\n"
            f"{reply_text}\n\n"
            f"Если у вас остались вопросы, напишите нам!",
            parse_mode="Markdown"
        )
        await message.answer("✅ Ответ отправлен клиенту!")
    except:
        await message.answer("❌ Не удалось отправить ответ (клиент заблокировал бота)")
    
    await state.clear()
    await show_new_orders(await message.answer("."))

@dp.callback_query(F.data == "accepted_orders")
async def show_accepted_orders(callback: CallbackQuery):
    if not is_seller(callback.from_user.id):
        await callback.answer("Доступ только для продавца!", show_alert=True)
        return
    
    orders = get_orders(status='accepted')
    if not orders:
        await callback.message.edit_text("📭 Нет принятых заказов.", reply_markup=seller_menu())
        await callback.answer()
        return
    
    text = "✅ *Принятые заказы:*\n\n"
    for order in orders:
        text += f"🆔 #{order[0]} - {order[2][:30]}\n📅 {order[6][:16]}\n\n"
    
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=seller_menu())
    await callback.answer()

@dp.callback_query(F.data == "rejected_orders")
async def show_rejected_orders(callback: CallbackQuery):
    if not is_seller(callback.from_user.id):
        await callback.answer("Доступ только для продавца!", show_alert=True)
        return
    
    orders = get_orders(status='rejected')
    if not orders:
        await callback.message.edit_text("📭 Нет отклонённых заказов.", reply_markup=seller_menu())
        await callback.answer()
        return
    
    text = "❌ *Отклонённые заказы:*\n\n"
    for order in orders:
        text += f"🆔 #{order[0]} - {order[2][:30]}\n📅 {order[6][:16]}\n\n"
    
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=seller_menu())
    await callback.answer()

@dp.callback_query(F.data == "stats")
async def show_stats(callback: CallbackQuery):
    if not is_seller(callback.from_user.id):
        await callback.answer("Доступ только для продавца!", show_alert=True)
        return
    
    total = get_order_count()
    pending = get_order_count('pending')
    accepted = get_order_count('accepted')
    rejected = get_order_count('rejected')
    
    cursor.execute("SELECT COUNT(*) FROM users WHERE role = 'client'")
    clients = cursor.fetchone()[0]
    
    text = f"📊 *Статистика*\n\n"
    text += f"📦 Всего заказов: {total}\n"
    text += f"⏳ В обработке: {pending}\n"
    text += f"✅ Принято: {accepted}\n"
    text += f"❌ Отклонено: {rejected}\n"
    text += f"👥 Клиентов: {clients}\n"
    
    if total > 0:
        text += f"\n📈 Конверсия: {accepted/total*100:.0f}%"
    
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=seller_menu())
    await callback.answer()

@dp.callback_query(F.data == "blocked_users")
async def show_blocked_users(callback: CallbackQuery):
    if not is_seller(callback.from_user.id):
        await callback.answer("Доступ только для продавца!", show_alert=True)
        return
    
    cursor.execute("SELECT COUNT(*) FROM users WHERE role = 'client' AND blocked = 1")
    count = cursor.fetchone()[0]
    
    if count == 0:
        await callback.message.edit_text("📭 Нет заблокированных пользователей.", reply_markup=seller_menu())
        await callback.answer()
        return
    
    await callback.message.edit_text(
        f"🚫 *Заблокированные пользователи:* (всего: {count})\n\n"
        f"Выберите пользователя для разблокировки:",
        reply_markup=blocked_users_keyboard(),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("unblock_"))
async def unblock_user_callback(callback: CallbackQuery):
    if not is_seller(callback.from_user.id):
        await callback.answer("Доступ только для продавца!", show_alert=True)
        return
    
    user_id = int(callback.data.split("_")[1])
    unblock_user(user_id)
    
    try:
        await bot.send_message(user_id, "🔓 *Вы разблокированы!* Теперь вы можете снова пользоваться ботом.", parse_mode="Markdown")
    except:
        pass
    
    await callback.answer("✅ Пользователь разблокирован!", show_alert=True)
    await show_blocked_users(callback)

@dp.callback_query(F.data == "about")
async def about(callback: CallbackQuery):
    user_id = callback.from_user.id
    text = (
        "ℹ️ *О компании*\n\n"
        "Мы — команда профессионалов в сфере IT и маркетинга.\n\n"
        "✅ Более 5 лет опыта\n"
        "✅ 100+ выполненных проектов\n"
        "✅ Индивидуальный подход\n\n"
        "С нами ваш бизнес выйдет на новый уровень!"
    )
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=main_menu(user_id))
    await callback.answer()

@dp.callback_query(F.data == "contacts")
async def contacts(callback: CallbackQuery):
    user_id = callback.from_user.id
    text = (
        "📞 *Контакты*\n\n"
        "📱 Телефон: +7 (999) 123-45-67\n"
        "📧 Email: info@company.ru\n"
        "💬 Telegram: @manager_bot\n\n"
        "🕐 Режим работы: 10:00 - 20:00 МСК"
    )
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=main_menu(user_id))
    await callback.answer()

@dp.callback_query(F.data == "logout")
async def logout(callback: CallbackQuery):
    cursor.execute("UPDATE users SET role = 'client' WHERE user_id = ?", (callback.from_user.id,))
    conn.commit()
    await callback.message.edit_text("🤝 Вы вышли из режима продавца.", reply_markup=client_menu())
    await callback.answer()

# ========== АВТОРИЗАЦИЯ ПРОДАВЦА ==========
@dp.message(Command("seller"))
async def seller_login_start(message: Message, state: FSMContext):
    await state.set_state(SellerLoginState.waiting_for_login)
    await message.answer("🔐 *Вход в панель продавца*\n\nВведите *логин*:", parse_mode="Markdown")

@dp.message(SellerLoginState.waiting_for_login)
async def seller_login_process(message: Message, state: FSMContext):
    if message.text == SELLER_LOGIN:
        await state.update_data(login=message.text)
        await state.set_state(SellerLoginState.waiting_for_password)
        await message.answer("🔐 Введите *пароль*:", parse_mode="Markdown")
    else:
        await message.answer("❌ Неверный логин. Попробуйте ещё раз или напишите /start")
        await state.clear()

@dp.message(SellerLoginState.waiting_for_password)
async def seller_password_process(message: Message, state: FSMContext):
    if message.text == SELLER_PASSWORD:
        cursor.execute("UPDATE users SET role = 'seller' WHERE user_id = ?", (message.from_user.id,))
        conn.commit()
        await message.answer(
            "✅ *Добро пожаловать в панель продавца!*\n\n"
            "📋 Новые заказы — принимайте и отвечайте клиентам.\n"
            "📊 Статистика — отслеживайте эффективность.\n"
            "🚫 Блокировка — управляйте доступом пользователей.",
            parse_mode="Markdown",
            reply_markup=seller_menu()
        )
    else:
        await message.answer("❌ Неверный пароль. Попробуйте ещё раз или напишите /start")
    await state.clear()

# ========== ВЕБ-СЕРВЕР И САМОПИНГ ==========
async def health_check(request):
    return web.Response(text="✅ Бот работает")

async def self_ping():
    while True:
        await asyncio.sleep(600)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(RENDER_URL, timeout=10) as resp:
                    print(f"[SELF-PING] {resp.status} - {datetime.now().strftime('%H:%M:%S')}")
        except:
            pass

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
    print("✅ CRM-бот запущен!")
    print(f"📍 Адрес: {RENDER_URL}")
    await start_web()
    asyncio.create_task(self_ping())
    print("🔄 Самопинг (каждые 10 минут) запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
