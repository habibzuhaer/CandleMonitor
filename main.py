import ccxt
import os
import asyncio
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes
)

# Загрузка переменных окружения
load_dotenv()

# ================== НАСТРОЙКИ ==================
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
BYBIT_API_KEY = os.getenv('BYBIT_API_KEY', '')
BYBIT_API_SECRET = os.getenv('BYBIT_API_SECRET', '')

SYMBOLS = {
    'ADA': 'ADA/USDT:USDT',
    'ETH': 'ETH/USDT:USDT',
    'SUI': 'SUI/USDT:USDT',
    'INJ': 'INJ/USDT:USDT'
}
TIMEFRAME = '15m'
MIN_PERCENT_CHANGE = 1.67
CANDLES_LIMIT = 200
CHECK_INTERVAL = 30
MIN_MESSAGE_INTERVAL = 300  # 5 минут между похожими сообщениями
SIMILARITY_THRESHOLD = 0.8  # Порог схожести свечей (0.8 = 80%)
# ===============================================

# Инициализация биржи
exchange = ccxt.bybit({
    'apiKey': BYBIT_API_KEY,
    'secret': BYBIT_API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'}
})

# Хранилище последних сообщений
message_history = {}


def format_time(dt):
    return dt.strftime('%Y-%m-%d %H:%M') + ' UTC'


def is_similar(candle1, candle2):
    """Проверка схожести двух свечей"""
    if candle1 is None or candle2 is None:
        return False

    # Сравниваем процент изменения и объем
    change_diff = abs(candle1['change'] - candle2['change']) / max(abs(candle1['change']), 1)
    volume_diff = abs(candle1['volume'] - candle2['volume']) / max(candle1['volume'], 1)

    return (change_diff < 0.2 and volume_diff < 0.3)  # 20% разницы в изменении, 30% в объеме


async def get_significant_candle(symbol):
    """Получение последней значительной свечи с проверкой частоты"""
    try:
        candles = await asyncio.to_thread(
            exchange.fetch_ohlcv, symbol, TIMEFRAME, limit=CANDLES_LIMIT
        )

        if not candles:
            return None

        # Получаем последнюю свечу
        last_candle = candles[-1]
        change = (last_candle[4] - last_candle[1]) / last_candle[1] * 100

        if abs(change) < MIN_PERCENT_CHANGE:
            return None

        candle_data = {
            'symbol': symbol,
            'time': datetime.fromtimestamp(last_candle[0] / 1000, timezone.utc),
            'open': last_candle[1],
            'high': last_candle[2],
            'low': last_candle[3],
            'close': last_candle[4],
            'change': change,
            'volume': last_candle[5]
        }

        # Проверяем историю сообщений
        now = datetime.now(timezone.utc)
        last_message = message_history.get(symbol)

        if last_message:
            time_diff = (now - last_message['time']).total_seconds()
            if time_diff < MIN_MESSAGE_INTERVAL and is_similar(last_message['candle'], candle_data):
                return None

        return candle_data

    except Exception as e:
        print(f"Ошибка получения данных для {symbol}: {str(e)}")
        return None


def create_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(name, callback_data=f"req_{symbol}")]
        for name, symbol in SYMBOLS.items()
    ])


async def send_candle_message(chat_id, candle_data, context=None, is_update=False):
    """Отправка/обновление сообщения о свече"""
    if not candle_data:
        return

    symbol = candle_data['symbol']
    direction = "🟢" if candle_data['change'] >= 0 else "🔴"

    message = (
        f"<b>{direction} {symbol.split(':')[0]} {abs(candle_data['change']):.2f}%</b>\n"
        f"┌ Время: <i>{format_time(candle_data['time'])}</i>\n"
        f"├ Цена: <b>{candle_data['close']:.4f}</b>\n"
        f"├ Объем: {candle_data['volume']:.2f} USDT\n"
        f"└ Диапазон: {candle_data['low']:.4f}-{candle_data['high']:.4f}"
    )

    # Обновляем историю сообщений
    message_history[symbol] = {
        'time': datetime.now(timezone.utc),
        'candle': candle_data
    }

    if is_update and symbol in message_history:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_history[symbol].get('message_id'),
                text=message,
                parse_mode='HTML',
                reply_markup=create_keyboard()
            )
            return
        except:
            pass

    sent = await context.bot.send_message(
        chat_id=chat_id,
        text=message,
        parse_mode='HTML',
        reply_markup=create_keyboard()
    )
    message_history[symbol]['message_id'] = sent.message_id


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка команды /start"""
    await update.message.reply_text(
        text="📊 Выберите пару для запроса последней крупной свечи (>1.67%):",
        reply_markup=create_keyboard()
    )


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатия кнопок"""
    query = update.callback_query
    await query.answer()

    if query.data.startswith('req_'):
        symbol = query.data.split('_')[1]
        candle_data = await get_significant_candle(symbol)

        if not candle_data:
            await query.edit_message_text(
                text=f"❌ Для {symbol} нет свечей >{MIN_PERCENT_CHANGE}%",
                reply_markup=create_keyboard()
            )
            return

        await send_candle_message(
            chat_id=query.message.chat_id,
            candle_data=candle_data,
            context=context,
            is_update=True
        )


async def check_market_updates(context: ContextTypes.DEFAULT_TYPE):
    """Проверка обновлений рынка с защитой от дублирования"""
    for symbol in SYMBOLS.values():
        candle_data = await get_significant_candle(symbol)
        if candle_data:
            await send_candle_message(
                chat_id=TELEGRAM_CHAT_ID,
                candle_data=candle_data,
                context=context,
                is_update=True
            )


async def init_bot(application):
    """Инициализация бота"""
    await application.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=f"🔔 Бот активирован | TF: {TIMEFRAME} | >{MIN_PERCENT_CHANGE}%"
    )
    application.job_queue.run_repeating(
        check_market_updates,
        interval=CHECK_INTERVAL,
        first=10
    )


def run_bot():
    """Запуск бота"""
    app = ApplicationBuilder() \
        .token(TELEGRAM_BOT_TOKEN) \
        .post_init(init_bot) \
        .build()

    app.add_handler(CommandHandler('start', start))
    app.add_handler(CallbackQueryHandler(handle_button))

    print(f"Бот запущен. Минимальное изменение: {MIN_PERCENT_CHANGE}%")
    print(f"Защита от дублирования: {MIN_MESSAGE_INTERVAL} сек")
    app.run_polling()


if __name__ == "__main__":
    run_bot()