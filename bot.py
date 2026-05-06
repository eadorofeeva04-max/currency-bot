import asyncio
import aiohttp
import os
import re
from datetime import datetime
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from flask import Flask
import threading

# ========== НАСТРОЙКИ ==========
TOKEN = "8696308891:AAHoPKGqjHRuPFBTI8d7sP9BvjalDPzBkqM"
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Flask app for Render
flask_app = Flask(__name__)

@flask_app.route('/')
def health_check():
    return "Bot is running!"

@flask_app.route('/health')
def health():
    return "OK"

# ========== КНОПКИ ГЛАВНОГО МЕНЮ ==========
menu_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="📊 Рассчитать курсовую разницу")]],
    resize_keyboard=True
)

# ========== СОСТОЯНИЯ ДЛЯ FSM ==========
class CurrencyStates(StatesGroup):
    waiting_for_date1 = State()
    waiting_for_date2 = State()

# ========== ФУНКЦИЯ ПАРСИНГА КУРСА ЦБ РФ ==========
async def get_cbr_rate(date: str):
    try:
        day, month, year = date.split('.')
        url_date = f"{day}/{month}/{year}"
        url = f"https://www.cbr.ru/scripts/XML_daily.asp?date_req={url_date}"
        
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
        print(f"Ошибка парсинга ЦБ: {e}")
        return None

# ========== ФУНКЦИЯ ПАРСИНГА КУРСА RAPIRA ==========
async def get_rapira_rate():
    try:
        url = "https://rapira.io/ru/exchange/USDT/RUB"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                text = await response.text()
                soup = BeautifulSoup(text, 'html.parser')
                # Пробуем разные селекторы
                rate_element = soup.find('div', class_='exchange-rate')
                if not rate_element:
                    rate_element = soup.find('span', class_='rate-value')
                if not rate_element:
                    # Ищем любую цифру с курсом
                    all_text = text.replace(',', '.')
                    numbers = re.findall(r'\d+\.?\d*', all_text)
                    for num in numbers:
                        num_float = float(num)
                        if 80 <= num_float <= 120:  # диапазон курса USDT/RUB
                            return num_float
                    return None
                
                rate_text = rate_element.text.strip()
                numbers = re.findall(r'\d+[\.,]?\d*', rate_text)
                if numbers:
                    rate = float(numbers[0].replace(',', '.'))
                    if 80 <= rate <= 120:
                        return rate
                return None
    except Exception as e:
        print(f"Ошибка парсинга Rapira: {e}")
        return None

# ========== ФУНКЦИЯ РАСЧЕТА ==========
def calculate_difference(old_rate, new_rate):
    difference = new_rate - old_rate
    percent_change = (difference / old_rate) * 100 if old_rate != 0 else 0
    return difference, percent_change

# ========== ОБРАБОТЧИКИ ==========
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "💰 <b>Калькулятор курсовой разницы</b>\n\n"
        "Я помогаю рассчитать, как изменился курс доллара (или USDT) "
        "между датой <b>поставки</b> и датой <b>оплаты</b>.\n\n"
        "Просто нажми кнопку ниже и введи две даты.",
        parse_mode="HTML",
        reply_markup=menu_kb
    )

@dp.message(lambda message: message.text == "📊 Рассчитать курсовую разницу")
async def start_calculation(message: Message, state: FSMContext):
    await state.set_state(CurrencyStates.waiting_for_date1)
    await message.answer(
        "📅 <b>Введите ДАТУ 1 (поставка)</b>\n\n"
        "Формат: <code>дд.мм.гггг</code>\n"
        "Пример: <code>25.05.2026</code>\n\n"
        "Или отправьте <b>сегодня</b> для текущей даты.",
        parse_mode="HTML"
    )

@dp.message(CurrencyStates.waiting_for_date1)
async def process_date1(message: Message, state: FSMContext):
    date_input = message.text.strip().lower()
    
    if date_input == "сегодня":
        date_input = datetime.now().strftime("%d.%m.%Y")
    
    # Проверка формата
    date_parts = date_input.split('.')
    if len(date_parts) != 3:
        await message.answer("❌ Неверный формат! Используйте <code>дд.мм.гггг</code> (например: 25.05.2026)", parse_mode="HTML")
        return
    
    day, month, year = date_parts
    if len(day) != 2 or len(month) != 2 or len(year) != 4 or not all(p.isdigit() for p in [day, month, year]):
        await message.answer("❌ Неверный формат! Используйте <code>дд.мм.гггг</code> (например: 25.05.2026)", parse_mode="HTML")
        return
    
    await message.answer("🔄 Получаю курс с сайта ЦБ РФ...")
    cbr_rate = await get_cbr_rate(date_input)
    
    if cbr_rate is None:
        await message.answer("⚠️ Не удалось получить курс с ЦБ РФ. Проверьте дату или попробуйте позже.")
        return
    
    await state.update_data(date1=date_input, cbr_rate1=cbr_rate)
    
    await message.answer("🔄 Получаю текущий курс Rapira...")
    rapira_rate = await get_rapira_rate()
    
    if rapira_rate:
        await state.update_data(rapira_rate1=rapira_rate)
        await message.answer(
            f"📌 <b>Дата 1 (Поставка):</b> {date_input}\n"
            f"🏦 <b>Курс ЦБ РФ:</b> 1$ = {cbr_rate:.2f} ₽\n"
            f"🪙 <b>Курс Rapira (USDT):</b> 1 USDT ≈ {rapira_rate:.2f} ₽\n\n"
            f"📅 Теперь введите <b>ДАТУ 2 (оплата)</b> в том же формате:",
            parse_mode="HTML"
        )
    else:
        await state.update_data(rapira_rate1=None)
        await message.answer(
            f"📌 <b>Дата 1 (Поставка):</b> {date_input}\n"
            f"🏦 <b>Курс ЦБ РФ:</b> 1$ = {cbr_rate:.2f} ₽\n"
            f"⚠️ Курс Rapira временно недоступен\n\n"
            f"📅 Теперь введите <b>ДАТУ 2 (оплата)</b>:",
            parse_mode="HTML"
        )
    
    await state.set_state(CurrencyStates.waiting_for_date2)

