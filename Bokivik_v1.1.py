# 📦 Полностью обновлённый бот для торговли в боковике с фазами, фильтрацией и Telegram-отчётами

import time
import math
import warnings
import json
import traceback
from datetime import datetime, timezone
import pandas as pd
from ta.trend import adx
from ta.momentum import RSIIndicator
from binance.client import Client
from binance.enums import ORDER_TYPE_MARKET, ORDER_TYPE_LIMIT, SIDE_BUY, SIDE_SELL
import telebot

warnings.filterwarnings("ignore", category=RuntimeWarning)

# КЛЮЧИ (оставлены как есть)
API_KEY = "RyBmhW46P2FrvDffq9V7xTZudskS3aBfWKU8ZnrtNjwkBA5DSzcpXDB41xITuywM"
API_SECRET = "J5VezQjvaM0wDYHpvbnesPMi7g6uy9HMlxiMq2DpAXlPkOxPXo8Nlzc8mGLsDODo"
TELEGRAM_TOKEN = "7915214060:AAEOeRNRHpQClOc1_8K3GOHkQVBKv7RgVL0"
TELEGRAM_CHAT_ID = "349999939"

ALLOWED_SYMBOLS = [
    'DOGEUSDT', 'TRXUSDT', 'XRPUSDT', 'BLZUSDT', 'HOOKUSDT',
    'ACHUSDT', 'AGIXUSDT', 'COTIUSDT', 'BICOUSDT', 'LINAUSDT',
    'LOOMUSDT', 'CELRUSDT'
]

RISK_PER_TRADE = 3
LEVERAGE = 10
CHECK_INTERVAL = 60
ORDER_TYPE_STOP_MARKET = 'STOP_MARKET'

bot = telebot.TeleBot(TELEGRAM_TOKEN)

try:
    client = Client(API_KEY, API_SECRET)
    client.ping()
    bot.send_message(TELEGRAM_CHAT_ID, "🤖 Бот запущен и подключен к Binance API!")
except Exception as e:
    bot.send_message(TELEGRAM_CHAT_ID, f"❌ Ошибка подключения к Binance: {e}")
    raise SystemExit(e)

symbol_info = {}
market_state = {}  # flat / trend
active_positions = {}
cooldowns = {}

try:
    with open("positions.json", "r") as f:
        active_positions = json.load(f)
except:
    pass

last_reconnect_time = 0

def send_message(text):
    bot.send_message(TELEGRAM_CHAT_ID, text)

def load_symbol_info():
    exchange_info = client.futures_exchange_info()
    for s in exchange_info['symbols']:
        symbol = s['symbol']
        step = tick = 0.0
        for f in s['filters']:
            if f['filterType'] == 'LOT_SIZE':
                step = float(f['stepSize'])
            if f['filterType'] == 'PRICE_FILTER':
                tick = float(f['tickSize'])
        symbol_info[symbol] = {'stepSize': step, 'tickSize': tick}

def round_step(value, step):
    return round(math.floor(value / step) * step, 8)

def get_klines(symbol, interval='1h', limit=50):
    data = client.futures_klines(symbol=symbol, interval=interval, limit=limit)
    df = pd.DataFrame(data)
    df.columns = ['time','open','high','low','close','volume','close_time','qav','num_trades','taker_base_vol','taker_quote_vol','ignore']
    df['close'] = df['close'].astype(float)
    df['high'] = df['high'].astype(float)
    df['low'] = df['low'].astype(float)
    df['open'] = df['open'].astype(float)
    return df

def is_flat(df):
    df['ADX'] = adx(df['high'], df['low'], df['close'], window=14)
    df['RSI'] = RSIIndicator(df['close'], window=14).rsi()

    ema_fast = df['close'].ewm(span=9).mean()
    ema_slow = df['close'].ewm(span=21).mean()
    ema_diff = abs(ema_fast.iloc[-1] - ema_fast.iloc[-5])

    if df['ADX'].isna().any() or df['RSI'].isna().any():
        return False

    adx_val = df['ADX'].dropna().iloc[-1]
    rsi_val = df['RSI'].dropna().iloc[-1]

    return adx_val < 28 and 30 < rsi_val < 70 and ema_diff < 0.005

def detect_range(df):
    recent = df[-20:]
    support = recent['low'].min()
    resistance = recent['high'].max()
    return support, resistance

