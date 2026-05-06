import asyncio
import aiohttp
import os
from bs4 import BeautifulSoup
from flask import Flask
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
import threading
import re

# ========== НАСТРОЙКИ ==========
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    print("⚠️ BOT_TOKEN не найден! Использую токен из кода (только для теста)")
    TOKEN = "8696308891:AAHoPKGqjHRuPFBTI8d7sP9BvjalDPzBkqM"

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Flask для Render (чтобы сервис не падал)
flask_app = Flask(__name__)

@flask_app.route('/')
def health_check():
    return "Bot is running!", 200

@flask_app.route('/health')
def health():
    return "OK", 200

# ========== КНОПКИ МЕНЮ ==========
menu_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="📊 Рассчитать курсовую разницу")]],
    resize_keyboard=True
)

# ========== СОСТОЯНИЯ FSM ==========
class CurrencyStates(StatesGroup):
    waiting_for_date1 = State()
    waiting_for_date2 = State()

# ========== ПАРСИНГ КУРСА ЦБ РФ ==========
async def get_cbr_rate(date: str):
    try:
        day, month, year = date.split('.')
        url = f"https://www.cbr.ru/scripts/XML_daily.asp?date_req={day}/{month}/{year}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                text = await response.text()
                soup = BeautifulSoup(text, 'xml')
                valute = soup.find('Valute', {'ID': 'R01235'})
                if valute:
                    value = valute.find('Value').text.replace(',', '.')
                    return float(value)
                return None
    except Exception as e:
        print(f"Ошибка ЦБ: {e}")
        return None

# ========== ПАРСИНГ КУРСА RAPIRA ==========
async def get_rapira_rate():
    try:
        url = "https://rapira.io/ru/exchange/USDT/RUB"
        headers = {'User-Agent': 'Mozilla/5.0'}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                text = await response.text()
                soup = BeautifulSoup(text, 'html.parser')
                rate_element = soup.find('div', class_='exchange-rate')
                if rate_element:
                    rate_text = rate_element.text.strip()
                    numbers = re.findall(r'\d+[\.,]?\d*', rate_text)
                    if numbers:
                        return float(numbers[0].replace(',', '.'))
                return None
    except:
        return None

# ========== ФУНКЦИЯ РАСЧЕТА ==========
def calculate_difference(old_rate, new_rate):
    diff = new_rate - old_rate
    percent = (diff / old_rate) * 100
    return diff, percent

# ========== ОБРАБОТЧИКИ КОМАНД ==========
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "💰 <b>Калькулятор курсовой разницы</b>\n\n"
        "Рассчитывает изменение курса доллара между датой поставки и датой оплаты.\n\n"
        "Нажмите кнопку ниже и введите две даты.",
        parse_mode="HTML",
        reply_markup=menu_kb
    )

@dp.message(lambda message: message.text == "📊 Рассчитать курсовую разницу")
async def start_calculation(message: Message, state: FSMContext):
    await state.set_state(CurrencyStates.waiting_for_date1)
    await message.answer(
        "📅 Введите <b>ДАТУ 1 (поставка)</b>\n\n"
        "Формат: <code>дд.мм.гггг</code>\n"
        "Пример: <code>25.05.2026</code>\n\n"
        "Или отправьте <b>сегодня</b>",
        parse_mode="HTML"
    )

@dp.message(CurrencyStates.waiting_for_date1)
async def process_date1(message: Message, state: FSMContext):
    date_input = message.text.strip().lower()
    
    if date_input == "сегодня":
        from datetime import datetime
        date_input = datetime.now().strftime("%d.%m.%Y")
    
    try:
        day, month, year = date_input.split('.')
        if len(day) != 2 or len(month) != 2 or len(year) != 4:
            raise ValueError
    except:
        await message.answer("❌ Неверный формат! Используйте <code>дд.мм.гггг</code>", parse_mode="HTML")
        return
    
    await message.answer("🔄 Получаю курс с сайта ЦБ РФ...")
    cbr_rate = await get_cbr_rate(date_input)
    
    if cbr_rate is None:
        await message.answer("⚠️ Не удалось получить курс. Проверьте дату.")
        return
    
    await state.update_data(date1=date_input, cbr_rate1=cbr_rate)
    await state.set_state(CurrencyStates.waiting_for_date2)
    await message.answer(
        f"✅ <b>Дата 1:</b> {date_input}\n"
        f"🏦 <b>Курс ЦБ РФ:</b> 1$ = {cbr_rate:.2f} ₽\n\n"
        f"📅 Теперь введите <b>ДАТУ 2 (оплата)</b>:",
        parse_mode="HTML"
    )

@dp.message(CurrencyStates.waiting_for_date2)
async def process_date2(message: Message, state: FSMContext):
    date_input = message.text.strip().lower()
    
    if date_input == "сегодня":
        from datetime import datetime
        date_input = datetime.now().strftime("%d.%m.%Y")
    
    try:
        day, month, year = date_input.split('.')
        if len(day) != 2 or len(month) != 2 or len(year) != 4:
            raise ValueError
    except:
        await message.answer("❌ Неверный формат! Используйте <code>дд.мм.гггг</code>", parse_mode="HTML")
        return
    
    data = await state.get_data()
    date1 = data.get('date1')
    rate1 = data.get('cbr_rate1')
    
    await message.answer("🔄 Получаю курс с сайта ЦБ РФ...")
    cbr_rate2 = await get_cbr_rate(date_input)
    
    if cbr_rate2 is None:
        await message.answer("⚠️ Не удалось получить курс. Проверьте дату.")
        return
    
    diff, percent = calculate_difference(rate1, cbr_rate2)
    
    result = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 <b>РЕЗУЛЬТАТ РАСЧЕТА</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📅 <b>Поставка</b> ({date1})
   1$ = {rate1:.2f} ₽

📅 <b>Оплата</b> ({date_input})
   1$ = {cbr_rate2:.2f} ₽

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📈 <b>ИЗМЕНЕНИЕ:</b>

💰 Разница: {diff:+.2f} ₽
📊 % изменения: {percent:+.2f}%

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
    await message.answer(result, parse_mode="HTML", reply_markup=menu_kb)
    await state.clear()

# ========== ЗАПУСК (FLASK + БОТ В ОТДЕЛЬНОМ ПОТОКЕ) ==========
def run_bot():
    asyncio.run(dp.start_polling(bot))

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    print("🤖 Бот запускается...")
    # Запускаем бота в фоновом потоке
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.start()
    # Запускаем Flask (для Render)
    run_flask()
