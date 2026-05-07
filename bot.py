import os
import sys
import asyncio
import logging
import re
from datetime import datetime, timedelta
from xml.etree import ElementTree

import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from flask import Flask

# ========== НАСТРОЙКИ ==========
print("Starting bot...")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    print("❌ ОШИБКА: BOT_TOKEN не задан!")
    sys.exit(1)

print(f"✅ Токен получен: {BOT_TOKEN[:10]}...")

CBR_URL = "https://cbr.ru/currency_base/daily/"
RAPIRA_API_URL = "https://api.rapira.net/api/v1/ticker?pair=USDT_RUB"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

HELP_TEXT = """
🏦 <b>Калькулятор курсовой разницы USD/RUB</b>

Бот на заданную дату:
• берёт официальный курс доллара США (ЦБ РФ)
• берёт курс USDT/RUB (биржа Rapira)
• считает процентную разницу: (Rapira - ЦБ) / ЦБ * 100%

📌 <b>Как использовать:</b>
1. Нажмите кнопку «📊 Рассчитать курсовую разницу»
2. Введите дату в формате дд.мм.гггг (или «сегодня»)
3. Бот покажет курс ЦБ, курс Rapira и разницу в %
"""

# ========== FSM ==========
class CurrencyCalc(StatesGroup):
    waiting_for_date = State()

# ========== КЛАВИАТУРЫ ==========
def get_main_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="📊 Рассчитать курсовую разницу"))
    builder.adjust(1)
    return builder.as_markup(resize_keyboard=True)

def get_back_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="🔙 Отмена"))
    return builder.as_markup(resize_keyboard=True)

# ========== ФУНКЦИИ ПОЛУЧЕНИЯ КУРСОВ ==========
async def fetch_cbr_usd_rate(date: datetime) -> float | None:
    """
    Парсит курс USD с официальной страницы ЦБ РФ на заданную дату.
    Возвращает float (рублей за 1 USD) или None.
    """
    # Формируем URL с параметром даты (на сайте ЦБ используется формат dd.mm.yyyy)
    date_str = date.strftime("%d.%m.%Y")
    url = f"{CBR_URL}?date_req={date_str}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as response:
                if response.status != 200:
                    logger.error(f"ЦБ: HTTP {response.status} для {date_str}")
                    return None
                html = await response.text()
                # Ищем строку с USD: <td>USD</td><td>1</td><td>Доллар США</td><td>75,2246</td>
                match = re.search(r'<td>USD</td>\s*<td>1</td>\s*<td>Доллар США</td>\s*<td>([\d,]+)</td>', html, re.IGNORECASE | re.DOTALL)
                if not match:
                    logger.warning(f"ЦБ: не найден курс USD для {date_str}")
                    return None
                rate_str = match.group(1).replace(',', '.')
                return float(rate_str)
    except Exception as e:
        logger.error(f"ЦБ ошибка для {date_str}: {e}")
        return None

async def fetch_rapira_usdt_rate() -> float | None:
    """Парсит курс USDT/RUB с сайта rapira.net"""
    url = "https://rapira.net/ru/exchange/USDT_RUB"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }) as response:
                if response.status != 200:
                    logger.error(f"Rapira: HTTP {response.status}")
                    return None

                html = await response.text()

                # ---- СПИСОК ВОЗМОЖНЫХ ПАТТЕРНОВ ДЛЯ ПОИСКА КУРСА ----
                patterns = [
                    r'"last":"?([\d.]+)"?',                 # JSON поле "last"
                    r'"price":"?([\d.]+)"?',                # JSON поле "price"
                    r'<span[^>]*class="[^"]*price[^"]*"[^>]*>([\d.]+)<', # <span class="...price...">
                    r'<div[^>]*class="[^"]*rate[^"]*"[^>]*>([\d.]+)<',   # <div class="...rate...">
                    r'USDT<\/span>\s*<span[^>]*>([\d.]+)<', # Табличный формат
                    r'₽\s*([\d.]+)\s*</div>',               # Цена в рублях в div
                    r'>([\d.]+)\s*₽<',                       # Цифры перед символом рубля
                ]

                for pattern in patterns:
                    match = re.search(pattern, html)
                    if match:
                        rate = float(match.group(1))
                        logger.info(f"Rapira курс найден: {rate} по паттерну {pattern}")
                        return rate

                logger.warning("Не удалось найти курс Rapira на странице")
                return None

    except Exception as e:
        logger.error(f"Ошибка парсинга Rapira: {e}")
        return None

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def parse_date(user_input: str) -> datetime | None:
    if user_input.lower() in ["сегодня", "today"]:
        return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    pattern = r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$"
    match = re.match(pattern, user_input.strip())
    if not match:
        return None

    day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
    if year < 1992 or year > datetime.now().year + 1:
        return None
    if month < 1 or month > 12 or day < 1 or day > 31:
        return None
    try:
        return datetime(year, month, day)
    except ValueError:
        return None

