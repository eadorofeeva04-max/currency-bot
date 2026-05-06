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
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from flask import Flask, request

# ========== НАСТРОЙКИ ==========
BOT_TOKEN = "ВАШ_ТОКЕН_ОТ_BOTFATHER"  # Замените на реальный токен!
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
4. Получите результат: абсолютную и процентную разницу

⚠️ <b>Важно:</b>
• Доступны даты с 1998 года по сегодня
• Используйте формат ДЕНЬ.МЕСЯЦ.ГОД (например: 15.08.2024)
• Бот не хранит историю ваших расчётов
"""

# ========== FSM СОСТОЯНИЯ ==========
class CurrencyCalc(StatesGroup):
    waiting_for_first_date = State()   # Ждём дату поставки
    waiting_for_second_date = State()  # Ждём дату оплаты


# ========== КЛАВИАТУРЫ ==========
def get_main_keyboard() -> ReplyKeyboardMarkup:
    """Главная клавиатура с кнопкой расчёта"""
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="📊 Рассчитать курсовую разницу"))
    builder.adjust(1)
    return builder.as_markup(resize_keyboard=True)


def get_back_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура с кнопкой отмены"""
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="🔙 Отмена"))
    return builder.as_markup(resize_keyboard=True)


# ========== ФУНКЦИИ РАБОТЫ С КУРСОМ ==========
async def fetch_usd_rate(date: datetime) -> dict | None:
    """
    Получает курс USD на указанную дату с API ЦБ РФ.
    Возвращает dict: {'rate': float, 'date_str': str} или None при ошибке.
    """
    # Форматируем дату для API ЦБ: день/месяц/год
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

                # Ищем элемент Valute с ID="R01235" (USD)
                for valute in root.findall(".//Valute"):
                    if valute.get("ID") == "R01235":
                        value = valute.find("Value").text
                        # Заменяем запятую на точку для преобразования в float
                        rate = float(value.replace(",", "."))
                        return {
                            "rate": rate,
                            "date_str": date.strftime("%d.%m.%Y")
                        }

                logger.warning(f"USD не найден в XML для даты {date_str}")
                return None

    except aiohttp.ClientError as e:
        logger.error(f"Ошибка сети для даты {date_str}: {e}")
        return None
    except (ElementTree.ParseError, ValueError, AttributeError) as e:
        logger.error(f"Ошибка парсинга XML для даты {date_str}: {e}")
        return None


