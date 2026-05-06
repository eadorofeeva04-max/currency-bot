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

CBR_API_URL = "https://www.cbr.ru/scripts/XML_daily.asp"

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ========== КОНСТАНТЫ ==========
HELP_TEXT = """
🏦 <b>Калькулятор курсовой разницы USD/RUB</b>

Бот рассчитывает изменение курса доллара США к рублю между двумя датами на основе официальных данных ЦБ РФ.

📌 <b>Как пользоваться:</b>
1. Нажмите кнопку «📊 Рассчитать курсовую разницу»
2. Введите дату поставки (формат: 01.03.2026 или «сегодня»)
3. Введите дату оплаты (тот же формат)
4. Получите результат

⚠️ <b>Важно:</b>
• Доступны даты с 1998 года
• Формат: ДЕНЬ.МЕСЯЦ.ГОД (например: 15.08.2024)
"""

# ========== FSM СОСТОЯНИЯ ==========
class CurrencyCalc(StatesGroup):
    waiting_for_first_date = State()
    waiting_for_second_date = State()


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


# ========== ФУНКЦИИ РАБОТЫ С КУРСОМ ==========
async def fetch_usd_rate(date: datetime) -> dict | None:
    date_str = date.strftime("%d/%m/%Y")
    url = f"{CBR_API_URL}?date_req={date_str}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as response:
                if response.status != 200:
                    logger.error(f"HTTP ошибка {response.status} для даты {date_str}")
                    return None

                xml_text = await response.text()
                root = ElementTree.fromstring(xml_text)

                for valute in root.findall(".//Valute"):
                    if valute.get("ID") == "R01235":
                        value = valute.find("Value").text
                        rate = float(value.replace(",", "."))
                        return {
                            "rate": rate,
                            "date_str": date.strftime("%d.%m.%Y")
                        }

                logger.warning(f"USD не найден для даты {date_str}")
                return None

    except Exception as e:
        logger.error(f"Ошибка получения курса для {date_str}: {e}")
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


def format_result(rate1: float, date1_str: str, rate2: float, date2_str: str) -> str:
    diff_abs = rate2 - rate1
    diff_percent = (diff_abs / rate1) * 100 if rate1 != 0 else 0
    sign = "+" if diff_abs >= 0 else ""

    if diff_abs > 0:
        interpretation = "📈 <b>Курс вырос</b> → стоимость закупки увеличилась"
    elif diff_abs < 0:
        interpretation = "📉 <b>Курс упал</b> → вы получили прибыль на разнице"
    else:
        interpretation = "⚖️ <b>Курс не изменился</b>"

    result_text = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 <b>РЕЗУЛЬТАТ РАСЧЕТА</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📅 <b>Дата 1 (Поставка):</b> {date1_str}
   💵 1$ = {rate1:.2f} ₽

📅 <b>Дата 2 (Оплата):</b> {date2_str}
   💵 1$ = {rate2:.2f} ₽

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📈 <b>ИЗМЕНЕНИЕ:</b>
💰 <b>Разница:</b> {sign}{diff_abs:.2f} ₽
📊 <b>% изменения:</b> {sign}{diff_percent:.2f}%
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

💡 {interpretation}