@dp.message(CurrencyStates.waiting_for_date2)
async def process_date2(message: Message, state: FSMContext):
    date_input = message.text.strip().lower()
    
    if date_input == "сегодня":
        date_input = datetime.now().strftime("%d.%m.%Y")
    
    # Проверка формата
    date_parts = date_input.split('.')
    if len(date_parts) != 3:
        await message.answer("❌ Неверный формат! Используйте <code>дд.мм.гггг</code> (например: 25.05.2026)", parse_mode="HTML")
        return
    
    day, month, year = date_parts
    if len(day) != 2 or len(month) != 2 or len(year) != 4 or not all(p.isdigit() for p in [day, month, year]):
        await message.answer("❌ Неверный формат! Используйте <code>дд.мм.гггг</code> (например: 25.05.2026)", parse_mode="HTML")
        return
    
    data = await state.get_data()
    date1 = data.get('date1')
    cbr_rate1 = data.get('cbr_rate1')
    
    if not cbr_rate1:
        await message.answer("⚠️ Ошибка: не найдены данные первой даты. Начните заново.", reply_markup=menu_kb)
        await state.clear()
        return
    
    await message.answer("🔄 Получаю курс с сайта ЦБ РФ...")
    cbr_rate2 = await get_cbr_rate(date_input)
    
    if cbr_rate2 is None:
        await message.answer("⚠️ Не удалось получить курс с ЦБ РФ. Проверьте дату или попробуйте позже.")
        return
    
    diff, percent = calculate_difference(cbr_rate1, cbr_rate2)
    diff_sign = "+" if diff >= 0 else ""
    percent_sign = "+" if percent >= 0 else ""
    
    result_text = (
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>РЕЗУЛЬТАТ РАСЧЕТА (ЦБ РФ)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📅 <b>Дата 1 (Поставка):</b> {date1}\n"
        f"   Курс: 1$ = {cbr_rate1:.2f} ₽\n\n"
        f"📅 <b>Дата 2 (Оплата):</b> {date_input}\n"
        f"   Курс: 1$ = {cbr_rate2:.2f} ₽\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 <b>ИЗМЕНЕНИЕ:</b>\n"
        f"💰 Разница: {diff_sign}{diff:.2f} ₽\n"
        f"📊 % изменения: {percent_sign}{percent:.2f}%\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    )
    
    rapira_rate1 = data.get('rapira_rate1')
    if rapira_rate1:
        await message.answer("🔄 Получаю актуальный курс Rapira...")
        rapira_rate2 = await get_rapira_rate()
        if rapira_rate2:
            diff_r, percent_r = calculate_difference(rapira_rate1, rapira_rate2)
            diff_sign_r = "+" if diff_r >= 0 else ""
            percent_sign_r = "+" if percent_r >= 0 else ""
            result_text += (
                f"\n🪙 <b>Rapira (USDT/RUB):</b>\n"
                f"   Старый: 1 USDT = {rapira_rate1:.2f} ₽\n"
                f"   Новый:  1 USDT = {rapira_rate2:.2f} ₽\n"
                f"   Разница: {diff_sign_r}{diff_r:.2f} ₽\n"
                f"   % изменения: {percent_sign_r}{percent_r:.2f}%\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            )
    
    result_text += (
        f"\n💡 <i>Если курс вырос (+%), ваша закупочная стоимость увеличилась.\n"
        f"Если курс упал (-%), вы получили прибыль на разнице.</i>\n\n"
        f"🔄 Нажмите кнопку меню для нового расчета."
    )
    
    await message.answer(result_text, parse_mode="HTML", reply_markup=menu_kb)
    await state.clear()

# ========== ГЛАВНАЯ ФУНКЦИЯ ЗАПУСКА ==========
async def main():
    """Запуск бота (правильный для aiogram 3.x)"""
    await dp.start_polling(bot)

def run_bot_in_thread():
    """Запуск бота в отдельном потоке с правильным event loop"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(main())

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    print("🚀 Запуск бота...")
    
    # Запускаем бота в отдельном потоке
    bot_thread = threading.Thread(target=run_bot_in_thread, daemon=True)
    bot_thread.start()
    
    print("🌐 Запуск Flask сервера...")
    # Запускаем Flask в основном потоке
    run_flask()