async def get_reserve_rate() -> dict | None:
    """
    Резервный источник курса USDT/RUB с Rapira.io.
    Используется только если ЦБ РФ недоступен.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.rapira.io/v1/rates/USDTRUB",
                timeout=5
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    rate = float(data.get("rate", 0))
                    if rate > 0:
                        return {
                            "rate": rate,
                            "source": "Rapira.io (USDT/RUB)"
                        }
    except Exception as e:
        logger.error(f"Ошибка получения резервного курса: {e}")
    return None


def parse_date(user_input: str) -> datetime | None:
    """
    Парсит дату из строки формата ДД.ММ.ГГГГ или слово "сегодня".
    Возвращает datetime или None при ошибке.
    """
    if user_input.lower() in ["сегодня", "today"]:
        return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    # Проверяем формат дд.мм.гггг
    pattern = r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$"
    match = re.match(pattern, user_input.strip())

    if not match:
        return None

    day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))

    # Базовая валидация
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
    """Форматирует результат расчёта в красивое сообщение"""
    diff_abs = rate2 - rate1
    diff_percent = (diff_abs / rate1) * 100 if rate1 != 0 else 0
    sign = "+" if diff_abs >= 0 else ""

    # Интерпретация
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


# ========== ОБРАБОТЧИКИ БОТА ==========
async def safe_send_message(
    message: types.Message,
    text: str,
    reply_markup=None,
    parse_mode="HTML"
):
    """Безопасная отправка сообщения с обработкой ошибок"""
    try:
        await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения: {e}")
        await message.answer(text, reply_markup=reply_markup)


@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    """Обработчик команды /start"""
    await state.clear()
    await safe_send_message(
        message,
        f"👋 <b>Добро пожаловать!</b>\n\n{HELP_TEXT}",
        reply_markup=get_main_keyboard()
    )


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    """Обработчик команды /help"""
    await safe_send_message(message, HELP_TEXT, reply_markup=get_main_keyboard())


@dp.message(F.text == "📊 Рассчитать курсовую разницу")
async def start_calculation(message: types.Message, state: FSMContext):
    """Запуск сценария расчёта"""
    await state.clear()
    await state.set_state(CurrencyCalc.waiting_for_first_date)
    await safe_send_message(
        message,
        "📅 Введите <b>дату поставки</b> в формате <code>дд.мм.гггг</code>\n"
        "или напишите <b>«сегодня»</b>:\n\n"
        "<i>Например: 15.03.2026 или сегодня</i>",
        reply_markup=get_back_keyboard()
    )


@dp.message(F.text == "🔙 Отмена", StateFilter(CurrencyCalc))
async def cancel_calculation(message: types.Message, state: FSMContext):
    """Отмена текущего расчёта"""
    await state.clear()
    await safe_send_message(
        message,
        "❌ Расчёт отменён.\n\nНажмите «📊 Рассчитать курсовую разницу», чтобы начать заново.",
        reply_markup=get_main_keyboard()
    )


@dp.message(CurrencyCalc.waiting_for_first_date)
async def process_first_date(message: types.Message, state: FSMContext):
    """Обработка первой даты (поставка)"""
    user_input = message.text.strip()

    # Проверяем, не хочет ли пользователь отменить
    if user_input == "🔙 Отмена":
        await cancel_calculation(message, state)
        return

    # Парсим дату
    first_date = parse_date(user_input)
    if first_date is None:
        await safe_send_message(
            message,
            "❌ <b>Неверный формат!</b>\n\n"
            "Используйте формат <code>дд.мм.гггг</code>\n"
            "или напишите <b>«сегодня»</b>.\n\n"
            "<i>Пример: 01.03.2026</i>",
            reply_markup=get_back_keyboard()
        )
        return

    # Проверяем, что дата не в будущем (более чем на 1 день)
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if first_date > today + timedelta(days=1):
        await safe_send_message(
            message,
            "⚠️ <b>Дата не может быть в будущем!</b>\n\n"
            "Введите корректную дату или «сегодня».",
            reply_markup=get_back_keyboard()
        )
        return

    # Отправляем статус "печатает"
    await message.bot.send_chat_action(message.chat.id, "typing")

    # Получаем курс
    await safe_send_message(message, "🔄 <i>Получаю курс с ЦБ РФ...</i>")
    rate_data = await fetch_usd_rate(first_date)

    if rate_data is None:
        # Пробуем резервный источник
        reserve = await get_reserve_rate()
        if reserve:
            await safe_send_message(
                message,
                f"⚠️ <b>Официальный курс ЦБ не найден.</b>\n"
                f"💰 <b>Актуальный резервный курс (USDT/RUB):</b> 1$ ≈ {reserve['rate']:.2f} ₽\n\n"
                f"<i>Источник: {reserve['source']}</i>\n\n"
                f"Для расчёта по точным датам проверьте правильность ввода.",
                reply_markup=get_main_keyboard()
            )
            await state.clear()
            return
        else:
            await safe_send_message(
                message,
                "⚠️ <b>Не удалось получить курс</b> для указанной даты.\n\n"
                "Возможные причины:\n"
                "• Дата слишком старая (до 1998 года)\n"
                "• Нерабочий день (курс не публиковался)\n"
                "• Проблемы с API ЦБ\n\n"
                "Попробуйте ввести другую дату или нажмите «Отмена».",
                reply_markup=get_back_keyboard()
            )
            return

    # Сохраняем данные первой даты
    await state.update_data(
        first_rate=rate_data["rate"],
        first_date_str=rate_data["date_str"]
    )
    await state.set_state(CurrencyCalc.waiting_for_second_date)

    await safe_send_message(
        message,
        f"✅ <b>Курс на {rate_data['date_str']}:</b> 1$ = {rate_data['rate']:.2f} ₽\n\n"
        f"📅 Теперь введите <b>дату оплаты</b> в формате <code>дд.мм.гггг</code>\n"
        f"или напишите <b>«сегодня»</b>:",
        reply_markup=get_back_keyboard()
    )


@dp.message(CurrencyCalc.waiting_for_second_date)
async def process_second_date(message: types.Message, state: FSMContext):
    """Обработка второй даты (оплата)"""
    user_input = message.text.strip()

    if user_input == "🔙 Отмена":
        await cancel_calculation(message, state)
        return

    # Парсим дату
    second_date = parse_date(user_input)
    if second_date is None:
        await safe_send_message(
            message,
            "❌ <b>Неверный формат!</b>\n\n"
            "Используйте формат <code>дд.мм.гггг</code>\n"
            "или напишите <b>«сегодня»</b>.",
            reply_markup=get_back_keyboard()
        )
        return

    # Проверяем, что дата не в будущем
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if second_date > today + timedelta(days=1):
        await safe_send_message(
            message,
            "⚠️ <b>Дата не может быть в будущем!</b>",
            reply_markup=get_back_keyboard()
        )
        return

    # Получаем данные первой даты из состояния
    data = await state.get_data()
    if not data:
        await safe_send_message(
            message,
            "❌ <b>Ошибка:</b> данные первой даты утеряны.\n"
            "Начните расчёт заново.",
            reply_markup=get_main_keyboard()
        )
        await state.clear()
        return

    first_rate = data["first_rate"]
    first_date_str = data["first_date_str"]

    # Отправляем статус
    await message.bot.send_chat_action(message.chat.id, "typing")
    await safe_send_message(message, "🔄 <i>Получаю курс с ЦБ РФ...</i>")

    # Получаем курс для второй даты
    rate_data = await fetch_usd_rate(second_date)

    if rate_data is None:
        await safe_send_message(
            message,
            f"⚠️ <b>Не удалось получить курс</b> для даты {second_date.strftime('%d.%m.%Y')}.\n\n"
            f"Курс на дату поставки: 1$ = {first_rate:.2f} ₽\n\n"
            f"Попробуйте ввести другую дату или нажмите «Отмена».",
            reply_markup=get_back_keyboard()
        )
        return

    second_rate = rate_data["rate"]
    second_date_str = rate_data["date_str"]

    # Формируем и отправляем результат
    result_text = format_result(first_rate, first_date_str, second_rate, second_date_str)
    await safe_send_message(message, result_text, reply_markup=get_main_keyboard())

    # Очищаем состояние
    await state.clear()


@dp.message()
async def handle_unknown(message: types.Message):
    """Обработка неизвестных сообщений"""
    await safe_send_message(
        message,
        "❓ <b>Неизвестная команда</b>\n\n"
        "Нажмите «📊 Рассчитать курсовую разницу» для начала расчёта\n"
        "или отправьте /help для справки.",
        reply_markup=get_main_keyboard()
    )


# ========== FLASK ДЛЯ HEALTH CHECK НА RENDER ==========
flask_app = Flask(__name__)


@flask_app.route("/", methods=["GET"])
def health_check():
    """Health check эндпоинт для Render"""
    return "OK", 200


def run_flask():
    """Запуск Flask-сервера для health check"""
    flask_app.run(host="0.0.0.0", port=8080)


# ========== ЗАПУСК БОТА ==========
async def main():
    """Главная асинхронная функция"""
    # Создаём бота и диспетчер
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    # Регистрируем все хендлеры
    dp.message.register(cmd_start, Command("start"))
    dp.message.register(cmd_help, Command("help"))
    dp.message.register(start_calculation, F.text == "📊 Рассчитать курсовую разницу")
    dp.message.register(cancel_calculation, F.text == "🔙 Отмена", StateFilter(CurrencyCalc))
    dp.message.register(process_first_date, CurrencyCalc.waiting_for_first_date)
    dp.message.register(process_second_date, CurrencyCalc.waiting_for_second_date)
    dp.message.register(handle_unknown)

    # Запускаем polling
    await dp.start_polling(bot)


if __name__ == "__main__":
    # Запускаем Flask в отдельном потоке для health check
    import threading
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Запускаем бота
    asyncio.run(main())
