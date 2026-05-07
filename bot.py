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
    print("Добавьте переменную окружения BOT_TOKEN в настройках Render")
    sys.exit(1)

print(f"✅ Токен получен: {BOT_TOKEN[:10]}...")

# Рабочий API ЦБ (XML)
CBR_API_URL = "https://www.cbr.ru/scripts/XML_daily.asp"

print(f"✅ Используется API ЦБ: {CBR_API_URL}")
print(f"✅ Используется API Rapira: https://api.rapira.net/open/market/rates")

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ========== КОНСТАНТЫ ==========
HELP_TEXT = """
🏦 <b>Калькулятор курсовой разницы USD/RUB</b>

Бот на заданную дату:
• берёт официальный курс доллара США (ЦБ РФ)
• берёт курс USDT/RUB (биржа Rapira)
• считает процентную разницу

📌 <b>Как использовать:</b>
1. Нажмите кнопку «📊 Рассчитать курсовую разницу»
2. Введите дату в формате дд.мм.гггг (или «сегодня»)
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

# --- ЭТА ФУНКЦИЯ РАБОТАЕТ (из вашего старого кода, через XML ЦБ) ---
async def fetch_cbr_usd_rate(date: datetime) -> dict | None:
    """Получает курс USD с ЦБ РФ на указанную дату (через XML API)"""
    date_str = date.strftime("%d/%m/%Y")
    url = f"{CBR_API_URL}?date_req={date_str}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as response:
                if response.status != 200:
                    logger.error(f"ЦБ: HTTP ошибка {response.status} для {date_str}")
                    return None

                xml_text = await response.text()
                root = ElementTree.fromstring(xml_text)

                for valute in root.findall(".//Valute"):
                    if valute.get("ID") == "R01235":  # USD
                        value = valute.find("Value").text
                        rate = float(value.replace(",", "."))
                        logger.info(f"ЦБ: курс USD = {rate} на {date_str}")
                        return {
                            "rate": rate,
                            "date_str": date.strftime("%d.%m.%Y")
                        }

                logger.warning(f"ЦБ: USD не найден для {date_str}")
                return None

    except Exception as e:
        logger.error(f"ЦБ ошибка для {date_str}: {e}")
        return None


# --- Функция для Rapira через официальное API ---
async def fetch_rapira_usdt_rate() -> float | None:
    """Получает курс USDT/RUB через официальный публичный эндпоинт Rapira"""
    url = "https://api.rapira.net/open/market/rates"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, 
                timeout=10,
                headers={'Accept': 'application/json'}
            ) as response:
                if response.status != 200:
                    logger.error(f"Rapira API: HTTP {response.status}")
                    return None
                
                data = await response.json()
                
                if data.get('code') != 0:
                    logger.error(f"Rapira API: ошибка {data.get('message')}")
                    return None
                
                for item in data.get('data', []):
                    if item.get('symbol') == 'USDT/RUB':
                        rate = item.get('close')
                        if rate:
                            logger.info(f"Rapira API: курс USDT/RUB = {rate}")
                            return float(rate)
                
                logger.warning("Rapira API: USDT/RUB не найден")
                return None
                
    except Exception as e:
        logger.error(f"Rapira API ошибка: {e}")
        return None


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
    if month < 1 or month > 12:
        return None
    if day < 1 or day > 31:
        return None

    try:
        return datetime(year, month, day)
    except ValueError:
        return None


def format_result(date_str: str, cbr_rate: float, rapira_rate: float) -> str:
    diff_percent = ((rapira_rate - cbr_rate) / cbr_rate) * 100
    sign = "+" if diff_percent >= 0 else ""
    
    if diff_percent > 0:
        trend = "📈 Rapira ВЫШЕ ЦБ"
    elif diff_percent < 0:
        trend = "📉 Rapira НИЖЕ ЦБ"
    else:
        trend = "⚖️ Курсы равны"

    return f"""
📊 <b>КУРСОВАЯ РАЗНИЦА</b> на {date_str}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🏦 <b>ЦБ РФ (USD):</b> 1$ = {cbr_rate:.2f} ₽
🔄 <b>Rapira (USDT):</b> 1₮ = {rapira_rate:.2f} ₽
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

💰 <b>Отклонение Rapira от ЦБ:</b> {sign}{diff_percent:.2f}%
💡 {trend}