🔄 Нажмите кнопку меню для нового расчёта
"""
    return result_text.strip()


# ========== СОЗДАНИЕ БОТА И ДИСПЕТЧЕРА ==========
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# ========== ОБРАБОТЧИКИ ==========
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        f"👋 <b>Добро пожаловать!</b>\n\n{HELP_TEXT}",
        reply_markup=get_main_keyboard(),
        parse_mode="HTML"
    )


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(HELP_TEXT, reply_markup=get_main_keyboard(), parse_mode="HTML")


@dp.message(F.text == "📊 Рассчитать курсовую разницу")
async def start_calculation(message: types.Message, state: FSMContext):
    await state.clear()
    await state.set_state(CurrencyCalc.waiting_for_first_date)
    await message.answer(
        "📅 Введите <b>дату поставки</b> в формате <code>дд.мм.гггг</code>\n"
        "или напишите <b>«сегодня»</b>:\n\n"
        "<i>Например: 15.03.2026 или сегодня</i>",
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


@dp.message(CurrencyCalc.waiting_for_first_date)
async def process_first_date(message: types.Message, state: FSMContext):
    user_input = message.text.strip()

    if user_input == "🔙 Отмена":
        await cancel_calculation(message, state)
        return

    first_date = parse_date(user_input)
    if first_date is None:
        await message.answer(
            "❌ <b>Неверный формат!</b>\n\n"
            "Используйте формат <code>дд.мм.гггг</code> или «сегодня».\n"
            "<i>Пример: 01.03.2026</i>",
            reply_markup=get_back_keyboard(),
            parse_mode="HTML"
        )
        return

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if first_date > today + timedelta(days=1):
        await message.answer(
            "⚠️ <b>Дата не может быть в будущем!</b>",
            reply_markup=get_back_keyboard(),
            parse_mode="HTML"
        )
        return

    await message.answer("🔄 <i>Получаю курс с ЦБ РФ...</i>", parse_mode="HTML")
    rate_data = await fetch_usd_rate(first_date)

    if rate_data is None:
        await message.answer(
            "⚠️ <b>Не удалось получить курс</b> для указанной даты.\n\n"
            "Попробуйте другую дату или нажмите «Отмена».",
            reply_markup=get_back_keyboard(),
            parse_mode="HTML"
        )
        return

    await state.update_data(
        first_rate=rate_data["rate"],
        first_date_str=rate_data["date_str"]
    )
    await state.set_state(CurrencyCalc.waiting_for_second_date)

    await message.answer(
        f"✅ <b>Курс на {rate_data['date_str']}:</b> 1$ = {rate_data['rate']:.2f} ₽\n\n"
        f"📅 Теперь введите <b>дату оплаты</b> в формате <code>дд.мм.гггг</code>\n"
        f"или напишите <b>«сегодня»</b>:",
        reply_markup=get_back_keyboard(),
        parse_mode="HTML"
    )


@dp.message(CurrencyCalc.waiting_for_second_date)
async def process_second_date(message: types.Message, state: FSMContext):
    user_input = message.text.strip()

    if user_input == "🔙 Отмена":
        await cancel_calculation(message, state)
        return

    second_date = parse_date(user_input)
    if second_date is None:
        await message.answer(
            "❌ <b>Неверный формат!</b>\n\n"
            "Используйте формат <code>дд.мм.гггг</code> или «сегодня».",
            reply_markup=get_back_keyboard(),
            parse_mode="HTML"
        )
        return

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if second_date > today + timedelta(days=1):
        await message.answer(
            "⚠️ <b>Дата не может быть в будущем!</b>",
            reply_markup=get_back_keyboard(),
            parse_mode="HTML"
        )
        return

    data = await state.get_data()
    if not data:
        await message.answer(
            "❌ <b>Ошибка:</b> данные утеряны. Начните заново.",
            reply_markup=get_main_keyboard()
        )
        await state.clear()
        return

    first_rate = data["first_rate"]
    first_date_str = data["first_date_str"]

    await message.answer("🔄 <i>Получаю курс с ЦБ РФ...</i>", parse_mode="HTML")
    rate_data = await fetch_usd_rate(second_date)

    if rate_data is None:
        await message.answer(
            f"⚠️ <b>Не удалось получить курс</b> для этой даты.\n\n"
            f"Курс на дату поставки: 1$ = {first_rate:.2f} ₽\n"
            f"Попробуйте другую дату.",
            reply_markup=get_back_keyboard(),
            parse_mode="HTML"
        )
        return

    result_text = format_result(first_rate, first_date_str, rate_data["rate"], rate_data["date_str"])
    await message.answer(result_text, reply_markup=get_main_keyboard(), parse_mode="HTML")
    await state.clear()


@dp.message()
async def handle_unknown(message: types.Message):
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
    try:
        await dp.start_polling(bot)
    except Exception as e:
        print(f"❌ Ошибка при запуске бота: {e}")
        raise

if __name__ == "__main__":
    # Запускаем Flask в отдельном потоке
    import threading
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # Запускаем бота
    asyncio.run(main())