def format_result(date_str: str, cbr_rate: float, rapira_rate: float) -> str:
    diff_percent = ((rapira_rate - cbr_rate) / cbr_rate) * 100
    sign = "+" if diff_percent >= 0 else ""
    if diff_percent > 0:
        interp = f"Rapira ВЫШЕ ЦБ на {sign}{diff_percent:.2f}%"
    elif diff_percent < 0:
        interp = f"Rapira НИЖЕ ЦБ на {sign}{diff_percent:.2f}%"
    else:
        interp = "Курсы равны"

    return f"""
📊 <b>КУРСОВАЯ РАЗНИЦА</b> на {date_str}

<b>ЦБ РФ (USD):</b> 1$ = {cbr_rate:.2f} ₽
<b>Rapira (USDT):</b> 1₮ = {rapira_rate:.2f} ₽

📈 <b>РАЗНИЦА (Rapira - ЦБ):</b>
💰 <b>% отклонения:</b> {sign}{diff_percent:.2f}%
💡 {interp}

🔄 Нажмите кнопку меню для нового расчёта
    """.strip()

# ========== БОТ ==========
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(f"{HELP_TEXT}", reply_markup=get_main_keyboard(), parse_mode="HTML")

@dp.message(F.text == "📊 Рассчитать курсовую разницу")
async def start_calculation(message: types.Message, state: FSMContext):
    await state.clear()
    await state.set_state(CurrencyCalc.waiting_for_date)
    await message.answer(
        "📅 Введите <b>дату</b> в формате <code>дд.мм.гггг</code> или «сегодня»:\n\n"
        "<i>Например: 07.05.2026 или сегодня</i>",
        reply_markup=get_back_keyboard(),
        parse_mode="HTML"
    )

@dp.message(F.text == "🔙 Отмена", StateFilter(CurrencyCalc))
async def cancel_calculation(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Расчёт отменён.", reply_markup=get_main_keyboard())

@dp.message(CurrencyCalc.waiting_for_date)
async def process_date(message: types.Message, state: FSMContext):
    user_input = message.text.strip()
    if user_input == "🔙 Отмена":
        await cancel_calculation(message, state)
        return

    selected_date = parse_date(user_input)
    if selected_date is None:
        await message.answer(
            "Неверный формат! Используйте <code>дд.мм.гггг</code> или «сегодня».",
            reply_markup=get_back_keyboard(),
            parse_mode="HTML"
        )
        return

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if selected_date > today:
        await message.answer("Дата не может быть в будущем.", reply_markup=get_back_keyboard())
        return

    await message.answer("Получаю данные...", parse_mode="HTML")
    # Получаем оба курса параллельно
    cbr_task = fetch_cbr_usd_rate(selected_date)
    rapira_task = fetch_rapira_usdt_rate()
    cbr_rate, rapira_rate = await asyncio.gather(cbr_task, rapira_task)

    if cbr_rate is None:
        await message.answer(
            f"Не удалось получить курс ЦБ на {selected_date.strftime('%d.%m.%Y')}.\n"
            "Проверьте дату (доступны с 1992 года) или попробуйте позже.",
            reply_markup=get_main_keyboard()
        )
        await state.clear()
        return

    if rapira_rate is None:
        await message.answer(
            "Не удалось получить текущий курс Rapira.\n"
            "Попробуйте позже.",
            reply_markup=get_main_keyboard()
        )
        await state.clear()
        return

    result_text = format_result(
        selected_date.strftime("%d.%m.%Y"),
        cbr_rate,
        rapira_rate
    )
    await message.answer(result_text, reply_markup=get_main_keyboard(), parse_mode="HTML")
    await state.clear()

@dp.message()
async def handle_unknown(message: types.Message):
    await message.answer(
        "Неизвестная команда.\nНажмите «📊 Рассчитать курсовую разницу» для расчёта.",
        reply_markup=get_main_keyboard()
    )

# ========== FLASK HEALTHCHECK ==========
flask_app = Flask(__name__)

@flask_app.route("/", methods=["GET"])
def health_check():
    return "OK", 200

def run_flask():
    flask_app.run(host="0.0.0.0", port=8080)

# ========== ЗАПУСК ==========
async def main():
    print("Бот запускается")
    try:
        await dp.start_polling(bot)
    except Exception as e:
        print(f"Ошибка: {e}")
        raise

if __name__ == "__main__":
    import threading
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.run(main())