🔄 Нажмите кнопку для нового расчёта
    """.strip()


# ========== БОТ ==========
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        f"👋 Добро пожаловать!\n\n{HELP_TEXT}",
        reply_markup=get_main_keyboard(),
        parse_mode="HTML"
    )

@dp.message(F.text == "📊 Рассчитать курсовую разницу")
async def start_calculation(message: types.Message, state: FSMContext):
    await state.clear()
    await state.set_state(CurrencyCalc.waiting_for_date)
    await message.answer(
        "📅 Введите <b>дату</b> в формате <code>дд.мм.гггг</code>\n"
        "или напишите <b>«сегодня»</b>:\n\n"
        "<i>Например: 07.05.2026 или сегодня</i>",
        reply_markup=get_back_keyboard(),
        parse_mode="HTML"
    )

@dp.message(F.text == "🔙 Отмена", StateFilter(CurrencyCalc))
async def cancel_calculation(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "❌ Расчёт отменён.\n\nНажмите «📊 Рассчитать курсовую разницу», чтобы начать заново.",
        reply_markup=get_main_keyboard()
    )

@dp.message(CurrencyCalc.waiting_for_date)
async def process_date(message: types.Message, state: FSMContext):
    user_input = message.text.strip()

    if user_input == "🔙 Отмена":
        await cancel_calculation(message, state)
        return

    selected_date = parse_date(user_input)
    if selected_date is None:
        await message.answer(
            "❌ <b>Неверный формат!</b>\n\n"
            "Используйте формат <code>дд.мм.гггг</code> или «сегодня».\n"
            "<i>Пример: 07.05.2026</i>",
            reply_markup=get_back_keyboard(),
            parse_mode="HTML"
        )
        return

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if selected_date > today + timedelta(days=1):
        await message.answer(
            "⚠️ <b>Дата не может быть в будущем!</b>",
            reply_markup=get_back_keyboard(),
            parse_mode="HTML"
        )
        return

    await message.answer("🔄 <i>Получаю курс с ЦБ РФ...</i>", parse_mode="HTML")
    cbr_data = await fetch_cbr_usd_rate(selected_date)

    if cbr_data is None:
        await message.answer(
            "⚠️ <b>Не удалось получить курс ЦБ</b> для указанной даты.\n\n"
            "Попробуйте другую дату или нажмите «Отмена».",
            reply_markup=get_back_keyboard(),
            parse_mode="HTML"
        )
        await state.clear()
        return

    cbr_rate = cbr_data["rate"]
    date_str = cbr_data["date_str"]

    await message.answer(
        f"✅ <b>Курс ЦБ получен:</b> 1$ = {cbr_rate:.2f} ₽\n\n"
        f"🔄 <i>Теперь получаю курс с Rapira...</i>",
        parse_mode="HTML"
    )
    
    rapira_rate = await fetch_rapira_usdt_rate()

    if rapira_rate is None:
        await message.answer(
            f"⚠️ <b>Курс ЦБ получен, но Rapira не отвечает</b>\n\n"
            f"🏦 <b>Курс ЦБ РФ:</b> 1$ = {cbr_rate:.2f} ₽\n\n"
            f"❌ <b>Rapira:</b> не удалось получить курс USDT/RUB\n\n"
            f"Попробуйте позже.",
            reply_markup=get_main_keyboard(),
            parse_mode="HTML"
        )
        await state.clear()
        return

    result_text = format_result(date_str, cbr_rate, rapira_rate)
    await message.answer(result_text, reply_markup=get_main_keyboard(), parse_mode="HTML")
    await state.clear()

@dp.message()
async def handle_unknown(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state == CurrencyCalc.waiting_for_date:
        await message.answer(
            "📅 Пожалуйста, введите дату в формате <code>дд.мм.гггг</code>\n"
            "или нажмите «🔙 Отмена»",
            parse_mode="HTML",
            reply_markup=get_back_keyboard()
        )
    else:
        await message.answer(
            "❓ <b>Неизвестная команда</b>\n\n"
            "Нажмите «📊 Рассчитать курсовую разницу» для начала расчёта\n"
            "или отправьте /help для справки.",
            reply_markup=get_main_keyboard(),
            parse_mode="HTML"
        )

# ========== FLASK ДЛЯ HEALTH CHECK ==========
flask_app = Flask(__name__)

@flask_app.route("/", methods=["GET"])
def health_check():
    return "OK", 200

def run_flask():
    flask_app.run(host="0.0.0.0", port=8080)

# ========== ЗАПУСК ==========
async def main():
    print("🚀 Бот запускается...")
    print("✅ ЦБ РФ: XML API")
    print("✅ Rapira: https://api.rapira.net/open/market/rates")
    try:
        await dp.start_polling(bot)
    except Exception as e:
        print(f"❌ Ошибка при запуске бота: {e}")
        raise

if __name__ == "__main__":
    import threading
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    asyncio.run(main())