def get_price(symbol):
    return float(client.futures_ticker(symbol=symbol)['lastPrice'])

def calculate_tp_sl(entry, direction, symbol):
    sl_percent = 0.015
    tp_percent = 0.045
    qty = (RISK_PER_TRADE / (entry * sl_percent)) * LEVERAGE
    tick = symbol_info[symbol]['tickSize']
    step = symbol_info[symbol]['stepSize']
    qty = round_step(qty, step)

    if direction == 'long':
        tp = entry * (1 + tp_percent)
        sl = entry * (1 - sl_percent)
    else:
        tp = entry * (1 - tp_percent)
        sl = entry * (1 + sl_percent)

    tp = round_step(tp, tick)
    sl = round_step(sl, tick)
    return tp, sl, qty

def place_order(symbol, side, qty, sl, tp):
    try:
        client.futures_create_order(symbol=symbol, side=SIDE_BUY if side=='long' else SIDE_SELL,
            type=ORDER_TYPE_MARKET, quantity=qty)

        client.futures_create_order(symbol=symbol, side=SIDE_SELL if side=='long' else SIDE_BUY,
            type=ORDER_TYPE_STOP_MARKET, stopPrice=round(sl, 4), closePosition=True, timeInForce='GTC', reduceOnly=True)

        client.futures_create_order(symbol=symbol, side=SIDE_SELL if side=='long' else SIDE_BUY,
            type=ORDER_TYPE_LIMIT, price=round(tp, 4), timeInForce='GTC', reduceOnly=True, quantity=qty)

        return True
    except Exception as e:
        send_message(f"❌ Ошибка при размещении ордера: {e}")
        return False

def analyze_symbol(symbol):
    if cooldowns.get(symbol) and time.time() < cooldowns[symbol]:
        return
    df = get_klines(symbol)
    entry = get_price(symbol)
    support, resistance = detect_range(df)
    flat = is_flat(df)

    state = "flat" if flat else "trend"
    if market_state.get(symbol) != state:
        market_state[symbol] = state
        send_message(f"🔁 {symbol}: теперь фаза — {state.upper()}")

    if not flat:
        return

    if resistance - support < entry * 0.01:
        send_message(f"⚠️ {symbol}: рендж слишком узкий ({support:.4f}–{resistance:.4f}) — пропущено")
        return

    direction = None
    if entry <= support * 1.003:
        direction = 'long'
    elif entry >= resistance * 0.997:
        direction = 'short'
    else:
        send_message(f"⏳ {symbol}: во флэте, но цена в центре ({entry:.4f}) — ждём касания границы")
        return

    tp, sl, qty = calculate_tp_sl(entry, direction, symbol)
    risk_reward = abs(tp - entry) / abs(entry - sl)
    if risk_reward < 2.5:
        send_message(f"❌ {symbol}: плохое TP/SL соотношение ({risk_reward:.2f}) — пропуск")
        return

    if place_order(symbol, direction, qty, sl, tp):
        active_positions[symbol] = True
        send_message(f"📈 Открыта сделка по {symbol} ({direction.upper()})\nEntry: {entry}\nTP: {tp}, SL: {sl}\nQty: {qty}, x{LEVERAGE}")


def check_closed_positions():
    global active_positions
    try:
        positions = client.futures_position_information()
        for pos in positions:
            symbol = pos['symbol']
            amt = float(pos['positionAmt'])
            if symbol in active_positions and amt == 0:
                active_positions.pop(symbol, None)
                cooldowns[symbol] = time.time() + 60 * 60  # 1 час перерыв
                send_message(f"✅ Позиция по {symbol} ЗАКРЫТА. Ушёл в cooldown на 1 час")
    except Exception as e:
        send_message(f"⚠️ Ошибка проверки позиций: {e}")

# === ИНИЦИАЛИЗАЦИЯ ===
load_symbol_info()
send_message("📊 Начинаем анализ...")

# === ОСНОВНОЙ ЦИКЛ ===
while True:
    for symbol in ALLOWED_SYMBOLS:
        if symbol not in active_positions:
            try:
                analyze_symbol(symbol)
            except Exception as e:
                send_message(f"⚠️ Ошибка в {symbol}: {e}")
    check_closed_positions()
    with open("positions.json", "w") as f:
        json.dump(active_positions, f)
    time.sleep(CHECK_INTERVAL)
