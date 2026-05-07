import os
import sys
import asyncio
import logging
import re
from datetime import datetime

import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from flask import Flask

# ========== НАСТРОЙКИ ==========
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    print("❌ НЕТ ТОКЕНА!")
    sys.exit(1)

CBR_URL = "https://cbr.ru/currency_base/daily/"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
def get_main_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="📊 Рассчитать курсовую разницу"))
    return builder.as_markup(resize_keyboard=True)

def get_back_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="🔙 Отмена"))
    return builder.as_markup(resize_keyboard=True)

# ========== ПАРСИНГ КУРСОВ ==========
async def fetch_cbr_usd_rate(date: datetime) -> float | None:
    """Парсит курс USD с официальной страницы ЦБ РФ на заданную дату"""
    date_str = date.strftime("%d.%m.%Y")
    url = f"{CBR_URL}?date_req={date_str}"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as response:
                if response.status != 200:
                    logger.error(f"ЦБ: HTTP {response.status}")
                    return None
                html = await response.text()
                match = re.search(r'<td>USD<td>\s*<td>1<td>\s*<tr>Доллар США<td>\s*<td>([\d,]+)</tr>', html, re.IGNORECASE | re.DOTALL)
                if not match:
                    logger.warning(f"ЦБ: курс USD не найден для {date_str}")
                    return None
                rate = float(match.group(1).replace(',', '.'))
                logger.info(f"ЦБ: курс USD = {rate} на {date_str}")
                return rate
    except Exception as e:
        logger.error(f"ЦБ ошибка: {e}")
        return None

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
    
    match = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$", user_input.strip())
    if not match:
        return None
    try:
        return datetime(int(match.group(3)), int(match.group(2)), int(match.group(1)))
    except:
        return None

def format_result(date_str: str, cbr_rate: float, rapira_rate: float) -> str:
    diff_percent = ((rapira_rate - cbr_rate) / cbr_rate) * 100
    sign = "+" if diff_percent >= 0 else ""
    
    if diff_percent > 0:
        trend = "Rapira ВЫШЕ ЦБ"
    elif diff_percent < 0:
        trend = "Rapira НИЖЕ ЦБ"
    else:
        trend = "Курсы равны"
    
    return f"""
📊 <b>КУРСОВАЯ РАЗНИЦА</b> на {date_str}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🏦 <b>ЦБ РФ (USD):</b> 1$ = {cbr_rate:.2f} ₽
🔄 <b>Rapira (USDT):</b> 1₮ = {rapira_rate:.2f} ₽
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

💰 <b>Отклонение Rapira от ЦБ:</b> {sign}{diff_percent:.2f}%
📈 <b>Тенденция:</b> {trend}

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
    await state.set_state(CurrencyCalc.waiting_for_date)
    await message.answer(
        "📅 Введите дату (дд.мм.гггг) или «сегодня»:\n\n"
        "<i>Пример: 07.05.2026</i>",
        reply_markup=get_back_keyboard(),
        parse_mode="HTML"
    )

@dp.message(F.text == "🔙 Отмена", StateFilter(CurrencyCalc.waiting_for_date))
async def cancel_calculation(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Расчёт отменён", reply_markup=get_main_keyboard())

@dp.message(CurrencyCalc.waiting_for_date)
async def process_date(message: types.Message, state: FSMContext):
    selected_date = parse_date(message.text)
    if not selected_date:
        await message.answer(
            "❌ Неверный формат!\n\n"
            "Используйте: <code>дд.мм.гггг</code>\n"
            "Пример: <code>07.05.2026</code>",
            reply_markup=get_back_keyboard(),
            parse_mode="HTML"
        )
        return
    
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if selected_date > today:
        await message.answer(
            "❌ Дата не может быть в будущем!\n\n"
            f"Сегодня: {today.strftime('%d.%m.%Y')}",
            reply_markup=get_back_keyboard()
        )
        return
    
    # Отправляем первое сообщение о начале проверки
    status_msg = await message.answer("🔍 <b>Начинаю проверку курсов...</b>\n\n⏳ Это может занять несколько секунд...", parse_mode="HTML")
    
    # Получаем курс ЦБ
    await status_msg.edit_text("🔍 <b>Проверяю курс ЦБ РФ...</b>\n\n🏦 Запрашиваю курс доллара США...", parse_mode="HTML")
    cbr_rate = await fetch_cbr_usd_rate(selected_date)
    
    if cbr_rate is None:
        await status_msg.edit_text(
            f"❌ <b>Не удалось получить курс ЦБ РФ</b>\n\n"
            f"📅 Дата: {selected_date.strftime('%d.%m.%Y')}\n"
            f"⚠️ Возможные причины:\n"
            f"• Слишком старая дата (до 1992 года)\n"
            f"• Выходной/праздничный день (курс не публиковался)\n"
            f"• Проблемы с сайтом ЦБ\n\n"
            f"💡 Попробуйте другую дату или нажмите «Отмена»",
            reply_markup=get_main_keyboard(),
            parse_mode="HTML"
        )
        await state.clear()
        return
    
    # Курс ЦБ получен успешно
    await status_msg.edit_text(
        f"✅ <b>Курс ЦБ РФ получен!</b>\n\n"
        f"🏦 <b>USD/RUB на {selected_date.strftime('%d.%m.%Y')}:</b>\n"
        f"💵 1$ = {cbr_rate:.2f} ₽\n\n"
        f"🔄 <b>Теперь проверяю курс Rapira...</b>",
        parse_mode="HTML"
    )
    
    # Получаем курс Rapira
    rapira_rate = await fetch_rapira_usdt_rate()
    
    if rapira_rate is None:
        await status_msg.edit_text(
            f"⚠️ <b>Курс ЦБ РФ получен, но Rapira не отвечает</b>\n\n"
            f"🏦 <b>Курс ЦБ РФ:</b> 1$ = {cbr_rate:.2f} ₽\n\n"
            f"❌ <b>Rapira:</b> не удалось получить курс USDT/RUB\n\n"
            f"Возможные причины:\n"
            f"• Проблемы с API Rapira\n"
            f"• Технические работы на бирже\n"
            f"• Превышен лимит запросов\n\n"
            f"💡 Попробуйте позже или нажмите кнопку для нового расчёта",
            reply_markup=get_main_keyboard(),
            parse_mode="HTML"
        )
        await state.clear()
        return
    
    # Оба курса получены успешно
    result_text = format_result(selected_date.strftime("%d.%m.%Y"), cbr_rate, rapira_rate)
    
    await status_msg.delete()  # Удаляем сообщение о статусе
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
            "❓ Неизвестная команда\n\n"
            "Нажмите «📊 Рассчитать курсовую разницу» для расчёта",
            reply_markup=get_main_keyboard()
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
    print("🚀 Бот запущен!")
    print("✅ Используется официальное API Rapira: https://api.rapira.net/open/market/rates")
    try:
        await dp.start_polling(bot)
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        raise

if __name__ == "__main__":
    import threading
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.run(main())
